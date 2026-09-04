# notifications/sms.py
"""
Shared Twilio SMS helper. Pulled out of booking_tools.py into its own module
so both booking_tools.py and payment_tools.py can send texts without a
circular import between the two.
"""
import os

from twilio.rest import Client


def send_sms(to_phone: str, message_body: str) -> dict:
    """Sends a plain-text SMS via Twilio. Returns {'sent': bool, ...}."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_phone = os.environ.get("TWILIO_PHONE_NUMBER")

    if not all([account_sid, auth_token, from_phone]):
        print("[SMS Warning]: Twilio credentials missing. Skipping message dispatch.")
        return {"sent": False, "reason": "twilio_credentials_missing"}

    try:
        client = Client(account_sid, auth_token)
        message = client.messages.create(body=message_body, from_=from_phone, to=to_phone)
        print(f"[SMS Success]: Message sent via Twilio. SID: {message.sid}")
        return {"sent": True, "sid": message.sid}
    except Exception as e:
        print(f"[SMS Error]: Failed to send notification via Twilio: {e}")
        return {"sent": False, "reason": str(e)}


def send_booking_confirmation_sms(
    to_phone: str,
    traveler_name: str,
    booking_type: str,
    reference_code: str,
    itinerary_lines: list = None,
) -> dict:
    """`itinerary_lines` is optional so hotel bookings (which have no flight
    legs) can keep calling this the same way they always have -- see
    tools/hotel_booking_tools.py. When flight legs are supplied (see
    tools/booking_tools.py's _flight_itinerary_legs/_format_leg_line), each
    line is already-formatted plain text (flight code, route, times) and
    needs no further translation -- flight numbers/IATA codes/clock times
    read the same in either language."""
    message_body = (
        f"🎯 [Travel AI Agent]\n"
        f"Chào {traveler_name}, mã xác nhận {booking_type} của bạn là: {reference_code}.\n"
        f"Hi {traveler_name}, your {booking_type} confirmation code is: {reference_code}."
    )
    if itinerary_lines:
        message_body += "\n" + "\n".join(itinerary_lines)
    return send_sms(to_phone, message_body)


def send_payment_link_sms(to_phone: str, amount_display: str, checkout_url: str) -> dict:
    message_body = (
        f"🎯 [Travel AI Agent]\n"
        f"Vui lòng thanh toán {amount_display} tại: {checkout_url} (liên kết hết hạn sau 30 phút).\n"
        f"Please complete your {amount_display} payment here: {checkout_url} (link expires in 30 minutes)."
    )
    return send_sms(to_phone, message_body)
