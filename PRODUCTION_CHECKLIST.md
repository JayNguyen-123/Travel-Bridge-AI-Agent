# Production readiness checklist

What this rewrite covers, and what's still a real decision or task for you
before this handles genuine money and real travelers. Organized so you can
work down it.

## Covered by this codebase

- **Payment collection**: Stripe Checkout, amount derived server-side from
  the Amadeus offer (never from the LLM), payment status set only by a
  signature-verified webhook.
- **Booking-payment linkage**: a booking cannot be confirmed without a
  matching `paid` payment row for the exact offer (hash-compared).
- **Idempotency**: retried "confirm" calls return the existing booking
  instead of double-booking or double-charging.
- **Correct Amadeus API host** (was pointed at a non-functional URL).
- **Correct offer key names surviving translation** (`middleware/translator.py`
  used to rename `price`/`currency` dict keys to Vietnamese recursively,
  which broke every real flight payment attempt -- see the caveat below for
  detail, now fixed and regression-tested).
- **Correct traveler identity fields** (was hardcoding a fake DOB/gender/email
  for every real booking).
- **Real post-booking itinerary delivery**: a real Amadeus flight booking now
  sends both a richer SMS (flight numbers, route, and departure/arrival times
  per leg, not just the confirmation code) and an HTML e-ticket-style email
  via SendGrid (`notifications/email.py`), built from the same booking
  confirmation the agent reads aloud during the call. Before this, the only
  durable record a traveler got was a terse SMS with just the PNR -- see the
  SendGrid caveat below before trusting real delivery.
- **Correct international phone parsing** (was assuming every traveler is
  Vietnamese).
- **Real Google ADK API usage** (`Agent`, `Runner`, `LiveRequestQueue`,
  `RunConfig` -- the original draft's classes didn't exist).
- **Pooled DB connections**, non-blocking tool calls (`asyncio.to_thread`
  around every blocking network/DB call so one slow request doesn't stall
  other concurrent voice sessions).
- **Secrets via env vars / Secret Manager**, not hardcoded or notebook-pasted.
- **Cancellation with automatic refund** for the DOT 24-hour / 7-day-out
  window (14 CFR 259.5-aligned): full Stripe refund issued automatically once
  Amadeus confirms the cancellation. Outside that window, the booking is
  still cancelled with Amadeus immediately, but refund amount is routed to
  human review (`refund_status: "pending_manual_review"`) rather than guessed.
- **Human escalation path** for anything not automated (date/itinerary
  changes, reissuance, disputed refunds): logged to a real `support_requests`
  table with SMS confirmation to the customer, instead of the agent just
  apologizing with nothing recorded.
- **Booking failure mid-flow** (payment succeeded, Amadeus booking call
  failed): a four-tier resolution engine in `tools/booking_tools.py` --
  retry transient errors, re-check for a comparable fare within an absorbed-
  cost threshold and rebook it, auto-refund if no fare exists, and escalate
  to the admin queue as `urgent` if even the refund fails. Never leaves an
  unhandled 500 with a charge nobody knows about.
- **Admin UI** (`server/admin.py` + `server/static/admin.html`) for a human
  to actually work the `support_requests` queue and any booking pending a
  manual refund decision -- list, view detail, resolve, issue a refund, or
  deny one, all gated by `ADMIN_API_KEY`. This used to be "just a table plus
  an optional SMS ping, no admin UI" -- see the caveats below on what this
  admin UI is (and isn't) production-grade for.
- **Group / family bookings**: multiple travelers on one booking, one
  Stripe charge for the whole party (Amadeus already prices `price.total`
  as the group total, so payment collection is unchanged). Traveler ids
  used in the Amadeus order-creation payload are read from the offer's own
  `travelerPricings` slots, never invented client-side. Traveler count is
  validated against what the offer was actually priced for before booking.
  Every traveler is persisted (`booking_travelers` table), and cancellation
  identity checks match against any traveler on the booking, not just the
  lead one.
- **GUARANTEE-only hotel booking** (`tools/hotel_booking_tools.py`): a single room, no payment
  collected through this app -- see the two caveats immediately below before trusting this.
- **Round-trip flight search and booking**: `search_flights` takes an optional
  `returnDate`, forwarded to Amadeus as its own param so a single offer covers
  both legs at one combined price, booked/paid for as one transaction like any
  other offer (`tools/flight_tools.py`).
- **Multi-city flight search and booking**: `search_multi_city_flights` takes a
  JSON array of 2-6 `{origin, destination, date}` legs and searches them as one
  itinerary via Amadeus's POST Flight Offers Search (`originDestinations`
  array), priced and booked as one combined offer, one transaction, exactly
  like a one-way or round-trip offer -- `confirm_flight_booking` needed no
  changes, since it already works off however many itineraries an offer has
  (`tools/flight_tools.py`). See the caveat below on what's out of scope
  (per-leg cabin/fare-basis) and what's unverified against a live account.
