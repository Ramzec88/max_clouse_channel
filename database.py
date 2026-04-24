import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import config

_lock = threading.Lock()

DDL = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    payment_id   TEXT    NOT NULL UNIQUE,
    status       TEXT    NOT NULL DEFAULT 'pending',
    channel_id   INTEGER NOT NULL,
    created_at   TEXT    NOT NULL,
    expires_at   TEXT,
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sub_user   ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_sub_status ON subscriptions(status);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.executescript(DDL)


def save_pending_payment(user_id: int, payment_id: str, channel_id: int) -> None:
    now = _now()
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO subscriptions (user_id, payment_id, status, channel_id, created_at)"
            " VALUES (?, ?, 'pending', ?, ?)",
            (user_id, payment_id, channel_id, now),
        )


def get_pending_payments() -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE status = 'pending'"
        ).fetchall()
    return [dict(r) for r in rows]


def activate_subscription(payment_id: str) -> int:
    now = _now()
    expires = _expiry_from_now()
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE subscriptions SET status='active', confirmed_at=?, expires_at=?"
            " WHERE payment_id=?",
            (now, expires, payment_id),
        )
        row = conn.execute(
            "SELECT user_id FROM subscriptions WHERE payment_id=?", (payment_id,)
        ).fetchone()
    return row["user_id"]


def cancel_payment(payment_id: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE subscriptions SET status='canceled' WHERE payment_id=?",
            (payment_id,),
        )


def get_active_subscription(user_id: int) -> dict | None:
    now = _now()
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND status='active' AND expires_at > ?"
            " ORDER BY expires_at DESC LIMIT 1",
            (user_id, now),
        ).fetchone()
    return dict(row) if row else None


def has_pending_payment(user_id: int) -> bool:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM subscriptions WHERE user_id=? AND status='pending'",
            (user_id,),
        ).fetchone()
    return row is not None


def get_expired_subscriptions() -> list[dict]:
    now = _now()
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE status='active' AND expires_at <= ?",
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_expired(payment_id: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE subscriptions SET status='expired' WHERE payment_id=?",
            (payment_id,),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expiry_from_now() -> str:
    now = datetime.now(timezone.utc)
    if config.SUBSCRIPTION_MINUTES is not None:
        return (now + timedelta(minutes=config.SUBSCRIPTION_MINUTES)).isoformat()
    # Приблизительный месяц: 30 дней * SUBSCRIPTION_MONTHS
    return (now + timedelta(days=30 * config.SUBSCRIPTION_MONTHS)).isoformat()
