# tests/test_booking_resolution.py
"""
Unit tests for the booking failure-resolution engine (tools/booking_tools.py):
Tier 1 (retry classification), Tier 2 (fare-rebook threshold math and the
route/date extraction it depends on). Network calls are mocked; nothing here
touches a real Amadeus/Stripe endpoint.

Tier 3/4 (refund + escalation) are thin DB/Stripe glue over already-tested
primitives (create_refund, is tested indirectly via
test_cancellation_policy.py's sibling code path) -- not re-mocked here to
avoid a low-value test that's mostly asserting mock plumbing.
"""
from unittest.mock import MagicMock

import pytest

from tools.booking_tools import (
    AMADEUS_RETRYABLE_STATUS_CODES,
    _extract_all_legs,
    _is_simple_round_trip,
    _tier1_retry_booking,
    _tier2_fare_recheck_and_rebook,
    _try_amadeus_booking,
    _within_rebook_threshold,
)


# --- Tier 1: retry classification -------------------------------------------

def test_retryable_status_codes_are_transient_server_errors():
    assert 503 in AMADEUS_RETRYABLE_STATUS_CODES
    assert 429 in AMADEUS_RETRYABLE_STATUS_CODES
    # A 400 usually means the fare/segment itself is gone (e.g. Amadeus
    # "SEGMENT SELL FAILURE") -- retrying the identical request can't help,
    # so it must NOT be in the retryable set (Tier 2 handles this instead).
    assert 400 not in AMADEUS_RETRYABLE_STATUS_CODES
    assert 404 not in AMADEUS_RETRYABLE_STATUS_CODES


def test_try_amadeus_booking_success(mocker):
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"data": {"id": "abc"}}
    mocker.patch("tools.booking_tools.requests.post", return_value=mock_response)

    result, error = _try_amadeus_booking({"data": {}})
    assert error is None
    assert result == {"data": {"id": "abc"}}


def test_try_amadeus_booking_marks_5xx_as_retryable(mocker):
    mock_response = MagicMock(status_code=503, text="upstream unavailable")
    mocker.patch("tools.booking_tools.requests.post", return_value=mock_response)

    result, error = _try_amadeus_booking({"data": {}})
    assert result is None
    assert error["retryable"] is True
    assert error["status_code"] == 503


def test_try_amadeus_booking_marks_400_as_not_retryable(mocker):
    mock_response = MagicMock(status_code=400, text="SEGMENT SELL FAILURE")
    mocker.patch("tools.booking_tools.requests.post", return_value=mock_response)

    result, error = _try_amadeus_booking({"data": {}})
    assert result is None
    assert error["retryable"] is False


def test_try_amadeus_booking_treats_network_error_as_retryable(mocker):
    import requests
    mocker.patch("tools.booking_tools.requests.post", side_effect=requests.exceptions.Timeout("timed out"))

    result, error = _try_amadeus_booking({"data": {}})
    assert result is None
    assert error["retryable"] is True
    assert error["status_code"] is None


def test_tier1_stops_immediately_on_non_retryable_error(mocker):
    mock_response = MagicMock(status_code=400, text="bad request")
    post_mock = mocker.patch("tools.booking_tools.requests.post", return_value=mock_response)
    mocker.patch("tools.booking_tools.time.sleep")

    result, error = _tier1_retry_booking({"data": {}})
    assert result is None
    assert post_mock.call_count == 1  # no retries wasted on a non-retryable error


def test_tier1_retries_transient_errors_up_to_the_configured_limit(mocker):
    mock_response = MagicMock(status_code=503, text="unavailable")
    post_mock = mocker.patch("tools.booking_tools.requests.post", return_value=mock_response)
    sleep_mock = mocker.patch("tools.booking_tools.time.sleep")

    result, error = _tier1_retry_booking({"data": {}})
    assert result is None
    # BOOKING_RETRY_ATTEMPTS defaults to 2 extra attempts -> 3 total calls,
    # with a sleep between each of the first two (not after the last).
    assert post_mock.call_count == 3
    assert sleep_mock.call_count == 2


def test_tier1_succeeds_after_a_transient_failure(mocker):
    fail_response = MagicMock(status_code=503, text="unavailable")
    ok_response = MagicMock(status_code=200)
    ok_response.json.return_value = {"data": {"id": "xyz"}}
    mocker.patch("tools.booking_tools.requests.post", side_effect=[fail_response, ok_response])
    mocker.patch("tools.booking_tools.time.sleep")

    result, error = _tier1_retry_booking({"data": {}})
    assert error is None
    assert result == {"data": {"id": "xyz"}}


