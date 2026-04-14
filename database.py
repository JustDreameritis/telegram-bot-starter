"""
database.py — SQLite persistence layer for the Telegram Bot Starter Kit.

Provides async, ORM-style helpers for user management, subscription tracking,
alert history, and user preferences.  All operations use aiosqlite so they
never block the asyncio event loop.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import aiosqlite

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------

@dataclass
class User:
    """Represents a Telegram user stored in the database."""

    telegram_id: int
    username: str
    first_name: str
    is_subscribed: bool = False
    categories: str = ""          # Comma-separated list of subscribed categories
    timezone: str = "UTC"
    alert_frequency: int = 3600   # Seconds between alerts
    quiet_start: int = 23         # Local hour (0-23)
    quiet_end: int = 7            # Local hour (0-23)
    created_at: str = field(default_factory=lambda: _now())
    last_seen: str = field(default_factory=lambda: _now())


@dataclass
class AlertRecord:
    """Represents a sent alert stored in history."""

    id: Optional[int]
    user_id: int
    category: str
    message: str
    sent_at: str = field(default_factory=lambda: _now())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Database manager
# ---------------------------------------------------------------------------

class Database:
    """
    Thin async wrapper around an aiosqlite connection.

    Usage::

        db = Database("bot_data.db")
        await db.init()
        # ... use db ...
        await db.close()

    Or use as an async context manager::

        async with Database("bot_data.db") as db:
            user = await db.get_user(12345)
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Open the connection and create tables if they do not exist."""
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._create_tables()
        logger.info("Database initialised at %s", self._path)

    async def close(self) -> None:
        """Close the underlying connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("Database connection closed")

    async def __aenter__(self) -> "Database":
        await self.init()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.init() has not been called yet.")
        return self._conn

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def _create_tables(self) -> None:
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id    INTEGER PRIMARY KEY,
                username       TEXT    NOT NULL DEFAULT '',
                first_name     TEXT    NOT NULL DEFAULT '',
                is_subscribed  INTEGER NOT NULL DEFAULT 0,
                categories     TEXT    NOT NULL DEFAULT '',
                timezone       TEXT    NOT NULL DEFAULT 'UTC',
                alert_frequency INTEGER NOT NULL DEFAULT 3600,
                quiet_start    INTEGER NOT NULL DEFAULT 23,
                quiet_end      INTEGER NOT NULL DEFAULT 7,
                created_at     TEXT    NOT NULL,
                last_seen      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS alert_history (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER NOT NULL,
                category TEXT    NOT NULL DEFAULT '',
                message  TEXT    NOT NULL,
                sent_at  TEXT    NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(telegram_id)
            );

            CREATE INDEX IF NOT EXISTS idx_alert_history_user
                ON alert_history(user_id);
            CREATE INDEX IF NOT EXISTS idx_alert_history_sent
                ON alert_history(sent_at);
        """)
        await self.conn.commit()

    # ------------------------------------------------------------------
    # User helpers
    # ------------------------------------------------------------------

    async def upsert_user(self, user: User) -> None:
        """Insert or update a user record."""
        await self.conn.execute("""
            INSERT INTO users
                (telegram_id, username, first_name, is_subscribed, categories,
                 timezone, alert_frequency, quiet_start, quiet_end,
                 created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username        = excluded.username,
                first_name      = excluded.first_name,
                is_subscribed   = excluded.is_subscribed,
                categories      = excluded.categories,
                timezone        = excluded.timezone,
                alert_frequency = excluded.alert_frequency,
                quiet_start     = excluded.quiet_start,
                quiet_end       = excluded.quiet_end,
                last_seen       = excluded.last_seen
        """, (
            user.telegram_id, user.username, user.first_name,
            int(user.is_subscribed), user.categories,
            user.timezone, user.alert_frequency,
            user.quiet_start, user.quiet_end,
            user.created_at, user.last_seen,
        ))
        await self.conn.commit()

    async def get_user(self, telegram_id: int) -> Optional[User]:
        """Return a :class:`User` by Telegram ID, or None if not found."""
        async with self.conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_user(row)

    async def get_all_users(self) -> list[User]:
        """Return all users in the database."""
        async with self.conn.execute("SELECT * FROM users ORDER BY created_at") as cur:
            rows = await cur.fetchall()
        return [_row_to_user(r) for r in rows]

    async def get_subscribers(self) -> list[User]:
        """Return only users with is_subscribed = 1."""
        async with self.conn.execute(
            "SELECT * FROM users WHERE is_subscribed = 1 ORDER BY created_at"
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_user(r) for r in rows]

    async def set_subscribed(self, telegram_id: int, subscribed: bool) -> None:
        """Update subscription status for a single user."""
        await self.conn.execute(
            "UPDATE users SET is_subscribed = ? WHERE telegram_id = ?",
            (int(subscribed), telegram_id),
        )
        await self.conn.commit()

    async def update_categories(self, telegram_id: int, categories: list[str]) -> None:
        """Persist a user's subscribed alert categories."""
        await self.conn.execute(
            "UPDATE users SET categories = ? WHERE telegram_id = ?",
            (",".join(categories), telegram_id),
        )
        await self.conn.commit()

    async def update_preferences(
        self,
        telegram_id: int,
        *,
        timezone: Optional[str] = None,
        alert_frequency: Optional[int] = None,
        quiet_start: Optional[int] = None,
        quiet_end: Optional[int] = None,
    ) -> None:
        """Update one or more user preference fields."""
        updates: list[str] = []
        params: list[object] = []

        if timezone is not None:
            updates.append("timezone = ?")
            params.append(timezone)
        if alert_frequency is not None:
            updates.append("alert_frequency = ?")
            params.append(alert_frequency)
        if quiet_start is not None:
            updates.append("quiet_start = ?")
            params.append(quiet_start)
        if quiet_end is not None:
            updates.append("quiet_end = ?")
            params.append(quiet_end)

        if not updates:
            return

        params.append(telegram_id)
        sql = f"UPDATE users SET {', '.join(updates)} WHERE telegram_id = ?"
        await self.conn.execute(sql, params)
        await self.conn.commit()

    async def update_last_seen(self, telegram_id: int) -> None:
        """Stamp the last_seen field for a user."""
        await self.conn.execute(
            "UPDATE users SET last_seen = ? WHERE telegram_id = ?",
            (_now(), telegram_id),
        )
        await self.conn.commit()

    async def delete_user(self, telegram_id: int) -> None:
        """Remove a user and their alert history."""
        await self.conn.execute(
            "DELETE FROM alert_history WHERE user_id = ?", (telegram_id,)
        )
        await self.conn.execute(
            "DELETE FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        await self.conn.commit()

    async def user_count(self) -> int:
        """Return total number of users."""
        async with self.conn.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def subscriber_count(self) -> int:
        """Return number of subscribed users."""
        async with self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_subscribed = 1"
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Alert history
    # ------------------------------------------------------------------

    async def log_alert(self, user_id: int, category: str, message: str) -> int:
        """Append an alert to history; returns the new row id."""
        cur = await self.conn.execute(
            "INSERT INTO alert_history (user_id, category, message, sent_at) VALUES (?, ?, ?, ?)",
            (user_id, category, message, _now()),
        )
        await self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_recent_alerts(self, limit: int = 50) -> list[AlertRecord]:
        """Return the *limit* most recent alert records across all users."""
        async with self.conn.execute(
            "SELECT * FROM alert_history ORDER BY sent_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [AlertRecord(
            id=r["id"],
            user_id=r["user_id"],
            category=r["category"],
            message=r["message"],
            sent_at=r["sent_at"],
        ) for r in rows]

    async def alert_count(self) -> int:
        """Return total number of alerts sent."""
        async with self.conn.execute("SELECT COUNT(*) FROM alert_history") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def last_alert_time(self) -> Optional[str]:
        """Return ISO timestamp of the most recent alert, or None."""
        async with self.conn.execute(
            "SELECT sent_at FROM alert_history ORDER BY sent_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return row["sent_at"] if row else None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_to_user(row: aiosqlite.Row) -> User:
    return User(
        telegram_id=row["telegram_id"],
        username=row["username"],
        first_name=row["first_name"],
        is_subscribed=bool(row["is_subscribed"]),
        categories=row["categories"],
        timezone=row["timezone"],
        alert_frequency=row["alert_frequency"],
        quiet_start=row["quiet_start"],
        quiet_end=row["quiet_end"],
        created_at=row["created_at"],
        last_seen=row["last_seen"],
    )


# ---------------------------------------------------------------------------
# Async context manager helper for one-off scripts / tests
# ---------------------------------------------------------------------------

@asynccontextmanager
async def open_database(path: str) -> AsyncIterator[Database]:
    """Convenience context manager for scripts that need a quick DB handle."""
    db = Database(path)
    await db.init()
    try:
        yield db
    finally:
        await db.close()
