"""
scheduler.py — Scheduled task runner for the Telegram Bot Starter Kit.

Uses APScheduler (AsyncIOScheduler) wired to the python-telegram-bot
Application so all jobs run inside the same event loop and have access
to the bot context (bot_data, bot.send_message, etc.).

Scheduled jobs:
    health_check          — Every 5 minutes: verify the bot is alive
    hourly_status_report  — Every hour: send a summary to all admins
    daily_digest          — Every day at 08:00 UTC: send a digest to subscribers
    alert_check           — Configurable interval (default 5 min): simulate alert delivery
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import Application

from config import Config
from database import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

async def _health_check(bot: Bot, admin_ids: list[int]) -> None:
    """
    Ping the Telegram API to confirm connectivity.

    Logs success; on failure, notifies admins so they know the bot
    is struggling to reach Telegram's servers.
    """
    try:
        me = await bot.get_me()
        logger.info("[health_check] Bot is alive: @%s", me.username)
    except Exception as exc:
        logger.error("[health_check] Bot health check FAILED: %s", exc)
        for admin_id in admin_ids:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"Health check FAILED at {datetime.now(timezone.utc).isoformat()}\n\nError: {exc}",
                )
            except Exception:
                pass  # If we can't reach Telegram, no point retrying here


async def _hourly_status_report(bot: Bot, db: Database, admin_ids: list[int]) -> None:
    """
    Send an hourly summary of bot activity to all admins.
    """
    try:
        total_users = await db.user_count()
        subscriber_count = await db.subscriber_count()
        alert_count = await db.alert_count()
        last_alert = await db.last_alert_time() or "never"

        text = (
            f"*Hourly Status Report*\n"
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"Users: `{total_users}` | Subscribers: `{subscriber_count}`\n"
            f"Total alerts: `{alert_count}` | Last: `{last_alert}`"
        )
        for admin_id in admin_ids:
            await bot.send_message(chat_id=admin_id, text=text, parse_mode=ParseMode.MARKDOWN)
        logger.info("[hourly_status_report] Sent to %d admin(s)", len(admin_ids))
    except Exception as exc:
        logger.error("[hourly_status_report] Failed: %s", exc)


async def _daily_digest(bot: Bot, db: Database) -> None:
    """
    Send a daily digest to all subscribed users.

    In a real bot, this would pull your actual data source.  Here we
    send a placeholder that developers can replace with real content.
    """
    try:
        subscribers = await db.get_subscribers()
        if not subscribers:
            logger.info("[daily_digest] No subscribers — skipping")
            return

        now_str = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
        message = (
            f"*Daily Digest — {now_str}*\n\n"
            "This is your automated daily summary.\n\n"
            "• Replace this content with your real data source\n"
            "• Customise the format in `scheduler.py → _daily_digest`\n"
            "• Supports full Markdown formatting\n\n"
            "_Manage alerts: /subscribe | /settings | /unsubscribe_"
        )

        sent = 0
        for user in subscribers:
            # Respect quiet hours — skip users who are in their quiet window
            if _in_quiet_hours(user.quiet_start, user.quiet_end):
                logger.debug("[daily_digest] Skipping user %d (quiet hours)", user.telegram_id)
                continue
            try:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN,
                )
                await db.log_alert(user.telegram_id, "daily_digest", "Daily digest sent")
                sent += 1
            except Exception as exc:
                logger.warning("[daily_digest] Failed for user %d: %s", user.telegram_id, exc)

        logger.info("[daily_digest] Sent to %d/%d subscribers", sent, len(subscribers))
    except Exception as exc:
        logger.error("[daily_digest] Job failed: %s", exc)


async def _alert_check(bot: Bot, db: Database, cfg: Config) -> None:
    """
    Periodic alert delivery worker.

    This is the hook for your custom alert logic.  Connect it to your
    data source (API, database, webhook queue) and call bot.send_message
    for each pending alert.  The example below demonstrates the pattern.
    """
    try:
        # ----------------------------------------------------------------
        # TODO: Replace this with your real alert source.
        # Example:
        #   pending = await fetch_pending_alerts()
        #   for alert in pending:
        #       subscribers = await db.get_subscribers()
        #       for user in subscribers:
        #           if alert.category in user.categories.split(","):
        #               await bot.send_message(...)
        # ----------------------------------------------------------------
        logger.debug("[alert_check] Tick — no pending alerts (replace with real source)")
    except Exception as exc:
        logger.error("[alert_check] Failed: %s", exc)


# ---------------------------------------------------------------------------
# Quiet-hours helper
# ---------------------------------------------------------------------------

def _in_quiet_hours(start: int, end: int) -> bool:
    """
    Return True if the current UTC hour falls within the quiet window.

    Handles overnight ranges (e.g. start=23, end=7).
    """
    current_hour = datetime.now(timezone.utc).hour
    if start <= end:
        return start <= current_hour < end
    # Overnight: e.g. 23 → 7
    return current_hour >= start or current_hour < end


# ---------------------------------------------------------------------------
# Scheduler factory
# ---------------------------------------------------------------------------

def build_scheduler(app: Application) -> AsyncIOScheduler:  # type: ignore[type-arg]
    """
    Create and configure an AsyncIOScheduler tied to the Application.

    All job functions receive the bot and db from bot_data so they can
    operate independently without needing an Update or Context.

    Returns the configured (but not yet started) scheduler — bot.py
    starts it after the Application is running.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")

    bot: Bot = app.bot
    db: Database = app.bot_data["db"]
    cfg: Config = app.bot_data["config"]
    admin_ids: list[int] = cfg.admin_ids_list

    # Health check every 5 minutes
    scheduler.add_job(
        _health_check,
        trigger=IntervalTrigger(minutes=5),
        args=[bot, admin_ids],
        id="health_check",
        replace_existing=True,
        misfire_grace_time=60,
    )

    # Hourly status report to admins
    scheduler.add_job(
        _hourly_status_report,
        trigger=IntervalTrigger(hours=1),
        args=[bot, db, admin_ids],
        id="hourly_status",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # Daily digest at 08:00 UTC
    scheduler.add_job(
        _daily_digest,
        trigger=CronTrigger(hour=8, minute=0, timezone="UTC"),
        args=[bot, db],
        id="daily_digest",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Alert check on configurable interval (default 5 min / 300 s)
    scheduler.add_job(
        _alert_check,
        trigger=IntervalTrigger(seconds=cfg.alert_check_interval),
        args=[bot, db, cfg],
        id="alert_check",
        replace_existing=True,
        misfire_grace_time=cfg.alert_check_interval,
    )

    logger.info(
        "Scheduler configured: health=5m, hourly, daily@08:00, alert_check=%ds",
        cfg.alert_check_interval,
    )
    return scheduler