- **Tier 2 fare-rebook fallback covers every itinerary shape above**:
  `_extract_all_legs` (`tools/booking_tools.py`) pulls every leg out of the
  original offer -- however many there are -- and `_is_simple_round_trip`
  routes a plain there-and-back pair through the simple GET re-search
  (preserving the return date) and everything else (3+ legs, or a 2-leg
  "open-jaw" that doesn't return to its starting city) through the same
  multi-city POST re-search the search tool uses. A failed booking of any
  shape still gets an automatic rebook attempt on the same route before
  falling back to Tier 3, instead of silently downgrading to one-way or
  skipping recovery entirely.
- **Basic tests** for pricing math, the payment-verification gate, the
  cancellation-eligibility window logic, the booking failure-resolution
  engine's retry classification and fare-rebook threshold math, group-
  booking traveler-slot/payload logic and the `MAX_PARTY_SIZE` cap, the
  admin UI's auth gate, the admin UI's traveler-roster lookup, hotel
  booking's policy-gating/payload/idempotency logic, the itinerary
  SMS/email notification logic (flight-leg extraction/formatting, the
  richer-SMS backward compatibility hotel bookings rely on, and SendGrid's
  fail-open behavior on missing credentials, a missing recipient, or a
  provider exception), round-trip search/rebook (`returnDate` forwarded
  or correctly omitted, a backwards date pair rejected before any network
  call, and Tier 2 preserving the return date on rebook), and multi-city
  search/rebook (leg-count/field/chronological-order validation, the exact
  POST request body Amadeus expects, and Tier 2 correctly distinguishing a
  plain round trip from a true multi-city or open-jaw itinerary -- this is
  also the test suite that caught a real bug during development: an early
  version of `_is_simple_round_trip` only checked that the return leg's
  *destination* matched the original origin, which wrongly classified an
  open-jaw trip like SGN→BKK-then-HAN→SGN as a simple round trip because
  both happen to end at SGN).

## Still your call -- work through before a real launch

**Session persistence at scale.** `InMemoryRunner` keeps conversation state
in one process's memory. `deploy/DEPLOY.md` uses `--session-affinity` +
`--min-instances=1` as a stopgap, but a restart, deploy, or autoscale event
drops in-flight sessions. For real multi-instance production, replace it
with a persistent ADK `SessionService` (check current ADK docs for the
supported backends) or accept "a dropped call means the traveler calls back
and starts over" as your actual failure mode and design the UX around it.

**Amadeus production access.** The sandbox environment used here has
synthetic data and different rate limits than production. Amadeus requires a
formal move-to-production step (and, for real ticket issuance, usually a
commercial agreement / IATA or similar accreditation depending on your
market) -- that's a business process, not a code change.

**Refunds and cancellations -- what's still missing.** The 24-hour window is
automatic, and there's now an admin UI to work everything else. You still
need: (1) a real fare-rules-based refund policy for cancellations outside
the 24-hour window (the code deliberately does *not* guess a penalty amount
-- verify what Amadeus's self-service tier actually exposes for fare
rules/penalties, or have a human read the fare conditions, before promising
customers a specific partial-refund percentage, and before an admin clicks
"issue refund" on a case that should only be partial -- today they'd have to
compute and enter that amount themselves); (2) a process for the reverse
case -- the airline cancelling or significantly changing a flight after your
customer paid -- which isn't handled at all here and usually carries its own
regulatory obligations (e.g. DOT's rules on airline-caused
cancellations/refunds, generally stronger than the 24-hour rule); (3) a
documented policy for who eats fare-difference/no-show costs on anything
outside the automatic path.

**The admin UI is minimal by design -- know its limits.** (1) Auth is one
shared `ADMIN_API_KEY` via HTTP Basic, not per-agent login/RBAC -- fine for
one or two internal people, not for a support team, and it gives no audit
trail of *who* clicked what beyond the free-text `resolved_by` name they
type in. (2) No pagination, search, or filtering beyond open/in-progress/
closed -- will not scale past a small queue. (3) Tier 2's automatic fare
re-check now re-searches however many legs the original offer had
(`_extract_all_legs`) -- a plain round trip via the simple GET endpoint with
`returnDate`, and a true multi-city or open-jaw itinerary via the same
multi-city POST search the search tool uses (`_is_simple_round_trip` decides
which). So a failed booking of any of these shapes still gets an automatic
rebook attempt on the same route instead of silently downgrading to one-way
or skipping recovery. What's still out of scope for Tier 2 (and for search):
mixed one-way-plus-return-on-different-cabin/fare-basis itineraries -- see
the multi-city caveat below. (4) `FARE_REBOOK_THRESHOLD_PERCENT`
is a blunt percentage the business absorbs sight-unseen, not a real
fare-rules read -- revisit the default (10%) against your actual margins.

**Group bookings -- what's still missing.** (1) The Amadeus traveler-id/
`travelerType` contract this code relies on (`travelerPricings[].travelerId`
must be reused verbatim in the order-creation payload) reflects documented,
standard GDS behavior, but could not be freshly re-verified against live
Amadeus docs or a real sandbox booking call while this was built -- do that
verification (or a real Amadeus sandbox group booking) before trusting it
with real money. (2) Cancellation is all-or-nothing for the whole booking --
there is no partial/per-traveler cancellation at this Amadeus API tier, by
design; a customer wanting to drop one traveler from a group is routed to
`request_human_support`, same as a date change. (3) The party-size cap
(`MAX_PARTY_SIZE`, default 9, enforced in both `search_flights` and
`confirm_flight_booking`) is a number this app picked, not a verified
Amadeus limit -- the real per-request passenger limit varies by Amadeus
contract/tier and isn't documented anywhere this app can check at runtime;
confirm the actual limit for your account and adjust `MAX_PARTY_SIZE`
accordingly (raising it doesn't remove the need to verify Amadeus will
actually accept that many travelers in one order).

**Round-trip search is real code, but the `itineraries[1]`-is-the-return-leg
shape is unverified against a live Amadeus response.** Everything this app
controls (forwarding `returnDate` to Amadeus, rejecting a backwards date
pair before spending a request, Tier 2 preserving the return date on
rebook, the itinerary SMS/email flattening every leg) is built and
manually verified against a synthetic offer shaped the way Amadeus's own
docs describe. What hasn't been re-confirmed while building this in a
network-restricted sandbox: that a real Amadeus sandbox/production response
to a `returnDate` search actually comes back as exactly two itineraries in
that order, that `price.total` really is the combined round-trip total (not,
say, per-leg), and that the booking/order-creation call accepts a
round-trip offer with no extra fields beyond what a one-way offer needs.
Run one real round-trip search-and-book against the Amadeus sandbox before
trusting this with a real customer.

**Multi-city search is real code, but the POST request shape is unverified
against a live Amadeus response -- more so than round-trip.** Round-trip
search only added a query param (`returnDate`) to the same GET endpoint
already in use; multi-city switches to an entirely different HTTP method
and body shape (`originDestinations`/`travelers`/`sources` in a POST JSON
body, with an `X-HTTP-Method-Override: GET` header) that this codebase had
never called before. Everything this app controls (leg-count/field/
chronological-order validation before any network call, the exact request
body built, Tier 2 routing a multi-city or open-jaw offer through the same
POST path) is built and manually verified against Amadeus's documented
request/response shape, not against a live account. What specifically
hasn't been re-confirmed: whether `X-HTTP-Method-Override: GET` is actually
required (vs. Amadeus accepting a plain POST, or expecting something else
entirely) on your account/API version, whether the response shape really
matches the GET endpoint's (an offer with N `itineraries`, `price.total` as
one combined figure) closely enough for `process_and_convert_all` and the
booking/order-creation call to handle it with no changes, and whether
Amadeus's self-service tier exposes this POST endpoint at all versus it
being reserved for a higher contract tier. Run one real multi-city
search-and-book against the Amadeus sandbox before trusting this with a
real customer -- more so than any other feature in this checklist.

