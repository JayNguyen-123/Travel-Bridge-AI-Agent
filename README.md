# Bilingual Travel Voice Agent

A bilingual (English/Vietnamese) voice AI travel concierge built on Google's
Agent Development Kit (ADK) and the Gemini Live API. It searches flights and
hotels via Amadeus, collects payment through Stripe Checkout before booking,
and confirms real flight bookings by SMS (via Twilio) and by a proper
itinerary email (via SendGrid).

This is the production-oriented rewrite of an earlier notebook prototype.
See `PRODUCTION_CHECKLIST.md` for what's covered here and what's still your
call to make before a real launch.

## Architecture

```
Browser (mic)                 Cloud Run (FastAPI)                External
──────────────                ─────────────────────              ────────
index.html/app.js  ── WS ──▶  /ws/{user_id}
                               │  ADK Runner + LiveRequestQueue ──▶ Gemini Live API
                               │  agents/orchestrator.py (Agent + tools)
                               │       │
                               │       ├─ tools/flight_tools.py  ──▶ Amadeus API
                               │       ├─ tools/hotel_tools.py   ──▶ Amadeus API
                               │       ├─ tools/payment_tools.py ──▶ Stripe Checkout
                               │       │        │                    (link texted via Twilio)
                               │       │        ▼
                               │       │   db: payments (pending)
                               │       │
                               │       └─ tools/booking_tools.py
                               │                │  (checks payments.status == 'paid'
                               │                │   before calling Amadeus)
                               │                ▼
                               │           db: travel_bookings
                               │
                               ├─ POST /stripe/webhook  ◀── Stripe (signed events)
                               │        (only path allowed to set payments.status = 'paid')
                               │
                               └─ GET /admin  (server/admin.py, HTTP Basic / ADMIN_API_KEY)
                                        human agent works: support_requests (date changes,
                                        refund reviews, Tier 4 booking-failure escalations)
                                        + travel_bookings pending a manual refund decision
```

The core security property: **the LLM never controls money.** The charge
amount is derived server-side from the Amadeus offer's own `price` field
(`payments/stripe_client.py`), and a payment is only ever marked `paid` by
the signature-verified Stripe webhook handler (`server/main.py`) -- never by
a tool call the agent/LLM can influence. `confirm_flight_booking` refuses to
book unless it finds a `paid` row whose `offer_hash` matches the exact offer
being booked. The same principle applies to cancellations: refund amounts
come from the stored payment record, never from the LLM.

### Group / family bookings

A single booking can cover multiple travelers -- a family, a group of
friends -- as one Amadeus order paid for with one Stripe charge:

- `search_flights` takes `adults`/`children`/`infants` counts. The
  `price.total` Amadeus returns is already the total for the whole party,
  not a per-person price, so the existing single-charge payment flow
  (`create_payment_checkout`) needs no changes at all for group bookings.
- **Capped at `MAX_PARTY_SIZE` travelers per booking (default 9, env-
  configurable)** -- enforced independently in both `search_flights`
  (`tools/flight_tools.py`) and `confirm_flight_booking` (`tools/booking_tools.py`,
  so a caller can't bypass the search-time cap by just passing a longer
  traveler list). A bigger group gets a clear error and is routed to
  `request_human_support` instead of a partially-working self-service flow.
- `confirm_flight_booking` takes `travelers_json_str`, a JSON array with one
  object per traveler (first/last name, DOB, gender), collected in the same
  adults-then-children-then-infants order the search used. Its length must
  exactly match the traveler count the offer was priced for, or the tool
  returns `traveler_count_mismatch` rather than silently booking the wrong
  party size.
- Each Amadeus flight offer carries its own `travelerPricings` array, with
  the `travelerId`/`travelerType` slots the order-creation call is required
  to reuse -- `tools/booking_tools.py` reads those ids from the offer itself
  (`_traveler_pricing_slots`) rather than inventing them client-side. This
  traveler-id-matching convention reflects Amadeus's documented API
  behavior, but could not be freshly re-verified against live Amadeus docs
  while building this in a network-restricted sandbox -- confirm it against
  current Amadeus documentation, or a real sandbox booking call, before
  relying on it in production.
- On success, every traveler is persisted to a `booking_travelers` table
  (one row per traveler, `db/schema.sql`) alongside the existing
  `travel_bookings` row, whose own `traveler_name` field now holds just the
  lead/contact traveler's display name (e.g. `"Jane Doe (+2 more)"`).
