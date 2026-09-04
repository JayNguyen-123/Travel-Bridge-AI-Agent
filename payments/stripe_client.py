# payments/stripe_client.py
"""
Thin Stripe wrapper. Kept separate from tools/payment_tools.py so the pricing
math (this file) and the ADK-tool-facing function signatures (payment_tools.py)
can be tested and reasoned about independently.

SECURITY PRINCIPLE: the charge amount is always derived from the Amadeus
flight offer's own price.total/price.currency fields, computed server-side in
this module. The voice agent (and therefore the LLM) never supplies a number
that ends up as a Stripe charge amount -- it only ever sees the result.
"""
import os
import time

import stripe

# Stripe's published zero-decimal currency list (amounts are whole units, not
# cents) -- https://docs.stripe.com/currencies#zero-decimal. Trimmed to the
# currencies this agent is realistically likely to see from Amadeus; extend
# if you start seeing others.
ZERO_DECIMAL_CURRENCIES = {
    "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG",
    "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
}

CHECKOUT_EXPIRY_SECONDS = 30 * 60  # 30 minutes


def _stripe():
    """Lazily configure the Stripe SDK so importing this module doesn't
    require STRIPE_SECRET_KEY to already be set (useful for unit tests)."""
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
    if not stripe.api_key:
        raise RuntimeError("STRIPE_SECRET_KEY environment variable is missing.")
    return stripe


def price_from_flight_offer(flight_offer: dict) -> tuple[str, str]:
    """Extracts (total, currency) straight from an Amadeus flight offer's
    price block. Raises ValueError if the offer doesn't have one."""
    price = flight_offer.get("price", {})
    total = price.get("total")
    currency = price.get("currency")
    if not total or not currency:
        raise ValueError("flight_offer is missing price.total / price.currency")
    return str(total), currency.upper()


def to_stripe_amount(total: str, currency: str) -> int:
    """Converts a decimal amount string into Stripe's integer 'smallest unit'
    representation, honoring zero-decimal currencies like VND/JPY."""
    amount = float(total)
    if currency.upper() in ZERO_DECIMAL_CURRENCIES:
        return int(round(amount))
    return int(round(amount * 100))


def format_amount_for_display(total: str, currency: str) -> str:
    currency = currency.upper()
    if currency in ZERO_DECIMAL_CURRENCIES:
        return f"{float(total):,.0f} {currency}".replace(",", ".")
    return f"{float(total):,.2f} {currency}"


def create_checkout_session(
    *,
    stripe_amount: int,
    currency: str,
    customer_email: str,
    idempotency_key: str,
    offer_hash: str,
    success_url: str,
    cancel_url: str,
):
    """Creates a Stripe Checkout Session for an exact, pre-computed amount.
    Pass `idempotency_key` twice on purpose: once as the Stripe API
    idempotency key (so a network retry can't double-create a session), and
    once in `metadata` (so the webhook handler can find our internal record)."""
    s = _stripe()
    return s.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        customer_email=customer_email or None,
        line_items=[{
            "price_data": {
                "currency": currency.lower(),
                "product_data": {"name": "Flight booking"},
                "unit_amount": stripe_amount,
            },
            "quantity": 1,
        }],
        metadata={"idempotency_key": idempotency_key, "offer_hash": offer_hash},
        success_url=success_url,
        cancel_url=cancel_url,
        expires_at=int(time.time()) + CHECKOUT_EXPIRY_SECONDS,
        idempotency_key=f"checkout-{idempotency_key}",
    )


def create_refund(payment_intent_id: str, amount: int):
    """Issues a Stripe refund for an exact, pre-computed amount (Stripe's
    smallest-unit integer, same convention as to_stripe_amount()). Callers
    must always pass a real amount pulled from the `payments` row being
    refunded -- never a value the LLM computed or guessed."""
    s = _stripe()
    return s.Refund.create(
        payment_intent=payment_intent_id,
        amount=amount,
        idempotency_key=f"refund-{payment_intent_id}-{amount}",
    )


def construct_webhook_event(payload: bytes, sig_header: str, webhook_secret: str):
    """Verifies the Stripe-Signature header and returns the parsed event.
    Raises stripe.error.SignatureVerificationError / ValueError on failure --
    callers must reject the request (HTTP 400) rather than trust unverified
    payloads. This is the ONLY path that is allowed to mark a payment 'paid'."""
    s = _stripe()
    return s.Webhook.construct_event(payload, sig_header, webhook_secret)
