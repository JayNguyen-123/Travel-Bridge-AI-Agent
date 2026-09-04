# tools/booking_tools.py
"""
Finalizes a flight booking -- but only once a real, webhook-verified payment
exists for the exact offer being booked. This is the core of the production
payment design:

  1. create_payment_checkout (payment_tools.py) derives the charge amount from
     the Amadeus offer itself and creates a Stripe Checkout session + a
     'pending' row in `payments`, keyed by a server-generated idempotency_key.
  2. Only the Stripe webhook handler (server/main.py), which verifies Stripe's
     signature, is allowed to flip that row to status='paid'.
  3. confirm_flight_booking (this file) looks up the payment row by
     idempotency_key and refuses to proceed unless status == 'paid' AND the
     stored offer_hash matches the flight offer being booked right now (so a
     cheaper offer can't be paid for and a pricier one booked in its place).

The agent/LLM never gets to assert "the user paid" in a way this code
believes -- it can only ask, via check_payment_status, what the database
(populated solely by the verified webhook) actually says.
"""
import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

import phonenumbers
import requests
import stripe

from db.database import get_db_connection, release_db_connection
from middleware.translator import process_and_convert_all
from notifications.email import send_flight_itinerary_email
from notifications.sms import send_booking_confirmation_sms, send_sms
from payments.stripe_client import create_refund, price_from_flight_offer, to_stripe_amount
from tools.flight_tools import _post_multi_city_search

AMADEUS_BASE_URL = os.environ.get("AMADEUS_BASE_URL", "https://test.api.amadeus.com")

# ---------------------------------------------------------------------------
# Booking failure-resolution engine
#
# What happens if the Amadeus booking call itself fails *after* payment has
# already been verified? The customer has paid; something must happen next
# other than a bare {"error": ...} the agent apologizes into. Four tiers, run
# in order, each falling through to the next only if it can't resolve things:
#
#   Tier 1 -- Auto-Retry Engine: retry the exact same booking a few times.
#             Only for errors that look transient (network hiccup, Amadeus
#             5xx/429) -- a definitive "fare no longer available" error skips
#             straight to Tier 2 rather than retrying something that can't
#             succeed.
#   Tier 2 -- Fare Availability Check: re-search the same route/date. If a
#             comparable fare exists priced within FARE_REBOOK_THRESHOLD_PERCENT
#             of what the customer already paid, book that instead -- the
#             business absorbs any difference; the customer is never charged
#             more than they already paid.
#   Tier 3 -- Auto-Refund & Notify: no acceptable fare exists -- issue a full
#             Stripe refund for the original payment and tell the customer.
#   Tier 4 -- Manual Ops Queue: the refund itself failed. Escalate to
#             support_requests as 'urgent' and notify both the customer and
#             ops (OPS_NOTIFICATION_PHONE) -- this is the only tier that can
#             leave the customer's outcome undetermined, and it's the one
#             that must never fail silently.
#
# Every path out of this engine leaves the customer in one of three known
# states: booked (Tier 1/2), refunded (Tier 3), or an urgent human ticket
# (Tier 4). None of them leave a charge with nothing recorded about it.
# ---------------------------------------------------------------------------
AMADEUS_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
BOOKING_RETRY_ATTEMPTS = int(os.environ.get("BOOKING_RETRY_ATTEMPTS", "2"))  # extra attempts beyond the first
BOOKING_RETRY_BACKOFF_SECONDS = float(os.environ.get("BOOKING_RETRY_BACKOFF_SECONDS", "1.5"))
FARE_REBOOK_THRESHOLD_PERCENT = float(os.environ.get("FARE_REBOOK_THRESHOLD_PERCENT", "10"))

# Same default as tools/flight_tools.py's MAX_PARTY_SIZE (kept as an
# independent constant, not a shared import, to avoid a cross-module
# dependency between two otherwise-independent ADK tool files). Enforced
# here too so a caller can't route around the search-time cap by simply
# passing a longer travelers_json_str than what was actually searched --
# _validate_traveler_count would already catch a length mismatch against the
# offer, but this catches the oversized-group case on its own terms with a
# clearer message.
MAX_PARTY_SIZE = int(os.environ.get("MAX_PARTY_SIZE", "9"))

# DOT 14 CFR 259.5(b)(4): a reservation made 7+ days before departure can be
# cancelled without penalty for 24 hours after booking. We apply this as a
# blanket policy (not just for US-covered carriers) since we -- not the
# airline -- are the merchant of record collecting payment, and it removes a
# real dispute/chargeback risk regardless of the rule's exact legal scope.
# This is a business/compliance default, not a legal conclusion -- confirm
# against counsel for your actual jurisdictions before launch.
FREE_CANCELLATION_WINDOW = timedelta(hours=24)
FREE_CANCELLATION_MIN_DEPARTURE_LEAD = timedelta(days=7)


def _extract_earliest_departure(flight_offer: dict):
    """Pulls the first segment's departure datetime out of an Amadeus flight
    offer (itineraries[0].segments[0].departure.at), used only to evaluate
    the free-cancellation window. Returns a timezone-aware datetime, or None
    if the offer doesn't have the expected shape."""
    try:
        itineraries = flight_offer.get("itineraries", [])
        departure_str = itineraries[0]["segments"][0]["departure"]["at"]
        dt = datetime.fromisoformat(departure_str)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def _as_utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _format_leg_datetime(iso_str) -> str:
    """'2026-07-01T08:30:00' -> 'Jul 01, 08:30'. Falls back to the raw string
    (or '?') rather than raising -- this only feeds human-facing SMS/email
    text, never a booking or money decision."""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%b %d, %H:%M")
    except (ValueError, TypeError):
        return iso_str or "?"