- **Cancellation is whole-booking-only.** There is no partial/per-traveler
  cancellation at this Amadeus API tier -- `cancel_booking_request` cancels
  every traveler on the booking together. A caller identifying by last name
  is matched against *any* traveler on the booking, not just the lead one
  (with a fallback to the old single-name match for bookings made before
  `booking_travelers` existed). Requests to cancel or change only some
  travelers on a group booking route to `request_human_support` instead, the
  same as any other unsupported change.

### Round-trip flights

`search_flights` takes an optional `returnDate` (`tools/flight_tools.py`). Passing
it forwards Amadeus's own `returnDate` query param, which turns each result into a
single round-trip offer -- `itineraries[0]` is the outbound leg, `itineraries[1]` is
the return leg, and `price.total` is already the combined total for both, priced and
paid for as one Stripe charge and one `confirm_flight_booking` call, exactly like a
one-way offer. Omitting `returnDate` searches one-way, as before. `agents/orchestrator.py`
now always asks whether the traveler wants one-way or round trip and, for a round trip,
collects the return date before calling `search_flights`.

Two places that previously assumed "one itinerary" were updated so a round trip
doesn't silently regress to one-way:

- The SMS/email itinerary notifications sent after a real booking (`_flight_itinerary_legs`
  in `tools/booking_tools.py`, `notifications/email.py`) already flattened every
  `itineraries[].segments[]`, not just the first, so no change was needed there -- a
  round-trip confirmation lists both legs automatically.
- The Tier 2 automatic fare-rebook fallback (`_tier2_fare_recheck_and_rebook` in
  `tools/booking_tools.py`) re-searches the original route/date(s) if the first booking
  attempt fails. It now extracts every leg of the original offer (`_extract_all_legs`)
  and re-supplies all of them, so a failed round-trip (or multi-city -- see below)
  booking that gets automatically rebooked keeps its original shape instead of quietly
  becoming one-way.

**Not yet handled:** mixed one-way-plus-return-on-different-cabin/fare-basis itineraries
(e.g. economy outbound, business return, priced as two separate one-way fares rather than
one combined fare) -- out of scope for this feature and still treated as unsupported.

### Multi-city flights

`search_multi_city_flights(legs_json_str, adults, children, infants)` (`tools/flight_tools.py`)
handles a genuine multi-destination trip -- e.g. SGN→BKK, then BKK→NRT, then NRT→SGN --
which is NOT the same as a round trip (going somewhere and coming straight back) and can't
be expressed with `search_flights`'s `returnDate`. `legs_json_str` is a JSON array of
`{"origin", "destination", "date"}` objects, one per leg, in travel order (2 to
`MAX_MULTI_CITY_LEGS` legs, default 6 -- Amadeus's own documented cap); a leg dated before
the previous one is rejected before any network call. Because this needs a different shape
of request than `search_flights`'s simple `origin`/`destination`/`departureDate` query
params, it uses Amadeus's POST form of the same Flight Offers Search resource instead of
the GET form -- a JSON body with one `originDestinations` entry per leg and a `travelers`
array (`_build_multi_city_search_body`), sent with an `X-HTTP-Method-Override: GET` header
Amadeus's docs call for on that endpoint. Same passengers and cabin apply to every leg --
there's no support for different travelers or a different cabin per leg. The result is one
combined offer, one combined `price.total`, booked and paid for as a single transaction
exactly like a one-way or round-trip offer -- `confirm_flight_booking` needed no changes,
since it already works generically off however many itineraries an offer has.

Tier 2's automatic fare-rebook fallback also understands multi-city: `_extract_all_legs`
pulls every leg out of the original offer, and `_is_simple_round_trip` tells a plain
there-and-back pair (still re-searched the simple way, via `returnDate`) apart from a true
multi-city itinerary -- 3+ legs, or a 2-leg "open-jaw" trip that doesn't return to its
starting city (e.g. SGN→BKK, then HAN→SGN) -- which gets re-searched through the same
multi-city POST path (`_post_multi_city_search`, shared between the search tool and Tier 2
so the request shape is defined in exactly one place). So a failed multi-city booking still
gets an automatic rebook attempt on the same multi-leg route before falling back to a
refund, instead of always skipping straight to Tier 3.

**Unverified against a live Amadeus account**, same caveat as round-trip search: the POST
request body shape (`originDestinations`/`travelers`/`sources`) and the
`X-HTTP-Method-Override` header follow Amadeus's documented API, but this was built and
manually verified against that documentation in a network-restricted sandbox, not against
a real sandbox/production call. See `PRODUCTION_CHECKLIST.md`.

### Hotel booking (GUARANTEE-policy only)

`confirm_hotel_booking` books a single hotel room, but only for offers whose payment policy is
GUARANTEE (`policies.paymentType`) -- meaning the property charges the guest's card at
check-in/checkout, not through this app. There is no payment step for hotels at all: no Stripe
checkout, no `payments` row, nothing charged here ever. DEPOSIT/PREPAY offers (money due now or
at booking) and multi-room requests are explicitly refused and routed to
`request_human_support` -- this is deliberate scope, not a gap. **This entire feature rests on
an assumption that could not be verified while it was built** -- that Amadeus's hotel-order API
actually allows a GUARANTEE booking without card data -- because this build environment had no
outbound network path to Amadeus. See `HOTEL_BOOKING_SCOPE.md` section 4 for exactly what's
unverified and a script to confirm it against a real sandbox before trusting this feature.

### Cancellations & changes

- **Cancel within 24 hours of booking** (and departure was 7+ days out at
  booking time, matching DOT 14 CFR 259.5): `cancel_booking_request` cancels
  with Amadeus and issues an automatic full Stripe refund.
- **Cancel outside that window**: still cancelled with Amadeus immediately,
  but the refund is routed to `support_requests` for human review rather than
  an amount this code invents.
- **Date/itinerary changes**: not automated. `request_human_support` logs a
  request and texts the customer that a person will follow up -- this
  project's Amadeus tier doesn't support self-service reissue/exchange.

See `PRODUCTION_CHECKLIST.md` for what's still missing around this (a real
fare-rules-based partial-refund policy, and handling the reverse case of an
airline-initiated cancellation).

