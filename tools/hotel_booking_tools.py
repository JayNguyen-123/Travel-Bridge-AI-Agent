# tools/hotel_booking_tools.py
"""
GUARANTEE-policy-only hotel booking. See HOTEL_BOOKING_SCOPE.md for the full
design rationale; this is the short version.

A hotel offer's own `policies.paymentType` (see _hotel_offer_payment_policy)
is one of GUARANTEE / DEPOSIT / PREPAY, set by the property/rate, not by us:

  - GUARANTEE: a card secures the room; the property charges the guest
    directly at check-in/checkout. Nothing is charged through this app.
  - DEPOSIT / PREPAY: a real charge is due now or at booking time.

This module supports GUARANTEE ONLY. DEPOSIT/PREPAY offers are refused with
a clear error (`policy_not_supported`) -- the orchestrator routes those to
request_human_support instead of attempting them. That's a deliberate scope
cut, not an oversight: charging a DEPOSIT/PREPAY offer would need a real
payment-architecture decision (see HOTEL_BOOKING_SCOPE.md section 1) that
hasn't been made.

Because a GUARANTEE booking never charges anything through this app, there
is no Stripe step and no `payments` row for hotels -- unlike flights, guest
collection goes straight to Amadeus order creation. Idempotency is handled
differently as a result: instead of threading an idempotency_key through a
prior checkout call (there is no prior call), this module derives one
deterministically from the exact offer + guest list, so an accidental retry
with the same inputs naturally reuses the same booking instead of creating a
duplicate.

============================================================================
UNVERIFIED, load-bearing assumption -- read before trusting this in
production:

    Whether Amadeus's Hotel Booking API (POST /v2/booking/hotel-orders)
    actually allows creating a GUARANTEE order WITHOUT submitting card data
    in the request's `payment` block could not be confirmed while this was
    built: Amadeus's own documentation pages returned no usable technical
    content when fetched directly, and this environment had no outbound
    network path to Amadeus's API to test it live (see HOTEL_BOOKING_SCOPE.md
    for what was and wasn't reachable). The `policies.paymentType` field
    path itself, and the general v2 request/response shape referenced below,
    come from a secondary documentation mirror, not Amadeus's primary docs.

    This module is written to fail LOUDLY and SPECIFICALLY if that
    assumption is wrong: `_try_amadeus_hotel_booking` surfaces Amadeus's raw
    rejection reason verbatim rather than guessing or retrying blindly. The
    first real sandbox call against this code will either confirm the
    assumption or show exactly what Amadeus actually requires -- treat that
    as the real verification step this design doc called for, not this
    comment.
============================================================================
"""
import asyncio
import hashlib
import json
import os

import phonenumbers
import requests

from db.database import get_db_connection, release_db_connection
from notifications.sms import send_booking_confirmation_sms

AMADEUS_BASE_URL = os.environ.get("AMADEUS_BASE_URL", "https://test.api.amadeus.com")

# Same default and same "independent constant, not a shared import" reasoning
# as tools/flight_tools.py's MAX_PARTY_SIZE / tools/booking_tools.py's copy
# of it -- kept separate so this file has no dependency on the flight tool
# files. Guests across all rooms; v1 only supports a single room (see
# ROOM_QUANTITY below), so this is effectively "guests in that one room."
MAX_HOTEL_GUESTS = int(os.environ.get("MAX_HOTEL_GUESTS", "9"))

# v1 scope cut (HOTEL_BOOKING_SCOPE.md open question #3): single room only.
# Every guest is assigned room_number = 1. Multi-room support is a real
# feature, not just a constant to change -- it needs its own
# guest-to-room-assignment design in the orchestrator instructions too.
SINGLE_ROOM_NUMBER = 1


# ---------------------------------------------------------------------------
# Offer inspection
# ---------------------------------------------------------------------------

