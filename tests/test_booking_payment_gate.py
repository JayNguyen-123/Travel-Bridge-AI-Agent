"""
Verifies the core security property of tools/booking_tools.py: a booking can
only proceed with a webhook-verified, matching payment record -- and that
traveler input (a JSON array, to support group/family bookings) is validated
before any of that. Exercises `_confirm_flight_booking_sync` directly
(bypassing the asyncio.to_thread wrapper) against a fake DB connection so no
real Postgres is required.
"""
import hashlib
import json

import tools.booking_tools as booking_tools


class _FakeCursor:
    def __init__(self, fetchone_result):
        self._fetchone_result = fetchone_result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_args, **_kwargs):
        pass

    def fetchone(self):
        return self._fetchone_result


class _FakeConn:
    def __init__(self, fetchone_result):
        self._fetchone_result = fetchone_result

    def cursor(self):
        return _FakeCursor(self._fetchone_result)

    def commit(self):
        pass


FLIGHT_OFFER = json.dumps({"price": {"currency": "USD", "total": "500.00"}})
OFFER_HASH = hashlib.sha256(FLIGHT_OFFER.encode("utf-8")).hexdigest()

ONE_TRAVELER = json.dumps([{
    "first_name": "Jane", "last_name": "Doe",
    "date_of_birth": "1990-01-01", "gender": "FEMALE",
}])

BOOKING_KWARGS = dict(
    flight_offer_json_str=FLIGHT_OFFER,
    travelers_json_str=ONE_TRAVELER,
    # Deliberately avoids the 555 exchange (NANP reserves parts of it for
    # fictional use, e.g. movies/TV) so this doesn't risk failing
    # phonenumbers' is_valid_number() check for reasons unrelated to the
    # booking-gate logic under test.
    contact_phone_number="+14043721234",
    contact_email="jane@example.com",
    idempotency_key="test-key-123",
)

# payments SELECT now returns 5 columns: status, offer_hash, payment_intent_id,
# amount_total, currency -- see tools/booking_tools.py's payment-verify block.


def test_no_payment_record_blocks_booking(monkeypatch):
    monkeypatch.setattr(booking_tools, "get_db_connection", lambda: _FakeConn(None))
    monkeypatch.setattr(booking_tools, "release_db_connection", lambda conn: None)

    result = booking_tools._confirm_flight_booking_sync(**BOOKING_KWARGS)
    assert result["error"] == "payment_required"


def test_pending_payment_blocks_booking(monkeypatch):
    monkeypatch.setattr(
        booking_tools, "get_db_connection",
        lambda: _FakeConn(("pending", OFFER_HASH, None, None, None)),
    )
    monkeypatch.setattr(booking_tools, "release_db_connection", lambda conn: None)

    result = booking_tools._confirm_flight_booking_sync(**BOOKING_KWARGS)
    assert result["error"] == "payment_required"


def test_mismatched_offer_hash_blocks_booking(monkeypatch):
    monkeypatch.setattr(
        booking_tools, "get_db_connection",
        lambda: _FakeConn(("paid", "a-different-hash", "pi_123", 50000, "USD")),
    )
    monkeypatch.setattr(booking_tools, "release_db_connection", lambda conn: None)

    result = booking_tools._confirm_flight_booking_sync(**BOOKING_KWARGS)
    assert result["error"] == "offer_mismatch"


def test_invalid_phone_number_rejected_before_payment_check(monkeypatch):
    # Traveler validation and phone parsing both happen before the DB is
    # ever touched -- the fake connection here would raise if actually used,
    # so this also proves the ordering.
    monkeypatch.setattr(
        booking_tools, "get_db_connection",
        lambda: (_ for _ in ()).throw(AssertionError("DB should not be reached")),
    )

    kwargs = dict(BOOKING_KWARGS, contact_phone_number="not-a-phone-number")
    result = booking_tools._confirm_flight_booking_sync(**kwargs)
    assert result["error"] == "invalid_phone_number"


def test_invalid_traveler_gender_rejected():
    bad_traveler = json.dumps([{
        "first_name": "Jane", "last_name": "Doe",
        "date_of_birth": "1990-01-01", "gender": "OTHER",
    }])
    kwargs = dict(BOOKING_KWARGS, travelers_json_str=bad_traveler)
    result = booking_tools._confirm_flight_booking_sync(**kwargs)
    assert result["error"] == "invalid_travelers"


def test_missing_traveler_field_rejected():
    incomplete_traveler = json.dumps([{"first_name": "Jane", "last_name": "Doe", "gender": "FEMALE"}])  # no date_of_birth
    kwargs = dict(BOOKING_KWARGS, travelers_json_str=incomplete_traveler)
    result = booking_tools._confirm_flight_booking_sync(**kwargs)
    assert result["error"] == "invalid_travelers"


def test_empty_traveler_list_rejected():
    kwargs = dict(BOOKING_KWARGS, travelers_json_str="[]")
    result = booking_tools._confirm_flight_booking_sync(**kwargs)
    assert result["error"] == "invalid_travelers"


def test_malformed_travelers_json_rejected():
    kwargs = dict(BOOKING_KWARGS, travelers_json_str="not json")
    result = booking_tools._confirm_flight_booking_sync(**kwargs)
    assert result["error"] == "invalid_travelers"
