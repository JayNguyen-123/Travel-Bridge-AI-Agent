# tests/test_hotel_booking.py
"""
Unit tests for GUARANTEE-only hotel booking in tools/hotel_booking_tools.py:
- Reading an offer's own payment policy and refusing anything but GUARANTEE.
- Validating the guest list the agent collects.
- Deterministic idempotency-key derivation (no prior payment step to thread
  a key through, unlike flights -- see the module's docstring).
- Building the Amadeus order-creation payload WITHOUT a payment/card block.
- Best-effort confirmation-number extraction from a booking response.
- Identity-matching against any guest on the booking for lookup/cancellation.

None of this exercises a real Amadeus call -- see HOTEL_BOOKING_SCOPE.md and
tools/hotel_booking_tools.py's module docstring for what remains unverified
against a live sandbox (specifically: whether Amadeus actually allows a
GUARANTEE hotel-order without card data at all).
"""
import json

import tools.hotel_booking_tools as hotel_booking_tools


# --- payment policy extraction -----------------------------------------------

def test_payment_policy_reads_paymentType_case_insensitively():
    assert hotel_booking_tools._hotel_offer_payment_policy(
        {"policies": {"paymentType": "guarantee"}}
    ) == "GUARANTEE"
    assert hotel_booking_tools._hotel_offer_payment_policy(
        {"policies": {"paymentType": "DEPOSIT"}}
    ) == "DEPOSIT"


def test_payment_policy_falls_back_to_nested_offers_list():
    offer_wrapper = {"offers": [{"policies": {"paymentType": "prepay"}}]}
    assert hotel_booking_tools._hotel_offer_payment_policy(offer_wrapper) == "PREPAY"


def test_payment_policy_none_when_undetermined():
    assert hotel_booking_tools._hotel_offer_payment_policy({}) is None
    assert hotel_booking_tools._hotel_offer_payment_policy({"policies": {}}) is None
    assert hotel_booking_tools._hotel_offer_payment_policy("not a dict") is None


# --- guest validation ----------------------------------------------------------

def test_validate_guests_rejects_non_list():
    assert hotel_booking_tools._validate_guests({"first_name": "A"}) != ""


def test_validate_guests_rejects_empty_list():
    assert hotel_booking_tools._validate_guests([]) != ""


def test_validate_guests_rejects_missing_field():
    error = hotel_booking_tools._validate_guests([{"first_name": "Jane"}])
    assert "last_name" in error


def test_validate_guests_rejects_over_cap():
    cap = hotel_booking_tools.MAX_HOTEL_GUESTS
    guests = [{"first_name": f"G{i}", "last_name": "X"} for i in range(cap + 1)]
    error = hotel_booking_tools._validate_guests(guests)
    assert error != "" and str(cap) in error


def test_validate_guests_accepts_at_cap():
    cap = hotel_booking_tools.MAX_HOTEL_GUESTS
    guests = [{"first_name": f"G{i}", "last_name": "X"} for i in range(cap)]
    assert hotel_booking_tools._validate_guests(guests) == ""


def test_validate_guests_accepts_well_formed():
    guests = [{"first_name": "Jane", "last_name": "Doe"}, {"first_name": "John", "last_name": "Doe"}]
    assert hotel_booking_tools._validate_guests(guests) == ""


# --- idempotency key -----------------------------------------------------------

def test_idempotency_key_is_deterministic_for_identical_inputs():
    k1 = hotel_booking_tools._make_hotel_idempotency_key("offer-a", "guests-a", "+12025551234")
    k2 = hotel_booking_tools._make_hotel_idempotency_key("offer-a", "guests-a", "+12025551234")
    assert k1 == k2


def test_idempotency_key_differs_for_different_inputs():
    k1 = hotel_booking_tools._make_hotel_idempotency_key("offer-a", "guests-a", "+12025551234")
    k2 = hotel_booking_tools._make_hotel_idempotency_key("offer-a", "guests-b", "+12025551234")
    k3 = hotel_booking_tools._make_hotel_idempotency_key("offer-b", "guests-a", "+12025551234")
    assert len({k1, k2, k3}) == 3


