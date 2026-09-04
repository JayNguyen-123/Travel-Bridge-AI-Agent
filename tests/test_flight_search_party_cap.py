# tests/test_flight_search_party_cap.py
"""
Unit tests for tools/flight_tools.py's party-size cap (MAX_PARTY_SIZE,
default 9). _search_flights_sync checks the cap -- and returns -- before it
ever calls requests.get, so an oversized party never reaches the network;
these tests lean on that ordering instead of monkeypatching Amadeus itself.
"""
import tools.flight_tools as flight_tools


def test_rejects_party_larger_than_cap_via_adults_alone(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")

    result = flight_tools._search_flights_sync(
        "SGN", "CDG", "2026-12-01", adults=flight_tools.MAX_PARTY_SIZE + 1,
    )
    assert "error" in result
    assert str(flight_tools.MAX_PARTY_SIZE) in result["error"]


def test_rejects_party_larger_than_cap_summed_across_traveler_types(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")

    # No single count (adults/children/infants) exceeds the cap on its own --
    # only their sum does. Uses MAX_PARTY_SIZE + 1 split across all three so
    # this stays correct even if the default cap changes.
    cap = flight_tools.MAX_PARTY_SIZE
    adults = (cap // 2) + 1
    children = cap // 3
    infants = cap - adults - children + 2  # pushes the total 1 over the cap
    infants = max(infants, 0)
    total = adults + children + infants
    assert total > cap  # sanity-check the arithmetic above actually overshoots

    result = flight_tools._search_flights_sync(
        "SGN", "CDG", "2026-12-01", adults=adults, children=children, infants=min(infants, adults),
    )
    assert "error" in result
    assert str(cap) in result["error"]


def test_allows_party_exactly_at_cap_and_reaches_amadeus(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")

    def fake_get(*_args, **_kwargs):
        # Proves the code passed the cap check and the infants<=adults check
        # and actually attempted the Amadeus call -- caught by the same
        # except block a real connection failure would hit.
        raise flight_tools.requests.exceptions.ConnectionError("reached-amadeus-marker")

    monkeypatch.setattr(flight_tools.requests, "get", fake_get)

    result = flight_tools._search_flights_sync(
        "SGN", "CDG", "2026-12-01", adults=flight_tools.MAX_PARTY_SIZE,
    )
    assert result["error"] == "Failed to connect to Amadeus Flight service"
    assert "reached-amadeus-marker" in result["details"]


def test_cap_check_runs_before_infant_count_check(monkeypatch):
    monkeypatch.setenv("AMADEUS_ACCESS_TOKEN", "test-token")

    # infants > adults would normally trip the separate "infants cannot
    # exceed adults" error -- but an oversized party should be rejected for
    # being oversized first, with a message about the cap, not about infants.
    result = flight_tools._search_flights_sync(
        "SGN", "CDG", "2026-12-01",
        adults=1, children=0, infants=flight_tools.MAX_PARTY_SIZE + 5,
    )
    assert str(flight_tools.MAX_PARTY_SIZE) in result["error"]
