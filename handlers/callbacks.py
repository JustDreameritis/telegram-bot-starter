"""
handlers/callbacks.py — Inline keyboard callback query handlers.

Handles all CallbackQuery updates that originate from inline keyboards
built in commands.py, admin.py, and anywhere else in the bot.

Registered callback patterns:
    subscribe_start     — Re-triggers the subscribe flow from the /start keyboard
    help_show           — Show help text from the /start keyboard
    status_show         — Show status from the /start keyboard
    settings_show       — Show settings panel from the /start keyboard
    cat_toggle:<name>   — Toggle an alert category on/off
    cat_save            — Persist category selections and close the panel
    unsub_confirm       — Confirm unsubscribe
    unsub_cancel        — Cancel unsubscribe
    set_*               — Settings field selection (handled by ConversationHandler,
                          but also routed here for non-conversation contexts)
    settings_close      — Dismiss the settings message
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from config import Config
from database import Database
from handlers.commands import (
    ALERT_CATEGORIES,
    HELP_TEXT,
    _cfg,  # type: ignore[attr-defined]
    _db,   # type: ignore[attr-defined]
    _ensure_user,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routing dispatcher
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Central dispatcher for all callback queries.

    Routes to the appropriate handler based on the callback_data prefix.
    Always calls `query.answer()` to dismiss the loading spinner.
    """
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    data: str = query.data or ""
    logger.debug("Callback received: %r from user %s", data, update.effective_user)

    if data == "subscribe_start":
        await _callback_subscribe(update, context)
    elif data == "help_show":
        await _callback_help(update, context)
    elif data == "status_show":
        await _callback_status(update, context)
    elif data == "settings_show":
        await _callback_settings_show(update, context)
    elif data.startswith("cat_toggle:"):
        await _callback_cat_toggle(update, context, data.split(":", 1)[1])
    elif data == "cat_save":
        await _callback_cat_save(update, context)
    elif data == "unsub_confirm":
        await _callback_unsub_confirm(update, context)
    elif data == "unsub_cancel":
        await _callback_unsub_cancel(update, context)
    elif data == "settings_close":
        await query.edit_message_text("Settings closed. Use /settings to reopen.")
    elif data.startswith("set_"):
        # Handled inside the settings ConversationHandler; nothing extra needed.
        pass
    else:
        logger.warning("Unhandled callback data: %r", data)
        await query.edit_message_text("Unknown action. Please use /start to begin again.")


# ---------------------------------------------------------------------------
# Individual callback implementations
# ---------------------------------------------------------------------------

