# tests/test_multi_city_flight_search.py
"""
Unit tests for multi-city flight search (tools/flight_tools.py's
search_multi_city_flights / _search_multi_city_flights_sync).

A genuine multi-city trip (3+ distinct destinations, or an "open-jaw" trip
that doesn't return to its starting city) needs Amadeus's POST-based Flight
Offers Search with an `originDestinations` array, not the simple GET search
search_flights uses. These tests only check what this app controls: input
validation before any network call, the exact request body/headers built for
Amadeus, and that legs/party data are forwarded correctly. The shared
itinerary-flattening/SMS/email code (tests/test_itinerary_notifications.py)
and Tier 2's multi-city rebook path (tests/test_booking_resolution.py)
already cover what happens with a multi-leg offer once one comes back.
"""
import json

import pytest

import tools.flight_tools as flight_tools


def _legs(*pairs):
    """pairs like [("SGN", "BKK", "2026-07-01"), ...] -> the legs list shape
    _search_multi_city_flights_sync expects."""
    return [{"origin": o, "destination": d, "date": dt} for o, d, dt in pairs]


# --- input validation, all before any network call --------------------------

def test_rejects_a_single_leg(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")
    result = flight_tools._search_multi_city_flights_sync(_legs(("SGN", "BKK", "2026-07-01")))
    assert "error" in result
    assert "search_flights" in result["error"]


def test_rejects_zero_legs(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")
    result = flight_tools._search_multi_city_flights_sync([])
    assert "error" in result


def test_rejects_more_legs_than_the_cap(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")
    too_many = _legs(*[("SGN", "BKK", "2026-07-01")] * (flight_tools.MAX_MULTI_CITY_LEGS + 1))
    result = flight_tools._search_multi_city_flights_sync(too_many)
    assert "error" in result
    assert str(flight_tools.MAX_MULTI_CITY_LEGS) in result["error"]


def test_rejects_a_leg_missing_a_required_field(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")
    legs = [{"origin": "SGN", "destination": "BKK", "date": "2026-07-01"}, {"origin": "BKK", "date": "2026-07-05"}]
    result = flight_tools._search_multi_city_flights_sync(legs)
    assert "error" in result
    assert "Leg 2" in result["error"]


def test_rejects_legs_out_of_chronological_order(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")
    legs = _legs(("SGN", "BKK", "2026-07-05"), ("BKK", "NRT", "2026-07-01"))
    result = flight_tools._search_multi_city_flights_sync(legs)
    assert "error" in result
    assert "chronological order" in result["error"]


def test_rejects_party_larger_than_cap(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")
    legs = _legs(("SGN", "BKK", "2026-07-01"), ("BKK", "NRT", "2026-07-05"))
    result = flight_tools._search_multi_city_flights_sync(legs, adults=flight_tools.MAX_PARTY_SIZE + 1)
    assert "error" in result
    assert str(flight_tools.MAX_PARTY_SIZE) in result["error"]


def test_rejects_more_infants_than_adults(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")
    legs = _legs(("SGN", "BKK", "2026-07-01"), ("BKK", "NRT", "2026-07-05"))
    result = flight_tools._search_multi_city_flights_sync(legs, adults=1, infants=2)
    assert "error" in result
    assert "infant" in result["error"].lower()


def test_none_of_the_validation_failures_reach_amadeus(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")

    def fail(*_a, **_kw):
        raise AssertionError("must not call Amadeus when input validation fails")
    monkeypatch.setattr(flight_tools.requests, "post", fail)

    flight_tools._search_multi_city_flights_sync(_legs(("SGN", "BKK", "2026-07-01")))  # too few legs
    flight_tools._search_multi_city_flights_sync(_legs(
        ("SGN", "BKK", "2026-07-05"), ("BKK", "NRT", "2026-07-01"),  # out of order
    ))


# --- _build_multi_city_search_body: request shape ---------------------------

def test_build_body_has_one_origin_destination_entry_per_leg_in_order():
    legs = _legs(("sgn", "bkk", "2026-07-01"), ("bkk", "nrt", "2026-07-05"), ("nrt", "sgn", "2026-07-10"))
    body = flight_tools._build_multi_city_search_body(legs, adults=1)
    ods = body["originDestinations"]
    assert [od["id"] for od in ods] == ["1", "2", "3"]
    assert ods[0]["originLocationCode"] == "SGN"  # uppercased
    assert ods[0]["destinationLocationCode"] == "BKK"
    assert ods[1]["departureDateTimeRange"]["date"] == "2026-07-05"
    assert ods[2]["originLocationCode"] == "NRT"


def test_build_body_travelers_follow_adults_then_children_then_infants_convention():
    body = flight_tools._build_multi_city_search_body(
        _legs(("SGN", "BKK", "2026-07-01"), ("BKK", "NRT", "2026-07-05")),
        adults=2, children=1, infants=1,
    )
    travelers = body["travelers"]
    assert [t["travelerType"] for t in travelers] == ["ADULT", "ADULT", "CHILD", "HELD_INFANT"]
    assert [t["id"] for t in travelers] == ["1", "2", "3", "4"]


def test_build_body_includes_gds_source():
    body = flight_tools._build_multi_city_search_body(_legs(("SGN", "BKK", "2026-07-01"), ("BKK", "NRT", "2026-07-05")), adults=1)
    assert body["sources"] == ["GDS"]


# --- _post_multi_city_search: the actual HTTP call ---------------------------

def test_post_multi_city_search_sends_the_method_override_header_and_json_body(monkeypatch):
    captured = {}

    class _FakeResponse:
        status_code = 200
        def json(self):
            return {"data": [{"id": "offer1"}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(flight_tools.requests, "post", fake_post)

    legs = _legs(("SGN", "BKK", "2026-07-01"), ("BKK", "NRT", "2026-07-05"))
    raw_json, error = flight_tools._post_multi_city_search(legs, 1, 0, 0, "fake-token")

    assert error is None
    assert raw_json == {"data": [{"id": "offer1"}]}
    assert "flight-offers" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer fake-token"
    assert captured["headers"]["X-HTTP-Method-Override"] == "GET"
    assert captured["json"]["originDestinations"][0]["originLocationCode"] == "SGN"


def test_post_multi_city_search_reports_non_200_as_an_error(monkeypatch):
    class _FakeResponse:
        status_code = 400
        text = "INVALID DATA RECEIVED"

    monkeypatch.setattr(flight_tools.requests, "post", lambda *a, **kw: _FakeResponse())

    raw_json, error = flight_tools._post_multi_city_search(
        _legs(("SGN", "BKK", "2026-07-01"), ("BKK", "NRT", "2026-07-05")), 1, 0, 0, "fake-token",
    )
    assert raw_json is None
    assert error["error"] == "Amadeus API returned status code 400"
    assert "INVALID DATA" in error["details"]


def test_post_multi_city_search_reports_network_error(monkeypatch):
    def raise_conn_error(*_a, **_kw):
        raise flight_tools.requests.exceptions.ConnectionError("no route to host")

    monkeypatch.setattr(flight_tools.requests, "post", raise_conn_error)

    raw_json, error = flight_tools._post_multi_city_search(
        _legs(("SGN", "BKK", "2026-07-01"), ("BKK", "NRT", "2026-07-05")), 1, 0, 0, "fake-token",
    )
    assert raw_json is None
    assert "no route to host" in error["details"]


# --- async search_multi_city_flights: JSON parsing + end-to-end -------------

def test_async_wrapper_rejects_invalid_json():
    import asyncio
    result = asyncio.run(flight_tools.search_multi_city_flights("not valid json"))
    assert "error" in result


def test_async_wrapper_forwards_parsed_legs_through_to_the_search(monkeypatch):
    import asyncio
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        class _FakeResponse:
            status_code = 200
            def json(self):
                return {"data": []}
        return _FakeResponse()

    monkeypatch.setattr(flight_tools.requests, "post", fake_post)

    legs_json_str = json.dumps([
        {"origin": "SGN", "destination": "BKK", "date": "2026-07-01"},
        {"origin": "BKK", "destination": "NRT", "date": "2026-07-05"},
        {"origin": "NRT", "destination": "SGN", "date": "2026-07-10"},
    ])
    out = asyncio.run(flight_tools.search_multi_city_flights(legs_json_str, adults=2))
    assert "error" not in out
    assert len(captured["json"]["originDestinations"]) == 3
    assert len(captured["json"]["travelers"]) == 2