def _flight_itinerary_legs(flight_offer: dict) -> list:
    """Flattens an Amadeus flight offer's itineraries[].segments[] into a
    flat list of simple per-flight-leg dicts, for the SMS/email confirmations
    sent after a real booking succeeds -- see the 'who sends the itinerary'
    gap this closes in README.md. Purely a display concern: never used for
    pricing or booking decisions, and defensive about a missing/malformed
    segment (skips it rather than raising) since a formatting problem here
    should never blow up a booking that already succeeded."""
    legs = []
    for itinerary in flight_offer.get("itineraries", []) or []:
        for segment in itinerary.get("segments", []) or []:
            try:
                departure = segment["departure"]
                arrival = segment["arrival"]
                legs.append({
                    "carrier_code": segment.get("carrierCode", ""),
                    "flight_number": segment.get("number", ""),
                    "origin": departure["iataCode"],
                    "destination": arrival["iataCode"],
                    "departure_at": departure["at"],
                    "arrival_at": arrival["at"],
                    "departure_display": _format_leg_datetime(departure["at"]),
                    "arrival_display": _format_leg_datetime(arrival["at"]),
                })
            except (KeyError, TypeError):
                continue
    return legs


def _format_leg_line(leg: dict) -> str:
    """One compact line per flight leg for SMS, e.g.
    'VN603 SGN→BKK Jul 01, 08:30→Jul 01, 10:00'."""
    flight_code = f"{leg.get('carrier_code', '')}{leg.get('flight_number', '')}".strip() or "?"
    return (
        f"{flight_code} {leg.get('origin', '?')}→{leg.get('destination', '?')} "
        f"{leg.get('departure_display', '?')}→{leg.get('arrival_display', '?')}"
    )


def is_eligible_for_free_cancellation(booked_at, departure_at, now=None) -> bool:
    """Pure DOT-24-hour-rule eligibility check, kept separate from any I/O so
    it's directly unit-testable. All three args are datetimes (naive treated
    as UTC)."""
    if booked_at is None or departure_at is None:
        return False
    now = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    booked_at = _as_utc(booked_at)
    departure_at = _as_utc(departure_at)
    within_window = (now - booked_at) <= FREE_CANCELLATION_WINDOW
    departed_far_enough_out = (departure_at - booked_at) >= FREE_CANCELLATION_MIN_DEPARTURE_LEAD
    return within_window and departed_far_enough_out


def _parse_traveler_phone(phone_number: str):
    """Returns (country_calling_code, national_number) or raises ValueError.
    Replaces the old draft's `phone_number.replace("+84", "")` hack, which
    assumed every traveler had a Vietnamese number."""
    try:
        parsed = phonenumbers.parse(phone_number, None)
        if not phonenumbers.is_valid_number(parsed):
            raise ValueError("number failed validity check")
        return str(parsed.country_code), str(parsed.national_number)
    except (phonenumbers.NumberParseException, ValueError) as e:
        raise ValueError(f"phone_number must be a valid full international number (e.g. +12025551234): {e}")


def _traveler_pricing_slots(flight_offer: dict) -> list:
    """Returns the traveler "slots" Amadeus already priced this offer for --
    [{"id": travelerId, "travelerType": ...}, ...] -- taken from the offer's
    own travelerPricings (set when it was searched with a given adults/
    children/infants count). This matters because the order-creation
    payload's travelers[].id values are NOT ours to invent: they must reuse
    exactly the ids Amadeus already assigned in travelerPricings, in the
    same order (adults, then children, then infants -- Amadeus's own
    convention). A booking with N travelers only works if the offer itself
    was priced for N -- see _validate_traveler_count below."""
    pricings = flight_offer.get("travelerPricings", [])
    return [{"id": p.get("travelerId"), "travelerType": p.get("travelerType", "ADULT")} for p in pricings]


def _passenger_counts(flight_offer: dict) -> dict:
    """Counts adults/children/infants from the offer's travelerPricings.
    Used to re-search Tier 2 with the SAME party composition instead of
    silently searching as if it were a single adult (which would find a
    cheaper-looking fare that can't actually seat the whole group)."""
    counts = {"adults": 0, "children": 0, "infants": 0}
    for slot in _traveler_pricing_slots(flight_offer):
        t = slot["travelerType"]
        if t == "CHILD":
            counts["children"] += 1
        elif t in ("HELD_INFANT", "SEATED_INFANT"):
            counts["infants"] += 1
        else:
            counts["adults"] += 1  # ADULT, or an unrecognized type -- default bucket
    if counts["adults"] == 0:
        counts["adults"] = 1  # Amadeus search requires at least one adult
    return counts


def _validate_traveler_count(flight_offer: dict, travelers: list):
    """Returns an error string if `travelers` doesn't have exactly as many
    entries as the offer was priced for, or None if it's fine (or
    unverifiable -- an offer with no travelerPricings is let through and
    left for Amadeus itself to accept or reject)."""
    slots = _traveler_pricing_slots(flight_offer)
    if not slots:
        return None
    if len(slots) != len(travelers):
        return (
            f"This offer was priced for {len(slots)} traveler(s), but {len(travelers)} were provided. "
            f"The number of travelers must exactly match how many were included in the flight search "
            f"(adults + children + infants)."
        )
    return None


