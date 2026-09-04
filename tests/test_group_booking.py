# tests/test_group_booking.py
"""
Unit tests for group/family booking support in tools/booking_tools.py:
- Reading Amadeus's own traveler-pricing "slots" (ids/types) from an offer,
  and never inventing traveler ids ourselves.
- Deriving adults/children/infants counts from those slots (used by Tier 2
  to re-search with the same party composition).
- Validating the traveler list the agent collects (shape, required fields,
  and that its length matches what the offer was actually priced for).
- Building the Amadeus order-creation payload from a traveler list.
- Cancellation identity-matching against ANY traveler on a group booking,
  not just the lead one.
"""
from datetime import datetime, timedelta, timezone

import tools.booking_tools as booking_tools


def _offer_with_pricings(types):
    return {"travelerPricings": [{"travelerId": str(i + 1), "travelerType": t} for i, t in enumerate(types)]}


# --- traveler-pricing slots + passenger counts -------------------------------

def test_traveler_pricing_slots_reads_ids_and_types_in_order():
    offer = _offer_with_pricings(["ADULT", "ADULT", "CHILD"])
    slots = booking_tools._traveler_pricing_slots(offer)
    assert slots == [
        {"id": "1", "travelerType": "ADULT"},
        {"id": "2", "travelerType": "ADULT"},
        {"id": "3", "travelerType": "CHILD"},
    ]


def test_traveler_pricing_slots_empty_for_offer_with_none():
    assert booking_tools._traveler_pricing_slots({}) == []


def test_passenger_counts_splits_adults_children_infants():
    offer = _offer_with_pricings(["ADULT", "ADULT", "CHILD", "HELD_INFANT"])
    assert booking_tools._passenger_counts(offer) == {"adults": 2, "children": 1, "infants": 1}


def test_passenger_counts_defaults_to_one_adult_when_offer_has_no_pricings():
    # Can't tell the original party size -- fall back to a safe single-adult
    # re-search rather than erroring Tier 2 out entirely.
    assert booking_tools._passenger_counts({}) == {"adults": 1, "children": 0, "infants": 0}


# --- traveler-count validation -----------------------------------------------

def test_validate_traveler_count_accepts_matching_count():
    offer = _offer_with_pricings(["ADULT", "ADULT"])
    travelers = [{"first_name": "A"}, {"first_name": "B"}]
    assert booking_tools._validate_traveler_count(offer, travelers) is None


def test_validate_traveler_count_rejects_mismatch():
    offer = _offer_with_pricings(["ADULT", "ADULT", "CHILD"])
    travelers = [{"first_name": "A"}, {"first_name": "B"}]
    error = booking_tools._validate_traveler_count(offer, travelers)
    assert error is not None
    assert "3 traveler" in error and "2 were provided" in error


def test_validate_traveler_count_lets_offers_without_pricings_through():
    # No travelerPricings on the offer -- can't verify, so don't block;
    # Amadeus itself is the final arbiter of a malformed traveler list.
    assert booking_tools._validate_traveler_count({}, [{"first_name": "A"}]) is None


# --- traveler shape validation ------------------------------------------------

def test_validate_travelers_rejects_non_list():
    assert booking_tools._validate_travelers({"first_name": "A"}) != ""


def test_validate_travelers_rejects_empty_list():
    assert booking_tools._validate_travelers([]) != ""


def test_validate_travelers_rejects_missing_field():
    error = booking_tools._validate_travelers([{"first_name": "A", "last_name": "B", "gender": "MALE"}])
    assert "date_of_birth" in error


def test_validate_travelers_rejects_bad_gender():
    error = booking_tools._validate_travelers([{
        "first_name": "A", "last_name": "B", "date_of_birth": "2000-01-01", "gender": "X",
    }])
    assert "gender" in error


def test_validate_travelers_rejects_party_larger_than_cap():
    cap = booking_tools.MAX_PARTY_SIZE
    travelers = [
        {"first_name": f"T{i}", "last_name": "Group", "date_of_birth": "2000-01-01", "gender": "MALE"}
        for i in range(cap + 1)
    ]
    error = booking_tools._validate_travelers(travelers)
    assert error != ""
    assert str(cap) in error


def test_validate_travelers_accepts_party_exactly_at_cap():
    cap = booking_tools.MAX_PARTY_SIZE
    travelers = [
        {"first_name": f"T{i}", "last_name": "Group", "date_of_birth": "2000-01-01", "gender": "MALE"}
        for i in range(cap)
    ]
    assert booking_tools._validate_travelers(travelers) == ""


def test_validate_travelers_accepts_well_formed_group():
    travelers = [
        {"first_name": "John", "last_name": "Smith", "date_of_birth": "1985-04-02", "gender": "MALE"},
        {"first_name": "Jane", "last_name": "Smith", "date_of_birth": "1987-03-04", "gender": "FEMALE"},
        {"first_name": "Amy", "last_name": "Smith", "date_of_birth": "2015-06-01", "gender": "FEMALE"},
    ]
    assert booking_tools._validate_travelers(travelers) == ""


# --- booking payload construction --------------------------------------------

