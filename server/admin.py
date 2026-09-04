# server/admin.py
"""
Minimal internal admin UI for the human side of two things this system
deliberately does NOT fully automate:

  1. support_requests -- date/itinerary changes, refund reviews the customer
     asked about, and Tier 4 booking-failure escalations
     (tools/support_tools.py, tools/booking_tools.py).
  2. Bookings still awaiting a manual refund decision -- a cancellation that
     fell outside the DOT 24-hour auto-refund window, or one whose automatic
     refund attempt errored (tools/booking_tools.py's `refund_status`
     'pending_manual_review' / 'auto_refund_failed'). These often have no
     support_requests row at all unless the customer happened to call back
     and ask -- without this second half, an agent would have no way to
     discover most of them.

AUTH: a single shared ADMIN_API_KEY checked via HTTP Basic (any username,
that key as the password). This is deliberately minimal -- enough to keep
the queue off the open internet for one or two internal ops people, NOT a
substitute for real per-agent login/RBAC before a wider team uses this. See
PRODUCTION_CHECKLIST.md.

Every action that moves money (refund) goes through create_refund() in
payments/stripe_client.py -- the same function the automatic Tier 3 path
uses -- so the same idempotency-key convention and Stripe error handling
apply here. An admin can never refund more than what was actually paid
(amount_total on the payments row) unless they explicitly pass a smaller
partial amount.
"""
import os
import secrets
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from db.database import get_db_connection, release_db_connection
from payments.stripe_client import create_refund

router = APIRouter()
_security = HTTPBasic()
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

BOOKING_REFUND_REVIEW_STATUSES = ("pending_manual_review", "auto_refund_failed")


def require_admin_auth(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    admin_key = os.environ.get("ADMIN_API_KEY")
    if not admin_key:
        # Fail closed: an unset key means "no one is authorized to use this
        # right now", not "anyone is". Set ADMIN_API_KEY to enable /admin.
        raise HTTPException(status_code=503, detail="Admin UI is not configured (ADMIN_API_KEY unset).")
    # constant-time comparison -- this is a shared secret, treat it like one.
    if not secrets.compare_digest(credentials.password, admin_key):
        raise HTTPException(
            status_code=401, detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Basic realm=\"admin\""},
        )
    return credentials.username or "admin"


class ResolveBody(BaseModel):
    resolved_by: str
    resolution_notes: str = ""
    new_status: str = "closed"  # 'in_progress' | 'closed'


class RefundBody(BaseModel):
    resolved_by: str
    amount: Optional[int] = None  # Stripe smallest-unit integer; defaults to the full amount paid
    notes: str = ""


class DenyRefundBody(BaseModel):
    resolved_by: str
    notes: str = ""


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@router.get("/admin")
async def admin_page():
    # Deliberately unauthenticated: this just serves the static shell (no
    # data in it). admin.html prompts for credentials itself and attaches
    # them to every /admin/api/* call -- those endpoints are what's actually
    # gated by require_admin_auth. Serving the page itself behind HTTP Basic
    # too would trigger the browser's *native* auth dialog on top of the
    # page's own prompt, which is redundant and confusing.
    return FileResponse(os.path.join(STATIC_DIR, "admin.html"))


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

_SUPPORT_REQUEST_COLUMNS = [
    "id", "reference_code", "traveler_name", "phone_number", "request_type",
    "priority", "status", "details", "idempotency_key", "created_at", "resolved_at", "resolved_by",
]
_BOOKING_REVIEW_COLUMNS = [
    "idempotency_key", "reference_code", "traveler_name", "phone_number",
    "price_amount", "currency_type", "refund_status", "cancelled_at", "booking_notes",
]