def _build_booking_payload(flight_offer: dict, travelers: list, slots: list,
                            contact_email: str, country_calling_code: str, national_number: str) -> dict:
    """Builds the Amadeus flight-order travelers array by zipping caller-
    supplied traveler details with the offer's own pricing slots (same
    order: adults first, then children, then infants) -- so each traveler's
    Amadeus `id` is always one the offer was actually priced for, never one
    this app made up."""
    amadeus_travelers = []
    for slot, traveler in zip(slots, travelers):
        amadeus_travelers.append({
            "id": slot["id"],
            "dateOfBirth": traveler["date_of_birth"],
            "name": {"firstName": traveler["first_name"].upper(), "lastName": traveler["last_name"].upper()},
            "gender": traveler["gender"].upper(),
            "contact": {
                "emailAddress": traveler.get("email") or contact_email,
                "phones": [{
                    "deviceType": "MOBILE",
                    "countryCallingCode": country_calling_code,
                    "number": national_number,
                }],
            },
        })
    return {"data": {"type": "flight-order", "flightOffers": [flight_offer], "travelers": amadeus_travelers}}


def _traveler_summary(travelers: list) -> str:
    """Short display string for a traveler list, safe for a VARCHAR column
    (travel_bookings.traveler_name / support_requests.traveler_name) --
    the full roster always lives in booking_travelers or in a ticket's
    free-text `details`/`reason`, never truncated there."""
    if not travelers:
        return "unknown traveler"
    lead = f"{travelers[0]['first_name']} {travelers[0]['last_name']}"
    return lead if len(travelers) == 1 else f"{lead} (+{len(travelers) - 1} more)"


