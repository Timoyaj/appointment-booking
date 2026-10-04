"""Signup, login and bearer tokens.

Passwords are hashed with scrypt (stdlib ``hashlib``) and stored as
``scrypt$n$r$p$salt$hash`` — never in plaintext. Tokens are opaque random
strings and a user may hold any number of them concurrently.

A token issued here also gets a **session row** saying when it expires, so a
token that leaks stops being useful on its own and a diner can sign out of the
device they left behind. Tokens that predate sessions — anything a snapshot
carried in — have no row and never expire, so importing an older export does not
sign every diner out. Login is throttled per address: a run of failures locks
that address out for a cooling-off window, which is the cheapest defence there
is against someone guessing a password at machine speed.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from datetime import timedelta

from . import repo
from .clock import now
from .errors import (
    email_taken,
    too_many_attempts,
    unauthenticated,
    validation_failed,
)
from .tztime import rfc3339

# How long a new token works for, and how many failures in a row lock an address
# out. Both are deployment choices rather than parts of the service's contract,
# so they are read from the environment and have defaults a restaurant can live
# with: a month-long session is what a diner expects from a booking site, and
# eight failures in fifteen minutes is not a person typing.
DEFAULT_TOKEN_TTL_MINUTES = 43_200  # 30 days
DEFAULT_LOGIN_WINDOW_MINUTES = 15
DEFAULT_LOGIN_MAX_FAILURES = 8


def token_ttl_minutes() -> int:
    return _env_int("TABLEKEEPER_TOKEN_TTL_MINUTES", DEFAULT_TOKEN_TTL_MINUTES)


def login_window_minutes() -> int:
    return _env_int("TABLEKEEPER_LOGIN_WINDOW_MINUTES", DEFAULT_LOGIN_WINDOW_MINUTES)


def login_max_failures() -> int:
    return _env_int("TABLEKEEPER_LOGIN_MAX_FAILURES", DEFAULT_LOGIN_MAX_FAILURES)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default

# local@domain: one '@', non-empty parts, no whitespace. Deliberately permissive
# beyond that — the spec only requires this shape.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")

MIN_PASSWORD_LENGTH = 8

_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(digest_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest_hex)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_user_id() -> str:
    return f"u_{secrets.token_hex(8)}"


def validate_credentials(*, email: object, password: object, display_name: object) -> None:
    """Shared signup/login field rules (types are checked by the caller)."""
    if not isinstance(email, str) or not EMAIL_RE.match(email):
        raise validation_failed("email must be of the form local@domain")
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise validation_failed(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if display_name is not None and not isinstance(display_name, str):
        raise validation_failed("display_name must be a string")


def issue_token(conn: sqlite3.Connection, *, user_id: str, created_at: str) -> str:
    """Mint a token and the session that says when it stops working."""
    token = new_token()
    repo.insert_token(conn, token=token, user_id=user_id, created_at=created_at)
    expires_at = rfc3339(now() + timedelta(minutes=token_ttl_minutes()))
    repo.insert_session(conn, token=token, expires_at=expires_at)
    return token


def signup(
    conn: sqlite3.Connection, *, email: str, password: str, display_name: str
) -> dict:
    if repo.get_user_by_email(conn, email) is not None:
        raise email_taken()
    user_id = new_user_id()
    while repo.get_user(conn, user_id) is not None:  # pragma: no cover - 2^64 space
        user_id = new_user_id()
    created_at = rfc3339(now())
    repo.insert_user(
        conn,
        user_id=user_id,
        email=email,
        password=hash_password(password),
        display_name=display_name,
        created_at=created_at,
    )
    token = issue_token(conn, user_id=user_id, created_at=created_at)
    return {"user_id": user_id, "display_name": display_name, "token": token}


def login(conn: sqlite3.Connection, *, email: str, password: str) -> dict:
    """Sign in, or fail — slowly and only so often.

    A locked-out address is refused *before* the password is checked, so a
    locked-out attacker learns nothing about whether the password was right, and
    the refusal is 429 rather than 401 because the credentials may well be
    correct: it is the caller's recent behaviour that is the problem.
    """
    _check_throttle(conn, email)
    user = repo.get_user_by_email(conn, email)
    # Same error for an unknown email and a wrong password: do not reveal which.
    if user is None or not verify_password(password, user["password"]):
        raise unauthenticated("Unknown email or wrong password")
    repo.clear_login_attempts(conn, _throttle_key(email))
    return {
        "user_id": user["id"],
        "display_name": user["display_name"],
        "token": issue_token(conn, user_id=user["id"], created_at=rfc3339(now())),
    }


def _throttle_key(email: str) -> str:
    """One counter per address, case-insensitively — the same account however it
    is typed."""
    return email.strip().lower()


def _check_throttle(conn: sqlite3.Connection, email: str) -> None:
    window = rfc3339(now() - timedelta(minutes=login_window_minutes()))
    failures = repo.failed_login_count(conn, _throttle_key(email), since=window)
    if failures >= login_max_failures():
        raise too_many_attempts(
            f"Too many failed sign-in attempts for this address; try again in "
            f"{login_window_minutes()} minutes"
        )


def record_failure(conn: sqlite3.Connection, email: str) -> None:
    """Count one failed sign-in against an address.

    Called from *outside* the transaction that refused the attempt: that
    transaction rolls back, so a counter written inside it would be undone by the
    very failure it was meant to remember and the address would never lock out.
    """
    repo.insert_login_attempt(
        conn, email=_throttle_key(email), at=rfc3339(now()), succeeded=False
    )


def logout(conn: sqlite3.Connection, token: str) -> bool:
    """Revoke one token. False when it was not a live session — signing out twice
    is not an error, and a token that never expires is revoked by deleting it."""
    if repo.revoke_session(conn, token, revoked_at=rfc3339(now())):
        return True
    return False


def logout_everywhere(conn: sqlite3.Connection, user_id: str) -> int:
    """Sign out of every device: live sessions are revoked and every token the
    user holds is deleted, including ones issued before sessions existed."""
    revoked = repo.revoke_sessions_for_user(conn, user_id, revoked_at=rfc3339(now()))
    deleted = repo.delete_tokens_for_user(conn, user_id)
    return max(revoked, deleted)


def authenticate(conn: sqlite3.Connection, authorization: str | None) -> dict:
    """Resolve ``Authorization: Bearer <token>`` to a user, or 401."""
    if not authorization:
        raise unauthenticated("Missing Authorization header")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise unauthenticated("Authorization header must be 'Bearer <token>'")
    presented = parts[1].strip()
    session = repo.get_token(conn, presented)
    if session is None:
        raise unauthenticated("Unknown bearer token")
    _check_session(conn, presented)
    return {
        "user_id": session["user_id"],
        "display_name": session["display_name"],
        "token": session["token"],
    }


def _check_session(conn: sqlite3.Connection, token: str) -> None:
    """Refuse a token that has been signed out or has run out.

    A token with no session row is one that predates sessions and is treated as
    it always was: valid.
    """
    record = repo.get_session(conn, token)
    if record is None:
        return
    if record["revoked_at"] is not None:
        raise unauthenticated("This token has been signed out")
    if record["expires_at"] <= rfc3339(now()):
        raise unauthenticated("This token has expired")
