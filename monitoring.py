"""
monitoring.py — FastAPI webhook receiver for the Telegram Bot Starter Kit.

Runs as a separate ASGI server (uvicorn) alongside the Telegram bot.
External services POST JSON payloads to this server; the server validates
the signature, routes the alert to the appropriate category, and forwards
it to subscribed Telegram users.

Endpoints:
    POST /webhook/{source}  — Receive an alert from a named source
    GET  /health            — Liveness probe for load balancers / uptime monitors
    GET  /stats             — Brief stats JSON (alert count, last alert)

Security:
    Every inbound request must carry an X-Webhook-Secret header that matches
    WEBHOOK_SECRET in the environment.  Requests without a valid secret
    receive a 403 response and are logged as warnings.

Running this module:
    uvicorn monitoring:app --host 0.0.0.0 --port 8443

Integration with the Telegram bot:
    The FastAPI app stores a reference to the Telegram Bot instance in
    `app.state.bot` and the Database in `app.state.db`.  Set these in
    bot.py after both the bot and FastAPI server are initialised.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from telegram import Bot
from telegram.constants import ParseMode

from database import Database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Telegram Bot Webhook Receiver",
    description="Receives external webhook payloads and routes them as Telegram alerts.",
    version="1.0.0",
)

# These are set by bot.py at startup
# app.state.bot: Bot
# app.state.db: Database
# app.state.webhook_secret: str
# app.state.alert_count: int  (in-process counter; not persistent)


# ---------------------------------------------------------------------------
# Startup / shutdown events
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _on_startup() -> None:
    logger.info("Webhook server starting up")
    if not hasattr(app.state, "alert_count"):
        app.state.alert_count = 0


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    logger.info("Webhook server shutting down")


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def _verify_secret(header_secret: str, expected_secret: str) -> bool:
    """
    Constant-time comparison of the provided and expected webhook secrets.

    Using hmac.compare_digest prevents timing-based attacks.
    """
    return hmac.compare_digest(
        header_secret.encode("utf-8"),
        expected_secret.encode("utf-8"),
    )


def _verify_hmac_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Optional HMAC-SHA256 signature check for sources that sign their payloads
    (e.g. GitHub, Stripe, Datadog).

    Signature format expected: ``sha256=<hex-digest>``
    """
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


# ---------------------------------------------------------------------------
# Route helpers
# ---------------------------------------------------------------------------

def _get_bot(request: Request) -> Bot:
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bot not initialised yet.",
        )
    return bot  # type: ignore[return-value]


def _get_db(request: Request) -> Database:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not initialised yet.",
        )
    return db  # type: ignore[return-value]


def _get_secret(request: Request) -> str:
    return getattr(request.app.state, "webhook_secret", "")


# ---------------------------------------------------------------------------
# Payload routing
# ---------------------------------------------------------------------------

CATEGORY_MAP: dict[str, str] = {
    "news": "News",
    "price": "Price Alerts",
    "market": "Market Updates",
    "system": "System Status",
}


def _route_category(source: str, payload: dict[str, Any]) -> str:
    """
    Derive the alert category from the webhook source name or payload type.

    Falls back to "System Status" for unknown sources.
    """
    # Allow payload to override: {"category": "Price Alerts", ...}
    if "category" in payload:
        return str(payload["category"])

    for key, category in CATEGORY_MAP.items():
        if key in source.lower():
            return category

    return "System Status"


def _format_alert(source: str, payload: dict[str, Any]) -> str:
    """
    Build a human-readable Telegram message from a webhook payload.

    Developers customise this function to match their data source schema.
    """
    title = payload.get("title", f"Alert from {source}")
    body = payload.get("message", payload.get("body", payload.get("text", "")))
    severity = payload.get("severity", payload.get("level", "")).upper()
    timestamp = payload.get("timestamp", datetime.now(timezone.utc).isoformat())

    parts = [f"*{title}*"]
    if severity:
        parts.append(f"Severity: `{severity}`")
    if body:
        parts.append(body)
    parts.append(f"_Source: {source} | {timestamp}_")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", tags=["monitoring"])
