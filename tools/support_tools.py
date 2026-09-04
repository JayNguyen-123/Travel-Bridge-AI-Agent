# tools/support_tools.py
"""
Escalation path for anything this system can't safely automate -- most
importantly date/itinerary changes and reissuance, which the Amadeus
self-service tier this project uses generally doesn't support end-to-end
(full fare recalculation + reissue typically needs a higher GDS access tier
or a dedicated servicing integration; confirm what your actual Amadeus
contract includes before promising customers self-service changes).

Rather than have the agent apologize into the void, this logs a real,
queryable request and notifies both the customer and (optionally) an ops
phone number.
"""
import asyncio
import os

from db.database import get_db_connection, release_db_connection
from notifications.sms import send_sms
from tools.booking_tools import _lookup_booking_sync

VALID_REQUEST_TYPES = {"date_change", "refund_review", "general"}
# "booking_failure" is a valid request_type too, but it's written directly by
# tools/booking_tools.py's Tier 4 escalation (a code path, not the agent), so
# it's intentionally left out of the agent-facing set above.


def _request_human_support_sync(reference_code: str, last_name: str, request_details: str, request_type: str = "general") -> dict:
    if request_type not in VALID_REQUEST_TYPES:
        request_type = "general"

    lookup = _lookup_booking_sync(reference_code, last_name)
    if "error" in lookup:
        return lookup

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO support_requests (reference_code, traveler_name, phone_number, request_type, details, idempotency_key)
                SELECT %s, tb.traveler_name, tb.phone_number, %s, %s, tb.idempotency_key
                FROM travel_bookings tb WHERE tb.reference_code = %s
                """,
                (reference_code, request_type, request_details, reference_code),
            )
            cur.execute(
                "SELECT phone_number FROM travel_bookings WHERE reference_code = %s",
                (reference_code,),
            )
            phone_row = cur.fetchone()
        conn.commit()
    finally:
        release_db_connection(conn)

    phone_number = phone_row[0] if phone_row else None
    if phone_number:
        send_sms(
            phone_number,
            f"We've logged your request about booking {reference_code}. "
            f"A support agent will follow up with you directly -- this is not automated.",
        )

    ops_phone = os.environ.get("OPS_NOTIFICATION_PHONE")
    if ops_phone:
        send_sms(ops_phone, f"[Support queue] {request_type} for {reference_code}: {request_details[:140]}")

    return {"logged": True, "reference_code": reference_code, "request_type": request_type}


async def request_human_support(reference_code: str, last_name: str, request_details: str, request_type: str = "general") -> dict:
    """
    Logs a request for a human support agent to follow up -- use this for
    anything you cannot resolve yourself, most importantly date/itinerary
    changes and reissuance, which are NOT automated in this system. Also use
    it for a cancellation refund that came back "pending_manual_review" if
    the customer wants to check on it.

    Never tell the customer a date/itinerary change is done, or promise a
    specific refund amount for a non-24-hour cancellation -- only a human
    reviewing the fare rules and Amadeus's response can determine that.

    Args:
        reference_code (str): The booking's confirmation code.
        last_name (str): Traveler's last name, to verify against the booking.
        request_details (str): Plain-language summary of what they need,
            in their own words / your best transcription of it.
        request_type (str): "date_change", "refund_review", or "general".

    Returns:
        dict: {"logged": true, ...} on success, or an {"error": ...} dict
        ("not_found"/"identity_mismatch") if the booking/name didn't match --
        in that case do not log a request, ask the customer to double check
        their reference code and name.
    """
    return await asyncio.to_thread(_request_human_support_sync, reference_code, last_name, request_details, request_type)