def _try_amadeus_booking(booking_payload: dict):
    """One attempt to POST a flight-order to Amadeus.
    Returns (result_json, None) on success, or (None, error_info) on failure,
    where error_info = {'retryable': bool, 'status_code': int|None, 'details': str}."""
    token = os.environ.get("AMADEUS_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        response = requests.post(f"{AMADEUS_BASE_URL}/v1/booking/flight-orders", headers=headers, json=booking_payload, timeout=15)
    except requests.exceptions.RequestException as e:
        return None, {"retryable": True, "status_code": None, "details": f"network error: {e}"}

    if response.status_code in (200, 201):
        return response.json(), None

    retryable = response.status_code in AMADEUS_RETRYABLE_STATUS_CODES
    return None, {"retryable": retryable, "status_code": response.status_code, "details": response.text}


def _tier1_retry_booking(booking_payload: dict):
    """Tier 1: retries the booking up to (1 + BOOKING_RETRY_ATTEMPTS) times,
    but only continues retrying while the failure looks transient. Returns
    (result_json, None) on success, or (None, last_error_info) if every
    attempt failed."""
    last_error = None
    total_attempts = 1 + max(BOOKING_RETRY_ATTEMPTS, 0)
    for attempt in range(1, total_attempts + 1):
        result, error = _try_amadeus_booking(booking_payload)
        if result is not None:
            return result, None
        last_error = error
        print(f"[Booking Tier1] attempt {attempt}/{total_attempts} failed: {error}")
        if not error.get("retryable") or attempt == total_attempts:
            break
        time.sleep(BOOKING_RETRY_BACKOFF_SECONDS * attempt)
    return None, last_error


def _extract_all_legs(flight_offer: dict) -> list:
    """Pulls [{"origin", "destination", "date"}, ...] out of an Amadeus
    flight offer, one entry per itinerary, in order -- for re-searching in
    Tier 2. A one-way offer yields a single-item list; a round-trip offer
    (search_flights's returnDate) yields two; a genuine multi-city offer
    (search_multi_city_flights) yields as many as were searched. Each leg's
    `origin`/`destination` come from that itinerary's first departure and
    last arrival (so a connecting itinerary with layovers still reduces to
    one leg), and `date` from the first segment's departure date. Raises on
    an unexpected offer shape (caller treats that as Tier 2 being
    unavailable)."""
    legs = []
    for itinerary in flight_offer["itineraries"]:
        segments = itinerary["segments"]
        legs.append({
            "origin": segments[0]["departure"]["iataCode"],
            "destination": segments[-1]["arrival"]["iataCode"],
            "date": segments[0]["departure"]["at"][:10],
        })
    return legs


def _is_simple_round_trip(legs: list) -> bool:
    """True if `legs` (from _extract_all_legs) is exactly a there-and-back
    pair -- leg 2 flies back from where leg 1 landed to where leg 1 started
    -- the shape search_flights's `returnDate` produces, re-searchable with
    the simple GET endpoint search_flights itself uses. Checking only that
    leg 2 ends back at leg 1's origin is NOT enough: an open-jaw trip (e.g.
    SGN->BKK, then HAN->SGN) also ends at the same city but departs from a
    different one, and needs the multi-city POST re-search
    (_post_multi_city_search) instead, same as 3+ legs."""
    return (
        len(legs) == 2
        and legs[1]["origin"] == legs[0]["destination"]
        and legs[1]["destination"] == legs[0]["origin"]
    )


def _within_rebook_threshold(paid_stripe_amount: int, alt_stripe_amount: int, threshold_percent: float) -> bool:
    """Pure comparison, kept separate from any I/O so it's directly
    unit-testable: is an alternative fare's price close enough to what the
    customer already paid that the business can absorb the difference
    without charging them again?"""
    if paid_stripe_amount is None or paid_stripe_amount <= 0:
        return False
    return alt_stripe_amount <= paid_stripe_amount * (1 + threshold_percent / 100)


def _tier2_fare_recheck_and_rebook(flight_offer: dict, travelers: list, contact_email: str,
                                    country_calling_code: str, national_number: str,
                                    paid_stripe_amount, paid_currency):
    """Tier 2: re-searches the original route/date(s) -- with the SAME party
    composition (adults/children/infants) as the original offer, not just
    "1 adult" -- for a comparable fare. If the original offer was a round
    trip (a second itinerary present), the re-search also asks for the same
    return date, so a rebook here can't silently downgrade a round-trip
    booking to one-way (see _extract_all_legs/_is_simple_round_trip). If the
    original offer was a genuine multi-city itinerary (3+ legs, or an
    open-jaw 2-leg trip that doesn't return to its starting city), the
    re-search goes through the same multi-city POST search multi-city
    bookings use (tools/flight_tools.py's _post_multi_city_search), so a
    failed multi-city booking still gets an automatic rebook attempt on the
    same multi-leg route instead of skipping straight to Tier 3. If the cheapest
    comparable fare found is within FARE_REBOOK_THRESHOLD_PERCENT of what
    was already paid, books it instead (absorbing any difference). Returns
    (result_json, booked_offer, None) on success, or (None, None,
    reason_str) if nothing acceptable was found or the rebooking attempt
    itself failed."""
    try:
        all_legs = _extract_all_legs(flight_offer)
    except (KeyError, IndexError, TypeError) as e:
        return None, None, f"could not determine route/date to re-search: {e}"

    counts = _passenger_counts(flight_offer)
    token = os.environ.get("AMADEUS_ACCESS_TOKEN")

    if len(all_legs) >= 2 and not _is_simple_round_trip(all_legs):
        raw_json, post_error = _post_multi_city_search(all_legs, counts["adults"], counts["children"], counts["infants"], token)
        if post_error:
            detail = post_error.get("details") or post_error.get("error") or "unknown error"
            return None, None, f"multi-city fare re-search failed: {detail}"
        offers = raw_json.get("data", [])
    else:
        origin, destination, departure_date = all_legs[0]["origin"], all_legs[0]["destination"], all_legs[0]["date"]
        return_date = all_legs[1]["date"] if len(all_legs) == 2 else None

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        params = {
            "originLocationCode": origin, "destinationLocationCode": destination,
            "departureDate": departure_date, "adults": counts["adults"],
        }
        if counts["children"]:
            params["children"] = counts["children"]
        if counts["infants"]:
            params["infants"] = counts["infants"]
        if return_date:
            params["returnDate"] = return_date

        try:
            resp = requests.get(f"{AMADEUS_BASE_URL}/v2/shopping/flight-offers", headers=headers, params=params, timeout=10)
        except requests.exceptions.RequestException as e:
            return None, None, f"fare re-search failed: {e}"

        if resp.status_code != 200:
            return None, None, f"fare re-search returned status {resp.status_code}"

        offers = resp.json().get("data", [])

    if not offers:
        return None, None, "no comparable fares currently available on this route/date for the same party size"

    def _offer_total(o):
        try:
            return float(o.get("price", {}).get("total", "inf"))
        except (TypeError, ValueError):
            return float("inf")

    cheapest = min(offers, key=_offer_total)
    try:
        alt_total, alt_currency = price_from_flight_offer(cheapest)
        alt_stripe_amount = to_stripe_amount(alt_total, alt_currency)
    except ValueError:
        return None, None, "the cheapest alternative fare had no usable price"

    if alt_currency != paid_currency:
        return None, None, f"alternative fare currency ({alt_currency}) differs from paid currency ({paid_currency})"

    if not _within_rebook_threshold(paid_stripe_amount, alt_stripe_amount, FARE_REBOOK_THRESHOLD_PERCENT):
        return None, None, (
            f"cheapest alternative ({alt_stripe_amount}) exceeds the "
            f"{FARE_REBOOK_THRESHOLD_PERCENT:.0f}% absorb threshold over what was paid ({paid_stripe_amount})"
        )

    alt_slots = _traveler_pricing_slots(cheapest)
    if len(alt_slots) != len(travelers):
        # The re-search asked for the same adults/children/infants counts,
        # so this shouldn't happen -- but never zip a mismatched traveler
        # list against a fresh offer's ids.
        return None, None, "the alternative fare's traveler-pricing count didn't match the original party size"

    alt_payload = _build_booking_payload(cheapest, travelers, alt_slots, contact_email, country_calling_code, national_number)
    result, error = _try_amadeus_booking(alt_payload)
    if result is None:
        return None, None, f"rebooking the alternative fare also failed: {error}"

    return result, cheapest, None


def _tier3_auto_refund(payment_intent_id: str, amount: int):
    """Tier 3. Returns (refund_id, None) on success, or (None, error_str)."""
    try:
        refund = create_refund(payment_intent_id, amount=amount)
        return refund.id, None
    except stripe.error.StripeError as e:
        return None, str(e)


def _tier4_escalate(*, idempotency_key: str, phone_number: str, traveler_name: str, reason: str) -> dict:
    """Tier 4: the last resort. Logs an urgent support ticket and marks the
    payment as needing manual review, then notifies both the customer and
    ops -- this is the one path in the whole engine that must never fail
    silently, since it's what runs when everything before it already has."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO support_requests
                    (traveler_name, phone_number, request_type, priority, details, idempotency_key)
                VALUES (%s, %s, 'booking_failure', 'urgent', %s, %s)
                """,
                (traveler_name, phone_number, reason, idempotency_key),
            )
            cur.execute(
                """
                UPDATE payments
                SET status = 'failed_needs_manual_review', resolution_notes = %s, updated_at = now()
                WHERE idempotency_key = %s
                """,
                (reason, idempotency_key),
            )
        conn.commit()
    finally:
        release_db_connection(conn)

    if phone_number:
        send_sms(
            phone_number,
            "We're sorry -- we ran into an issue completing your flight booking. A support agent has been "
            "notified and will follow up with you directly to resolve this, including your payment.",
        )
    ops_phone = os.environ.get("OPS_NOTIFICATION_PHONE")
    if ops_phone:
        send_sms(ops_phone, f"[URGENT booking failure] idempotency_key={idempotency_key}: {reason[:200]}")

    print(f"[Booking Tier4 escalated] idempotency_key={idempotency_key}: {reason}")
    return {
        "error": "booking_failed_escalated",
        "message": "The flight could not be booked, and the automatic refund also failed. "
                    "A human support agent has been notified and will resolve this directly.",
    }


