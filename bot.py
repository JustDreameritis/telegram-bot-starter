"""
bot.py — Main entry point for the Telegram Bot Starter Kit.

Bootstraps the Application, wires up all handlers, starts the APScheduler,
launches the FastAPI webhook server in a background thread, and runs the
Telegram polling loop until SIGINT / SIGTERM.

Usage:
    python bot.py

Environment:
    See .env.example for all required and optional variables.
    Copy .env.example to .env and fill in your values before running.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, ApplicationBuilder

from config import load_config
from database import Database
from handlers import (
    register_admin_handlers,
    register_callback_handlers,
    register_command_handlers,
)
from monitoring import app as fastapi_app, run_server
from scheduler import build_scheduler

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _configure_logging(level: str) -> None:
    """Configure root logger with a clean format."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # Quiet noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: "telegram.ext.CallbackContext") -> None:  # type: ignore[name-defined]
    """
    Global error handler — logs all unhandled exceptions from handlers.

    In production you could also notify admins or send to an error tracker.
    """
    import traceback
    logger.error(
        "Unhandled exception while processing update: %s",
        context.error,
        exc_info=context.error,
    )
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "An unexpected error occurred. The developers have been notified."
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Webhook server thread
# ---------------------------------------------------------------------------

def _start_webhook_server(
    bot: "telegram.Bot",  # type: ignore[name-defined]
    db: Database,
    webhook_secret: str,
    port: int,
    log_level: str,
) -> threading.Thread:
    """
    Launch the FastAPI / uvicorn webhook server in a daemon thread.

    Daemon threads are killed automatically when the main process exits.
    """
    fastapi_app.state.bot = bot
    fastapi_app.state.db = db
    fastapi_app.state.webhook_secret = webhook_secret
    fastapi_app.state.alert_count = 0

    thread = threading.Thread(
        target=run_server,
        kwargs={
            "host": "0.0.0.0",
            "port": port,
            "bot": None,   # Already set on app.state above
            "db": None,
            "webhook_secret": "",
            "log_level": log_level.lower(),
        },
        daemon=True,
        name="webhook-server",
    )
    thread.start()
    logger.info("Webhook server started on port %d (thread: %s)", port, thread.name)
    return thread


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

async def post_init(application: Application) -> None:  # type: ignore[type-arg]
    """
    Called by the Application after all handlers are registered but before
    polling starts.  A good place for async initialisation.
    """
    db: Database = application.bot_data["db"]
    await db.init()
    logger.info("Database initialised")


async def post_shutdown(application: Application) -> None:  # type: ignore[type-arg]
    """
    Called by the Application during graceful shutdown.
    """
    db: Database = application.bot_data.get("db")  # type: ignore[assignment]
    if db:
        await db.close()
        logger.info("Database connection closed")

    scheduler = application.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def build_application(cfg: "config.Config") -> Application:  # type: ignore[type-arg,name-defined]
    """
    Construct and configure the python-telegram-bot Application.

    Stores shared state (config, db, start_time) in bot_data so all
    handlers and scheduled jobs can access it without globals.
    """
    import config as cfg_module  # local import to avoid circular at module level

    db = Database(cfg.database_path)

    application = (
        ApplicationBuilder()
        .token(cfg.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Shared state for all handlers
    application.bot_data["config"] = cfg
    application.bot_data["db"] = db
    application.bot_data["start_time"] = datetime.now(timezone.utc)

    # Register handlers
    register_command_handlers(application)
    register_admin_handlers(application)
    register_callback_handlers(application)

    # Error handler
    application.add_error_handler(error_handler)

    return application


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Entry point — load config, build the application, start all services,
    run the polling loop, and shut everything down gracefully on exit.
    """
    # 1. Load and validate configuration
    try:
        from config import load_config
        cfg = load_config()
    except RuntimeError as exc:
        # Config validation failed — print clearly and exit
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    # 2. Configure logging now that we have the level from config
    _configure_logging(cfg.log_level)
    logger.info("Starting Telegram Bot Starter Kit")

    # 3. Build Application
    application = build_application(cfg)

    # 4. Start webhook server in background thread
    _start_webhook_server(
        bot=application.bot,
        db=application.bot_data["db"],
        webhook_secret=cfg.webhook_secret,
        port=cfg.webhook_port,
        log_level=cfg.log_level,
    )

    # 5. Build and store the scheduler (started after Application runs)
    scheduler = build_scheduler(application)
    application.bot_data["scheduler"] = scheduler

    # 6. Hook scheduler start into Application lifecycle
    async def _start_scheduler(app: Application) -> None:  # type: ignore[type-arg]
        app.bot_data["scheduler"].start()
        logger.info("Scheduler started")

    application.post_init = _chain_post_init(application.post_init, _start_scheduler)  # type: ignore[method-assign]

    # 7. Run (blocking until SIGINT / SIGTERM)
    logger.info("Bot polling started. Press Ctrl+C to stop.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    logger.info("Bot stopped cleanly.")


def _chain_post_init(
    original: "Callable | None",  # type: ignore[name-defined]
    extra: "Callable",  # type: ignore[name-defined]
) -> "Callable":  # type: ignore[name-defined]
    """
    Wrap the existing post_init coroutine so we can add the scheduler
    start without overwriting the DB init that's already registered.
    """
    from typing import Callable as _Callable

    async def chained(app: Application) -> None:  # type: ignore[type-arg]
        if original is not None:
            await original(app)
        await extra(app)

    return chained


if __name__ == "__main__":
    main()
