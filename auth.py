"""User accounts, password hashing and login throttling, backed by Postgres.

Replaces the previous single shared APP_PASSWORD. Sessions stay stateless
(HMAC-signed cookie) so no session table is needed, but the token now carries
the user id and a token_version; bumping a user's token_version in the database
invalidates all of their existing cookies, which is how logout-everywhere and
account disabling take effect immediately.
"""

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ["DATABASE_URL"]
SECRET_KEY = os.environ["SECRET_KEY"].encode()
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

# Throttling: per-IP and per-account, counted over a rolling window.
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_FAILURES_PER_IP = 20
MAX_FAILURES_PER_ACCOUNT = 8

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=8, open=False, kwargs={"autocommit": True})
hasher = PasswordHasher()


@dataclass
class User:
    id: int
    email: str
    display_name: str
    is_admin: bool


def hash_password(password: str) -> str:
    return hasher.hash(password)


# ---------------------------------------------------------------- session token

def make_session_token(user_id: int, token_version: int) -> str:
    payload = f"{user_id}:{token_version}:{int(time.time()) + SESSION_MAX_AGE}"
    sig = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload.encode()).decode() + "." + sig


def parse_session_token(token: str):
    """Return (user_id, token_version) if the signature and expiry are good."""
    try:
        payload_b64, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(payload_b64.encode()).decode()
        expected = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        user_id, token_version, expiry = payload.split(":")
        if int(expiry) <= int(time.time()):
            return None
        return int(user_id), int(token_version)
    except Exception:
        return None


# ---------------------------------------------------------------------- queries

def get_user_for_token(token: str):
    """Resolve a cookie to a live user, re-checking the database each request.

    Costs one indexed lookup over a local socket. Doing it per request is what
    makes disabling an account or bumping token_version take effect immediately
    rather than whenever a 30-day cookie happens to expire.
    """
    parsed = parse_session_token(token or "")
    if not parsed:
        return None
    user_id, token_version = parsed
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, email, display_name, is_admin FROM users "
            "WHERE id = %s AND token_version = %s AND is_active",
            (user_id, token_version),
        ).fetchone()
    if not row:
        return None
    return User(id=row[0], email=row[1], display_name=row[2], is_admin=row[3])


def _recent_failures(conn, email: str, ip: str):
    row = conn.execute(
        "SELECT "
        " count(*) FILTER (WHERE ip = %s), "
        " count(*) FILTER (WHERE lower(email) = lower(%s)) "
        "FROM login_attempts "
        "WHERE NOT successful AND at > now() - make_interval(secs => %s)",
        (ip, email, LOGIN_WINDOW_SECONDS),
    ).fetchone()
    return row[0] or 0, row[1] or 0


def authenticate(email: str, password: str, ip: str):
    """Return (User, None) on success, or (None, reason) on failure."""
    email = (email or "").strip()
    with pool.connection() as conn:
        by_ip, by_account = _recent_failures(conn, email, ip)
        if by_ip >= MAX_FAILURES_PER_IP or by_account >= MAX_FAILURES_PER_ACCOUNT:
            return None, "too many attempts — wait a few minutes and try again"

        row = conn.execute(
            "SELECT id, email, display_name, password_hash, is_admin, is_active, token_version "
            "FROM users WHERE lower(email) = lower(%s)",
            (email,),
        ).fetchone()

        ok = False
        if row and row[5]:                       # exists and is_active
            try:
                hasher.verify(row[3], password)
                ok = True
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                ok = False
        else:
            # Spend comparable time on unknown accounts so the response time
            # doesn't reveal which emails exist.
            hasher.hash(password)

        conn.execute(
            "INSERT INTO login_attempts (email, ip, successful) VALUES (%s, %s, %s)",
            (email, ip, ok),
        )
        conn.execute(
            "DELETE FROM login_attempts WHERE at < now() - interval '7 days'"
        )

        if not ok:
            return None, "wrong email or password"

        if hasher.check_needs_rehash(row[3]):
            conn.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                         (hasher.hash(password), row[0]))
        conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (row[0],))

    return User(id=row[0], email=row[1], display_name=row[2], is_admin=row[4]), row[6]


# ------------------------------------------------------------------ management

def list_users():
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, email, display_name, is_admin, is_active, created_at, last_login_at "
            "FROM users ORDER BY lower(email)"
        ).fetchall()
    return [
        {"id": r[0], "email": r[1], "display_name": r[2], "is_admin": r[3],
         "is_active": r[4],
         "created_at": r[5].isoformat() if r[5] else None,
         "last_login_at": r[6].isoformat() if r[6] else None}
        for r in rows
    ]


def create_user(email: str, password: str, display_name: str = "", is_admin: bool = False):
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO users (email, display_name, password_hash, is_admin) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (email.strip(), display_name or email.split("@")[0], hash_password(password), is_admin),
        ).fetchone()
    return row[0]


def set_password(user_id: int, password: str):
    """Change a password and invalidate that user's existing sessions."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE users SET password_hash = %s, token_version = token_version + 1 WHERE id = %s",
            (hash_password(password), user_id),
        )


def set_active(user_id: int, active: bool):
    with pool.connection() as conn:
        conn.execute(
            "UPDATE users SET is_active = %s, token_version = token_version + 1 WHERE id = %s",
            (active, user_id),
        )


def revoke_sessions(user_id: int):
    with pool.connection() as conn:
        conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = %s", (user_id,))


def count_users() -> int:
    with pool.connection() as conn:
        return conn.execute("SELECT count(*) FROM users").fetchone()[0]
