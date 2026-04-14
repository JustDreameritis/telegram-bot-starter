"""
config.py — Configuration management for the Telegram Bot Starter Kit.

Loads settings from environment variables / .env file, validates required
values on startup, and exposes a typed Config object used throughout the app.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import FrozenSet

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load .env from project root (safe no-op if file is absent)
# ---------------------------------------------------------------------------
load_dotenv(dotenv_path=Path(__file__).parent / ".env")


def _require(key: str) -> str:
    """Return env var *key* or raise RuntimeError if it is unset/empty."""
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set. "
            "Copy .env.example to .env and fill in the values."
        )
    return value


def _int_env(key: str, default: int) -> int:
    """Return env var *key* as int, falling back to *default*."""
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Could not parse %s=%r as int; using default %d", key, raw, default)
        return default


def _admin_ids(raw: str) -> FrozenSet[int]:
    """Parse a comma-separated string of Telegram user IDs."""
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError:
                logger.warning("Ignoring non-integer admin ID: %r", part)
    return frozenset(ids)


# ---------------------------------------------------------------------------
# Typed configuration dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    """Immutable runtime configuration loaded from environment variables."""

    # Core bot settings
    bot_token: str
    admin_ids: FrozenSet[int]

    # Webhook / monitoring server
    webhook_secret: str
    webhook_port: int

    # Scheduler
    alert_check_interval: int  # seconds

    # Persistence
    database_path: str

    # Logging
    log_level: str

    # Quiet hours (local to user timezone, applied bot-side)
    quiet_hours_start: int  # 0-23
    quiet_hours_end: int    # 0-23

    # Default timezone label (informational; use pytz/zoneinfo for real tz logic)
    timezone: str

    # Derived helpers
    admin_ids_list: list[int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Bypass frozen=True for the derived field
        object.__setattr__(self, "admin_ids_list", sorted(self.admin_ids))

    def is_admin(self, user_id: int) -> bool:
        """Return True when *user_id* belongs to the configured admin list."""
        return user_id in self.admin_ids


def load_config() -> Config:
    """
    Build and validate a :class:`Config` from the current environment.

    Raises :class:`RuntimeError` for any missing required variable.
    """
    token = _require("TELEGRAM_BOT_TOKEN")
    raw_admin_ids = os.getenv("ADMIN_IDS", "").strip()
    if not raw_admin_ids:
        raise RuntimeError(
            "ADMIN_IDS is not set. Provide at least one Telegram user ID "
            "(find yours by messaging @userinfobot)."
        )
    admin_ids = _admin_ids(raw_admin_ids)
    if not admin_ids:
        raise RuntimeError("ADMIN_IDS contains no valid integer IDs.")

    cfg = Config(
        bot_token=token,
        admin_ids=admin_ids,
        webhook_secret=os.getenv("WEBHOOK_SECRET", "changeme").strip(),
        webhook_port=_int_env("WEBHOOK_PORT", 8443),
        alert_check_interval=_int_env("ALERT_CHECK_INTERVAL", 300),
        database_path=os.getenv("DATABASE_PATH", "bot_data.db").strip() or "bot_data.db",
        log_level=os.getenv("LOG_LEVEL", "INFO").upper().strip() or "INFO",
        quiet_hours_start=_int_env("QUIET_HOURS_START", 23),
        quiet_hours_end=_int_env("QUIET_HOURS_END", 7),
        timezone=os.getenv("TIMEZONE", "UTC").strip() or "UTC",
    )

    logger.info(
        "Config loaded: admins=%s, webhook_port=%d, db=%s, log_level=%s",
        list(cfg.admin_ids),
        cfg.webhook_port,
        cfg.database_path,
        cfg.log_level,
    )
    return cfg


# Module-level singleton — import and call load_config() in bot.py instead of
# re-parsing env everywhere.  Kept here for convenience during testing.
_config: Config | None = None


def get_config() -> Config:
    """Return the cached config, loading it on first call."""
    global _config
    if _config is None:
        _config = load_config()
    return _config
