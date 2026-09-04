import pytest

from payments.stripe_client import (
    format_amount_for_display,
    price_from_flight_offer,
    to_stripe_amount,
)


def test_price_from_flight_offer_reads_amadeus_shape():
    offer = {"price": {"currency": "USD", "total": "1045.13"}}
    total, currency = price_from_flight_offer(offer)
    assert total == "1045.13"
    assert currency == "USD"


def test_price_from_flight_offer_missing_price_raises():
    with pytest.raises(ValueError):
        price_from_flight_offer({"no": "price here"})


def test_to_stripe_amount_usd_uses_cents():
    assert to_stripe_amount("1045.13", "USD") == 104513


def test_to_stripe_amount_vnd_is_zero_decimal():
    # VND is a Stripe zero-decimal currency: no x100 multiplication.
    assert to_stripe_amount("27522000", "VND") == 27522000


def test_to_stripe_amount_rounds_correctly():
    assert to_stripe_amount("9.999", "USD") == 1000  # rounds to nearest cent


def test_format_amount_for_display_usd():
    assert format_amount_for_display("1045.13", "USD") == "1,045.13 USD"


def test_format_amount_for_display_vnd_has_no_decimals():
    assert format_amount_for_display("27522000", "VND") == "27.522.000 VND"
