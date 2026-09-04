from datetime import datetime, timedelta, timezone

from tools.booking_tools import is_eligible_for_free_cancellation, _extract_earliest_departure

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def test_within_24h_and_departure_far_enough_is_eligible():
    booked_at = NOW - timedelta(hours=1)
    departure_at = NOW + timedelta(days=20)
    assert is_eligible_for_free_cancellation(booked_at, departure_at, now=NOW) is True


def test_exactly_on_24h_boundary_is_eligible():
    booked_at = NOW - timedelta(hours=24)
    departure_at = booked_at + timedelta(days=10)
    assert is_eligible_for_free_cancellation(booked_at, departure_at, now=NOW) is True


def test_past_24h_window_is_not_eligible():
    booked_at = NOW - timedelta(hours=25)
    departure_at = NOW + timedelta(days=20)
    assert is_eligible_for_free_cancellation(booked_at, departure_at, now=NOW) is False


def test_departure_less_than_7_days_out_is_not_eligible():
    # DOT rule only applies when the reservation was made 7+ days before departure.
    booked_at = NOW - timedelta(hours=1)
    departure_at = booked_at + timedelta(days=3)
    assert is_eligible_for_free_cancellation(booked_at, departure_at, now=NOW) is False


def test_naive_datetimes_are_treated_as_utc():
    booked_at = (NOW - timedelta(hours=2)).replace(tzinfo=None)
    departure_at = (NOW + timedelta(days=30)).replace(tzinfo=None)
    assert is_eligible_for_free_cancellation(booked_at, departure_at, now=NOW) is True


def test_missing_departure_is_not_eligible():
    assert is_eligible_for_free_cancellation(NOW - timedelta(hours=1), None, now=NOW) is False


def test_extract_earliest_departure_reads_amadeus_shape():
    offer = {
        "itineraries": [
            {"segments": [{"departure": {"at": "2026-07-01T08:30:00"}}]}
        ]
    }
    dt = _extract_earliest_departure(offer)
    assert dt.year == 2026 and dt.month == 7 and dt.day == 1 and dt.hour == 8


def test_extract_earliest_departure_handles_malformed_offer():
    assert _extract_earliest_departure({}) is None
    assert _extract_earliest_departure({"itineraries": []}) is None