# --- Tier 2: rebook-threshold math + route/date extraction -----------------

def test_within_rebook_threshold_accepts_equal_price():
    assert _within_rebook_threshold(10000, 10000, 10) is True


def test_within_rebook_threshold_accepts_price_just_under_the_limit():
    assert _within_rebook_threshold(10000, 10999, 10) is True  # +9.99%


def test_within_rebook_threshold_rejects_price_over_the_limit():
    assert _within_rebook_threshold(10000, 11001, 10) is False  # +10.01%


def test_within_rebook_threshold_rejects_when_nothing_was_actually_paid():
    assert _within_rebook_threshold(None, 5000, 10) is False
    assert _within_rebook_threshold(0, 5000, 10) is False


def _sample_offer():
    return {
        "itineraries": [{
            "segments": [
                {"departure": {"iataCode": "SGN", "at": "2026-07-01T08:30:00"}, "arrival": {"iataCode": "BKK"}},
                {"departure": {"iataCode": "BKK"}, "arrival": {"iataCode": "CDG"}},
            ]
        }]
    }


def _sample_round_trip_offer():
    """Same outbound leg as _sample_offer, plus a second itinerary (the
    return leg) -- the shape Amadeus returns when search_flights is called
    with a returnDate."""
    offer = _sample_offer()
    offer["itineraries"].append({
        "segments": [
            {"departure": {"iataCode": "CDG", "at": "2026-07-08T14:00:00"}, "arrival": {"iataCode": "SGN"}},
        ]
    })
    return offer


def _sample_multi_city_offer():
    """A genuine 3-leg multi-city offer -- the shape
    search_multi_city_flights returns: three distinct destinations, not a
    there-and-back pair."""
    return {
        "itineraries": [
            {"segments": [{"departure": {"iataCode": "SGN", "at": "2026-07-01T08:30:00"}, "arrival": {"iataCode": "BKK"}}]},
            {"segments": [{"departure": {"iataCode": "BKK", "at": "2026-07-05T09:00:00"}, "arrival": {"iataCode": "NRT"}}]},
            {"segments": [{"departure": {"iataCode": "NRT", "at": "2026-07-10T10:00:00"}, "arrival": {"iataCode": "SGN"}}]},
        ]
    }


def _sample_open_jaw_offer():
    """A 2-leg trip that does NOT return to its starting city (SGN -> BKK,
    then HAN -> SGN) -- not a simple round trip, so it also needs the
    multi-city re-search path even though it's only 2 legs."""
    return {
        "itineraries": [
            {"segments": [{"departure": {"iataCode": "SGN", "at": "2026-07-01T08:30:00"}, "arrival": {"iataCode": "BKK"}}]},
            {"segments": [{"departure": {"iataCode": "HAN", "at": "2026-07-08T14:00:00"}, "arrival": {"iataCode": "SGN"}}]},
        ]
    }


# _tier2_fare_recheck_and_rebook's real call site (see the Tier 1 -> Tier 2
# handoff in _confirm_flight_booking_sync) always passes contact_email/
# country_calling_code/national_number too -- these tests use empty
# travelers/slots throughout so _build_booking_payload's zip() is a no-op,
# which keeps them focused purely on the search/threshold logic.
TIER2_CONTACT_ARGS = ("someone@example.com", "1", "2025551234")


def test_extract_all_legs_reads_first_departure_and_last_arrival_per_itinerary():
    legs = _extract_all_legs(_sample_offer())
    assert legs == [{"origin": "SGN", "destination": "CDG", "date": "2026-07-01"}]


def test_extract_all_legs_raises_on_malformed_offer():
    with pytest.raises((KeyError, IndexError, TypeError)):
        _extract_all_legs({})


def test_extract_all_legs_covers_every_itinerary_in_order_for_round_trip():
    legs = _extract_all_legs(_sample_round_trip_offer())
    assert legs == [
        {"origin": "SGN", "destination": "CDG", "date": "2026-07-01"},
        {"origin": "CDG", "destination": "SGN", "date": "2026-07-08"},
    ]


def test_extract_all_legs_covers_every_itinerary_in_order_for_multi_city():
    legs = _extract_all_legs(_sample_multi_city_offer())
    assert legs == [
        {"origin": "SGN", "destination": "BKK", "date": "2026-07-01"},
        {"origin": "BKK", "destination": "NRT", "date": "2026-07-05"},
        {"origin": "NRT", "destination": "SGN", "date": "2026-07-10"},
    ]