def test_build_booking_payload_assigns_amadeus_ids_from_slots_not_invented():
    offer = _offer_with_pricings(["ADULT", "CHILD"])
    slots = booking_tools._traveler_pricing_slots(offer)
    travelers = [
        {"first_name": "John", "last_name": "Smith", "date_of_birth": "1985-04-02", "gender": "MALE"},
        {"first_name": "Amy", "last_name": "Smith", "date_of_birth": "2015-06-01", "gender": "FEMALE"},
    ]
    payload = booking_tools._build_booking_payload(offer, travelers, slots, "family@example.com", "1", "2025551234")
    amadeus_travelers = payload["data"]["travelers"]
    assert [t["id"] for t in amadeus_travelers] == ["1", "2"]
    assert amadeus_travelers[0]["name"] == {"firstName": "JOHN", "lastName": "SMITH"}
    assert amadeus_travelers[1]["name"] == {"firstName": "AMY", "lastName": "SMITH"}
    assert all(t["contact"]["emailAddress"] == "family@example.com" for t in amadeus_travelers)


def test_build_booking_payload_prefers_per_traveler_email_over_contact_email():
    offer = _offer_with_pricings(["ADULT"])
    slots = booking_tools._traveler_pricing_slots(offer)
    travelers = [{
        "first_name": "John", "last_name": "Smith", "date_of_birth": "1985-04-02",
        "gender": "MALE", "email": "john@example.com",
    }]
    payload = booking_tools._build_booking_payload(offer, travelers, slots, "family@example.com", "1", "2025551234")
    assert payload["data"]["travelers"][0]["contact"]["emailAddress"] == "john@example.com"


# --- display summary ----------------------------------------------------------

def test_traveler_summary_single():
    assert booking_tools._traveler_summary([{"first_name": "Jane", "last_name": "Doe"}]) == "Jane Doe"


def test_traveler_summary_group():
    travelers = [
        {"first_name": "Jane", "last_name": "Doe"},
        {"first_name": "John", "last_name": "Doe"},
        {"first_name": "Amy", "last_name": "Doe"},
    ]
    assert booking_tools._traveler_summary(travelers) == "Jane Doe (+2 more)"


def test_traveler_summary_empty():
    assert booking_tools._traveler_summary([]) == "unknown traveler"


# --- cancellation identity check against any traveler on a group booking ----

class _FakeLookupCursor:
    def __init__(self, booking_row, traveler_rows):
        self._booking_row = booking_row
        self._traveler_rows = traveler_rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_args, **_kwargs):
        pass

    def fetchone(self):
        return self._booking_row

    def fetchall(self):
        return self._traveler_rows


class _FakeLookupConn:
    def __init__(self, booking_row, traveler_rows):
        self._booking_row = booking_row
        self._traveler_rows = traveler_rows

    def cursor(self):
        return _FakeLookupCursor(self._booking_row, self._traveler_rows)


NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
# idempotency_key, status, traveler_name, price_amount, currency_type, created_at, departure_at
BOOKING_ROW = (
    "test-key", "booked", "John Smith (+2 more)", "1200.00", "USD",
    NOW - timedelta(hours=1), NOW + timedelta(days=20),
)


def test_lookup_not_found_when_no_booking_row(monkeypatch):
    monkeypatch.setattr(booking_tools, "get_db_connection", lambda: _FakeLookupConn(None, []))
    monkeypatch.setattr(booking_tools, "release_db_connection", lambda conn: None)

    result = booking_tools._lookup_booking_sync("REF999", "Anyone")
    assert result["error"] == "not_found"


def test_lookup_matches_any_traveler_last_name_on_group_booking(monkeypatch):
    traveler_rows = [("John", "Smith"), ("Jane", "Doe"), ("Amy", "Smith")]
    monkeypatch.setattr(booking_tools, "get_db_connection", lambda: _FakeLookupConn(BOOKING_ROW, traveler_rows))
    monkeypatch.setattr(booking_tools, "release_db_connection", lambda conn: None)

    # "Doe" isn't the lead traveler's surname, but IS on the booking.
    result = booking_tools._lookup_booking_sync("REF123", "Doe")
    assert "error" not in result
    assert result["traveler_count"] == 3
    assert "Jane Doe" in result["travelers"]


def test_lookup_rejects_name_not_on_any_traveler(monkeypatch):
    traveler_rows = [("John", "Smith"), ("Jane", "Doe")]
    monkeypatch.setattr(booking_tools, "get_db_connection", lambda: _FakeLookupConn(BOOKING_ROW, traveler_rows))
    monkeypatch.setattr(booking_tools, "release_db_connection", lambda conn: None)

    result = booking_tools._lookup_booking_sync("REF123", "Nobody")
    assert result["error"] == "identity_mismatch"


def test_lookup_falls_back_to_lead_traveler_name_when_no_roster(monkeypatch):
    # A booking made before booking_travelers existed -- no roster rows.
    monkeypatch.setattr(booking_tools, "get_db_connection", lambda: _FakeLookupConn(BOOKING_ROW, []))
    monkeypatch.setattr(booking_tools, "release_db_connection", lambda conn: None)

    result = booking_tools._lookup_booking_sync("REF123", "Smith")
    assert "error" not in result
    assert result["traveler_count"] == 1
