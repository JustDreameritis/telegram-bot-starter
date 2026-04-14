"""
handlers/commands.py — User-facing command handlers.

Commands implemented:
    /start        Welcome message with inline keyboard
    /help         Full command reference
    /status       Bot uptime, subscriber count, last alert
    /subscribe    Subscribe to alert categories (multi-step conversation)
    /unsubscribe  Unsubscribe with confirmation dialog
    /settings     User preferences panel (timezone, frequency, quiet hours)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
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
from database import Database, User

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
SETTINGS_CHOOSE_FIELD = 0
SETTINGS_ENTER_VALUE = 1

# Category options offered during /subscribe
ALERT_CATEGORIES = ["News", "Price Alerts", "Market Updates", "System Status"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    """Extract the Database instance stored in bot_data."""
    return context.application.bot_data["db"]  # type: ignore[return-value]


def _cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    """Extract the Config instance stored in bot_data."""
    return context.application.bot_data["config"]  # type: ignore[return-value]


def _start_time(context: ContextTypes.DEFAULT_TYPE) -> datetime:
    return context.application.bot_data["start_time"]  # type: ignore[return-value]


async def _ensure_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Upsert the calling Telegram user into the database."""
    tg = update.effective_user
    if tg is None:
        return
    db = _db(context)
    existing = await db.get_user(tg.id)
    user = User(
        telegram_id=tg.id,
        username=tg.username or "",
        first_name=tg.first_name or "",
        is_subscribed=existing.is_subscribed if existing else False,
        categories=existing.categories if existing else "",
        timezone=existing.timezone if existing else "UTC",
        alert_frequency=existing.alert_frequency if existing else 3600,
        quiet_start=existing.quiet_start if existing else 23,
        quiet_end=existing.quiet_end if existing else 7,
    )
    await db.upsert_user(user)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Send a welcome message with an inline keyboard for quick navigation.

    Upserts the user on every call so new users are registered immediately.
    """
    await _ensure_user(update, context)

    tg = update.effective_user
    name = tg.first_name if tg else "there"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Subscribe to Alerts", callback_data="subscribe_start"),
            InlineKeyboardButton("My Settings", callback_data="settings_show"),
        ],
        [
            InlineKeyboardButton("Help", callback_data="help_show"),
            InlineKeyboardButton("Status", callback_data="status_show"),
        ],
    ])

    text = (
        f"Hello, *{name}*! Welcome to the Bot Starter Kit.\n\n"
        "I can send you scheduled alerts, monitor webhooks, and keep you "
        "updated in real-time.\n\n"
        "Use the buttons below or type /help to see all available commands."
    )

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    logger.info("User %s triggered /start", tg.id if tg else "unknown")


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

HELP_TEXT = """
*Available Commands*

*User Commands*
/start — Welcome screen with quick-action buttons
/help — Show this help message
/status — Bot health, uptime, and subscriber stats
/subscribe — Subscribe to alert categories
/unsubscribe — Cancel your subscription
/settings — View and update your preferences

*Admin Commands* _(restricted)_
/admin — Admin dashboard with live stats
/broadcast — Send a message to all subscribers
/stats — Detailed usage statistics

*Tips*
• Use inline keyboards where shown — no typing needed.
• Quiet hours suppress alerts between your configured hours.
• Change your timezone in /settings so alerts arrive at the right time.
""".strip()


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the full command reference."""
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Report bot uptime, subscriber count, and the time of the last sent alert.
    """
    db = _db(context)
    start_time = _start_time(context)

    uptime = datetime.now(timezone.utc) - start_time
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    subscriber_count = await db.subscriber_count()
    total_users = await db.user_count()
    last_alert = await db.last_alert_time()
    alert_count = await db.alert_count()

    last_alert_str = last_alert if last_alert else "No alerts sent yet"

    text = (
        f"*Bot Status*\n\n"
        f"Uptime: `{uptime_str}`\n"
        f"Total users: `{total_users}`\n"
        f"Subscribers: `{subscriber_count}`\n"
        f"Total alerts sent: `{alert_count}`\n"
        f"Last alert: `{last_alert_str}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# /subscribe (inline-keyboard category selection)
# ---------------------------------------------------------------------------

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Let the user pick alert categories via an inline keyboard.

    Existing subscriptions are shown as checked so the user can toggle them.
    """
    await _ensure_user(update, context)
    tg = update.effective_user
    if tg is None:
        return

    db = _db(context)
    user = await db.get_user(tg.id)
    current = set(user.categories.split(",")) if user and user.categories else set()

    buttons = []
    for cat in ALERT_CATEGORIES:
        label = f"✅ {cat}" if cat in current else cat
        buttons.append([InlineKeyboardButton(label, callback_data=f"cat_toggle:{cat}")])

    buttons.append([InlineKeyboardButton("Save & Close", callback_data="cat_save")])
    keyboard = InlineKeyboardMarkup(buttons)

    await update.message.reply_text(
        "*Select alert categories:*\n\nTap a category to toggle it on/off, "
        "then press *Save & Close*.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )
    logger.info("User %s opened subscription panel", tg.id)


# ---------------------------------------------------------------------------
# /unsubscribe
# ---------------------------------------------------------------------------

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask for confirmation before unsubscribing the user."""
    tg = update.effective_user
    if tg is None:
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Yes, unsubscribe me", callback_data="unsub_confirm"),
            InlineKeyboardButton("No, keep me subscribed", callback_data="unsub_cancel"),
        ]
    ])
    await update.message.reply_text(
        "Are you sure you want to unsubscribe from all alerts?",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# /settings — multi-step ConversationHandler
# ---------------------------------------------------------------------------

async def settings_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: show current settings and field selection keyboard."""
    await _ensure_user(update, context)
    tg = update.effective_user
    if tg is None:
        return ConversationHandler.END

    db = _db(context)
    user = await db.get_user(tg.id)
    if user is None:
        await update.message.reply_text("User record not found. Try /start first.")
        return ConversationHandler.END

    freq_label = f"{user.alert_frequency // 3600}h" if user.alert_frequency >= 3600 else f"{user.alert_frequency // 60}m"
    text = (
        f"*Your Settings*\n\n"
        f"Timezone: `{user.timezone}`\n"
        f"Alert frequency: `{freq_label}`\n"
        f"Quiet hours: `{user.quiet_start:02d}:00 – {user.quiet_end:02d}:00`\n\n"
        "Choose a field to update:"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Timezone", callback_data="set_timezone")],
        [InlineKeyboardButton("Alert Frequency", callback_data="set_frequency")],
        [InlineKeyboardButton("Quiet Hours Start", callback_data="set_quiet_start")],
        [InlineKeyboardButton("Quiet Hours End", callback_data="set_quiet_end")],
        [InlineKeyboardButton("Close", callback_data="settings_close")],
    ])

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    return SETTINGS_CHOOSE_FIELD