def _hotel_offer_payment_policy(hotel_offer: dict):
    """Best-effort extraction of an offer's payment policy type, returned
    upper-cased ('GUARANTEE' / 'DEPOSIT' / 'PREPAY'), or None if it can't be
    determined. Tries offer['policies']['paymentType'] first (the field path
    a documentation mirror showed -- see module docstring on why this isn't
    fully verified), then a couple of plausible fallback shapes, since the
    exact response structure for search vs. this offer JSON as later
    re-supplied by the agent could differ. Returns None rather than guessing
    when nothing recognizable is found -- callers must treat None as
    "not confirmed GUARANTEE" and refuse to book, never as "assume GUARANTEE."
    """
    if not isinstance(hotel_offer, dict):
        return None
    policies = hotel_offer.get("policies")
    if not isinstance(policies, dict):
        # Amadeus hotel search responses nest one offer per "offers" list
        # under the hotel; if the agent passed the whole search result
        # instead of a single offer, look one level down as a fallback.
        offers = hotel_offer.get("offers")
        if isinstance(offers, list) and offers and isinstance(offers[0], dict):
            policies = offers[0].get("policies")
    if not isinstance(policies, dict):
        return None

    payment_type = policies.get("paymentType")
    if isinstance(payment_type, str) and payment_type.strip():
        return payment_type.strip().upper()

    # Fallback: some shapes may express this as a single key present under
    # `policies` (e.g. {"guarantee": {...}}) rather than a paymentType
    # string. Best-effort only -- see the "returns None" note above.
    for candidate in ("guarantee", "deposit", "prepay"):
        if candidate in policies:
            return candidate.upper()
    return None


def _offer_hotel_id_and_name(hotel_offer: dict):
    hotel = hotel_offer.get("hotel", {}) if isinstance(hotel_offer.get("hotel"), dict) else {}
    return hotel.get("hotelId"), hotel.get("name")


def _offer_price_snapshot(hotel_offer: dict):
    """Informational only -- nothing is charged through this app for a
    GUARANTEE booking, so a missing/renamed price field here (see the
    key-translation note in HOTEL_BOOKING_SCOPE.md) only means the admin
    view shows a blank price, not a booking or security defect. Tries the
    raw Amadeus key first, then the Vietnamese-translated key that
    middleware/translator.py's process_and_convert_all renames "price" to,
    in case the agent is holding the already-translated search result
    rather than a raw offer."""
    price = hotel_offer.get("price") or hotel_offer.get("giá_tiền") or {}
    if not isinstance(price, dict):
        return None, None
    total = price.get("total") or price.get("amount")
    currency = price.get("currency") or price.get("tiền_tệ")
    return total, currency


# ---------------------------------------------------------------------------
# Guest validation
# ---------------------------------------------------------------------------

def _validate_guests(guests) -> str:
    """Returns an error string, or "" if well-formed. Mirrors
    tools/booking_tools.py's _validate_travelers -- same required fields,
    same non-negotiable shape checks, same reasoning for each."""
    if not isinstance(guests, list) or not guests:
        return "guests_json_str must decode to a non-empty JSON array of guest objects."
    if len(guests) > MAX_HOTEL_GUESTS:
        return (
            f"This system supports booking up to {MAX_HOTEL_GUESTS} guests at once "
            f"({len(guests)} were provided). For a larger group, please use "
            f"request_human_support instead."
        )
    for i, g in enumerate(guests, start=1):
        if not isinstance(g, dict):
            return f"guest #{i} is not an object."
        missing = [f for f in ("first_name", "last_name") if not g.get(f)]
        if missing:
            return f"guest #{i} is missing required field(s): {', '.join(missing)}."
    return ""


def _parse_guest_phone(phone_number: str):
    """Returns (country_calling_code, national_number) or raises ValueError.
    Duplicated from tools/booking_tools.py's _parse_traveler_phone rather
    than imported, on purpose -- see the module docstring's note on keeping
    hotel and flight tool files independent."""
    try:
        parsed = phonenumbers.parse(phone_number, None)
        if not phonenumbers.is_valid_number(parsed):
            raise ValueError("number failed validity check")
        return str(parsed.country_code), str(parsed.national_number)
    except (phonenumbers.NumberParseException, ValueError) as e:
        raise ValueError(f"phone_number must be a valid full international number (e.g. +12025551234): {e}")


# ---------------------------------------------------------------------------
# Amadeus payload + call
# ---------------------------------------------------------------------------

