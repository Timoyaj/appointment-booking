"""Signup, login and bearer tokens.

Passwords are hashed with scrypt (stdlib ``hashlib``) and stored as
``scrypt$n$r$p$salt$hash`` — never in plaintext. Tokens are opaque random
strings, do not expire, and a user may hold any number of them concurrently.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3

from . import repo
from .clock import now
from .errors import email_taken, unauthenticated, validation_failed
from .tztime import rfc3339

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
    token = new_token()
    repo.insert_token(conn, token=token, user_id=user_id, created_at=created_at)
    return {"user_id": user_id, "display_name": display_name, "token": token}


def login(conn: sqlite3.Connection, *, email: str, password: str) -> dict:
    user = repo.get_user_by_email(conn, email)
    # Same error for an unknown email and a wrong password: do not reveal which.
    if user is None or not verify_password(password, user["password"]):
        raise unauthenticated("Unknown email or wrong password")
    token = new_token()
    repo.insert_token(conn, token=token, user_id=user["id"], created_at=rfc3339(now()))
    return {
        "user_id": user["id"],
        "display_name": user["display_name"],
        "token": token,
    }


def authenticate(conn: sqlite3.Connection, authorization: str | None) -> dict:
    """Resolve ``Authorization: Bearer <token>`` to a user, or 401."""
    if not authorization:
        raise unauthenticated("Missing Authorization header")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise unauthenticated("Authorization header must be 'Bearer <token>'")
    session = repo.get_token(conn, parts[1].strip())
    if session is None:
        raise unauthenticated("Unknown bearer token")
    return {
        "user_id": session["user_id"],
        "display_name": session["display_name"],
        "token": session["token"],
    }