async def health() -> JSONResponse:
    """
    Liveness probe endpoint.

    Returns 200 with a JSON body confirming the server is alive.
    """
    return JSONResponse({
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/stats", tags=["monitoring"])
async def webhook_stats(
    request: Request,
    x_webhook_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Return in-process webhook stats.

    Requires the webhook secret header.
    """
    if not _verify_secret(x_webhook_secret, _get_secret(request)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid secret.")

    db = _get_db(request)
    alert_count = await db.alert_count()
    last_alert = await db.last_alert_time()

    return JSONResponse({
        "webhook_alerts_this_session": request.app.state.alert_count,
        "total_alerts_in_db": alert_count,
        "last_alert": last_alert,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.post("/webhook/{source}", status_code=status.HTTP_202_ACCEPTED, tags=["webhooks"])
async def receive_webhook(
    source: str,
    request: Request,
    x_webhook_secret: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
) -> JSONResponse:
    """
    Receive a webhook payload from an external service.

    The *source* path parameter identifies the originating service
    (e.g. ``/webhook/github``, ``/webhook/datadog``, ``/webhook/custom``).

    Authentication options (checked in order):
    1. ``X-Webhook-Secret`` header — simple shared secret (recommended for custom sources)
    2. ``X-Hub-Signature-256`` header — HMAC-SHA256 (GitHub-style)

    Payload format (JSON, all fields optional except for being JSON):
    ```json
    {
        "title": "Alert title",
        "message": "Human-readable alert body",
        "severity": "HIGH",
        "category": "Price Alerts",
        "timestamp": "2025-01-01T12:00:00Z"
    }
    ```
    """
    raw_body = await request.body()
    secret = _get_secret(request)

    # --- Auth ---
    if x_hub_signature_256:
        # GitHub-style HMAC
        if not _verify_hmac_signature(raw_body, x_hub_signature_256, secret):
            logger.warning("Invalid HMAC signature from source=%s", source)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature.")
    elif x_webhook_secret:
        if not _verify_secret(x_webhook_secret, secret):
            logger.warning("Invalid webhook secret from source=%s", source)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid secret.")
    else:
        logger.warning("Webhook from source=%s missing authentication", source)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required (X-Webhook-Secret or X-Hub-Signature-256).",
        )

    # --- Parse payload ---
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be valid JSON.",
        )

    category = _route_category(source, payload)
    message = _format_alert(source, payload)

    # --- Deliver to matching subscribers ---
    bot = _get_bot(request)
    db = _get_db(request)
    subscribers = await db.get_subscribers()

    delivered = 0
    for user in subscribers:
        user_cats = {c.strip() for c in user.categories.split(",") if c.strip()}
        if category not in user_cats:
            continue
        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=message,
                parse_mode=ParseMode.MARKDOWN,
            )
            await db.log_alert(user.telegram_id, category, payload.get("message", message[:100]))
            delivered += 1
        except Exception as exc:
            logger.warning("Failed to deliver alert to user %d: %s", user.telegram_id, exc)

    request.app.state.alert_count += 1
    logger.info(
        "Webhook from source=%s category=%s delivered to %d/%d subscribers",
        source, category, delivered, len(subscribers),
    )

    return JSONResponse({
        "accepted": True,
        "category": category,
        "delivered_to": delivered,
    })


# ---------------------------------------------------------------------------
# Server runner (for standalone use)
# ---------------------------------------------------------------------------

def run_server(
    host: str = "0.0.0.0",
    port: int = 8443,
    bot: "Bot | None" = None,
    db: "Database | None" = None,
    webhook_secret: str = "",
    log_level: str = "info",
) -> None:
    """
    Launch the webhook server with uvicorn.

    When called from bot.py, pass the bot and db instances so the webhook
    handlers can forward alerts.

    This function is blocking — run it in a separate thread or process,
    or use the asyncio approach in bot.py.
    """
    import uvicorn

    if bot is not None:
        app.state.bot = bot
    if db is not None:
        app.state.db = db
    app.state.webhook_secret = webhook_secret
    app.state.alert_count = 0

    uvicorn.run(app, host=host, port=port, log_level=log_level)