async def _callback_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the category-selection panel (same as /subscribe)."""
    await _ensure_user(update, context)
    query = update.callback_query
    tg = update.effective_user
    if tg is None or query is None:
        return

    db = _db(context)
    user = await db.get_user(tg.id)
    current = set(user.categories.split(",")) if user and user.categories else set()

    buttons = []
    for cat in ALERT_CATEGORIES:
        label = f"✅ {cat}" if cat in current else cat
        buttons.append([InlineKeyboardButton(label, callback_data=f"cat_toggle:{cat}")])
    buttons.append([InlineKeyboardButton("Save & Close", callback_data="cat_save")])

    await query.edit_message_text(
        "*Select alert categories:*\n\nTap a category to toggle, then press *Save & Close*.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _callback_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help text inline."""
    query = update.callback_query
    if query is None:
        return
    await query.edit_message_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def _callback_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show status inline (replicates /status logic)."""
    from datetime import datetime, timezone
    query = update.callback_query
    if query is None:
        return

    db = _db(context)
    start_time = context.application.bot_data["start_time"]

    uptime = datetime.now(timezone.utc) - start_time
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    subscriber_count = await db.subscriber_count()
    total_users = await db.user_count()
    last_alert = await db.last_alert_time() or "No alerts sent yet"
    alert_count = await db.alert_count()

    text = (
        f"*Bot Status*\n\n"
        f"Uptime: `{uptime_str}`\n"
        f"Total users: `{total_users}`\n"
        f"Subscribers: `{subscriber_count}`\n"
        f"Total alerts sent: `{alert_count}`\n"
        f"Last alert: `{last_alert}`"
    )
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)


async def _callback_settings_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show settings panel inline."""
    query = update.callback_query
    tg = update.effective_user
    if tg is None or query is None:
        return

    await _ensure_user(update, context)
    db = _db(context)
    user = await db.get_user(tg.id)
    if user is None:
        await query.edit_message_text("User record not found. Try /start first.")
        return

    freq_label = (
        f"{user.alert_frequency // 3600}h"
        if user.alert_frequency >= 3600
        else f"{user.alert_frequency // 60}m"
    )
    text = (
        f"*Your Settings*\n\n"
        f"Timezone: `{user.timezone}`\n"
        f"Alert frequency: `{freq_label}`\n"
        f"Quiet hours: `{user.quiet_start:02d}:00 – {user.quiet_end:02d}:00`\n\n"
        "To update, use /settings"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Close", callback_data="settings_close")]
    ])
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def _callback_cat_toggle(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    category: str,
) -> None:
    """Toggle a single category on/off and refresh the inline keyboard."""
    query = update.callback_query
    tg = update.effective_user
    if tg is None or query is None:
        return

    db = _db(context)
    user = await db.get_user(tg.id)
    current = set(user.categories.split(",")) if user and user.categories else set()

    if category in current:
        current.discard(category)
    else:
        current.add(category)

    # Persist immediately so partial state is not lost
    await db.update_categories(tg.id, [c for c in current if c])

    # Rebuild keyboard with updated tick marks
    buttons = []
    for cat in ALERT_CATEGORIES:
        label = f"✅ {cat}" if cat in current else cat
        buttons.append([InlineKeyboardButton(label, callback_data=f"cat_toggle:{cat}")])
    buttons.append([InlineKeyboardButton("Save & Close", callback_data="cat_save")])

    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
    logger.debug("User %d toggled category %r — current: %s", tg.id, category, current)


async def _callback_cat_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Finalise category selection and close the panel."""
    query = update.callback_query
    tg = update.effective_user
    if tg is None or query is None:
        return

    db = _db(context)

    # Mark user as subscribed if they have at least one category
    user = await db.get_user(tg.id)
    has_cats = bool(user and user.categories.strip())

    if has_cats:
        await db.set_subscribed(tg.id, True)
        cats_display = user.categories.replace(",", ", ")  # type: ignore[union-attr]
        await query.edit_message_text(
            f"Subscribed to: *{cats_display}*\n\nYou will receive alerts for these categories.\n"
            "Use /unsubscribe to stop at any time.",
            parse_mode=ParseMode.MARKDOWN,
        )
        logger.info("User %d subscribed to: %s", tg.id, user.categories)
    else:
        await db.set_subscribed(tg.id, False)
        await query.edit_message_text(
            "No categories selected — you are unsubscribed.\n"
            "Use /subscribe to choose categories."
        )
        logger.info("User %d cleared all categories", tg.id)


async def _callback_unsub_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm unsubscribe: clear categories and mark user as unsubscribed."""
    query = update.callback_query
    tg = update.effective_user
    if tg is None or query is None:
        return

    db = _db(context)
    await db.set_subscribed(tg.id, False)
    await db.update_categories(tg.id, [])

    await query.edit_message_text(
        "You have been unsubscribed from all alerts.\n"
        "Use /subscribe at any time to resubscribe."
    )
    logger.info("User %d unsubscribed", tg.id)


async def _callback_unsub_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel unsubscribe — keep the user subscribed."""
    query = update.callback_query
    if query is None:
        return
    await query.edit_message_text("Unsubscribe cancelled. You are still subscribed.")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_callback_handlers(app: Application) -> None:  # type: ignore[type-arg]
    """
    Attach the central callback dispatcher to the Application.

    A single CallbackQueryHandler routes all inline-keyboard taps.
    Call this once from bot.py during setup.
    """
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("Callback query handlers registered")