def _make_hotel_idempotency_key(hotel_offer_json_str: str, guests_json_str: str, contact_phone_number: str) -> str:
    """Deterministic dedup key -- see module docstring on why this differs
    from the flight flow's caller-threaded idempotency_key. Retrying with
    the exact same offer/guests/phone naturally collides with the same key
    and short-circuits to the existing booking (see
    _confirm_hotel_booking_sync); a genuinely new attempt (different offer,
    or a corrected guest list) naturally gets a new key instead."""
    digest_input = "|".join([hotel_offer_json_str, guests_json_str, contact_phone_number]).encode("utf-8")
    return hashlib.sha256(digest_input).hexdigest()[:32]


def _build_hotel_booking_payload(hotel_offer: dict, guests: list, contact_email: str,
                                  country_calling_code: str, national_number: str) -> dict:
    """Builds the Amadeus v2 hotel-order request body. Per the schema this
    was built against (see module docstring), room association only needs
    the offer's own `id` -- NOT the full offer object echoed back the way
    flight order creation needs the full flightOffers array -- which is
    good news for the middleware key-translation issue noted in
    HOTEL_BOOKING_SCOPE.md: this payload never embeds the (possibly
    key-renamed) offer object, only its `id` string.

    Deliberately omits the `payment` block entirely for a GUARANTEE booking
    -- this is the exact unverified assumption flagged at the top of this
    file. If Amadeus actually requires one regardless of policy, the booking
    call will fail with a specific 400-style error naming the missing field,
    surfaced verbatim by _try_amadeus_hotel_booking rather than silently
    mishandled here.
    """
    offer_id = hotel_offer.get("id")
    amadeus_guests = []
    room_associations_guest_refs = []
    for i, guest in enumerate(guests, start=1):
        tid = i  # Amadeus guest "tid" -- see the traveler-id caveat below.
        amadeus_guests.append({
            "tid": tid,
            "title": "MR" if str(guest.get("gender", "")).upper() == "MALE" else "MRS",
            "firstName": guest["first_name"].upper(),
            "lastName": guest["last_name"].upper(),
            "phone": f"+{country_calling_code}{national_number}",
            "email": guest.get("email") or contact_email,
        })
        room_associations_guest_refs.append({"guestReference": str(tid)})

    return {
        "data": {
            "type": "hotel-order",
            "guests": amadeus_guests,
            "roomAssociations": [{
                "guestReferences": room_associations_guest_refs,
                "hotelOfferId": offer_id,
            }],
            # No "payment" key -- see docstring above.
        }
    }


def _try_amadeus_hotel_booking(booking_payload: dict):
    """One attempt to POST a hotel-order to Amadeus. Returns (result_json,
    None) on success, or (None, error_info) on failure, where
    error_info = {'status_code': int|None, 'details': str}. Deliberately no
    retry/rebook tiering here the way tools/booking_tools.py has for
    flights -- a failed GUARANTEE booking never charged anything, so "tell
    the agent plainly it didn't work" is a complete, honest answer; there is
    nothing to refund and nothing silently lost either way."""
    token = os.environ.get("AMADEUS_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        response = requests.post(
            f"{AMADEUS_BASE_URL}/v2/booking/hotel-orders", headers=headers, json=booking_payload, timeout=15,
        )
    except requests.exceptions.RequestException as e:
        return None, {"status_code": None, "details": f"network error: {e}"}

    if response.status_code in (200, 201):
        return response.json(), None
    return None, {"status_code": response.status_code, "details": response.text}


def _extract_hotel_confirmation(result_json: dict):
    """Best-effort extraction of (hotel_order_id, reference_code) from a
    successful booking response. UNVERIFIED (see module docstring) --
    tries several plausible field paths and falls back to the order id
    itself if nothing more specific is found, rather than crashing or
    returning a placeholder that looks like a real confirmation number."""
    data = result_json.get("data", {}) if isinstance(result_json, dict) else {}
    hotel_order_id = data.get("id", "UNKNOWN_ID")

    reference_code = data.get("providerConfirmationId")
    if not reference_code:
        hotel_bookings = data.get("hotelBookings")
        if isinstance(hotel_bookings, list) and hotel_bookings and isinstance(hotel_bookings[0], dict):
            reference_code = hotel_bookings[0].get("hotelProviderInformation", {}).get("confirmationNumber")
    if not reference_code:
        associated = data.get("associatedRecords")
        if isinstance(associated, list) and associated and isinstance(associated[0], dict):
            reference_code = associated[0].get("reference")
    if not reference_code:
        reference_code = hotel_order_id

    return hotel_order_id, reference_code


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------