def _resolve_unbookable_payment(*, idempotency_key: str, payment_intent_id, amount_total, currency,
                                 phone_number: str, traveler_name: str, tier1_error, tier2_reason: str) -> dict:
    """Tier 3 + Tier 4: the flight could not be booked (original attempt,
    retries, and a comparable-fare rebook all failed or weren't possible),
    but the customer already paid. Always resolves to one of two known
    states -- refunded, or an urgent human ticket -- never a silent
    unresolved charge."""
    failure_summary = f"Amadeus booking unresolved. Tier1: {tier1_error}. Tier2: {tier2_reason}."

    if not payment_intent_id or not amount_total:
        # Shouldn't happen -- the webhook only marks a payment 'paid' once
        # Stripe reports a payment_intent -- but never guess a refund amount.
        return _tier4_escalate(
            idempotency_key=idempotency_key, phone_number=phone_number, traveler_name=traveler_name,
            reason=f"{failure_summary} No payment_intent_id/amount on file to auto-refund.",
        )

    refund_id, refund_error = _tier3_auto_refund(payment_intent_id, amount_total)
    if refund_id:
        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE payments
                    SET status = 'refunded', refund_id = %s, refund_amount = %s,
                        refunded_at = now(), resolution_notes = %s, updated_at = now()
                    WHERE idempotency_key = %s
                    """,
                    (refund_id, amount_total, failure_summary, idempotency_key),
                )
            conn.commit()
        finally:
            release_db_connection(conn)

        send_sms(
            phone_number,
            "We're sorry -- we weren't able to complete your flight booking, so we've issued a full refund. "
            "It should appear on your statement within a few business days.",
        )
        print(f"[Booking Tier3 success] idempotency_key={idempotency_key}: auto-refunded {amount_total} {currency}")
        return {
            "error": "booking_failed_refunded",
            "message": "The flight could not be booked, so a full refund was issued automatically.",
            "refund_status": "issued",
            "refund_amount": amount_total,
            "currency": currency,
        }

    return _tier4_escalate(
        idempotency_key=idempotency_key, phone_number=phone_number, traveler_name=traveler_name,
        reason=f"{failure_summary} Auto-refund also failed: {refund_error}.",
    )


def _validate_travelers(travelers) -> str:
    """Returns an error string, or "" if the traveler list is well-formed
    enough to proceed (individual field values like dateOfBirth are not
    format-checked here -- same as the original single-traveler code, Amadeus
    is the final arbiter of those)."""
    if not isinstance(travelers, list) or not travelers:
        return "travelers_json_str must decode to a non-empty JSON array of traveler objects."
    if len(travelers) > MAX_PARTY_SIZE:
        return (
            f"This system supports booking up to {MAX_PARTY_SIZE} travelers at once "
            f"({len(travelers)} were provided). For a larger group, please use "
            f"request_human_support instead."
        )
    for i, t in enumerate(travelers, start=1):
        if not isinstance(t, dict):
            return f"traveler #{i} is not an object."
        missing = [f for f in ("first_name", "last_name", "date_of_birth", "gender") if not t.get(f)]
        if missing:
            return f"traveler #{i} is missing required field(s): {', '.join(missing)}."
        if str(t["gender"]).upper() not in ("MALE", "FEMALE"):
            return f"traveler #{i}: gender must be MALE or FEMALE, got: {t['gender']!r}."
    return ""


def _confirm_flight_booking_sync(
    flight_offer_json_str: str,
    travelers_json_str: str,
    contact_phone_number: str,
    contact_email: str,
    idempotency_key: str,
) -> dict:
    try:
        travelers = json.loads(travelers_json_str)
    except Exception as e:
        return {"error": "invalid_travelers", "details": f"travelers_json_str was not valid JSON: {e}"}

    validation_error = _validate_travelers(travelers)
    if validation_error:
        return {"error": "invalid_travelers", "details": validation_error}

    try:
        country_calling_code, national_number = _parse_traveler_phone(contact_phone_number)
    except ValueError as e:
        return {"error": "invalid_phone_number", "details": str(e)}

    try:
        flight_offer = json.loads(flight_offer_json_str)
    except Exception as e:
        return {"error": "invalid_flight_offer", "details": str(e)}

    count_error = _validate_traveler_count(flight_offer, travelers)
    if count_error:
        return {"error": "traveler_count_mismatch", "details": count_error}

    offer_hash = hashlib.sha256(flight_offer_json_str.encode("utf-8")).hexdigest()

    # --- verify payment BEFORE touching Amadeus or spending any money ---
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, offer_hash, payment_intent_id, amount_total, currency "
                "FROM payments WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            payment_row = cur.fetchone()

            if not payment_row:
                return {
                    "error": "payment_required",
                    "details": "No payment record for this idempotency_key. Call create_payment_checkout first.",
                }
            payment_status, stored_offer_hash, payment_intent_id, amount_total, payment_currency = payment_row
            if payment_status != "paid":
                return {
                    "error": "payment_required",
                    "details": f"Payment status is '{payment_status}', not 'paid'. "
                               f"Call check_payment_status, or ask the user to finish paying.",
                }
            if stored_offer_hash != offer_hash:
                return {
                    "error": "offer_mismatch",
                    "details": "The flight offer being booked does not match the one that was paid for.",
                }

            # idempotent re-confirmation: if this key already produced a booking, return it
            cur.execute(
                "SELECT booking_id, reference_code FROM travel_bookings WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            existing = cur.fetchone()
            if existing:
                return {
                    "info": "duplicate_request_returned_existing_booking",
                    "booking_id": existing[0],
                    "reference_code": existing[1],
                }
    finally:
        release_db_connection(conn)
        conn = None

    # --- payment verified: now actually book with Amadeus, via the staged
    # failure-resolution engine defined above (Tier 1 retry -> Tier 2
    # rebook -> Tier 3 refund -> Tier 4 escalate) if the booking call itself
    # doesn't succeed on the first try ---
    slots = _traveler_pricing_slots(flight_offer)
    booking_payload = _build_booking_payload(flight_offer, travelers, slots, contact_email, country_calling_code, national_number)

    try:
        result_json, tier1_error = _tier1_retry_booking(booking_payload)
        booked_offer = flight_offer
        resolution_path = None
        booking_notes = None

        if result_json is None:
            print(f"[Booking Tier1 exhausted] idempotency_key={idempotency_key}: {tier1_error}")
            alt_result, alt_offer, tier2_reason = _tier2_fare_recheck_and_rebook(
                flight_offer, travelers, contact_email, country_calling_code, national_number,
                amount_total, payment_currency,
            )
            if alt_result is not None:
                result_json = alt_result
                booked_offer = alt_offer
                resolution_path = "tier2_rebook"
                booking_notes = f"Original fare unavailable ({tier1_error}). Auto-rebooked a comparable fare within the absorb threshold."
                print(f"[Booking Tier2 success] idempotency_key={idempotency_key}: {booking_notes}")
            else:
                print(f"[Booking Tier2 failed] idempotency_key={idempotency_key}: {tier2_reason}")
                return _resolve_unbookable_payment(
                    idempotency_key=idempotency_key,
                    payment_intent_id=payment_intent_id,
                    amount_total=amount_total,
                    currency=payment_currency,
                    phone_number=contact_phone_number,
                    traveler_name=_traveler_summary(travelers),
                    tier1_error=tier1_error,
                    tier2_reason=tier2_reason,
                )

        # --- a booking succeeded (original attempt, a Tier 1 retry, or a
        # Tier 2 rebook) -- persist it exactly like the original happy path ---
        booking_id = result_json.get("data", {}).get("id", "UNKNOWN_ID")
        reference_code = result_json.get("data", {}).get("associatedRecords", [{}])[0].get("reference", "UNKNOWN_REF")
        total_price = booked_offer.get("price", {}).get("total", "0.00")
        currency = booked_offer.get("price", {}).get("currency", "USD")
        departure_at = _extract_earliest_departure(booked_offer)
        booked_slots = _traveler_pricing_slots(booked_offer)  # whichever offer actually got booked

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO travel_bookings
                        (idempotency_key, booking_id, reference_code, booking_type,
                         traveler_name, phone_number, price_amount, currency_type, departure_at,
                         resolution_path, booking_notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (idempotency_key) DO NOTHING;
                    """,
                    (idempotency_key, booking_id, reference_code, "FLIGHT",
                     _traveler_summary(travelers), contact_phone_number, total_price, currency, departure_at,
                     resolution_path, booking_notes),
                )
                for slot, traveler in zip(booked_slots, travelers):
                    cur.execute(
                        """
                        INSERT INTO booking_travelers
                            (idempotency_key, amadeus_traveler_id, first_name, last_name,
                             date_of_birth, gender, traveler_type)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (idempotency_key, amadeus_traveler_id) DO NOTHING
                        """,
                        (idempotency_key, slot["id"], traveler["first_name"], traveler["last_name"],
                         traveler["date_of_birth"], traveler["gender"].upper(), slot["travelerType"]),
                    )
            conn.commit()
        finally:
            release_db_connection(conn)
        print(f"[DB Success]: Logged FLIGHT booking row {reference_code} into database ({len(travelers)} traveler(s)).")

        itinerary_legs = _flight_itinerary_legs(booked_offer)
        itinerary_lines = [_format_leg_line(leg) for leg in itinerary_legs]

        send_booking_confirmation_sms(
            to_phone=contact_phone_number,
            traveler_name=travelers[0]["first_name"],
            booking_type="vé máy bay (Flight)" if len(travelers) == 1 else f"vé máy bay cho {len(travelers)} người (Flight for {len(travelers)})",
            reference_code=reference_code,
            itinerary_lines=itinerary_lines,
        )
        send_flight_itinerary_email(
            to_email=contact_email,
            traveler_name=travelers[0]["first_name"],
            reference_code=reference_code,
            itinerary_legs=itinerary_legs,
            travelers=travelers,
            total_price=total_price,
            currency=currency,
        )

        confirmation = process_and_convert_all(result_json)
        if resolution_path == "tier2_rebook":
            confirmation["rebooked_notice"] = (
                "Your original fare was no longer available at the moment of booking. "
                "We automatically secured a comparable fare for you at no extra charge."
            )
        return confirmation
    except Exception as e:
        return {"error": "booking_exception", "details": str(e)}


