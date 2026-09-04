-- db/schema.sql
-- Applied automatically at startup by db.database.init_schema().
-- Run manually against a new database if you'd rather manage migrations by hand.

CREATE TABLE IF NOT EXISTS payments (
    id                   SERIAL PRIMARY KEY,
    idempotency_key      VARCHAR(64) UNIQUE NOT NULL,
    checkout_session_id  VARCHAR(120) UNIQUE,
    payment_intent_id    VARCHAR(120),
    -- pending -> paid -> refunded | failed_needs_manual_review
    --         -> failed | expired
    -- 'paid' is set only by the Stripe webhook handler, never by a tool call
    -- the agent/LLM can influence. 'refunded' and 'failed_needs_manual_review'
    -- are set by the booking failure-resolution engine (tools/booking_tools.py,
    -- Tier 3/4) or by an admin issuing a manual refund (server/admin.py) --
    -- see resolution_notes for what happened.
    status               VARCHAR(30) NOT NULL DEFAULT 'pending',
    amount_total         BIGINT,        -- Stripe "smallest unit" integer (cents, or whole VND)
    currency             VARCHAR(10),
    customer_email       VARCHAR(255),
    customer_phone       VARCHAR(30),
    offer_hash           VARCHAR(64),   -- sha256 of the exact flight_offer_json_str that was priced
    -- Set when a payment is refunded because the flight could never be
    -- booked (Tier 3 auto-refund, or a manual admin refund) -- NOT used for
    -- the separate post-booking cancellation-refund flow, which tracks its
    -- own refund fields on travel_bookings below.
    refund_id            VARCHAR(120),
    refund_amount        BIGINT,
    refunded_at          TIMESTAMP,
    -- Free-text audit trail: what the failure-resolution engine tried, or
    -- what an admin did manually. NULL on the normal happy path.
    resolution_notes     TEXT,
    created_at           TIMESTAMP DEFAULT now(),
    updated_at           TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS travel_bookings (
    id                SERIAL PRIMARY KEY,
    idempotency_key   VARCHAR(64) UNIQUE NOT NULL REFERENCES payments(idempotency_key),
    booking_id        VARCHAR(100),
    reference_code    VARCHAR(50),
    booking_type      VARCHAR(20),
    traveler_name     VARCHAR(150),
    phone_number      VARCHAR(30),
    price_amount      VARCHAR(30),
    currency_type     VARCHAR(10),
    -- booked -> cancelled. Set only by cancel_booking_request, after Amadeus
    -- itself confirms the order was cancelled.
    status            VARCHAR(20) NOT NULL DEFAULT 'booked',
    -- Earliest segment departure time from the booked offer, used solely to
    -- evaluate the DOT 24-hour / 7-day free-cancellation window (14 CFR
    -- 259.5) at cancellation time.
    departure_at      TIMESTAMP,
    cancelled_at      TIMESTAMP,
    -- none | full_auto | pending_manual_review | auto_refund_failed
    --      | manual (an admin issued the refund via server/admin.py)
    --      | denied (an admin reviewed it and determined no refund is owed)
    refund_status     VARCHAR(30) NOT NULL DEFAULT 'none',
    refund_id         VARCHAR(120),   -- Stripe refund id, set on full_auto or manual
    refund_amount     BIGINT,         -- Stripe "smallest unit" integer, set on full_auto or manual
    -- Which tier of the booking failure-resolution engine produced this
    -- booking, if any: NULL (normal happy path) | 'tier1_retry' | 'tier2_rebook'.
    -- See tools/booking_tools.py.
    resolution_path   VARCHAR(30),
    -- Free-text notes from the failure-resolution engine (e.g. what the
    -- original fare's error was, if this was a Tier 2 rebook) and/or from an
    -- admin resolving a refund review through server/admin.py.
    booking_notes     TEXT,
    created_at        TIMESTAMP DEFAULT now()
);

-- One row per traveler actually placed on a booking's Amadeus order --
-- travel_bookings.traveler_name stays as the lead/contact traveler's display
-- name (used for SMS greetings and quick admin display); this table is the
-- real roster. A booking with N travelers has N rows here.
CREATE TABLE IF NOT EXISTS booking_travelers (
    id                   SERIAL PRIMARY KEY,
    idempotency_key      VARCHAR(64) NOT NULL REFERENCES travel_bookings(idempotency_key),
    -- The traveler "id" Amadeus itself assigned in the flight offer's
    -- travelerPricings when it was searched -- reused verbatim in the
    -- order-creation payload's travelers[].id. Never invented by this app;
    -- see _traveler_pricing_slots() in tools/booking_tools.py.
    amadeus_traveler_id  VARCHAR(10) NOT NULL,
    first_name           VARCHAR(100) NOT NULL,
    last_name            VARCHAR(100) NOT NULL,
    date_of_birth        DATE,
    gender               VARCHAR(10),
    -- ADULT | CHILD | HELD_INFANT | SEATED_INFANT -- taken from the offer's
    -- travelerPricings for this traveler id, not asserted by the caller.
    traveler_type        VARCHAR(20) NOT NULL DEFAULT 'ADULT',
    created_at           TIMESTAMP DEFAULT now(),
    UNIQUE (idempotency_key, amadeus_traveler_id)
);

CREATE INDEX IF NOT EXISTS idx_booking_travelers_idempotency_key ON booking_travelers (idempotency_key);

-- GUARANTEE-policy hotel bookings ONLY (see HOTEL_BOOKING_SCOPE.md). A
-- GUARANTEE offer charges nothing through this app -- the hotel bills the
-- guest's card at the property -- so, unlike travel_bookings, this table
-- does NOT reference payments(idempotency_key): there is no payment row to
-- reference. idempotency_key here is a deterministic hash this app computes
-- itself from the exact offer + guest list (see
-- tools/hotel_booking_tools.py's _make_hotel_idempotency_key), not a key
-- threaded through from a prior checkout step the way flights use.
--
-- UNVERIFIED ASSUMPTION, flagged repeatedly through this table and the code
-- that writes to it: that Amadeus's hotel-order API actually allows creating
-- a GUARANTEE booking without submitting card data. This was not confirmed
-- against a live Amadeus sandbox call (no outbound network to Amadeus was
-- available while this was built) -- see HOTEL_BOOKING_SCOPE.md.
CREATE TABLE IF NOT EXISTS hotel_bookings (
    id                     SERIAL PRIMARY KEY,
    idempotency_key        VARCHAR(64) UNIQUE NOT NULL,
    hotel_order_id         VARCHAR(100),   -- Amadeus hotel-order id from the v2 response
    reference_code         VARCHAR(50),    -- confirmation number read back to the customer
    hotel_id                VARCHAR(20),    -- Amadeus hotelId
    hotel_name              VARCHAR(200),
    lead_guest_name         VARCHAR(150),   -- same "lead traveler" convention as travel_bookings
    phone_number             VARCHAR(30),
    check_in_date             DATE,
    check_out_date            DATE,
    room_quantity              INT NOT NULL DEFAULT 1,
    price_amount               VARCHAR(30),   -- informational only for GUARANTEE -- nothing was charged through this app
    currency_type               VARCHAR(10),
    payment_policy               VARCHAR(20) NOT NULL DEFAULT 'GUARANTEE',
    -- booked -> cancelled. No refund_status/refund_id/refund_amount fields --
    -- unlike travel_bookings, a GUARANTEE booking never has a charge on this
    -- side to refund, so those columns would always be meaningless here.
    status                      VARCHAR(20) NOT NULL DEFAULT 'booked',
    cancellation_deadline        TIMESTAMP,   -- from the offer's own policies.cancellation, if present
    cancelled_at                 TIMESTAMP,
    booking_notes                 TEXT,
    created_at                     TIMESTAMP DEFAULT now()
);

-- One row per guest, mirrors booking_travelers. room_number reserved for a
-- future multi-room version; v1 requires room_number = 1 for every guest
-- (single-room bookings only).
CREATE TABLE IF NOT EXISTS hotel_booking_guests (
    id                     SERIAL PRIMARY KEY,
    idempotency_key        VARCHAR(64) NOT NULL REFERENCES hotel_bookings(idempotency_key),
    amadeus_guest_tid      VARCHAR(10) NOT NULL,  -- Amadeus guest "tid", reused in roomAssociations
    room_number             INT NOT NULL DEFAULT 1,
    first_name                VARCHAR(100) NOT NULL,
    last_name                  VARCHAR(100) NOT NULL,
    created_at                   TIMESTAMP DEFAULT now(),
    UNIQUE (idempotency_key, amadeus_guest_tid)
);

CREATE INDEX IF NOT EXISTS idx_hotel_bookings_reference_code ON hotel_bookings (reference_code);
CREATE INDEX IF NOT EXISTS idx_hotel_booking_guests_idempotency_key ON hotel_booking_guests (idempotency_key);

-- Anything this system can't safely automate (date/itinerary changes,
-- cancellations that fall outside the DOT safe-harbor window, and a booking
-- that failed even after the automatic failure-resolution engine tried
-- retrying/rebooking/refunding it) becomes a row here instead of a promise
-- the agent can't keep. Worked by a human through server/admin.py.
CREATE TABLE IF NOT EXISTS support_requests (
    id                SERIAL PRIMARY KEY,
    reference_code    VARCHAR(50),
    traveler_name     VARCHAR(150),
    phone_number      VARCHAR(30),
    -- date_change | refund_review | general | booking_failure (the last one
    -- is written only by the Tier 4 escalation in tools/booking_tools.py)
    request_type      VARCHAR(30) NOT NULL DEFAULT 'general',
    -- normal | urgent -- 'urgent' is reserved for Tier 4 (a customer who
    -- paid and has neither a booking nor a confirmed refund).
    priority          VARCHAR(10) NOT NULL DEFAULT 'normal',
    details           TEXT,
    status            VARCHAR(20) NOT NULL DEFAULT 'open',     -- open | in_progress | closed
    -- Links this ticket to its payment/booking (when one exists) so the
    -- admin UI can pull payment status and issue/deny a refund directly
    -- instead of the agent hunting down the reference code by hand.
    idempotency_key   VARCHAR(64) REFERENCES payments(idempotency_key),
    resolved_at       TIMESTAMP,
    resolved_by       VARCHAR(100),
    resolution_notes  TEXT,
    created_at        TIMESTAMP DEFAULT now()
);

-- Dedupes Stripe webhook deliveries (Stripe retries on non-2xx and can send
-- the same event more than once even on success).
CREATE TABLE IF NOT EXISTS processed_webhook_events (
    event_id      VARCHAR(100) PRIMARY KEY,
    processed_at  TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payments_checkout_session ON payments (checkout_session_id);
CREATE INDEX IF NOT EXISTS idx_bookings_reference_code ON travel_bookings (reference_code);
CREATE INDEX IF NOT EXISTS idx_support_requests_reference_code ON support_requests (reference_code);

-- Used by the admin queue (server/admin.py) to list open tickets and
-- bookings still awaiting a manual refund decision.
CREATE INDEX IF NOT EXISTS idx_support_requests_status ON support_requests (status);
CREATE INDEX IF NOT EXISTS idx_support_requests_idempotency_key ON support_requests (idempotency_key);
CREATE INDEX IF NOT EXISTS idx_bookings_refund_status ON travel_bookings (refund_status);