def _confirm_hotel_booking_sync(hotel_offer_json_str: str, guests_json_str: str,
                                 contact_phone_number: str, contact_email: str) -> dict:
    try:
        guests = json.loads(guests_json_str)
    except Exception as e:
        return {"error": "invalid_guests", "details": f"guests_json_str was not valid JSON: {e}"}

    validation_error = _validate_guests(guests)
    if validation_error:
        return {"error": "invalid_guests", "details": validation_error}

    try:
        country_calling_code, national_number = _parse_guest_phone(contact_phone_number)
    except ValueError as e:
        return {"error": "invalid_phone_number", "details": str(e)}

    try:
        hotel_offer = json.loads(hotel_offer_json_str)
    except Exception as e:
        return {"error": "invalid_hotel_offer", "details": str(e)}

    policy = _hotel_offer_payment_policy(hotel_offer)
    if policy != "GUARANTEE":
        return {
            "error": "policy_not_supported",
            "details": (
                f"This offer's payment policy is {policy or 'unknown/undetermined'}, not GUARANTEE. "
                f"Only pay-at-property (GUARANTEE) hotel offers can be booked automatically today -- "
                f"route this to request_human_support instead of attempting it."
            ),
        }

    idempotency_key = _make_hotel_idempotency_key(hotel_offer_json_str, guests_json_str, contact_phone_number)

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # Idempotent short-circuit: an identical retry (same offer, same
            # guests, same phone) returns the existing booking rather than
            # attempting a duplicate Amadeus order.
            cur.execute(
                "SELECT reference_code, hotel_name, status FROM hotel_bookings WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            existing = cur.fetchone()
            if existing:
                reference_code, hotel_name, status = existing
                return {
                    "reference_code": reference_code,
                    "hotel_name": hotel_name,
                    "status": status,
                    "idempotency_key": idempotency_key,
                    "note": "This exact booking was already placed -- returning the existing confirmation.",
                }

            booking_payload = _build_hotel_booking_payload(
                hotel_offer, guests, contact_email, country_calling_code, national_number,
            )
            result_json, error = _try_amadeus_hotel_booking(booking_payload)
            if result_json is None:
                return {
                    "error": "hotel_booking_failed",
                    "details": (
                        f"Amadeus rejected the booking (status {error.get('status_code')}): "
                        f"{error.get('details')}. Nothing was charged -- no payment is collected for a "
                        f"GUARANTEE booking through this app. If this keeps failing, use "
                        f"request_human_support."
                    ),
                }

            hotel_order_id, reference_code = _extract_hotel_confirmation(result_json)
            hotel_id, hotel_name = _offer_hotel_id_and_name(hotel_offer)
            price_amount, currency_type = _offer_price_snapshot(hotel_offer)
            lead_guest_name = f"{guests[0]['first_name']} {guests[0]['last_name']}"
            if len(guests) > 1:
                lead_guest_name += f" (+{len(guests) - 1} more)"

            cur.execute(
                """
                INSERT INTO hotel_bookings
                    (idempotency_key, hotel_order_id, reference_code, hotel_id, hotel_name,
                     lead_guest_name, phone_number, check_in_date, check_out_date, room_quantity,
                     price_amount, currency_type, payment_policy, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'GUARANTEE', 'booked')
                """,
                (
                    idempotency_key, hotel_order_id, reference_code, hotel_id, hotel_name,
                    lead_guest_name, contact_phone_number,
                    hotel_offer.get("checkInDate"), hotel_offer.get("checkOutDate"), 1,
                    price_amount, currency_type,
                ),
            )
            for i, guest in enumerate(guests, start=1):
                cur.execute(
                    """
                    INSERT INTO hotel_booking_guests
                        (idempotency_key, amadeus_guest_tid, room_number, first_name, last_name)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (idempotency_key, amadeus_guest_tid) DO NOTHING
                    """,
                    (idempotency_key, str(i), SINGLE_ROOM_NUMBER, guest["first_name"], guest["last_name"]),
                )
        conn.commit()
    finally:
        release_db_connection(conn)

    send_booking_confirmation_sms(
        to_phone=contact_phone_number,
        traveler_name=guests[0]["first_name"],
        booking_type="đặt phòng khách sạn (Hotel)",
        reference_code=reference_code,
    )

    return {
        "reference_code": reference_code,
        "hotel_order_id": hotel_order_id,
        "hotel_name": hotel_name,
        "status": "booked",
        "idempotency_key": idempotency_key,
        "note": (
            "GUARANTEE booking -- nothing was charged. The property will charge the guest's own card "
            "at check-in/checkout per that hotel's policy."
        ),
    }


async def confirm_hotel_booking(hotel_offer_json_str: str, guests_json_str: str,
                                 contact_phone_number: str, contact_email: str) -> dict:
    """
    Books a hotel room -- GUARANTEE-policy offers ONLY. Nothing is charged
    through this app: the property bills the guest's own card at the
    property per that offer's terms. There is no payment step before this
    call, unlike confirm_flight_booking.

    Args:
        hotel_offer_json_str (str): The exact hotel offer JSON, unchanged,
            from a prior search_hotels result. MUST have a payment policy
            of GUARANTEE (check the offer's policies/paymentType before
            calling this -- see the workflow instructions) or this call
            returns 'policy_not_supported'.
        guests_json_str (str): JSON array of every guest staying in the
            room, e.g. '[{"first_name": "Jane", "last_name": "Doe"}]'.
            Capped at MAX_HOTEL_GUESTS (default 9) -- a larger group must go
            through request_human_support. v1 books a single room only.
        contact_phone_number (str): Full international format (e.g.
            +12025551234) -- used for every guest's contact record and the
            SMS confirmation.
        contact_email (str): Contact email for the party, used as the
            fallback contact email for any guest who doesn't have their own
            "email" field in guests_json_str.

    Returns:
        dict: The booking confirmation on success (reference_code to read
        back to the customer, hotel_order_id, hotel_name). On failure, an
        {"error": ...} dict:
          - "invalid_guests": guests_json_str didn't parse, was empty, a
            guest is missing a required field, or the party exceeds
            MAX_HOTEL_GUESTS -- see `details`.
          - "invalid_phone_number": contact_phone_number wasn't a valid
            full international number.
          - "invalid_hotel_offer": hotel_offer_json_str didn't parse.
          - "policy_not_supported": the offer isn't GUARANTEE-policy (or its
            policy couldn't be determined) -- this system cannot book it;
            route to request_human_support instead of retrying.
          - "hotel_booking_failed": Amadeus rejected the booking outright --
            see `details` for its raw reason. Nothing was charged either
            way. If this keeps happening, it may mean the "no card needed
            for GUARANTEE" assumption this tool was built on is wrong --
            see this module's docstring and HOTEL_BOOKING_SCOPE.md.
    """
    return await asyncio.to_thread(
        _confirm_hotel_booking_sync, hotel_offer_json_str, guests_json_str, contact_phone_number, contact_email,
    )


def _lookup_hotel_booking_sync(reference_code: str, last_name: str) -> dict:
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT idempotency_key, status, hotel_name, lead_guest_name, price_amount,
                       currency_type, check_in_date, check_out_date, cancelled_at
                FROM hotel_bookings WHERE reference_code = %s
                """,
                (reference_code,),
            )
            row = cur.fetchone()
            if not row:
                return {"error": "not_found", "details": "No hotel booking found with that reference code."}

            (idempotency_key, status, hotel_name, lead_guest_name, price_amount,
             currency_type, check_in_date, check_out_date, cancelled_at) = row

            cur.execute(
                "SELECT first_name, last_name FROM hotel_booking_guests WHERE idempotency_key = %s ORDER BY id",
                (idempotency_key,),
            )
            guest_rows = cur.fetchall()
    finally:
        release_db_connection(conn)

    guests_list = [f"{fn} {ln}" for fn, ln in guest_rows]
    name_matches = any(last_name.strip().upper() in (ln or "").upper() for _, ln in guest_rows)
    if not name_matches and not (guest_rows == [] and last_name.strip().upper() in (lead_guest_name or "").upper()):
        return {"error": "identity_mismatch", "details": "That last name doesn't match this booking."}

    return {
        "idempotency_key": idempotency_key,
        "status": status,
        "hotel_name": hotel_name,
        "guest_count": len(guests_list) or 1,
        "guests": guests_list,
        "price_amount": price_amount,
        "currency_type": currency_type,
        "check_in_date": str(check_in_date) if check_in_date else None,
        "check_out_date": str(check_out_date) if check_out_date else None,
        "cancelled_at": str(cancelled_at) if cancelled_at else None,
    }


async def lookup_hotel_booking_by_reference(reference_code: str, last_name: str) -> dict:
    """
    Looks up a hotel booking by its confirmation code and verifies the
    caller's identity against any guest on the booking (not just the lead
    guest) before revealing anything about it. Never confirm a cancellation
    or read out booking details without this check passing first.

    Args: reference_code (str), last_name (str) -- last name of ANY guest on
    the booking, used purely to verify the caller has a right to this
    booking's details.

    Returns: dict with status/guest details on success, or {"error":
    "not_found"} / {"error": "identity_mismatch"} -- never reveal which one
    on failure; ask the customer to double-check both values.
    """
    return await asyncio.to_thread(_lookup_hotel_booking_sync, reference_code, last_name)


def _cancel_hotel_booking_sync(idempotency_key: str) -> dict:
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT hotel_order_id, reference_code, status FROM hotel_bookings WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            row = cur.fetchone()
            if not row:
                return {"error": "not_found", "details": "No hotel booking found for that idempotency_key."}
            hotel_order_id, reference_code, status = row
            if status == "cancelled":
                return {"reference_code": reference_code, "status": "cancelled", "note": "Already cancelled."}
    finally:
        release_db_connection(conn)

    # UNVERIFIED endpoint (see module docstring): guessed by analogy with
    # flight orders' DELETE /v1/booking/flight-orders/{id}. Confirm against
    # a live sandbox call or Amadeus's own docs before relying on this.
    token = os.environ.get("AMADEUS_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.delete(
            f"{AMADEUS_BASE_URL}/v2/booking/hotel-orders/{hotel_order_id}", headers=headers, timeout=15,
        )
    except requests.exceptions.RequestException as e:
        return {"error": "amadeus_unreachable", "details": str(e)}

    if response.status_code not in (200, 204):
        return {"error": f"amadeus_cancel_failed ({response.status_code})", "details": response.text}

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE hotel_bookings SET status = 'cancelled', cancelled_at = now() WHERE idempotency_key = %s",
                (idempotency_key,),
            )
        conn.commit()
    finally:
        release_db_connection(conn)

    # No refund logic at all -- a GUARANTEE booking never charged anything
    # through this app, so there is nothing to refund here. Any no-show
    # penalty the property itself charges is between the property and the
    # guest's card on file, outside this system entirely.
    return {"reference_code": reference_code, "status": "cancelled"}


async def cancel_hotel_booking_request(idempotency_key: str) -> dict:
    """
    Cancels a GUARANTEE hotel booking. Always call
    lookup_hotel_booking_by_reference first to verify the caller's identity
    and get a clear cancellation confirmation from them before calling this
    -- there is no separate identity check inside this function.

    Since a GUARANTEE booking never charged anything through this app,
    there is no refund to process here -- cancellation just stops the
    reservation. Tell the customer plainly that any no-show/late-cancellation
    penalty the property itself may apply is between them and the property,
    per that hotel's own policy, not something this system controls or can
    see.

    Returns: {"reference_code": ..., "status": "cancelled"} on success, or
    an {"error": ...} dict ("not_found", "amadeus_unreachable",
    "amadeus_cancel_failed (<status>)") -- never claim a cancellation
    succeeded when this returned an error.
    """
    return await asyncio.to_thread(_cancel_hotel_booking_sync, idempotency_key)
