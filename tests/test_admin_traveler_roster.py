# tests/test_admin_traveler_roster.py
"""
Unit tests for server/admin.py's traveler-roster helpers, added so the admin
UI can show every traveler on a group booking (not just the lead traveler's
name in travel_bookings.traveler_name). Exercises _fetch_travelers and
_fetch_travelers_bulk directly against a fake cursor -- no real Postgres.
"""
import datetime

from server.admin import _fetch_travelers, _fetch_travelers_bulk


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.last_query = None
        self.last_params = None

    def execute(self, query, params=None):
        self.last_query = query
        self.last_params = params

    def fetchall(self):
        return self._rows


def test_fetch_travelers_returns_ordered_roster_with_iso_dates():
    rows = [
        ("Jane", "Doe", datetime.date(1990, 1, 1), "FEMALE", "ADULT"),
        ("Jack", "Doe", datetime.date(2016, 5, 1), "MALE", "CHILD"),
    ]
    cur = _FakeCursor(rows)

    result = _fetch_travelers(cur, "key-1")

    assert result == [
        {"first_name": "Jane", "last_name": "Doe", "date_of_birth": "1990-01-01",
         "gender": "FEMALE", "traveler_type": "ADULT"},
        {"first_name": "Jack", "last_name": "Doe", "date_of_birth": "2016-05-01",
         "gender": "MALE", "traveler_type": "CHILD"},
    ]
    assert cur.last_params == ("key-1",)


def test_fetch_travelers_handles_missing_date_of_birth():
    rows = [("Jane", "Doe", None, "FEMALE", "ADULT")]
    cur = _FakeCursor(rows)

    result = _fetch_travelers(cur, "key-1")

    assert result[0]["date_of_birth"] is None


def test_fetch_travelers_empty_for_booking_with_no_roster():
    # A booking made before booking_travelers existed -- no rows for its key.
    cur = _FakeCursor([])

    assert _fetch_travelers(cur, "old-key") == []


def test_fetch_travelers_bulk_groups_by_idempotency_key():
    rows = [
        ("key-A", "Jane", "Doe", datetime.date(1990, 1, 1), "FEMALE", "ADULT"),
        ("key-A", "Jack", "Doe", datetime.date(2016, 5, 1), "MALE", "CHILD"),
        ("key-B", "Sam", "Lee", datetime.date(1985, 3, 3), "MALE", "ADULT"),
    ]
    cur = _FakeCursor(rows)

    result = _fetch_travelers_bulk(cur, ["key-A", "key-B", "key-C"])

    assert result["key-A"] == [
        {"first_name": "Jane", "last_name": "Doe", "date_of_birth": "1990-01-01",
         "gender": "FEMALE", "traveler_type": "ADULT"},
        {"first_name": "Jack", "last_name": "Doe", "date_of_birth": "2016-05-01",
         "gender": "MALE", "traveler_type": "CHILD"},
    ]
    assert result["key-B"] == [
        {"first_name": "Sam", "last_name": "Lee", "date_of_birth": "1985-03-03",
         "gender": "MALE", "traveler_type": "ADULT"},
    ]
    # key-C had no rows at all -- correctly absent, not an empty list.
    assert "key-C" not in result


def test_fetch_travelers_bulk_short_circuits_on_empty_key_list():
    cur = _FakeCursor([])

    result = _fetch_travelers_bulk(cur, [])

    assert result == {}
    # No query should have been issued for an empty key list.
    assert cur.last_query is None