# --- payload construction -------------------------------------------------------

def test_build_payload_omits_payment_block_entirely():
    offer = {"id": "OFFER123"}
    guests = [{"first_name": "Jane", "last_name": "Doe", "gender": "FEMALE"}]
    payload = hotel_booking_tools._build_hotel_booking_payload(offer, guests, "family@example.com", "1", "2025551234")
    assert "payment" not in payload["data"]


def test_build_payload_maps_guests_to_room_association_in_order():
    offer = {"id": "OFFER123"}
    guests = [
        {"first_name": "Jane", "last_name": "Doe", "gender": "FEMALE"},
        {"first_name": "John", "last_name": "Doe", "gender": "MALE"},
    ]
    payload = hotel_booking_tools._build_hotel_booking_payload(offer, guests, "family@example.com", "1", "2025551234")
    amadeus_guests = payload["data"]["guests"]
    assert [g["tid"] for g in amadeus_guests] == [1, 2]
    assert amadeus_guests[0]["title"] == "MRS"
    assert amadeus_guests[1]["title"] == "MR"
    refs = payload["data"]["roomAssociations"][0]["guestReferences"]
    assert [r["guestReference"] for r in refs] == ["1", "2"]
    assert payload["data"]["roomAssociations"][0]["hotelOfferId"] == "OFFER123"


def test_build_payload_prefers_per_guest_email_over_contact_email():
    offer = {"id": "OFFER1"}
    guests = [{"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"}]
    payload = hotel_booking_tools._build_hotel_booking_payload(offer, guests, "family@example.com", "1", "2025551234")
    assert payload["data"]["guests"][0]["email"] == "jane@example.com"


# --- confirmation-number extraction ---------------------------------------------

def test_extract_confirmation_prefers_providerConfirmationId():
    oid, ref = hotel_booking_tools._extract_hotel_confirmation(
        {"data": {"id": "ORD1", "providerConfirmationId": "CONF1"}}
    )
    assert (oid, ref) == ("ORD1", "CONF1")


def test_extract_confirmation_falls_back_to_order_id():
    oid, ref = hotel_booking_tools._extract_hotel_confirmation({"data": {"id": "ORD2"}})
    assert (oid, ref) == ("ORD2", "ORD2")


# --- policy gating happens before any DB access ---------------------------------

def test_non_guarantee_offer_rejected_before_db_touched(monkeypatch):
    monkeypatch.setattr(
        hotel_booking_tools, "get_db_connection",
        lambda: (_ for _ in ()).throw(AssertionError("DB should not be reached for a rejected policy")),
    )

    result = hotel_booking_tools._confirm_hotel_booking_sync(
        json.dumps({"id": "X", "policies": {"paymentType": "deposit"}}),
        json.dumps([{"first_name": "A", "last_name": "B"}]),
        "+14043721234", "a@example.com",
    )
    assert result["error"] == "policy_not_supported"


def test_offer_with_undetermined_policy_rejected_not_assumed_guarantee(monkeypatch):
    monkeypatch.setattr(
        hotel_booking_tools, "get_db_connection",
        lambda: (_ for _ in ()).throw(AssertionError("DB should not be reached")),
    )

    result = hotel_booking_tools._confirm_hotel_booking_sync(
        json.dumps({"id": "X"}),  # no policies at all
        json.dumps([{"first_name": "A", "last_name": "B"}]),
        "+14043721234", "a@example.com",
    )
    assert result["error"] == "policy_not_supported"


def test_invalid_guests_rejected_before_policy_or_db_check():
    result = hotel_booking_tools._confirm_hotel_booking_sync(
        json.dumps({"id": "X", "policies": {"paymentType": "guarantee"}}),
        "not json",
        "+14043721234", "a@example.com",
    )
    assert result["error"] == "invalid_guests"


def test_invalid_phone_rejected_before_db_touched(monkeypatch):
    monkeypatch.setattr(
        hotel_booking_tools, "get_db_connection",
        lambda: (_ for _ in ()).throw(AssertionError("DB should not be reached")),
    )

    result = hotel_booking_tools._confirm_hotel_booking_sync(
        json.dumps({"id": "X", "policies": {"paymentType": "guarantee"}}),
        json.dumps([{"first_name": "A", "last_name": "B"}]),
        "not-a-phone-number", "a@example.com",
    )
    assert result["error"] == "invalid_phone_number"


