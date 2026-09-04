# tools/payment_tools.py
"""
ADK-facing payment tools. These are the only two payment operations the voice
agent can invoke:

  create_payment_checkout  -- start a Stripe Checkout session for a specific,
                               already-quoted flight offer, and text the
                               customer a payment link.
  check_payment_status     -- poll whether that session has been paid yet.

Neither tool ever marks a payment as "paid" -- only the Stripe webhook
handler (server/main.py -> payments/stripe_client.construct_webhook_event)
does that, because only it holds a signature proving the request really came
from Stripe. That split is deliberate: it means nothing the LLM says or does
can talk this system into believing a booking was paid for.
"""
import asyncio
import hashlib
import json
import os
import uuid

import stripe

from db.database import get_db_connection, release_db_connection
from notifications.sms import send_payment_link_sms
from payments.stripe_client import (
    create_checkout_session,
    format_amount_for_display,
    price_from_flight_offer,
    to_stripe_amount,
)


def _create_payment_checkout_sync(flight_offer_json_str: str, customer_email: str, customer_phone: str) -> dict:
    try:
        flight_offer = json.loads(flight_offer_json_str)
    except Exception as e:
        return {"error": "invalid_flight_offer", "details": f"flight_offer_json_str was not valid JSON: {e}"}

    try:
        total, currency = price_from_flight_offer(flight_offer)
        stripe_amount = to_stripe_amount(total, currency)
    except ValueError as e:
        return {"error": "invalid_flight_offer", "details": str(e)}

    idempotency_key = uuid.uuid4().hex
    offer_hash = hashlib.sha256(flight_offer_json_str.encode("utf-8")).hexdigest()
    display_amount = format_amount_for_display(total, currency)

    success_url = os.environ.get("CHECKOUT_SUCCESS_URL", "https://example.com/payment-success")
    cancel_url = os.environ.get("CHECKOUT_CANCEL_URL", "https://example.com/payment-cancelled")

    try:
        session = create_checkout_session(
            stripe_amount=stripe_amount,
            currency=currency,
            customer_email=customer_email,
            idempotency_key=idempotency_key,
            offer_hash=offer_hash,
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except stripe.error.StripeError as e:
        return {"error": "stripe_error", "details": str(e)}
    except RuntimeError as e:
        # STRIPE_SECRET_KEY missing
        return {"error": "payment_not_configured", "details": str(e)}

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payments
                    (idempotency_key, checkout_session_id, status, amount_total,
                     currency, customer_email, customer_phone, offer_hash)
                VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s)
                """,
                (idempotency_key, session.id, stripe_amount, currency, customer_email, customer_phone, offer_hash),
            )
        conn.commit()
    finally:
        release_db_connection(conn)

    sms_result = send_payment_link_sms(customer_phone, display_amount, session.url)

    return {
        "idempotency_key": idempotency_key,
        "checkout_url": session.url,
        "amount": total,
        "currency": currency,
        "display_amount": display_amount,
        "expires_in_minutes": 30,
        "sms_sent": sms_result.get("sent", False),
    }


async def create_payment_checkout(flight_offer_json_str: str, customer_email: str, customer_phone: str) -> dict:
    """
    Starts a Stripe Checkout payment for the EXACT price on `flight_offer_json_str`
    (never a number you make up) and texts the customer a secure payment link.

    Args:
        flight_offer_json_str (str): The exact flight offer JSON the customer
            just agreed to book, taken verbatim from a prior search_flights result.
        customer_email (str): Traveler's email, for the Stripe receipt.
        customer_phone (str): Traveler's phone in full international format
            (e.g. +12025551234) -- the payment link is texted here.

    Returns:
        dict: On success, an `idempotency_key` you MUST pass to
        check_payment_status and confirm_flight_booking, plus the
        checkout_url/display_amount for you to reference out loud. On
        failure, an `error` field explaining what went wrong.
    """
    return await asyncio.to_thread(_create_payment_checkout_sync, flight_offer_json_str, customer_email, customer_phone)


def _check_payment_status_sync(idempotency_key: str) -> dict:
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, amount_total, currency FROM payments WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            row = cur.fetchone()
    finally:
        release_db_connection(conn)

    if not row:
        return {"error": "unknown_idempotency_key", "details": "No payment was started with this key."}

    status, amount_total, currency = row
    return {"status": status, "amount_total": amount_total, "currency": currency}


async def check_payment_status(idempotency_key: str) -> dict:
    """
    Checks whether the payment started by create_payment_checkout has completed.

    Args:
        idempotency_key (str): The idempotency_key returned by create_payment_checkout.

    Returns:
        dict: {"status": "pending" | "paid" | "failed" | "expired", ...}.
        Only call confirm_flight_booking once status == "paid".
    """
    return await asyncio.to_thread(_check_payment_status_sync, idempotency_key)