@router.get("/admin/api/queue")
async def get_queue(status: str = "open", include_resolved_bookings: bool = False, _: str = Depends(require_admin_auth)):
    """Combined view: open support tickets plus bookings still awaiting a
    manual refund decision. `status` filters support_requests only
    ('open' | 'in_progress' | 'closed' | 'all')."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            if status == "all":
                cur.execute(
                    f"""
                    SELECT {', '.join(_SUPPORT_REQUEST_COLUMNS)} FROM support_requests
                    ORDER BY (priority = 'urgent') DESC, created_at ASC
                    """
                )
            else:
                cur.execute(
                    f"""
                    SELECT {', '.join(_SUPPORT_REQUEST_COLUMNS)} FROM support_requests
                    WHERE status = %s
                    ORDER BY (priority = 'urgent') DESC, created_at ASC
                    """,
                    (status,),
                )
            support_rows = cur.fetchall()

            if include_resolved_bookings:
                cur.execute(
                    f"""
                    SELECT {', '.join(_BOOKING_REVIEW_COLUMNS)} FROM travel_bookings
                    WHERE refund_status IN ('pending_manual_review', 'auto_refund_failed', 'manual', 'denied')
                    ORDER BY (refund_status = 'auto_refund_failed') DESC, cancelled_at ASC NULLS LAST
                    """
                )
            else:
                cur.execute(
                    f"""
                    SELECT {', '.join(_BOOKING_REVIEW_COLUMNS)} FROM travel_bookings
                    WHERE refund_status IN ('pending_manual_review', 'auto_refund_failed')
                    ORDER BY (refund_status = 'auto_refund_failed') DESC, cancelled_at ASC NULLS LAST
                    """
                )
            booking_rows = cur.fetchall()

            bookings = [dict(zip(_BOOKING_REVIEW_COLUMNS, row)) for row in booking_rows]
            travelers_by_key = _fetch_travelers_bulk(cur, [b["idempotency_key"] for b in bookings])
            for b in bookings:
                b["travelers"] = travelers_by_key.get(b["idempotency_key"], [])
    finally:
        release_db_connection(conn)

    return {
        "support_requests": [dict(zip(_SUPPORT_REQUEST_COLUMNS, row)) for row in support_rows],
        "bookings_pending_refund": bookings,
    }


@router.get("/admin/api/support-requests/{request_id}")
async def get_support_request(request_id: int, _: str = Depends(require_admin_auth)):
    detail_columns = _SUPPORT_REQUEST_COLUMNS + ["resolution_notes"]
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(detail_columns)} FROM support_requests WHERE id = %s",
                (request_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="support request not found")
            result = dict(zip(detail_columns, row))

            payment = None
            travelers = []
            if result["idempotency_key"]:
                payment = _fetch_payment(cur, result["idempotency_key"])
                travelers = _fetch_travelers(cur, result["idempotency_key"])
    finally:
        release_db_connection(conn)

    result["payment"] = payment
    result["travelers"] = travelers
    return result


def _fetch_payment(cur, idempotency_key: str):
    cur.execute(
        """
        SELECT status, amount_total, currency, payment_intent_id,
               refund_id, refund_amount, refunded_at
        FROM payments WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = ["status", "amount_total", "currency", "payment_intent_id", "refund_id", "refund_amount", "refunded_at"]
    return dict(zip(cols, row))


_TRAVELER_COLUMNS = ["first_name", "last_name", "date_of_birth", "gender", "traveler_type"]


def _fetch_travelers(cur, idempotency_key: str) -> list:
    """Full traveler roster for one booking (tools/booking_tools.py's
    booking_travelers table), ordered the same way they were collected. A
    single-traveler booking still returns one row here -- travel_bookings'
    own `traveler_name` only ever shows the lead traveler (or "Lead (+N
    more)" for a group), so this is the only place the admin UI can see
    everyone actually on a group booking. Empty for a pre-existing booking
    made before booking_travelers existed."""
    cur.execute(
        f"""
        SELECT {', '.join(_TRAVELER_COLUMNS)} FROM booking_travelers
        WHERE idempotency_key = %s ORDER BY id
        """,
        (idempotency_key,),
    )
    rows = cur.fetchall()
    return [
        {**dict(zip(_TRAVELER_COLUMNS, row)),
         "date_of_birth": row[2].isoformat() if row[2] else None}
        for row in rows
    ]


def _fetch_travelers_bulk(cur, idempotency_keys: list) -> dict:
    """Same as _fetch_travelers but for many bookings in one query -- used by
    the queue list so it doesn't issue one extra query per row."""
    if not idempotency_keys:
        return {}
    cur.execute(
        f"""
        SELECT idempotency_key, {', '.join(_TRAVELER_COLUMNS)} FROM booking_travelers
        WHERE idempotency_key = ANY(%s) ORDER BY idempotency_key, id
        """,
        (idempotency_keys,),
    )
    by_key = {}
    for row in cur.fetchall():
        key = row[0]
        traveler = {**dict(zip(_TRAVELER_COLUMNS, row[1:])),
                    "date_of_birth": row[3].isoformat() if row[3] else None}
        by_key.setdefault(key, []).append(traveler)
    return by_key


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