async def settings_choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle field-selection callbacks from the settings keyboard."""
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer()

    field_map = {
        "set_timezone": ("timezone", "Enter your timezone (e.g. `Europe/London`, `America/New_York`, `UTC`):"),
        "set_frequency": ("alert_frequency", "Enter alert frequency in minutes (e.g. `60` for hourly, `1440` for daily):"),
        "set_quiet_start": ("quiet_start", "Enter quiet hours START (0-23, e.g. `22` for 10 PM):"),
        "set_quiet_end": ("quiet_end", "Enter quiet hours END (0-23, e.g. `8` for 8 AM):"),
    }

    data = query.data or ""

    if data == "settings_close":
        await query.edit_message_text("Settings closed. Use /settings to reopen.")
        return ConversationHandler.END

    if data not in field_map:
        return ConversationHandler.END

    field, prompt = field_map[data]
    context.user_data["settings_field"] = field  # type: ignore[index]
    await query.edit_message_text(prompt, parse_mode=ParseMode.MARKDOWN)
    return SETTINGS_ENTER_VALUE


async def settings_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate and persist the value the user typed."""
    tg = update.effective_user
    if tg is None or update.message is None:
        return ConversationHandler.END

    field = context.user_data.get("settings_field")  # type: ignore[union-attr]
    raw = update.message.text or ""
    db = _db(context)

    try:
        if field == "timezone":
            await db.update_preferences(tg.id, timezone=raw.strip())
            await update.message.reply_text(f"Timezone updated to `{raw.strip()}`.", parse_mode=ParseMode.MARKDOWN)

        elif field == "alert_frequency":
            minutes = int(raw.strip())
            if minutes < 1:
                raise ValueError("Frequency must be at least 1 minute.")
            seconds = minutes * 60
            await db.update_preferences(tg.id, alert_frequency=seconds)
            await update.message.reply_text(f"Alert frequency updated to every {minutes} minute(s).")

        elif field in ("quiet_start", "quiet_end"):
            hour = int(raw.strip())
            if not 0 <= hour <= 23:
                raise ValueError("Hour must be 0–23.")
            kwargs = {field: hour}
            await db.update_preferences(tg.id, **kwargs)
            label = "start" if field == "quiet_start" else "end"
            await update.message.reply_text(f"Quiet hours {label} updated to {hour:02d}:00.")

        else:
            await update.message.reply_text("Unknown field. Use /settings to start over.")

    except (ValueError, TypeError) as exc:
        await update.message.reply_text(f"Invalid value: {exc}\n\nUse /settings to try again.")

    return ConversationHandler.END


async def settings_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the settings conversation."""
    if update.message:
        await update.message.reply_text("Settings update cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_command_handlers(app: Application) -> None:  # type: ignore[type-arg]
    """
    Attach all user-facing command handlers to the Application.

    Call this once from bot.py during setup.
    """
    # Simple commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))

    # Multi-step settings conversation
    settings_conv = ConversationHandler(
        entry_points=[CommandHandler("settings", settings_show)],
        states={
            SETTINGS_CHOOSE_FIELD: [
                # Callback queries from the inline keyboard
                MessageHandler(filters.TEXT & ~filters.COMMAND, settings_cancel),
            ],
            SETTINGS_ENTER_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, settings_receive),
            ],
        },
        fallbacks=[CommandHandler("cancel", settings_cancel)],
        # Allow the callback handler in callbacks.py to handle settings
        # inline keyboard presses by NOT blocking callback queries here.
        per_message=False,
    )
    app.add_handler(settings_conv)

    logger.info("User command handlers registered")
