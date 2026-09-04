from middleware.translator import parse_iso_duration, process_and_convert_all, _rate_cache
from payments.stripe_client import price_from_flight_offer


def test_parse_iso_duration_hours_and_minutes():
    assert parse_iso_duration("PT14H15M") == "14 tiếng 15 phút"


def test_parse_iso_duration_minutes_only():
    assert parse_iso_duration("PT45M") == "45 phút"


def test_parse_iso_duration_zero():
    assert parse_iso_duration("PT") == "0 phút"


def test_parse_iso_duration_passthrough_for_non_iso():
    assert parse_iso_duration("not-a-duration") == "not-a-duration"


def test_process_and_convert_all_adds_dual_currency(monkeypatch):
    # Freeze the FX cache so this test doesn't depend on network access.
    monkeypatch.setitem(_rate_cache, "last_fetched", 10**12)  # far future -> skip refresh
    payload = {"price": {"currency": "USD", "total": "100.00"}}
    result = process_and_convert_all(payload)
    price_block = result["price"]
    assert "converted_price_USD" in price_block
    assert "converted_price_VND" in price_block
    assert price_block["converted_price_USD"] == "$100.00"


def test_process_and_convert_all_translates_cabin_value_but_not_the_key(monkeypatch):
    monkeypatch.setitem(_rate_cache, "last_fetched", 10**12)
    payload = {"cabin": "ECONOMY"}
    result = process_and_convert_all(payload)
    # The VALUE is translated for natural spoken Vietnamese...
    assert result["cabin"] == "Hạng phổ thông"
    # ...but the KEY is not -- see AMADEUS_DICTIONARY's docstring on why
    # renaming dict keys (this used to turn "cabin" into "hạng_vé", and
    # "price"/"currency" into "giá_tiền"/"tiền_tệ") was a real bug: it broke
    # every downstream reader that expects Amadeus's actual field names,
    # including price_from_flight_offer below.
    assert "hạng_vé" not in result


def test_process_and_convert_all_never_renames_price_or_currency_keys_at_any_depth(monkeypatch):
    monkeypatch.setitem(_rate_cache, "last_fetched", 10**12)
    payload = {
        "price": {"currency": "USD", "total": "500.00"},
        "travelerPricings": [
            {"travelerId": "1", "price": {"currency": "USD", "total": "500.00"}},
        ],
    }
    result = process_and_convert_all(payload)
    assert result["price"]["currency"] == "USD"
    assert result["travelerPricings"][0]["price"]["currency"] == "USD"
    assert "giá_tiền" not in result
    assert "tiền_tệ" not in result["price"]


def test_process_and_convert_all_output_is_readable_by_price_from_flight_offer(monkeypatch):
    # The actual regression this guards: search_flights's output is the ONLY
    # copy of an offer the agent ever holds, and it's passed straight into
    # create_payment_checkout -> price_from_flight_offer. If key-renaming
    # ever comes back, this fails with the same ValueError real bookings hit.
    monkeypatch.setitem(_rate_cache, "last_fetched", 10**12)
    raw_amadeus_offer = {"price": {"currency": "USD", "total": "742.30"}, "id": "OFFER1"}

    agent_visible_offer = process_and_convert_all(raw_amadeus_offer)
    total, currency = price_from_flight_offer(agent_visible_offer)

    assert total == "742.30"
    assert currency == "USD"
