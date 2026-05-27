import psycopg2
import psycopg2.pool
import psycopg2.extras
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import config

_pool: psycopg2.pool.ThreadedConnectionPool | None = None

DDL = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id           SERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    payment_id   TEXT   NOT NULL UNIQUE,
    status       TEXT   NOT NULL DEFAULT 'pending',
    channel_id   BIGINT NOT NULL,
    email        TEXT,
    created_at   TIMESTAMPTZ NOT NULL,
    expires_at   TIMESTAMPTZ,
    confirmed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_sub_user   ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_sub_status ON subscriptions(status);

CREATE TABLE IF NOT EXISTS users (
    user_id    BIGINT PRIMARY KEY,
    name       TEXT,
    username   TEXT,
    updated_at TIMESTAMPTZ NOT NULL
);
"""

# Добавляем колонку email если её нет (для уже существующих БД)
_MIGRATE = """
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS email TEXT;
CREATE INDEX IF NOT EXISTS idx_sub_email ON subscriptions(email);
"""


def init_db() -> None:
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(1, 5, config.DATABASE_URL)
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(_MIGRATE)
        conn.commit()


@contextmanager
def _get_conn():
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        _pool.putconn(conn)


def _cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def save_pending_payment(user_id: int, payment_id: str, channel_id: int, email: str) -> None:
    now = datetime.now(timezone.utc)
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO subscriptions (user_id, payment_id, status, channel_id, email, created_at)"
                " VALUES (%s, %s, 'pending', %s, %s, %s)",
                (user_id, payment_id, channel_id, email, now),
            )
        conn.commit()


def get_pending_payments() -> list[dict]:
    with _get_conn() as conn:
        with _cursor(conn) as cur:
            cur.execute("SELECT * FROM subscriptions WHERE status = 'pending'")
            return [dict(r) for r in cur.fetchall()]


def activate_subscription(payment_id: str) -> int:
    now = datetime.now(timezone.utc)
    expires = _expiry_from_now()
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE subscriptions SET status='active', confirmed_at=%s, expires_at=%s"
                " WHERE payment_id=%s RETURNING user_id",
                (now, expires, payment_id),
            )
            row = cur.fetchone()
        conn.commit()
    return row[0]


def cancel_payment(payment_id: str) -> None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE subscriptions SET status='canceled' WHERE payment_id=%s",
                (payment_id,),
            )
        conn.commit()


def get_active_subscription(user_id: int) -> dict | None:
    now = datetime.now(timezone.utc)
    with _get_conn() as conn:
        with _cursor(conn) as cur:
            cur.execute(
                "SELECT * FROM subscriptions WHERE user_id=%s AND status='active'"
                " AND expires_at > %s ORDER BY expires_at DESC LIMIT 1",
                (user_id, now),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def has_pending_payment(user_id: int) -> bool:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM subscriptions WHERE user_id=%s AND status='pending'",
                (user_id,),
            )
            return cur.fetchone() is not None


def get_expired_subscriptions() -> list[dict]:
    now = datetime.now(timezone.utc)
    with _get_conn() as conn:
        with _cursor(conn) as cur:
            cur.execute(
                "SELECT * FROM subscriptions WHERE status='active' AND expires_at <= %s",
                (now,),
            )
            return [dict(r) for r in cur.fetchall()]


def mark_expired(payment_id: str) -> None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE subscriptions SET status='expired' WHERE payment_id=%s",
                (payment_id,),
            )
        conn.commit()


def get_active_subscriptions_all() -> list[dict]:
    now = datetime.now(timezone.utc)
    with _get_conn() as conn:
        with _cursor(conn) as cur:
            cur.execute(
                "SELECT * FROM subscriptions WHERE status='active' AND expires_at > %s",
                (now,),
            )
            return [dict(r) for r in cur.fetchall()]


def upsert_user(user_id: int, name: str | None, username: str | None) -> None:
    now = datetime.now(timezone.utc)
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (user_id, name, username, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                    SET name = EXCLUDED.name,
                        username = EXCLUDED.username,
                        updated_at = EXCLUDED.updated_at
                """,
                (user_id, name, username, now),
            )
        conn.commit()