### Booking failures (payment succeeded, but the Amadeus booking call didn't)

`confirm_flight_booking` runs a four-tier resolution engine
(`tools/booking_tools.py`) instead of just apologizing:

1. **Auto-Retry Engine**: retries the same booking a few times
   (`BOOKING_RETRY_ATTEMPTS`), but only for errors that look transient
   (network error, Amadeus 429/5xx) -- a definitive "fare gone" error skips
   straight to step 2 rather than retrying something that can't succeed.
2. **Fare Availability Check**: re-searches the same route/date. If a
   comparable fare is priced within `FARE_REBOOK_THRESHOLD_PERCENT` of what
   the customer already paid, books that instead -- the business absorbs any
   difference; the customer is never charged again.
3. **Auto-Refund & Notify**: no acceptable fare exists -- issues a full
   Stripe refund and texts the customer.
4. **Manual Ops Queue**: the refund itself failed -- logs an `urgent`
   `support_requests` ticket and texts both the customer and
   `OPS_NOTIFICATION_PHONE`.

Every path resolves the customer to one of three known states: booked
(step 1/2), refunded (step 3), or an urgent human ticket (step 4) -- never a
silent charge with nothing recorded.

### Admin UI

`GET /admin` (`server/admin.py` + `server/static/admin.html`) is a small
internal page for a human agent to work the queue: open `support_requests`
tickets, and `travel_bookings` still awaiting a manual refund decision
(cancellations outside the 24-hour auto-refund window, or one whose
auto-refund attempt itself failed). From there they can resolve a ticket,
issue a refund (calls the same `create_refund` Stripe wrapper the automatic
paths use), or deny one with a note -- all keyed by the payment's
`idempotency_key`, so the queue, `payments`, and `travel_bookings` stay in
sync. Ticket and booking detail views also show the full traveler roster
(from `booking_travelers`) when there is one, not just the lead traveler's
name -- so an admin working a group booking's ticket can see every name on
it, not only whoever the booking record's `traveler_name` field displays.

