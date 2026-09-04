# tests/test_itinerary_notifications.py
"""
Unit tests for the itinerary notifications added to close the "who sends
the user their airplane itinerary" gap:
- tools/booking_tools.py's _flight_itinerary_legs/_format_leg_line, which
  flatten an Amadeus flight offer into simple per-leg display data.
- notifications/sms.py's send_booking_confirmation_sms now optionally
  appending itinerary lines, while staying backward compatible with the
  hotel-booking call site that never passes any.
- notifications/email.py's send_flight_itinerary_email, including its
  fail-open behavior (never raises) when SendGrid credentials or a
  recipient address are missing.
"""
import notifications.email as email_mod
import notifications.sms as sms_mod
import tools.booking_tools as booking_tools
from notifications.sms import send_booking_confirmation_sms


def _offer_with_itineraries():
    return {
        "price": {"currency": "USD", "total": "742.30"},
        "itineraries": [
            {
                "segments": [
                    {
                        "departure": {"iataCode": "SGN", "at": "2026-07-01T08:30:00"},
                        "arrival": {"iataCode": "BKK", "at": "2026-07-01T10:00:00"},
                        "carrierCode": "VN",
                        "number": "603",
                    },
                ],
            },
            {
                "segments": [
                    {
                        "departure": {"iataCode": "BKK", "at": "2026-07-08T14:00:00"},
                        "arrival": {"iataCode": "SGN", "at": "2026-07-08T15:45:00"},
                        "carrierCode": "VN",
                        "number": "604",
                    },
                ],
            },
        ],
    }


# --- _flight_itinerary_legs / _format_leg_line -------------------------------

def test_flight_itinerary_legs_flattens_every_segment_across_itineraries():
    legs = booking_tools._flight_itinerary_legs(_offer_with_itineraries())
    assert len(legs) == 2
    assert legs[0]["origin"] == "SGN" and legs[0]["destination"] == "BKK"
    assert legs[1]["origin"] == "BKK" and legs[1]["destination"] == "SGN"
    assert legs[0]["carrier_code"] == "VN" and legs[0]["flight_number"] == "603"


def test_flight_itinerary_legs_formats_display_datetimes():
    legs = booking_tools._flight_itinerary_legs(_offer_with_itineraries())
    assert legs[0]["departure_display"] == "Jul 01, 08:30"
    assert legs[0]["arrival_display"] == "Jul 01, 10:00"


def test_flight_itinerary_legs_empty_for_offer_with_no_itineraries():
    assert booking_tools._flight_itinerary_legs({}) == []


def test_flight_itinerary_legs_skips_malformed_segment_instead_of_raising():
    # Missing "arrival" entirely -- must be skipped, not blow up a booking
    # that already succeeded just because the SMS/email formatting choked.
    offer = {"itineraries": [{"segments": [{"departure": {"iataCode": "SGN"}}]}]}
    assert booking_tools._flight_itinerary_legs(offer) == []


def test_format_leg_line_matches_expected_compact_shape():
    legs = booking_tools._flight_itinerary_legs(_offer_with_itineraries())
    line = booking_tools._format_leg_line(legs[0])
    assert line == "VN603 SGN→BKK Jul 01, 08:30→Jul 01, 10:00"


def test_format_leg_datetime_falls_back_to_raw_string_on_bad_input():
    assert booking_tools._format_leg_datetime("not-a-date") == "not-a-date"
    assert booking_tools._format_leg_datetime(None) == "?"


# --- send_booking_confirmation_sms (richer SMS, backward compatible) --------

def test_confirmation_sms_appends_itinerary_lines_when_provided(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        sms_mod, "send_sms",
        lambda to_phone, message_body: captured.update(to=to_phone, body=message_body) or {"sent": True},
    )
    legs = booking_tools._flight_itinerary_legs(_offer_with_itineraries())
    lines = [booking_tools._format_leg_line(leg) for leg in legs]

    send_booking_confirmation_sms(
        to_phone="+15551234567", traveler_name="Jane",
        booking_type="vé máy bay (Flight)", reference_code="ABC123",
        itinerary_lines=lines,
    )
    assert "ABC123" in captured["body"]
    assert lines[0] in captured["body"]
    assert lines[1] in captured["body"]


def test_confirmation_sms_backward_compatible_with_no_itinerary_lines(monkeypatch):
    # This is exactly how tools/hotel_booking_tools.py still calls it.
    captured = {}
    monkeypatch.setattr(
        sms_mod, "send_sms",
        lambda to_phone, message_body: captured.update(body=message_body) or {"sent": True},
    )
    send_booking_confirmation_sms(
        to_phone="+15551234567", traveler_name="Jane",
        booking_type="hotel (Hotel)", reference_code="HTL999",
    )
    assert "HTL999" in captured["body"]
    # No stray itinerary section appended when none was given.
    assert captured["body"].count("\n") == 2


# --- send_flight_itinerary_email ---------------------------------------------

def test_itinerary_email_fails_open_when_sendgrid_credentials_missing(monkeypatch):
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    monkeypatch.delenv("SENDGRID_FROM_EMAIL", raising=False)
    result = email_mod.send_email("someone@example.com", "Subject", "<p>hi</p>")
    assert result == {"sent": False, "reason": "sendgrid_credentials_missing"}


def test_itinerary_email_fails_open_when_recipient_missing(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.fake")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "bookings@example.com")
    result = email_mod.send_email("", "Subject", "<p>hi</p>")
    assert result == {"sent": False, "reason": "no_recipient_email"}


def test_itinerary_email_sends_via_sendgrid_and_reports_status(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.fake")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "bookings@example.com")

    class _FakeResponse:
        status_code = 202

    class _FakeSendGridAPIClient:
        def __init__(self, api_key):
            self.api_key = api_key

        def send(self, message):
            return _FakeResponse()

    monkeypatch.setattr(email_mod, "SendGridAPIClient", _FakeSendGridAPIClient)
    monkeypatch.setattr(email_mod, "Mail", lambda **kwargs: kwargs)

    legs = booking_tools._flight_itinerary_legs(_offer_with_itineraries())
    result = email_mod.send_flight_itinerary_email(
        to_email="jane@example.com", traveler_name="Jane", reference_code="ABC123",
        itinerary_legs=legs,
        travelers=[{"first_name": "Jane", "last_name": "Doe"}],
        total_price="742.30", currency="USD",
    )
    assert result == {"sent": True, "status_code": 202}


def test_itinerary_email_never_raises_on_sendgrid_exception(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.fake")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "bookings@example.com")

    class _ExplodingClient:
        def __init__(self, api_key):
            pass

        def send(self, message):
            raise RuntimeError("SendGrid is down")

    monkeypatch.setattr(email_mod, "SendGridAPIClient", _ExplodingClient)
    monkeypatch.setattr(email_mod, "Mail", lambda **kwargs: kwargs)

    result = email_mod.send_flight_itinerary_email(
        to_email="jane@example.com", traveler_name="Jane", reference_code="ABC123",
        itinerary_legs=[], travelers=[], total_price="0.00", currency="USD",
    )
    assert result["sent"] is False
    assert "SendGrid is down" in result["reason"]
