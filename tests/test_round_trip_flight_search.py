# tests/test_round_trip_flight_search.py
"""
Unit tests for round-trip flight search (tools/flight_tools.py's optional
`returnDate` parameter on _search_flights_sync/search_flights).

Amadeus's own param name is `returnDate` -- supplying it turns each result
into a single round-trip offer (itineraries[0]=outbound, itineraries[1]=
return) priced as one combined total, rather than this app needing to stitch
together two separate one-way searches. These tests only check what this app
controls: that `returnDate` is forwarded (or correctly omitted) in the
request to Amadeus, and that an obviously-backwards date range is rejected
before spending a network call on it. The itinerary-shape handling on the
other side (multiple itineraries -> multiple SMS/email lines) is already
covered by tests/test_itinerary_notifications.py.
"""
import asyncio

import tools.flight_tools as flight_tools


def test_returns_error_before_calling_amadeus_when_return_date_precedes_departure(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")

    def fake_get(*_args, **_kwargs):
        raise AssertionError("must not call Amadeus when returnDate < departureDate")

    monkeypatch.setattr(flight_tools.requests, "get", fake_get)

    result = flight_tools._search_flights_sync(
        "SGN", "CDG", "2026-07-08", returnDate="2026-07-01",
    )
    assert result == {"error": "returnDate cannot be before departureDate."}


def test_allows_return_date_equal_to_departure_date(monkeypatch):
    # A same-day round trip is unusual but not invalid -- only a returnDate
    # strictly *before* departureDate should be rejected up front.
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")

    def fake_get(*_args, **_kwargs):
        raise flight_tools.requests.exceptions.ConnectionError("reached-amadeus-marker")

    monkeypatch.setattr(flight_tools.requests, "get", fake_get)

    result = flight_tools._search_flights_sync(
        "SGN", "CDG", "2026-07-08", returnDate="2026-07-08",
    )
    assert "reached-amadeus-marker" in result["details"]


def test_return_date_is_forwarded_to_amadeus_as_its_own_param(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params
        raise flight_tools.requests.exceptions.ConnectionError("reached-amadeus-marker")

    monkeypatch.setattr(flight_tools.requests, "get", fake_get)

    flight_tools._search_flights_sync(
        "sgn", "cdg", "2026-07-01", adults=2, children=1, returnDate="2026-07-08",
    )
    assert captured["params"]["returnDate"] == "2026-07-08"
    assert captured["params"]["departureDate"] == "2026-07-01"
    assert captured["params"]["adults"] == 2
    assert captured["params"]["children"] == 1


def test_return_date_omitted_from_params_for_a_one_way_search(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params
        raise flight_tools.requests.exceptions.ConnectionError("reached-amadeus-marker")

    monkeypatch.setattr(flight_tools.requests, "get", fake_get)

    flight_tools._search_flights_sync("SGN", "CDG", "2026-07-01")
    assert "returnDate" not in captured["params"]


def test_async_search_flights_forwards_return_date_through_to_thread(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params
        raise flight_tools.requests.exceptions.ConnectionError("reached-amadeus-marker")

    monkeypatch.setattr(flight_tools.requests, "get", fake_get)

    asyncio.run(flight_tools.search_flights(
        "SGN", "CDG", "2026-07-01", adults=1, returnDate="2026-07-15",
    ))
    assert captured["params"]["returnDate"] == "2026-07-15"