async def confirm_flight_booking(
    flight_offer_json_str: str,
    travelers_json_str: str,
    contact_phone_number: str,
    contact_email: str,
    idempotency_key: str,
) -> dict:
    """
    Finalizes a flight reservation via Amadeus for one or more travelers --
    but only after verifying a real, webhook-confirmed Stripe payment exists
    for this exact offer. Works for a solo booking or a group/family booking
    (the whole party is one booking, one payment, one confirmation code).

    Args:
        flight_offer_json_str (str): The exact flight offer JSON, unchanged
            from the one passed to create_payment_checkout.
        travelers_json_str (str): JSON array of every traveler on this
            booking, e.g.
            '[{"first_name": "John", "last_name": "Smith", "date_of_birth":
            "1985-04-02", "gender": "MALE"}, {"first_name": "Amy",
            "last_name": "Smith", "date_of_birth": "2015-06-01", "gender":
            "FEMALE"}]'. MUST have exactly as many entries as the offer was
            searched for (adults + children + infants), and MUST list them
            in that same order -- all adults first, then children, then
            infants -- matching the counts you passed to search_flights.
            Getting the order wrong assigns the wrong person to the wrong
            seat/fare type, so double-check it, don't just guess. Capped at
            MAX_PARTY_SIZE travelers (default 9) -- a larger group must go
            through request_human_support instead.
        contact_phone_number (str): Full international format (e.g.
            +12025551234) -- used for every traveler's contact record and
            for the SMS confirmation. One shared contact number for the
            whole party is normal and expected.
        contact_email (str): Contact email for the party, used as the
            fallback contact email for any traveler who doesn't have their
            own `"email"` field in travelers_json_str.
        idempotency_key (str): The key returned by create_payment_checkout.
            Reusing the same key on a retry safely returns the same booking
            instead of creating a duplicate.

    Returns:
        dict: The booking confirmation (translated/currency-converted) on
        success -- if it includes a "rebooked_notice" field, read it to the
        user: their original fare was gone and a comparable one was
        automatically booked at no extra charge.

        On failure, an {"error": ...} dict:
          - "invalid_travelers": travelers_json_str didn't parse, a
            traveler is missing a required field, or the party is larger
            than MAX_PARTY_SIZE -- see `details`.
          - "traveler_count_mismatch": the number of travelers you passed
            doesn't match how many the offer was searched/priced for --
            recount and try again, don't just drop or add someone.
          - "payment_required" / "offer_mismatch": fixable -- see `details`.
          - "booking_failed_refunded": the flight could not be booked after
            retrying and checking for an alternative fare, so a full refund
            was issued automatically. Tell the user plainly: no booking, but
            a refund is on the way (`refund_amount`/`currency` given).
          - "booking_failed_escalated": the booking failed AND the automatic
            refund failed. A human has already been notified -- tell the
            user a support agent will follow up directly on both the
            booking and the payment. Do not promise a specific refund
            amount or timeline yourself.
        Never claim success when a tool reported an error.
    """
    return await asyncio.to_thread(
        _confirm_flight_booking_sync,
        flight_offer_json_str, travelers_json_str, contact_phone_number, contact_email, idempotency_key,
    )


