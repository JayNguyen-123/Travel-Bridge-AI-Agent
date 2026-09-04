# server/main.py
"""
FastAPI server: a WebSocket voice endpoint driven by the real Google ADK
Runner / LiveRequestQueue / RunConfig API, plus a Stripe webhook endpoint.

The ADK wiring here (InMemoryRunner, awaiting session_service.create_session,
RunConfig(response_modalities=...), LiveRequestQueue, runner.run_live(),
Blob/Content/Part message shapes, and the event fields checked below) is
adapted directly from Google's own official example app:
https://github.com/google/adk-docs/blob/main/examples/python/snippets/streaming/adk-streaming/app/main.py
That sample uses Server-Sent Events; this file adapts the same verified ADK
calls to a WebSocket transport (bidirectional over one connection, which
suits a continuous voice session better). If ADK's live-streaming API
changes, re-diff this file against the current version of that sample.

An earlier draft of this project called a nonexistent
`AgentRuntime(...).start_web_server(...)` -- that class was never real.
"""
import asyncio
import base64
import json
import logging
import os

import stripe
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.adk.agents import LiveRequestQueue
from google.adk.agents.run_config import RunConfig
from google.adk.runners import InMemoryRunner
from google.genai import types
from google.genai.types import Blob, Content, Part

from agents.orchestrator import voice_travel_agent
from db.database import get_db_connection, init_schema, release_db_connection
from payments.stripe_client import construct_webhook_event
from server.admin import router as admin_router

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("travel_agent")

APP_NAME = "VoiceTravelAgent"
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="Bilingual Travel Voice Agent")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
# Internal support/refund-review admin UI -- see server/admin.py for the
# auth model (HTTP Basic against ADMIN_API_KEY, fails closed if unset).
app.include_router(admin_router)


def _check_required_env() -> list[str]:
    required = [
        "GEMINI_API_KEY", "AMADEUS_ACCESS_TOKEN",
        "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD",
        "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
    ]
    return [k for k in required if not os.environ.get(k)]


@app.on_event("startup")
async def on_startup():
    missing = _check_required_env()
    if missing:
        # Fail fast and loud rather than let a live call discover a missing
        # key mid-conversation.
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")
    await asyncio.to_thread(init_schema)
    logger.info("Startup checks passed; schema ensured.")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ---------------------------------------------------------------------------
# Voice session (WebSocket)
# ---------------------------------------------------------------------------

async def _start_agent_session(user_id: str, is_audio: bool):
    """Creates an ADK Runner + session + LiveRequestQueue for one connection.
    Mirrors google/adk-docs' start_agent_session(), using InMemoryRunner.

    KNOWN PRODUCTION LIMITATION: InMemoryRunner keeps session state in this
    process's memory only. That's fine for a single Cloud Run instance with
    session-affinity, but a session will NOT survive an instance restart or
    being routed to a different instance. See PRODUCTION_CHECKLIST.md.
    """
    runner = InMemoryRunner(app_name=APP_NAME, agent=voice_travel_agent)
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id=user_id)

    modality = "AUDIO" if is_audio else "TEXT"
    run_config = RunConfig(
        response_modalities=[modality],
        session_resumption=types.SessionResumptionConfig(),
    )

    live_request_queue = LiveRequestQueue()
    live_events = runner.run_live(session=session, live_request_queue=live_request_queue, run_config=run_config)
    return live_events, live_request_queue


async def _client_to_agent(websocket: WebSocket, live_request_queue: LiveRequestQueue) -> None:
    """Reads {"mime_type": "text/plain"|"audio/pcm", "data": ...} JSON frames
    from the browser and feeds them into ADK's LiveRequestQueue."""
    while True:
        message = await websocket.receive_json()
        mime_type = message.get("mime_type")
        data = message.get("data")

        if mime_type == "text/plain":
            content = Content(role="user", parts=[Part.from_text(text=data)])
            live_request_queue.send_content(content=content)
        elif mime_type == "audio/pcm":
            decoded = base64.b64decode(data)
            live_request_queue.send_realtime(Blob(data=decoded, mime_type="audio/pcm;rate=16000"))
        else:
            logger.warning("Ignoring unsupported inbound mime_type: %s", mime_type)


async def _agent_to_client(websocket: WebSocket, live_events) -> None:
    """Streams ADK live_events back to the browser as the same
    {"mime_type": ..., "data": ...} JSON shape, plus turn_complete/interrupted
    control frames."""
    async for event in live_events:
        if event.turn_complete or event.interrupted:
            await websocket.send_json({"turn_complete": event.turn_complete, "interrupted": event.interrupted})
            continue

        part = event.content and event.content.parts and event.content.parts[0]
        if not part:
            continue

        if part.inline_data and part.inline_data.mime_type and part.inline_data.mime_type.startswith("audio/pcm"):
            audio_data = part.inline_data.data
            if audio_data:
                await websocket.send_json({
                    "mime_type": "audio/pcm",
                    "data": base64.b64encode(audio_data).decode("ascii"),
                })
            continue

        if part.text and event.partial:
            await websocket.send_json({"mime_type": "text/plain", "data": part.text})


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str, is_audio: str = "false"):
    await websocket.accept()
    logger.info("Client %s connected, audio mode: %s", user_id, is_audio)

    live_events, live_request_queue = await _start_agent_session(user_id, is_audio == "true")

    to_agent_task = asyncio.create_task(_client_to_agent(websocket, live_request_queue))
    to_client_task = asyncio.create_task(_agent_to_client(websocket, live_events))

    try:
        done, pending = await asyncio.wait(
            {to_agent_task, to_client_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                logger.exception("Voice session task failed", exc_info=exc)
    finally:
        live_request_queue.close()
        logger.info("Client %s disconnected", user_id)


# ---------------------------------------------------------------------------
# Stripe webhook -- the ONLY path allowed to mark a payment 'paid'
# ---------------------------------------------------------------------------

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")

    try:
        event = await asyncio.to_thread(construct_webhook_event, payload, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning("Rejected webhook with invalid signature: %s", e)
        raise HTTPException(status_code=400, detail="invalid signature")

    await asyncio.to_thread(_process_stripe_event_sync, event)
    return {"status": "ok"}


def _process_stripe_event_sync(event: dict) -> None:
    event_id = event["id"]
    event_type = event["type"]
    obj = event["data"]["object"]

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM processed_webhook_events WHERE event_id = %s", (event_id,))
            if cur.fetchone():
                logger.info("Webhook event %s already processed, skipping.", event_id)
                return

            if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
                session_id = obj["id"]
                payment_intent_id = obj.get("payment_intent")
                new_status = "paid" if obj.get("payment_status") == "paid" else "pending"
                cur.execute(
                    "UPDATE payments SET status = %s, payment_intent_id = %s, updated_at = now() "
                    "WHERE checkout_session_id = %s",
                    (new_status, payment_intent_id, session_id),
                )
            elif event_type == "checkout.session.expired":
                cur.execute(
                    "UPDATE payments SET status = 'expired', updated_at = now() WHERE checkout_session_id = %s",
                    (obj["id"],),
                )
            elif event_type == "checkout.session.async_payment_failed":
                cur.execute(
                    "UPDATE payments SET status = 'failed', updated_at = now() WHERE checkout_session_id = %s",
                    (obj["id"],),
                )
            else:
                logger.debug("Ignoring unhandled Stripe event type: %s", event_type)

            cur.execute(
                "INSERT INTO processed_webhook_events (event_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (event_id,),
            )
        conn.commit()
    finally:
        release_db_connection(conn)