**Admin UI and group bookings.** The admin UI now shows a booking's full
traveler roster (`_fetch_travelers`/`_fetch_travelers_bulk` in
`server/admin.py`, pulled from `booking_travelers`) on both the support-
ticket detail view and the booking-refund-review view, not just the lead
traveler's name from `travel_bookings.traveler_name`. What it still doesn't
do: there's no dedicated search/filter by traveler name (only by the ticket/
booking's own reference code), and a pre-existing booking made before
`booking_travelers` existed will show an empty roster there (the same
lead-name fallback `_lookup_booking_sync` uses for cancellations isn't
mirrored in the admin UI).

**Hotel booking now exists, but only for GUARANTEE-policy (pay-at-property) offers, and it is
UNVERIFIED against a real Amadeus sandbox.** `tools/hotel_booking_tools.py` +
`confirm_hotel_booking`/`lookup_hotel_booking_by_reference`/`cancel_hotel_booking_request` book a
single room, no payment collected through this app -- the whole design rests on Amadeus's
hotel-order API accepting a GUARANTEE booking without card data, which could not be confirmed
here (no outbound network to Amadeus from this build environment). Do not treat this as working
until someone runs the verification script in `HOTEL_BOOKING_SCOPE.md` section 4 against a real
sandbox. DEPOSIT/PREPAY offers and multi-room bookings are explicitly refused
(`policy_not_supported`) and routed to `request_human_support` -- that part is deliberate scope,
not a bug. Full detail, including two more specific unknowns introduced while building this
(the confirmation-response shape and the cancellation endpoint), is in `HOTEL_BOOKING_SCOPE.md`
section 4.

