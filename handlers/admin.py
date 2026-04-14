"""
handlers/admin.py — Admin-only command handlers.

Commands:
    /admin      Dashboard: user/subscriber counts, uptime
    /broadcast  Send a custom message to all current subscribers
    /stats      Detailed usage analytics

All handlers are guarded by the `admin_only` decorator which checks the
caller's Telegram ID against ADMIN_IDS from the config.
"""

from __future__ import annotations

import functools
import logging
from datetime import datetime, timezone
from typing import Callable, Coroutine, Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import Config
from database import Database

logger = logging.getLogger(__name__)

# Conversation state for /broadcast
BROADCAST_MESSAGE = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]  # type: ignore[return-value]


def _cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["config"]  # type: ignore[return-value]


def _start_time(context: ContextTypes.DEFAULT_TYPE) -> datetime:
    return context.application.bot_data["start_time"]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Admin guard decorator
# ---------------------------------------------------------------------------

HandlerFunc = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, Any]]


def admin_only(func: HandlerFunc) -> HandlerFunc:
    """
    Decorator that silently ignores calls from non-admin users.

    Reads the ADMIN_IDS from the Config stored in bot_data, so no global
    state is needed.
    """
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        tg = update.effective_user
        if tg is None:
            return None

        cfg = _cfg(context)
        if not cfg.is_admin(tg.id):
            logger.warning(
                "Non-admin user %d (%s) attempted to use admin command %s",
                tg.id,
                tg.username,
                func.__name__,
            )
            await update.message.reply_text(
                "Access denied. This command is for administrators only."
            )
            return None

        return await func(update, context)

    return wrapper  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# /admin — dashboard
# ---------------------------------------------------------------------------

@admin_only
async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Show the admin dashboard: live stats and quick-action summary.
    """
    db = _db(context)
    start_time = _start_time(context)

    uptime = datetime.now(timezone.utc) - start_time
    hours, rem = divmod(int(uptime.total_seconds()), 3600)
    minutes = rem // 60

    total_users = await db.user_count()
    subscriber_count = await db.subscriber_count()
    alert_count = await db.alert_count()
    last_alert = await db.last_alert_time() or "never"
    recent_alerts = await db.get_recent_alerts(limit=5)

    recent_lines = "\n".join(
        f"  • [{a.category}] {a.message[:40]}…" if len(a.message) > 40 else f"  • [{a.category}] {a.message}"
        for a in recent_alerts
    ) or "  (none)"

    text = (
        f"*Admin Dashboard*\n\n"
        f"Bot uptime: `{hours}h {minutes}m`\n"
        f"Total users: `{total_users}`\n"
        f"Subscribers: `{subscriber_count}`\n"
        f"Alerts sent: `{alert_count}`\n"
        f"Last alert: `{last_alert}`\n\n"
        f"*Recent Alerts*\n{recent_lines}\n\n"
        f"Commands: /broadcast /stats"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    logger.info("Admin %d viewed dashboard", update.effective_user.id)


# ---------------------------------------------------------------------------
# /broadcast — ConversationHandler
# ---------------------------------------------------------------------------

@admin_only
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt the admin to type the broadcast message."""
    db = _db(context)
    count = await db.subscriber_count()
    await update.message.reply_text(
        f"You are about to broadcast to *{count}* subscriber(s).\n\n"
        "Type your message now, or /cancel to abort.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return BROADCAST_MESSAGE


async def broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the message text and send it to all subscribers."""
    tg = update.effective_user
    cfg = _cfg(context)
    if tg is None or not cfg.is_admin(tg.id):
        return ConversationHandler.END

    message_text = update.message.text
    if not message_text:
        await update.message.reply_text("Empty message — broadcast cancelled.")
        return ConversationHandler.END

    db = _db(context)
    subscribers = await db.get_subscribers()

    sent = 0
    failed = 0
    for user in subscribers:
        try:
            await context.bot.send_message(
                chat_id=user.telegram_id,
                text=f"📢 *Broadcast*\n\n{message_text}",
                parse_mode=ParseMode.MARKDOWN,
            )
            await db.log_alert(user.telegram_id, "broadcast", message_text)
            sent += 1
        except Exception as exc:
            logger.warning("Failed to send broadcast to %d: %s", user.telegram_id, exc)
            failed += 1

    await update.message.reply_text(
        f"Broadcast complete.\nSent: {sent}  |  Failed: {failed}"
    )
    logger.info("Admin %d broadcast to %d users (%d failed)", tg.id, sent, failed)
    return ConversationHandler.END


async def broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel a pending broadcast."""
    await update.message.reply_text("Broadcast cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /stats — detailed analytics
# ---------------------------------------------------------------------------

@admin_only
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Show detailed usage statistics: category breakdown, recent activity.
    """
    db = _db(context)

    all_users = await db.get_all_users()
    subscribers = [u for u in all_users if u.is_subscribed]

    # Category breakdown
    cat_counts: dict[str, int] = {}
    for user in subscribers:
        for cat in user.categories.split(","):
            cat = cat.strip()
            if cat:
                cat_counts[cat] = cat_counts.get(cat, 0) + 1

    cat_lines = "\n".join(
        f"  {cat}: {count}" for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1])
    ) or "  (none)"

    # Timezone distribution
    tz_counts: dict[str, int] = {}
    for user in all_users:
        tz_counts[user.timezone] = tz_counts.get(user.timezone, 0) + 1

    tz_lines = "\n".join(
        f"  {tz}: {count}" for tz, count in sorted(tz_counts.items(), key=lambda x: -x[1])
    )

    total_alerts = await db.alert_count()
    last_alert = await db.last_alert_time() or "never"

    text = (
        f"*Usage Statistics*\n\n"
        f"Total users: `{len(all_users)}`\n"
        f"Active subscribers: `{len(subscribers)}`\n"
        f"Total alerts: `{total_alerts}`\n"
        f"Last alert: `{last_alert}`\n\n"
        f"*Category Subscriptions*\n{cat_lines}\n\n"
        f"*Timezone Distribution*\n{tz_lines}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    logger.info("Admin %d viewed stats", update.effective_user.id)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_admin_handlers(app: Application) -> None:  # type: ignore[type-arg]
    """
    Attach all admin command handlers to the Application.

    Call this once from bot.py during setup.
    """
    app.add_handler(CommandHandler("admin", admin_dashboard))
    app.add_handler(CommandHandler("stats", stats))

    broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start)],
        states={
            BROADCAST_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_send),
            ],
        },
        fallbacks=[CommandHandler("cancel", broadcast_cancel)],
        per_message=False,
    )
    app.add_handler(broadcast_conv)

    logger.info("Admin command handlers registered")