@router.post("/admin/api/support-requests/{request_id}/resolve")
async def resolve_support_request(request_id: int, body: ResolveBody, _: str = Depends(require_admin_auth)):
    """Closes (or moves to in_progress) a ticket WITHOUT touching money --
    for date_change/general tickets, or a refund_review the admin resolved
    outside this tool. Use the /refund or /deny-refund endpoints below for
    anything that should move (or explicitly not move) a payment."""
    if body.new_status not in ("in_progress", "closed"):
        raise HTTPException(status_code=400, detail="new_status must be 'in_progress' or 'closed'")

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE support_requests
                SET status = %s, resolved_by = %s, resolution_notes = %s,
                    resolved_at = CASE WHEN %s = 'closed' THEN now() ELSE resolved_at END
                WHERE id = %s
                RETURNING id
                """,
                (body.new_status, body.resolved_by, body.resolution_notes, body.new_status, request_id),
            )
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="support request not found")
        conn.commit()
    finally:
        release_db_connection(conn)

    return {"id": request_id, "status": body.new_status}


@router.post("/admin/api/idempotency/{idempotency_key}/refund")
async def manual_refund(idempotency_key: str, body: RefundBody, _: str = Depends(require_admin_auth)):
    """Issues a manual Stripe refund for a payment, identified by its
    idempotency_key -- works whether the case came in as a support_requests
    ticket or as a travel_bookings row pending refund review. Syncs both
    tables (and closes any open support ticket referencing this key) so the
    queue and the payment/booking records never disagree."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            payment = _fetch_payment(cur, idempotency_key)
            if not payment:
                raise HTTPException(status_code=404, detail="no payment record for this idempotency_key")
            if payment["refund_id"]:
                raise HTTPException(status_code=409, detail=f"already refunded (refund_id={payment['refund_id']})")
            if not payment["payment_intent_id"]:
                raise HTTPException(status_code=422, detail="payment has no payment_intent_id to refund")

            amount_total = payment["amount_total"] or 0
            refund_amount = body.amount if body.amount is not None else amount_total
            if refund_amount <= 0 or refund_amount > amount_total:
                raise HTTPException(status_code=422, detail=f"amount must be between 1 and {amount_total}")
            payment_intent_id = payment["payment_intent_id"]
            currency = payment["currency"]
    finally:
        release_db_connection(conn)

    try:
        refund = create_refund(payment_intent_id, amount=refund_amount)
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe refund failed: {e}")

    note = f"Manual refund by {body.resolved_by}: {body.notes}".strip()
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE payments
                SET status = 'refunded', refund_id = %s, refund_amount = %s, refunded_at = now(),
                    resolution_notes = %s, updated_at = now()
                WHERE idempotency_key = %s
                """,
                (refund.id, refund_amount, note, idempotency_key),
            )
            # Best-effort: keep travel_bookings in sync if a booking exists
            # for this key (a Tier 4 escalation from an unbooked payment has
            # no matching row, and that's fine).
            cur.execute(
                """
                UPDATE travel_bookings
                SET refund_status = 'manual', refund_id = %s, refund_amount = %s,
                    booking_notes = COALESCE(booking_notes || ' | ', '') || %s
                WHERE idempotency_key = %s
                """,
                (refund.id, refund_amount, note, idempotency_key),
            )
            cur.execute(
                """
                UPDATE support_requests
                SET status = 'closed', resolved_by = %s, resolution_notes = %s, resolved_at = now()
                WHERE idempotency_key = %s AND status != 'closed'
                """,
                (body.resolved_by, note, idempotency_key),
            )
        conn.commit()
    finally:
        release_db_connection(conn)

    return {"idempotency_key": idempotency_key, "refund_id": refund.id, "refund_amount": refund_amount, "currency": currency}


@router.post("/admin/api/idempotency/{idempotency_key}/deny-refund")
async def deny_refund(idempotency_key: str, body: DenyRefundBody, _: str = Depends(require_admin_auth)):
    """Closes out a refund review WITHOUT issuing a refund through this tool
    -- e.g. the fare rules don't support one, or it was already handled
    outside this system. Never moves money; only records the decision."""
    note = f"Refund denied by {body.resolved_by}: {body.notes}".strip()
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE travel_bookings
                SET refund_status = 'denied',
                    booking_notes = COALESCE(booking_notes || ' | ', '') || %s
                WHERE idempotency_key = %s
                """,
                (note, idempotency_key),
            )
            booking_updated = cur.rowcount > 0

            cur.execute(
                """
                UPDATE support_requests
                SET status = 'closed', resolved_by = %s, resolution_notes = %s, resolved_at = now()
                WHERE idempotency_key = %s AND status != 'closed'
                """,
                (body.resolved_by, note, idempotency_key),
            )
            ticket_updated = cur.rowcount > 0
        conn.commit()
    finally:
        release_db_connection(conn)

    if not booking_updated and not ticket_updated:
        raise HTTPException(status_code=404, detail="no booking or support request found for this idempotency_key")

    return {"idempotency_key": idempotency_key, "denied": True}