Auth is HTTP Basic against a single `ADMIN_API_KEY` env var (any username,
that value as the password) -- deliberately minimal, fine for one or two
internal ops people hitting a URL that isn't publicly advertised. Leave
`ADMIN_API_KEY` unset to disable `/admin` entirely (it fails closed with
503, not open). See `PRODUCTION_CHECKLIST.md` before a wider team relies on
this -- it is not real per-agent login/RBAC.

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in real values; .env is gitignored
```

You need: a Gemini API key, an Amadeus Self-Service (test) app, a Postgres
database, Twilio credentials, and a Stripe test-mode account. Run a local
Postgres however you like (Docker: `docker run -e POSTGRES_PASSWORD=... -p
5432:5432 postgres:16`); the app applies `db/schema.sql` itself on startup.

Forward Stripe webhook events to your machine while developing:

```bash
stripe listen --forward-to localhost:8080/stripe/webhook
# copy the printed whsec_... into STRIPE_WEBHOOK_SECRET in .env
```

Run the server:

```bash
uvicorn server.main:app --reload --port 8080
```

Open `http://localhost:8080`, click Connect, then Start mic, and talk to it
in English or Vietnamese. A "book a flight" request will text a Stripe
Checkout link to whatever phone number you give it -- use Stripe's test
card `4242 4242 4242 4242`, any future expiry, any CVC.

## Tests

```bash
pytest
```

Covers: the FX/translation middleware, Stripe amount derivation (including
zero-decimal currencies like VND), the booking tool's payment-verification
gate (rejects unpaid/mismatched/missing payment records, and invalid/
mismatched-count/oversized traveler lists), group-booking traveler-slot/
payload logic and cancellation identity-matching against any traveler on a
group booking, the `MAX_PARTY_SIZE` cap on both `search_flights` and
`confirm_flight_booking`, the cancellation eligibility window, the booking
failure-resolution engine's retry classification and fare-rebook threshold
math, the admin UI's auth gate, the admin UI's traveler-roster lookup,
GUARANTEE-only hotel booking's policy-gating, guest validation, payload
construction, and identity-matching (`tests/test_hotel_booking.py`), the
post-booking itinerary SMS/email notifications -- flight-leg extraction and
formatting, the richer SMS staying backward compatible with hotel bookings,
and SendGrid's fail-open behavior on missing credentials, a missing
recipient, or a provider exception (`tests/test_itinerary_notifications.py`),
round-trip flight search -- `returnDate` forwarded to (or correctly
omitted from) the Amadeus request, a backwards return/departure date pair
rejected before any network call, and the Tier 2 fare-rebook fallback
preserving a round trip's return date instead of silently rebooking it as
one-way (`tests/test_round_trip_flight_search.py`,
`tests/test_booking_resolution.py`), and multi-city flight search -- input
validation (leg count, missing fields, out-of-order dates, party-size cap)
rejecting bad input before any network call, the exact POST request body/
headers built for Amadeus, and Tier 2 correctly telling a plain round trip
apart from a true multi-city or open-jaw itinerary and re-searching each
through the right path (`tests/test_multi_city_flight_search.py`,
`tests/test_booking_resolution.py`).

## Deploying

See `deploy/DEPLOY.md`.

## Project layout

```
agents/orchestrator.py      ADK Agent definition + bilingual booking/payment instructions
tools/flight_tools.py       Amadeus flight search (adults/children/infants, group-priced total)
tools/hotel_tools.py        Amadeus hotel search
tools/hotel_booking_tools.py  confirm_hotel_booking (GUARANTEE-policy only, no payment step), lookup_hotel_booking_by_reference, cancel_hotel_booking_request -- see HOTEL_BOOKING_SCOPE.md before trusting this
tools/payment_tools.py      create_payment_checkout / check_payment_status (ADK-facing)
tools/booking_tools.py      confirm_flight_booking (multi-traveler + 4-tier failure-resolution engine), lookup_booking_by_reference, cancel_booking_request (whole-booking-only)
tools/support_tools.py      request_human_support -- escalation queue for changes/manual refund review
payments/stripe_client.py   Stripe SDK wrapper + price derivation from Amadeus offers + refunds
notifications/sms.py        Twilio SMS helper (booking confirmations w/ itinerary lines + payment links)
notifications/email.py      SendGrid itinerary/e-ticket-style email, sent after a real flight booking
middleware/translator.py    Recursive EN->VN key/value translation + display currency conversion
db/database.py, schema.sql  Pooled Postgres connections + schema
server/main.py              FastAPI: /ws voice endpoint, /stripe/webhook, /healthz
server/admin.py             Admin API: support/refund queue, HTTP Basic auth (ADMIN_API_KEY)
server/static/               Browser voice client (travel-agency-styled landing page + app.js) + admin.html (queue UI)
tests/                      Unit tests
deploy/DEPLOY.md            Cloud Run deployment steps
PRODUCTION_CHECKLIST.md     What's covered vs. what's still your call
```