def get_user(user_id: int) -> dict | None:
    with _get_conn() as conn:
        with _cursor(conn) as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def find_by_name(query: str) -> list[dict]:
    pattern = f"%{query.lower()}%"
    with _get_conn() as conn:
        with _cursor(conn) as cur:
            cur.execute(
                """
                SELECT s.user_id, s.payment_id, s.status, s.email,
                       s.created_at, s.confirmed_at, s.expires_at,
                       u.name, u.username
                FROM subscriptions s
                LEFT JOIN users u ON u.user_id = s.user_id
                WHERE lower(u.name) LIKE %s OR lower(u.username) LIKE %s
                ORDER BY s.created_at DESC
                """,
                (pattern, pattern),
            )
            return [dict(r) for r in cur.fetchall()]


def find_by_email(email: str) -> list[dict]:
    with _get_conn() as conn:
        with _cursor(conn) as cur:
            cur.execute(
                """
                SELECT s.user_id, s.payment_id, s.status, s.email,
                       s.created_at, s.confirmed_at, s.expires_at,
                       u.name, u.username
                FROM subscriptions s
                LEFT JOIN users u ON u.user_id = s.user_id
                WHERE lower(s.email) = lower(%s)
                ORDER BY s.created_at DESC
                """,
                (email,),
            )
            return [dict(r) for r in cur.fetchall()]


def get_active_subscribers_with_info() -> list[dict]:
    now = datetime.now(timezone.utc)
    with _get_conn() as conn:
        with _cursor(conn) as cur:
            cur.execute(
                """
                SELECT s.user_id, s.expires_at, u.name, u.username
                FROM subscriptions s
                LEFT JOIN users u ON u.user_id = s.user_id
                WHERE s.status = 'active' AND s.expires_at > %s
                ORDER BY s.expires_at ASC
                """,
                (now,),
            )
            return [dict(r) for r in cur.fetchall()]


def get_subscription_counts(user_id: int) -> tuple[int, int]:
    """Возвращает (всего подписок в системе, подписок у этого пользователя)."""
    with _get_conn() as conn:
        with _cursor(conn) as cur:
            cur.execute(
                "SELECT COUNT(*) FROM subscriptions"
                " WHERE confirmed_at IS NOT NULL AND status IN ('active', 'expired')"
            )
            total = cur.fetchone()["count"]
            cur.execute(
                "SELECT COUNT(*) FROM subscriptions"
                " WHERE user_id = %s AND confirmed_at IS NOT NULL AND status IN ('active', 'expired')",
                (user_id,),
            )
            user_count = cur.fetchone()["count"]
    return total, user_count


def get_stats() -> dict:
    now = datetime.now(timezone.utc)
    msk = timezone(timedelta(hours=3))
    today_start = (
        datetime.now(msk)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )
    month_start = today_start.replace(day=1)
    next_24h = now + timedelta(hours=24)

    with _get_conn() as conn:
        with _cursor(conn) as cur:
            cur.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE status='active' AND expires_at > %s",
                (now,),
            )
            active = cur.fetchone()["count"]

            cur.execute(
                "SELECT COUNT(*) FROM subscriptions"
                " WHERE confirmed_at >= %s AND status IN ('active', 'expired')",
                (today_start,),
            )
            new_today = cur.fetchone()["count"]

            cur.execute(
                "SELECT COUNT(*) FROM subscriptions"
                " WHERE confirmed_at >= %s AND status IN ('active', 'expired')",
                (month_start,),
            )
            new_month = cur.fetchone()["count"]

            cur.execute(
                "SELECT COUNT(*) FROM subscriptions"
                " WHERE confirmed_at IS NOT NULL AND status IN ('active', 'expired')"
            )
            total = cur.fetchone()["count"]

            cur.execute(
                "SELECT COUNT(*) FROM subscriptions"
                " WHERE status='active' AND expires_at BETWEEN %s AND %s",
                (now, next_24h),
            )
            expiring_soon = cur.fetchone()["count"]

    return {
        "active": active,
        "new_today": new_today,
        "new_month": new_month,
        "total": total,
        "expiring_soon": expiring_soon,
    }


def _expiry_from_now() -> datetime:
    now = datetime.now(timezone.utc)
    if config.SUBSCRIPTION_MINUTES is not None:
        return now + timedelta(minutes=config.SUBSCRIPTION_MINUTES)
    return now + timedelta(days=30 * config.SUBSCRIPTION_MONTHS)