def test_is_simple_round_trip_true_for_a_there_and_back_pair():
    assert _is_simple_round_trip(_extract_all_legs(_sample_round_trip_offer())) is True


def test_is_simple_round_trip_false_for_one_way():
    assert _is_simple_round_trip(_extract_all_legs(_sample_offer())) is False


def test_is_simple_round_trip_false_for_genuine_multi_city():
    assert _is_simple_round_trip(_extract_all_legs(_sample_multi_city_offer())) is False


def test_is_simple_round_trip_false_for_open_jaw():
    # Two legs, but the second doesn't return to the first leg's origin --
    # this needs the multi-city re-search path, not the simple GET one.
    assert _is_simple_round_trip(_extract_all_legs(_sample_open_jaw_offer())) is False


def test_tier2_rebooks_when_cheapest_alternative_is_within_threshold(mocker):
    search_response = MagicMock(status_code=200)
    search_response.json.return_value = {"data": [
        {"price": {"total": "410.00", "currency": "USD"}},
        {"price": {"total": "500.00", "currency": "USD"}},
    ]}
    mocker.patch("tools.booking_tools.requests.get", return_value=search_response)

    booking_response = MagicMock(status_code=200)
    booking_response.json.return_value = {"data": {"id": "rebooked"}}
    mocker.patch("tools.booking_tools.requests.post", return_value=booking_response)

    original_offer = _sample_offer()
    result, booked_offer, reason = _tier2_fare_recheck_and_rebook(
        original_offer, [], *TIER2_CONTACT_ARGS,
        paid_stripe_amount=40000, paid_currency="USD",  # customer already paid $400.00
    )
    assert reason is None
    assert result == {"data": {"id": "rebooked"}}
    assert booked_offer["price"]["total"] == "410.00"  # picked the cheapest, not the first


def test_tier2_gives_up_when_cheapest_alternative_exceeds_threshold(mocker):
    search_response = MagicMock(status_code=200)
    search_response.json.return_value = {"data": [{"price": {"total": "600.00", "currency": "USD"}}]}
    mocker.patch("tools.booking_tools.requests.get", return_value=search_response)

    result, booked_offer, reason = _tier2_fare_recheck_and_rebook(
        _sample_offer(), [], *TIER2_CONTACT_ARGS,
        paid_stripe_amount=40000, paid_currency="USD",
    )
    assert result is None
    assert booked_offer is None
    assert "threshold" in reason


def test_tier2_gives_up_when_no_comparable_fares_found(mocker):
    search_response = MagicMock(status_code=200)
    search_response.json.return_value = {"data": []}
    mocker.patch("tools.booking_tools.requests.get", return_value=search_response)

    result, booked_offer, reason = _tier2_fare_recheck_and_rebook(
        _sample_offer(), [], *TIER2_CONTACT_ARGS,
        paid_stripe_amount=40000, paid_currency="USD",
    )
    assert result is None
    assert "no comparable fares" in reason


def test_tier2_preserves_round_trip_by_passing_return_date_to_the_re_search(mocker):
    """The real bug this closes: Tier 2 used to build its own re-search
    params from scratch and never included returnDate, so a round-trip
    booking that failed and fell into Tier 2 recovery would silently get
    rebooked as one-way. Assert the re-search actually asks for the return
    leg when the original offer had one."""
    search_response = MagicMock(status_code=200)
    search_response.json.return_value = {"data": [{"price": {"total": "410.00", "currency": "USD"}}]}
    get_mock = mocker.patch("tools.booking_tools.requests.get", return_value=search_response)

    booking_response = MagicMock(status_code=200)
    booking_response.json.return_value = {"data": {"id": "rebooked"}}
    mocker.patch("tools.booking_tools.requests.post", return_value=booking_response)

    _tier2_fare_recheck_and_rebook(
        _sample_round_trip_offer(), [], *TIER2_CONTACT_ARGS,
        paid_stripe_amount=40000, paid_currency="USD",
    )
    called_params = get_mock.call_args.kwargs["params"]
    assert called_params["returnDate"] == "2026-07-08"
    assert called_params["departureDate"] == "2026-07-01"