**A pre-existing bug in the FLIGHT payment path, found while building the hotel work above --
now FIXED.** `middleware/translator.py`'s `process_and_convert_all` -- which every
`search_flights` and `search_hotels` result is run through before the agent ever sees it -- used
to rename dict keys literally named `price` and `currency` (to `giá_tiền`/`tiền_tệ`) recursively,
everywhere they appeared in an offer. Verified directly in this build's sandbox at the time:
feeding a sample offer through `process_and_convert_all` turned `offer["price"]["currency"]`
into `offer["giá_tiền"]["tiền_tệ"]`. `payments/stripe_client.py`'s `price_from_flight_offer`
reads `flight_offer.get("price", {})` -- against that renamed JSON (there is no other version of
the offer for the agent to pass back), that returned `{}` and raised `ValueError`, which
`create_payment_checkout` surfaced as `invalid_flight_offer`. That meant **every real flight
payment attempt would have failed**, not just an edge case.

Fix: `AMADEUS_DICTIONARY` no longer contains key-renaming entries -- it now holds only leaf-VALUE
translations (e.g. `"ECONOMY"` -> `"Hạng phổ thông"`), and `process_and_convert_all` always
preserves the original key name while walking a dict, only ever transforming the value. Verified
with two new permanent regression tests in `tests/test_translator.py`: one confirming `price`/
`currency`/`cabin` keys survive translation at every nesting depth (including inside
`travelerPricings[]`), and one end-to-end test asserting
`price_from_flight_offer(process_and_convert_all(raw_offer))` now returns the correct
`(total, currency)` tuple. Both were run manually against the real modules in this sandbox
(pytest itself can't execute here -- see the Tests section) and passed. Only tested against a
synthetic offer shape, not a live Amadeus response -- worth reconfirming against a real sandbox
search result, but the underlying transform logic is now provably key-preserving regardless of
offer shape.

**Itinerary email/SMS is real code, but SendGrid delivery is unverified against a live account.**
`notifications/email.py`'s `send_flight_itinerary_email` and the richer `send_booking_confirmation_sms`
(`notifications/sms.py`) were built and regression-tested (`tests/test_itinerary_notifications.py`) with
SendGrid's SDK stubbed out, the same way the rest of this project's external-API code was verified in a
network-restricted sandbox -- there was no outbound path to SendGrid's API here to confirm a real send.
Before trusting this in production: (1) `SENDGRID_API_KEY` must be a real key and `SENDGRID_FROM_EMAIL`
must be a sender address on a domain you've verified with SendGrid (SPF/DKIM) -- an unverified sender
gets silently spam-filtered or outright rejected far more often than Twilio SMS does; (2) both
`send_flight_itinerary_email` and the SMS path fail open by design (missing credentials, a missing
recipient address, or any SendGrid exception logs a warning and returns `{"sent": False, ...}` rather
than raising), which is the right call for "don't let a notification failure undo a booking that already
succeeded" -- but it also means a broken SendGrid integration fails silently unless someone is watching
the logs (see the Observability caveat below) or a customer complains; (3) itinerary display data
(`tools/booking_tools.py`'s `_flight_itinerary_legs`) was only tested against a synthetic offer shape,
not a live Amadeus response -- reconfirm the segment field names (`carrierCode`, `number`, `departure`/
`arrival.iataCode`/`.at`) against a real sandbox booking before relying on it; (4) hotel bookings
(`tools/hotel_booking_tools.py`) still only get the terse PNR-only SMS -- no itinerary line, no email --
this work was scoped to flights only.

**Date/itinerary changes are not automated, by design.** `request_human_support`
logs the request; a person has to actually do the rebooking through Amadeus's
full platform (or your Amadeus contract's actual servicing tier -- confirm
what that includes). Don't let "we have a support_requests table" get
mistaken for "changes are handled."

**Rate limiting & abuse.** No throttling exists on `/ws` connections,
`create_payment_checkout` calls, or `/admin` login attempts. Add rate
limiting (per IP, per phone number) before this is public -- otherwise
someone can spam Stripe session creation, tie up Gemini Live capacity, or
brute-force the admin key.

**Observability.** Logging is `print`/basic `logging` to stdout only. Before
real traffic, wire up structured logs, error tracking (e.g. Sentry), and
alerting on: webhook signature failures, Amadeus booking failures, DB
connection pool exhaustion, payment/booking count mismatches (a `paid`
payment with no corresponding booking row and no `support_requests` ticket
would mean the failure-resolution engine itself broke), and -- especially --
any Tier 4 escalation (`support_requests` row with `priority = 'urgent'`).
Right now the only signal is an SMS to `OPS_NOTIFICATION_PHONE`, if set; that
is not a substitute for real paging.

**PII / data retention.** `travel_bookings` and `payments` store name, DOB,
phone, email indefinitely with no retention policy. Depending on your
jurisdiction (GDPR, CCPA, Vietnam's PDPD, etc.) you likely need a retention/
deletion policy and a documented lawful basis for processing this data.

**Compliance disclosures.** A voice agent that books travel and takes payment
typically needs, at minimum: clear terms of service, a refund/cancellation
policy shown before payment, and (depending on jurisdiction) recording/
consent disclosure for the voice interaction itself.

**Load testing.** Nothing here has been load-tested. Concurrent Gemini Live
sessions, Amadeus rate limits, and the DB pool size (`DB_POOL_MAX`) all need
tuning against real expected concurrency before go-live.

**Multi-currency rounding at scale.** The zero-decimal currency list in
`payments/stripe_client.py` covers common cases; if you expect offers in
currencies not listed there, extend it (Stripe's list is authoritative:
https://docs.stripe.com/currencies#zero-decimal).

**Access control on the deployed service.** `deploy/DEPLOY.md` removes
`--allow-unauthenticated` by default. Decide your actual auth story (signed-
in frontend, API gateway, IAP) before pointing this at a public URL with real
payment collection enabled.