# --- lookup identity matching ----------------------------------------------------

class _FakeLookupCursor:
    def __init__(self, booking_row, guest_rows):
        self._booking_row = booking_row
        self._guest_rows = guest_rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_args, **_kwargs):
        pass

    def fetchone(self):
        return self._booking_row

    def fetchall(self):
        return self._guest_rows


class _FakeLookupConn:
    def __init__(self, booking_row, guest_rows):
        self._booking_row = booking_row
        self._guest_rows = guest_rows

    def cursor(self):
        return _FakeLookupCursor(self._booking_row, self._guest_rows)


# idempotency_key, status, hotel_name, lead_guest_name, price_amount, currency_type,
# check_in_date, check_out_date, cancelled_at
BOOKING_ROW = ("key-1", "booked", "Grand Hotel", "Jane Doe (+1 more)", "150.00", "USD",
               "2026-09-01", "2026-09-03", None)


def test_lookup_not_found_when_no_booking_row(monkeypatch):
    monkeypatch.setattr(hotel_booking_tools, "get_db_connection", lambda: _FakeLookupConn(None, []))
    monkeypatch.setattr(hotel_booking_tools, "release_db_connection", lambda conn: None)

    result = hotel_booking_tools._lookup_hotel_booking_sync("REF999", "Anyone")
    assert result["error"] == "not_found"


def test_lookup_matches_any_guest_last_name(monkeypatch):
    guest_rows = [("Jane", "Doe"), ("John", "Smith")]
    monkeypatch.setattr(hotel_booking_tools, "get_db_connection", lambda: _FakeLookupConn(BOOKING_ROW, guest_rows))
    monkeypatch.setattr(hotel_booking_tools, "release_db_connection", lambda conn: None)

    result = hotel_booking_tools._lookup_hotel_booking_sync("REF1", "Smith")
    assert "error" not in result
    assert result["guest_count"] == 2
    assert "John Smith" in result["guests"]


def test_lookup_rejects_name_not_on_any_guest(monkeypatch):
    guest_rows = [("Jane", "Doe")]
    monkeypatch.setattr(hotel_booking_tools, "get_db_connection", lambda: _FakeLookupConn(BOOKING_ROW, guest_rows))
    monkeypatch.setattr(hotel_booking_tools, "release_db_connection", lambda conn: None)

    result = hotel_booking_tools._lookup_hotel_booking_sync("REF1", "Nobody")
    assert result["error"] == "identity_mismatch"


# --- cancellation ------------------------------------------------------------------

class _FakeCancelCursor:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_args, **_kwargs):
        pass

    def fetchone(self):
        return self._row


class _FakeCancelConn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _FakeCancelCursor(self._row)

    def commit(self):
        pass


def test_cancel_not_found(monkeypatch):
    monkeypatch.setattr(hotel_booking_tools, "get_db_connection", lambda: _FakeCancelConn(None))
    monkeypatch.setattr(hotel_booking_tools, "release_db_connection", lambda conn: None)

    result = hotel_booking_tools._cancel_hotel_booking_sync("key-1")
    assert result["error"] == "not_found"


def test_cancel_already_cancelled_short_circuits_before_amadeus_call(monkeypatch):
    # hotel_order_id, reference_code, status
    monkeypatch.setattr(
        hotel_booking_tools, "get_db_connection",
        lambda: _FakeCancelConn(("ORD1", "REF1", "cancelled")),
    )
    monkeypatch.setattr(hotel_booking_tools, "release_db_connection", lambda conn: None)
    monkeypatch.setattr(
        hotel_booking_tools.requests, "delete",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Amadeus should not be called for an already-cancelled booking")),
    )

    result = hotel_booking_tools._cancel_hotel_booking_sync("key-1")
    assert result["status"] == "cancelled"
    assert result["reference_code"] == "REF1"
