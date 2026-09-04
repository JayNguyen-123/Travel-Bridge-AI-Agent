# Scoping: real hotel booking (payment + Amadeus order creation + `hotel_bookings`)

**Status update: Option A (GUARANTEE-only, no card capture) has been built** --
`tools/hotel_booking_tools.py`, the `hotel_bookings`/`hotel_booking_guests` schema, orchestrator
instructions, and tests all exist now. **It is built against the exact unverified assumption
this document flagged below, and that assumption is still unverified** -- this environment had
no outbound network path to actually call Amadeus's sandbox, so the one fact everything here
depends on (does Amadeus's hotel-order API really allow a GUARANTEE booking with no card data?)
was never confirmed. Section 4 (new) explains what was built, what's still just documented
uncertainty, and gives you a script to run the real test yourself. Sections 1-3 below are the
original scoping pass, left as-is since the reasoning still holds and the open questions in
Section 3 mostly still apply.

Original status note (superseded by the above, kept for context): design doc, nothing
implemented yet. `tools/hotel_tools.py` only searched; there was no payment path,
order-creation call, or `hotel_bookings` table.

## 1. The blocking question: how does payment actually work

This is not a detail to defer -- it changes the shape of everything below, so it comes first.

Based on Amadeus's own developer documentation (see Sources; some pages returned only
metadata when fetched directly and had to be cross-checked against a documentation mirror --
**verify all of this against a live Amadeus sandbox call before writing code, not just against
what's summarized here**), a hotel offer carries a `policies` block indicating one of three
payment types, and it varies **per rate**, not per hotel:

- **GUARANTEE** -- a card secures the room; nothing is charged at booking; the traveler pays
  the property at check-in/checkout. The card may be charged a no-show/violation penalty.
- **DEPOSIT** -- a percentage is charged at booking; the remainder is paid at the property.
- **PREPAY** -- the full amount is charged at the time of booking.

The Amadeus Hotel Booking API (`POST /v2/booking/hotel-orders`) expects the request body to
include the guests, a `roomAssociations` array mapping each guest to the `hotelOfferId`, and a
`payment` object. For a `CREDIT_CARD` method, that object nests **raw card data directly in
the request payload**:

```json
"payment": {
  "method": "CREDIT_CARD",
  "paymentCard": {
    "paymentCardInfo": {
      "vendorCode": "VI",
      "cardNumber": "4151289722471370",
      "expiryDate": "2026-08",
      "holderName": "BOB SMITH"
    }
  }
}
```

Amadeus's own guide says it transmits this to the hotel but does not validate it, and that
**PCI DSS compliance is on whoever submits it.** Whether a GUARANTEE-policy booking can omit
the card block entirely (i.e., guarantee some other way) is not confirmed by the sources this
scoping pass could reach -- that's the single most important thing to verify against a live
sandbox call before committing to an approach.

This is the opposite of how this project built flight payments on purpose. The entire point of
`payments/stripe_client.py` and the "Built so the AI never touches your money" design (see
README.md, the pitch deck) is that this app and the voice agent never see a real card number --
the customer pays on Stripe's own hosted page, and only a signed webhook can mark a payment
`paid`. A voice agent asking someone to speak a 16-digit card number aloud is its own separate
problem on top of the PCI one.

**Three options, in the order they should actually be evaluated:**

**Option A -- GUARANTEE-only, no card capture in this app (if Amadeus allows it).**
Restrict hotel booking to GUARANTEE-policy offers only (filter these out during search/quote,
same way `search_flights` already filters on party size). If Amadeus's API truly allows a
GUARANTEE booking without a card block, this app never touches payment data for hotels at all
-- Stripe isn't even involved, because there's nothing to charge; the hotel bills the traveler
directly at checkout. Lowest risk, smallest scope, but **entirely contingent on the unverified
question above** -- if Amadeus requires a card even for GUARANTEE bookings, this option doesn't
exist as stated and collapses into Option B or C.

**Option B -- capture and forward the raw card (not recommended).**
Build a form/flow that captures card details and passes them straight through to Amadeus, same
as the sample payload above. Fastest to build, but reintroduces exactly the PCI exposure this
system was deliberately built to avoid for flights, and is a poor fit for a voice channel in
particular. Would need real PCI DSS scoping (this app becomes a card-data handler) before it
could respectably go anywhere near production. Not recommended without a strong reason to
prefer it over A or C.

**Option C -- Stripe-mediated virtual card bridge.**
Charge or authorize the traveler through Stripe (consistent with how flights work today), then
have *this system* -- not the traveler -- hold a payment instrument to give Amadeus/the hotel.
In practice this means issuing a single-use virtual card number (e.g., via Stripe Issuing, or a
dedicated VCC provider some travel agencies use) funded by what the traveler paid, and
submitting *that* card to Amadeus instead of the traveler's real one. This is architecturally
the most consistent with the existing flight-payment design (Stripe stays the only thing that
ever sees the traveler's real card), but it is materially new infrastructure -- Stripe Issuing
is not currently integrated anywhere in this project, has its own approval/eligibility process
per country, and only makes sense for DEPOSIT/PREPAY offers (a GUARANTEE offer still needs
*a* card on file, virtual or not, to hold the room). This is the right long-term answer if
hotel volume matters and Amadeus does require a card either way, but it's a separate project in
its own right, not an add-on to a weekend of work.

**Recommendation:** spend a short, cheap research spike confirming what Amadeus's sandbox
actually accepts for a GUARANTEE booking (does it need a card block or not?) before deciding
between A and C. That single API call determines whether this is a small, self-contained
addition (A) or a multi-week infrastructure project (C). Don't scope further engineering time
until that's answered -- and don't default to B to avoid the wait.

## 2. What "mirroring flights" looks like, assuming a payment answer exists

Everything below assumes Option A or C resolves the payment question above. It mirrors the
existing flight architecture piece for piece.

### 2.1 Schema (`db/schema.sql`)

```sql
-- One row per hotel booking. Mirrors travel_bookings; payment linkage is
-- shared with flights via the same payments table (see 2.4 on product_type).
CREATE TABLE IF NOT EXISTS hotel_bookings (
    id                     SERIAL PRIMARY KEY,
    idempotency_key        VARCHAR(64) UNIQUE NOT NULL REFERENCES payments(idempotency_key),
    hotel_order_id         VARCHAR(100),   -- Amadeus hotel-order id from the v2 response
    reference_code         VARCHAR(50),    -- confirmation number read back to the customer
    hotel_id               VARCHAR(20),    -- Amadeus hotelId
    hotel_name              VARCHAR(200),
    lead_guest_name        VARCHAR(150),   -- same "lead traveler" convention as travel_bookings
    phone_number             VARCHAR(30),
    check_in_date            DATE,
    check_out_date           DATE,
    room_quantity             INT NOT NULL DEFAULT 1,
    price_amount              VARCHAR(30),
    currency_type             VARCHAR(10),
    payment_policy            VARCHAR(20),    -- GUARANTEE | DEPOSIT | PREPAY, from the offer at booking time
    status                    VARCHAR(20) NOT NULL DEFAULT 'booked',  -- booked -> cancelled
    cancellation_deadline     TIMESTAMP,      -- from the offer's own policies.cancellation -- NOT a fixed 24h rule, see 2.3
    cancelled_at              TIMESTAMP,
    refund_status             VARCHAR(30) NOT NULL DEFAULT 'none',
    refund_id                 VARCHAR(120),
    refund_amount              BIGINT,
    resolution_path            VARCHAR(30),
    booking_notes               TEXT,
    created_at                  TIMESTAMP DEFAULT now()
);

-- One row per guest, mirrors booking_travelers. room_number lets a v-next
-- version support multi-room bookings; v1 scope can require room_number = 1
-- for every guest (single-room bookings only) to cut scope, see 3.2.
CREATE TABLE IF NOT EXISTS hotel_booking_guests (
    id                     SERIAL PRIMARY KEY,
    idempotency_key        VARCHAR(64) NOT NULL REFERENCES hotel_bookings(idempotency_key),
    amadeus_guest_tid      VARCHAR(10) NOT NULL,  -- Amadeus guest "tid", reused in roomAssociations
    room_number             INT NOT NULL DEFAULT 1,
    first_name               VARCHAR(100) NOT NULL,
    last_name                 VARCHAR(100) NOT NULL,
    created_at                TIMESTAMP DEFAULT now(),
    UNIQUE (idempotency_key, amadeus_guest_tid)
);

CREATE INDEX IF NOT EXISTS idx_hotel_bookings_reference_code ON hotel_bookings (reference_code);
CREATE INDEX IF NOT EXISTS idx_hotel_booking_guests_idempotency_key ON hotel_booking_guests (idempotency_key);
```

### 2.2 Tools (`tools/hotel_tools.py` extended, or a new `tools/hotel_booking_tools.py`)

Following the flight file's own naming/structure so the two stay easy to compare:

- `_validate_guests(guests)` -- mirrors `_validate_travelers`: shape/required-field checks,
  plus a party-size cap mirroring `MAX_PARTY_SIZE` (rooms × guests-per-room, needs its own
  sane default).
- `_build_hotel_booking_payload(hotel_offer, guests, contact info)` -- mirrors
  `_build_booking_payload`: assigns Amadeus `tid`s from the offer's own guest slots if it
  exposes them, never invents them; needs the same anti-tamper discipline the flight code uses
  around `travelerPricings`, applied to whatever the hotel offer's equivalent structure is
  (verify the exact field name against a live offer response).
- `_confirm_hotel_booking_sync(hotel_offer_json_str, guests_json_str, contact_phone_number,
  contact_email, idempotency_key)` -- mirrors `_confirm_flight_booking_sync`: same
  payment-verification-before-Amadeus gate (reusing the `payments` table), same offer-hash
  check against what was actually priced, then `POST /v2/booking/hotel-orders`.
- `confirm_hotel_booking` -- async wrapper, ADK-facing, same `asyncio.to_thread` pattern.
- `lookup_hotel_booking_by_reference`, `cancel_hotel_booking_request` -- mirror the flight
  equivalents, including matching against any guest on the booking (same pattern as the
  group-booking cancellation work already done for flights).

**Reusable as-is:** `payments/stripe_client.py`'s `price_from_flight_offer` only reads
`offer["price"]["total"]`/`["currency"]` -- nothing flight-specific about it. Hotel offers
carry the same `price` shape (confirmed indirectly: `search_hotels` already runs hotel offers
through the same `process_and_convert_all` currency middleware flight offers use). Worth
renaming to `price_from_offer` and sharing it across both, rather than duplicating it -- a
small, low-risk cleanup to do as part of this work either way.

### 2.3 Cancellation / refund logic -- this is genuinely different from flights, not reusable

Flights use a clean, fixed rule: DOT's 24-hour/7-days-out safe harbor, the same for every
booking. Hotels don't have an equivalent universal rule -- each offer's own
`policies.cancellation` carries its own deadline and, on a DEPOSIT/PREPAY booking, its own
penalty terms, set by that specific property and rate, not by regulation. That means:

- The "free cancellation window" isn't a constant -- it has to be read from the specific offer
  at booking time and stored (`cancellation_deadline` above), then checked against `now()` at
  cancellation time, per booking.
- On a GUARANTEE booking, cancellation before the deadline is simple (nothing was charged, so
  there's nothing to refund -- just cancel the order). After the deadline, the hotel may charge
  a no-show penalty to the guaranteeing card, which -- depending on which payment option (1)
  lands on -- may or may not be a card this app controls or can even see.
- On a DEPOSIT/PREPAY booking, a refund after the deadline is a real fare-rules judgment call
  specific to that property, much closer to "outside the flight's 24-hour window" (which this
  project already routes to human review rather than guessing) than to the automatic path.

**Recommendation:** don't try to build an automatic-refund tier for hotels in a first version.
Route every hotel cancellation to the existing `support_requests` human-review queue
(`request_type='refund_review'`), using the stored `cancellation_deadline` only to tell the
human reviewer whether it's inside or outside the free window -- not to auto-refund. This is a
smaller, safer scope than replicating the flight failure-resolution engine's tiers for a domain
that doesn't have flights' one clean regulatory rule to build them on.

### 2.4 `payments` table

No schema change strictly required -- `create_payment_checkout`'s underlying `payments` table
is already product-agnostic (idempotency_key, amount, currency, offer_hash). Worth adding a
`product_type VARCHAR(10) NOT NULL DEFAULT 'flight'` column so the admin queue and any future
reporting can tell a hotel payment from a flight payment at a glance, since `hotel_bookings`
and `travel_bookings` will otherwise both hang off the same `payments` rows with no way to tell
which is which without a join to both tables.

### 2.5 Orchestrator instructions (`agents/orchestrator.py`)

A new instruction block mirroring `BILINGUAL_TRANSACTION_INSTRUCTION`'s flight steps 0-8:
collect check-in/check-out dates and room/guest counts, quote the GUARANTEE-vs-prepay
distinction *out loud* if the offer requires payment now vs. at the property (a real,
user-facing difference the agent must not gloss over), run payment only when the policy
requires it, confirm, and route cancellations through the human-review path described in 2.3
rather than promising an automatic refund. Also: fix the pre-existing inconsistency flagged
earlier (the top-level instruction already claims lodging is bookable) so it stops overpromising
relative to what's actually built, whichever the eventual implementation.

### 2.6 Admin UI (`server/admin.py` / `admin.html`)

Since hotel cancellations route entirely to human review (2.3), the admin queue becomes the
primary place hotel refund decisions get made -- more central than it is for flights today.
Smallest-risk approach: extend the existing `_BOOKING_REVIEW_COLUMNS` pattern with a parallel
query against `hotel_bookings`, tagged by type in the response, rather than trying to unify
`travel_bookings`/`hotel_bookings` into one table right away. A real unification (one generic
`bookings` table) is a reasonable v-next cleanup once both booking types exist and their actual
shapes are known, not a prerequisite to shipping this.

### 2.7 Tests

Mirror the existing suite one-for-one: a `test_hotel_booking_payment_gate.py` (payment
verification gate, same fake-cursor pattern as `test_booking_payment_gate.py`), a
`test_hotel_booking.py` (guest validation, payload building, cancellation identity-matching,
same pattern as `test_group_booking.py`), and whatever Option A/C's payment path needs
(e.g., a virtual-card issuance test double if Option C is chosen).

## 3. Explicit open questions -- need an answer before implementation starts

1. **Does Amadeus's sandbox accept a GUARANTEE-policy hotel-order without card data?** The one
   research question everything else depends on (Section 1). This needs a real sandbox API
   call, not another documentation search -- the docs reachable during this scoping pass
   didn't settle it.
2. **Which payment option (A/B/C)?** Contingent on #1, but leaning A first given the size gap
   between A and C.
3. **Multi-room bookings in v1, or single-room only?** The schema above allows for multi-room
   via `room_number`, but scoping v1 to one room per booking (still supporting multiple guests
   in that one room) would cut real complexity out of guest-to-room mapping and the orchestrator
   instructions. Recommend starting single-room, matching how flights started without the group
   feature and added it deliberately once the core flow was solid.
4. **Automatic refunds for hotels, ever, or human-review-only permanently?** Section 2.3
   recommends human-review-only for v1. Worth deciding explicitly rather than leaving it
   ambiguous, since it changes whether `hotel_bookings.refund_status` needs the same
   `full_auto` path flights have.
5. **Cap on rooms/guests per hotel booking?** Mirrors the `MAX_PARTY_SIZE` decision already
   made for flights (capped at 9, routes larger groups to `request_human_support`) -- needs its
   own number, not necessarily the same one.

## 4. What was actually built, what remains unverified, and how to close the gap

Built, following section 2 above with a few simplifications that fell out of Option A
specifically (no payment step at all, so several flight-mirroring pieces turned out unnecessary):

- **Schema**: `hotel_bookings` + `hotel_booking_guests` in `db/schema.sql`. Deliberately does
  **not** reference `payments(idempotency_key)` the way `travel_bookings` does -- a GUARANTEE
  booking never creates a payment row, so there's nothing to reference. No `refund_status`/
  `refund_id`/`refund_amount` columns either, for the same reason: nothing is ever charged
  through this app for a hotel, so there's nothing to refund.
- **`tools/hotel_booking_tools.py`** (new file, kept independent from `tools/booking_tools.py`
  on purpose, matching this project's existing pattern of not cross-importing between the flight
  and hotel tool files): policy extraction (`_hotel_offer_payment_policy`, reading
  `offer.policies.paymentType`), guest validation (`_validate_guests`, capped at
  `MAX_HOTEL_GUESTS`, default 9), a **deterministic** idempotency key hashed from the exact
  offer + guest list + phone (no prior checkout call exists to mint one the way flights do),
  payload construction that **omits the `payment` block entirely** (the exact unverified
  assumption), the booking/lookup/cancel calls, and a best-effort, clearly-flagged extraction of
  the confirmation number from Amadeus's response (the exact response shape wasn't verifiable
  either -- see below).
- **`agents/orchestrator.py`**: a full hotel-booking + hotel-cancellation instruction block.
  Explicitly tells the agent to check `policies.paymentType` before ever quoting a room as
  bookable, to say out loud that no payment is being collected now, and to route anything that
  isn't confirmed GUARANTEE (including "policy undetermined") to `request_human_support` rather
  than guessing. Also fixed the pre-existing inconsistency this doc flagged earlier -- the
  top-level instruction no longer claims unqualified lodging booking.
- **Tests**: `tests/test_hotel_booking.py`, mirroring the flight test suite's structure and
  fake-cursor patterns. Every pure-logic function in this set (policy extraction, guest
  validation, idempotency-key determinism, payload shape, confirmation-number fallback,
  identity-matching) was also hand-verified by loading the real module against stubbed
  dependencies in the sandbox this was built in, not just written and left unrun -- `pytest`
  itself still can't execute here (no PyPI access), same limitation as the rest of this project.

**What is still exactly as uncertain as Section 1 described.** Nothing above resolves the
blocking question -- it couldn't be resolved from this environment (see the verification script
below for why). The code is written to fail loudly and specifically if the assumption is wrong
(`_try_amadeus_hotel_booking` surfaces Amadeus's raw rejection reason verbatim), rather than to
silently do the wrong thing, but "fails clearly instead of silently" is not the same as
"verified to work." **Treat this as unverified until someone runs it against a real sandbox.**

Two more specific unknowns got introduced by actually writing the code, on top of the original
card question:

- **The exact hotel-order confirmation response shape** (`_extract_hotel_confirmation`) is a
  best-effort guess at a few plausible field paths (`providerConfirmationId`,
  `hotelBookings[].hotelProviderInformation.confirmationNumber`, falling back to the order id
  itself). The migration-guide mirror source didn't show a full success-response example.
- **The hotel-order cancellation endpoint** (`DELETE /v2/booking/hotel-orders/{id}` in
  `_cancel_hotel_booking_sync`) is guessed by analogy with the flight-orders cancellation
  endpoint this project already uses successfully. Not confirmed for hotels specifically.

### Run this to actually answer Section 1's question

This wasn't something I could do from this sandbox: outbound network to Amadeus's own host
doesn't reach it here (`curl` to `test.api.amadeus.com` timed out with no connection at all),
separate from the earlier issue of Amadeus's documentation pages returning no usable content --
two different failures pointing at the same gap. If you have real Amadeus sandbox credentials
and a normal internet connection, this settles Section 1 directly in a couple of minutes:

```bash
# 1) Get a sandbox OAuth token
curl -s -X POST "https://test.api.amadeus.com/v1/security/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&client_id=YOUR_API_KEY&client_secret=YOUR_API_SECRET" \
  | tee /tmp/amadeus_token.json

TOKEN=$(python3 -c "import json;print(json.load(open('/tmp/amadeus_token.json'))['access_token'])")

# 2) Search a hotel and find a GUARANTEE-policy offer -- swap HOTEL_ID for a real
#    Amadeus test-hotel id (their docs list a handful of stable sandbox hotel ids)
curl -s "https://test.api.amadeus.com/v3/shopping/hotel-offers?hotelIds=HOTEL_ID&checkInDate=2026-12-01&adults=1" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | grep -A3 paymentType

# 3) Take that offer's own "id" field and try booking WITHOUT a payment block --
#    this is the actual test. A 201 means Option A is confirmed as built. A 400
#    naming a missing payment/card field means Option A doesn't exist as scoped,
#    and tools/hotel_booking_tools.py needs to move to Option C instead.
curl -s -X POST "https://test.api.amadeus.com/v2/booking/hotel-orders" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "data": {
      "type": "hotel-order",
      "guests": [{"tid": 1, "title": "MR", "firstName": "TEST", "lastName": "TRAVELER",
                  "phone": "+12025551234", "email": "test@example.com"}],
      "roomAssociations": [{"guestReferences": [{"guestReference": "1"}], "hotelOfferId": "OFFER_ID_FROM_STEP_2"}]
    }
  }' | python3 -m json.tool
```

Whatever step 3 returns, that's the real answer -- report it back and this module's docstring,
this doc, and `PRODUCTION_CHECKLIST.md` can all drop their "unverified" language and either
confirm Option A or pivot to Option C.

### Separate, important finding surfaced while building this (not a hotel-specific bug) -- FIXED

While tracing how the agent's offer JSON flows into a booking payload, testing
`middleware/translator.py`'s `process_and_convert_all` directly showed it renamed dict keys
literally named `price` and `currency` (to `giá_tiền` / `tiền_tệ`) **recursively, everywhere
they appear** in an offer -- not just at the top level. That function is what both
`search_flights` and `search_hotels` run every result through before the agent ever sees it, so
the offer JSON the agent actually held and later passed back to `confirm_flight_booking` /
`create_payment_checkout` had these keys renamed throughout. `payments/stripe_client.py`'s
`price_from_flight_offer` reads `flight_offer.get("price", {})` -- which, against that renamed
JSON, returned `{}` and raised `ValueError`. This was a real, pre-existing defect in the
**flight** payment path (independent of this hotel work), serious enough to block every real
`create_payment_checkout` call.

**Status: fixed.** `AMADEUS_DICTIONARY` no longer contains key-renaming entries (`"price"`,
`"currency"`, `"duration"`, `"cabin"` were removed as dict keys); it now holds only leaf-VALUE
translations (enum codes like `"ECONOMY"` -> `"Hạng phổ thông"`). `process_and_convert_all`'s
recursive dict-walk now always preserves the original key name and only transforms the value
(translating known string enums, parsing ISO-8601 `duration` strings, and adding
`converted_price_VND`/`converted_price_USD` alongside the untouched original price fields) --
it never substitutes a Vietnamese key for an English one. Verified two ways in this sandbox
(no live Amadeus network access here, so this is verified against the transform logic itself,
not a live booking): (1) a nested-offer regression check confirming `price`/`currency`/`cabin`
keys survive translation at every depth, including inside `travelerPricings[]`; (2) an
end-to-end check that `price_from_flight_offer(process_and_convert_all(raw_offer))` now
returns the correct `(total, currency)` tuple instead of raising `ValueError`. Both are captured
as permanent regression tests in `tests/test_translator.py`
(`test_process_and_convert_all_never_renames_price_or_currency_keys_at_any_depth` and
`test_process_and_convert_all_output_is_readable_by_price_from_flight_offer`), so this can't
silently regress again. Still worth a real end-to-end sandbox booking to confirm against a live
Amadeus offer shape rather than only the synthetic one tested here.

## Sources

Amadeus's own developer portal (`developers.amadeus.com`) returned only page metadata when
fetched directly during this research pass (a recurring issue across this project's Amadeus
research, not specific to this topic) -- the technical detail above leans on a documentation
mirror as a secondary source. Treat every specific field name, endpoint, and payload shape here
as **"needs confirmation against a live Amadeus sandbox call or the primary docs,"** not as
verified fact:

- [Hotel APIs Tutorial -- Amadeus for Developers (mirror)](https://amadeus4dev.github.io/developer-guides/resources/hotels/)
- [Hotel Booking API Migration Guide -- Amadeus for Developers (mirror)](https://amadeus4dev.github.io/developer-guides/migration-guides/hotel-booking/)