# ---------------------------------------------------------------------------
# Cancellation
#
# A customer calling back later has a reference_code, not an idempotency_key
# (that only ever lived inside their original booking session) -- so
# cancellation is a two-step lookup-then-act flow, verified by last name
# rather than trusting a bare reference code (which is guessable/short).
# ---------------------------------------------------------------------------

def _lookup_booking_sync(reference_code: str, last_name: str) -> dict:
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tb.idempotency_key, tb.status, tb.traveler_name, tb.price_amount,
                       tb.currency_type, tb.created_at, tb.departure_at
                FROM travel_bookings tb
                WHERE tb.reference_code = %s
                """,
                (reference_code,),
            )
            row = cur.fetchone()
            if not row:
                return {"error": "not_found", "details": "No booking found with that reference code."}

            idempotency_key = row[0]
            cur.execute(
                "SELECT first_name, last_name FROM booking_travelers WHERE idempotency_key = %s ORDER BY id",
                (idempotency_key,),
            )
            traveler_rows = cur.fetchall()
    finally:
        release_db_connection(conn)

    idempotency_key, status, traveler_name, price_amount, currency_type, created_at, departure_at = row

    if traveler_rows:
        # Group bookings: the caller is entitled to act on this booking if
        # their last name matches ANY traveler on it, not just the lead one
        # -- a family calling back about their trip won't always share the
        # lead traveler's exact surname.
        travelers_list = [f"{fn} {ln}" for fn, ln in traveler_rows]
        name_matches = any(last_name.strip().upper() in (ln or "").upper() for _, ln in traveler_rows)
    else:
        # No roster row (e.g. a booking made before booking_travelers
        # existed) -- fall back to the lead traveler_name field so older
        # bookings don't suddenly become unlookupable.
        travelers_list = [traveler_name] if traveler_name else []
        name_matches = last_name.strip().upper() in (traveler_name or "").upper()

    if not name_matches:
        # Deliberately vague -- don't confirm/deny whether the reference code
        # itself is real to someone who doesn't know a traveler's name.
        return {"error": "identity_mismatch", "details": "That name doesn't match our records for this booking."}

    eligible = is_eligible_for_free_cancellation(created_at, departure_at)

    return {
        "idempotency_key": idempotency_key,
        "reference_code": reference_code,
        "status": status,
        "traveler_name": traveler_name,
        "traveler_count": len(travelers_list) or 1,
        "travelers": travelers_list,
        "price_amount": price_amount,
        "currency": currency_type,
        "free_cancellation_eligible": eligible,
    }


async def lookup_booking_by_reference(reference_code: str, last_name: str) -> dict:
    """
    Looks up an existing booking for a customer calling back about it. Always
    call this before cancel_booking_request or request_human_support so you
    have the idempotency_key and can tell the customer what to expect.

    Args:
        reference_code (str): The confirmation code the customer was texted.
        last_name (str): A last name on the booking, used to verify the
            caller is entitled to act on this booking. For a group/family
            booking this matches against ANY traveler on it, not just the
            person the SMS confirmation was addressed to -- so it's fine if
            the caller isn't the lead traveler, as long as they know a real
            name on the reservation. Do not skip this check.

    Returns:
        dict: Booking summary plus `traveler_count`/`travelers` (everyone on
        this booking) and `free_cancellation_eligible` (whether this
        qualifies for an automatic full refund right now), or an {"error": ...}
        dict ("not_found" or "identity_mismatch") if you should not proceed.
        Cancellation is whole-booking-only: there is no way to cancel just
        one traveler out of a group while keeping the rest booked. If the
        customer wants that, this isn't the tool -- use
        request_human_support instead and say so plainly.
    """
    return await asyncio.to_thread(_lookup_booking_sync, reference_code, last_name)


def _cancel_booking_sync(idempotency_key: str) -> dict:
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tb.booking_id, tb.reference_code, tb.status, tb.created_at, tb.departure_at,
                       tb.phone_number, p.payment_intent_id, p.amount_total, p.currency
                FROM travel_bookings tb
                JOIN payments p ON p.idempotency_key = tb.idempotency_key
                WHERE tb.idempotency_key = %s
                """,
                (idempotency_key,),
            )
            row = cur.fetchone()
    finally:
        release_db_connection(conn)

    if not row:
        return {"error": "booking_not_found", "details": "No booking found for this idempotency_key."}

    (booking_id, reference_code, status, created_at, departure_at,
     phone_number, payment_intent_id, amount_total, currency) = row

    if status == "cancelled":
        return {"info": "already_cancelled", "reference_code": reference_code}

    # --- cancel with Amadeus first. Don't tell the customer it's cancelled
    # (or touch any refund) until Amadeus actually confirms it. ---
    token = os.environ.get("AMADEUS_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.delete(f"{AMADEUS_BASE_URL}/v1/booking/flight-orders/{booking_id}", headers=headers, timeout=15)
    except requests.exceptions.RequestException as e:
        return {"error": "amadeus_unreachable", "details": str(e)}

    if response.status_code not in (200, 204):
        return {"error": f"amadeus_cancel_failed ({response.status_code})", "details": response.text}

    eligible_for_auto_refund = is_eligible_for_free_cancellation(created_at, departure_at)

    refund_status = "none"
    refund_id = None
    refund_amount = None
    if eligible_for_auto_refund and payment_intent_id and amount_total:
        try:
            refund = create_refund(payment_intent_id, amount=amount_total)
            refund_id = refund.id
            refund_amount = amount_total
            refund_status = "full_auto"
        except stripe.error.StripeError as e:
            # Amadeus is already cancelled at this point -- we do not undo
            # that. A failed auto-refund becomes a human follow-up instead of
            # a silently lost refund.
            refund_status = "auto_refund_failed"
            print(f"[Refund Error]: Stripe refund failed for {reference_code}: {e}")
    else:
        refund_status = "pending_manual_review"

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE travel_bookings
                SET status = 'cancelled', cancelled_at = now(),
                    refund_status = %s, refund_id = %s, refund_amount = %s
                WHERE idempotency_key = %s
                """,
                (refund_status, refund_id, refund_amount, idempotency_key),
            )
        conn.commit()
    finally:
        release_db_connection(conn)

    if refund_status == "full_auto":
        sms_note = "A full refund has been issued and should appear on your statement within a few business days."
    elif refund_status == "auto_refund_failed":
        sms_note = "Our support team will follow up shortly to process your refund."
    else:
        sms_note = "Our support team will review your fare's refund policy and follow up on any refund due."
    send_sms(phone_number, f"Your booking {reference_code} has been cancelled. {sms_note}")

    return {
        "reference_code": reference_code,
        "cancelled": True,
        "refund_status": refund_status,
        "refund_amount": refund_amount,
        "currency": currency if refund_status == "full_auto" else None,
    }


async def cancel_booking_request(idempotency_key: str) -> dict:
    """
    Cancels an existing flight booking -- the WHOLE booking, every traveler on
    it, in one action. There is no partial/per-traveler cancellation; if the
    customer wants to cancel only some travelers on a group booking and keep
    the rest, this tool cannot do that -- use request_human_support instead
    and tell them plainly. ALWAYS call lookup_booking_by_reference first and
    tell the user whether free_cancellation_eligible is true (and, for a
    group booking, that cancelling affects every traveler listed) before
    calling this -- and get one explicit confirmation from them first, same
    as for a new booking.

    Args:
        idempotency_key (str): From a prior lookup_booking_by_reference call.

    Returns:
        dict: On success, `cancelled: true` plus `refund_status`:
          - "full_auto": booked within the last 24 hours (and departure was
            7+ days out at booking time) -- already fully refunded automatically.
          - "pending_manual_review": cancelled with the airline, but outside
            the automatic-refund window -- a human will determine any refund
            per the fare's rules. Tell the user this plainly; do not imply a
            refund amount you don't have.
          - "auto_refund_failed": cancelled, refund attempt errored, a human
            will follow up.
        On failure, an {"error": ...} dict -- report it, don't claim the
        booking was cancelled if this returns an error.
    """
    return await asyncio.to_thread(_cancel_booking_sync, idempotency_key)