def test_tier2_omits_return_date_for_a_one_way_original_offer(mocker):
    search_response = MagicMock(status_code=200)
    search_response.json.return_value = {"data": [{"price": {"total": "410.00", "currency": "USD"}}]}
    get_mock = mocker.patch("tools.booking_tools.requests.get", return_value=search_response)
    mocker.patch("tools.booking_tools.requests.post", return_value=MagicMock(
        status_code=200, json=lambda: {"data": {"id": "rebooked"}},
    ))

    _tier2_fare_recheck_and_rebook(
        _sample_offer(), [], *TIER2_CONTACT_ARGS,
        paid_stripe_amount=40000, paid_currency="USD",
    )
    called_params = get_mock.call_args.kwargs["params"]
    assert "returnDate" not in called_params


# --- Tier 2: multi-city re-search -------------------------------------------
#
# A genuine multi-city offer (3+ legs, or a 2-leg open-jaw) can't be
# re-searched with the simple GET endpoint used above -- _tier2_fare_recheck_
# and_rebook routes it through _post_multi_city_search instead (imported
# from tools.flight_tools; same POST call search_multi_city_flights makes),
# so it's mocked at that boundary rather than at requests.get/post directly.

def test_tier2_routes_a_multi_city_offer_through_the_multi_city_search(mocker):
    post_mock = mocker.patch(
        "tools.booking_tools._post_multi_city_search",
        return_value=({"data": [{"price": {"total": "900.00", "currency": "USD"}}]}, None),
    )
    mocker.patch("tools.booking_tools.requests.get", side_effect=AssertionError("must not use the simple GET re-search for multi-city"))
    mocker.patch("tools.booking_tools.requests.post", return_value=MagicMock(
        status_code=200, json=lambda: {"data": {"id": "rebooked"}},
    ))

    result, booked_offer, reason = _tier2_fare_recheck_and_rebook(
        _sample_multi_city_offer(), [], *TIER2_CONTACT_ARGS,
        paid_stripe_amount=90000, paid_currency="USD",
    )
    assert reason is None
    assert result == {"data": {"id": "rebooked"}}

    # All three legs, in order, reached the multi-city search helper.
    call_args = post_mock.call_args
    legs_arg = call_args.args[0] if call_args.args else call_args.kwargs["legs"]
    assert [leg["origin"] for leg in legs_arg] == ["SGN", "BKK", "NRT"]
    assert [leg["destination"] for leg in legs_arg] == ["BKK", "NRT", "SGN"]


def test_tier2_routes_an_open_jaw_offer_through_the_multi_city_search_too(mocker):
    # Only 2 legs, but they don't return to the starting city -- still needs
    # the multi-city path, not the simple round-trip GET re-search.
    post_mock = mocker.patch(
        "tools.booking_tools._post_multi_city_search",
        return_value=({"data": [{"price": {"total": "410.00", "currency": "USD"}}]}, None),
    )
    mocker.patch("tools.booking_tools.requests.get", side_effect=AssertionError("must not use the simple GET re-search for an open-jaw trip"))
    mocker.patch("tools.booking_tools.requests.post", return_value=MagicMock(
        status_code=200, json=lambda: {"data": {"id": "rebooked"}},
    ))

    result, booked_offer, reason = _tier2_fare_recheck_and_rebook(
        _sample_open_jaw_offer(), [], *TIER2_CONTACT_ARGS,
        paid_stripe_amount=40000, paid_currency="USD",
    )
    assert reason is None
    assert post_mock.called


def test_tier2_reports_a_clean_reason_when_the_multi_city_search_itself_fails(mocker):
    mocker.patch(
        "tools.booking_tools._post_multi_city_search",
        return_value=(None, {"error": "Amadeus API returned status code 503", "details": "upstream unavailable"}),
    )

    result, booked_offer, reason = _tier2_fare_recheck_and_rebook(
        _sample_multi_city_offer(), [], *TIER2_CONTACT_ARGS,
        paid_stripe_amount=90000, paid_currency="USD",
    )
    assert result is None
    assert booked_offer is None
    assert "multi-city fare re-search failed" in reason
    assert "upstream unavailable" in reason


def test_tier2_gives_up_when_multi_city_re_search_finds_no_offers(mocker):
    mocker.patch(
        "tools.booking_tools._post_multi_city_search",
        return_value=({"data": []}, None),
    )

    result, booked_offer, reason = _tier2_fare_recheck_and_rebook(
        _sample_multi_city_offer(), [], *TIER2_CONTACT_ARGS,
        paid_stripe_amount=90000, paid_currency="USD",
    )
    assert result is None
    assert "no comparable fares" in reason
