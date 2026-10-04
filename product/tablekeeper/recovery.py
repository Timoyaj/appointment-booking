"""Getting back in, and proving who you are.

Two things every product needs and this one could not do: let somebody who has
forgotten their password set a new one, and let somebody prove they own the
address they signed up with.

Both work the same way, and the way they work is the interesting part:

* the service **never says whether an address is registered.** Asking for a reset
  always answers 202, whatever was sent, so the endpoint cannot be used to
  discover which of a restaurant's guests have accounts;
* the link is a **random token that is stored hashed**, so a database that leaks
  does not hand anybody a working reset link — the same reason passwords are
  hashed. (While the message is still queued its text necessarily holds the
  token; once the transport has taken it, the outbox scrubs the body.)
  hashed;
* it **expires**, it **works once**, and using it **signs every device out**;
* the message goes into the same outbox as everything else, inside the same
  transaction, so a reset that was written is a reset that will be sent.

Verification is recorded but, by default, not required: a diner can book the
moment they sign up. `TABLEKEEPER_REQUIRE_VERIFIED_EMAIL=1` makes opening a
restaurant wait for a confirmed address, which is the setting a deployment wants
before it starts taking money from strangers.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import timedelta

from . import notifications, repo
from .clock import now
from .errors import unauthenticated, validation_failed
from .tztime import rfc3339

DEFAULT_RESET_TTL_MINUTES = 60
DEFAULT_VERIFY_TTL_MINUTES = 60 * 24 * 7


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def reset_ttl_minutes() -> int:
    return _env_int("TABLEKEEPER_RESET_TTL_MINUTES", DEFAULT_RESET_TTL_MINUTES)


def verify_ttl_minutes() -> int:
    return _env_int("TABLEKEEPER_VERIFY_TTL_MINUTES", DEFAULT_VERIFY_TTL_MINUTES)


def require_verified_email() -> bool:
    return os.environ.get("TABLEKEEPER_REQUIRE_VERIFIED_EMAIL", "0") not in (
        "0", "false", "no", "",
    )


def _hash(token: str) -> str:
    """What is stored instead of the token itself.

    A plain SHA-256, not a password hash: the token is 32 random bytes, so there
    is nothing to guess and nothing to slow down — the point is only that the
    stored value is not the value that works.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, _hash(token)


# --------------------------------------------------------------------------- #
# password reset
# --------------------------------------------------------------------------- #
def request_reset(conn: sqlite3.Connection, *, email: str) -> None:
    """Write a reset link and a message about it, if that address has an account.

    Returns nothing and says nothing about whether it did: the caller answers 202
    either way, so this endpoint cannot be asked "does this person have an
    account here?".
    """
    user = repo.get_user_by_email(conn, email)
    if user is None:
        return
    token, token_hash = _new_token()
    created_at = rfc3339(now())
    expires_at = rfc3339(now() + timedelta(minutes=reset_ttl_minutes()))
    # One live reset per account: asking again replaces the last link rather than
    # leaving a handful of working ones in an inbox.
    repo.clear_password_resets(conn, user["id"])
    repo.insert_password_reset(
        conn,
        token_hash=token_hash,
        user_id=user["id"],
        created_at=created_at,
        expires_at=expires_at,
    )
    notifications.enqueue(
        conn,
        restaurant_id="",
        user_id=user["id"],
        reference=None,
        kind=notifications.PASSWORD_RESET,
        restaurant_name="",
        record={
            "reference": "-",
            "party_size": 1,
            "starts_at_local": "",
            "table_id": "-",
            "table_ids": ["-"],
        },
        extra={"token": token, "ttl_minutes": reset_ttl_minutes()},
        created_at=created_at,
    )


def confirm_reset(conn: sqlite3.Connection, *, token: str, new_password: str) -> dict:
    """Set a new password, once, and sign every device out.

    Every session is revoked because a reset is what somebody does when they think
    their account is not theirs any more — leaving the other devices signed in
    would defeat the point of the exercise.
    """
    from . import auth

    if not isinstance(new_password, str) or len(new_password) < auth.MIN_PASSWORD_LENGTH:
        raise validation_failed(
            f"'new_password' must be at least {auth.MIN_PASSWORD_LENGTH} characters"
        )
    record = repo.get_password_reset(conn, _hash(token))
    if record is None:
        raise unauthenticated("That reset link is not valid")
    if record["used_at"] is not None:
        raise unauthenticated("That reset link has already been used")
    if record["expires_at"] <= rfc3339(now()):
        raise unauthenticated("That reset link has expired")

    repo.update_user_password(
        conn, record["user_id"], password=auth.hash_password(new_password)
    )
    repo.mark_password_reset_used(conn, record["token_hash"], used_at=rfc3339(now()))
    revoked = auth.logout_everywhere(conn, record["user_id"])
    return {"user_id": record["user_id"], "password_changed": True, "sessions_revoked": revoked}


# --------------------------------------------------------------------------- #
# email verification
# --------------------------------------------------------------------------- #
def send_verification(conn: sqlite3.Connection, *, user_id: str) -> None:
    """Write a confirmation link for an address, unless it is already confirmed."""
    user = repo.get_user(conn, user_id)
    if user is None or is_verified(conn, user_id):
        return
    token, token_hash = _new_token()
    created_at = rfc3339(now())
    repo.clear_email_verifications(conn, user_id)
    repo.insert_email_verification(
        conn,
        token_hash=token_hash,
        user_id=user_id,
        created_at=created_at,
        expires_at=rfc3339(now() + timedelta(minutes=verify_ttl_minutes())),
    )
    notifications.enqueue(
        conn,
        restaurant_id="",
        user_id=user_id,
        reference=None,
        kind=notifications.EMAIL_VERIFICATION,
        restaurant_name="",
        record={
            "reference": "-",
            "party_size": 1,
            "starts_at_local": "",
            "table_id": "-",
            "table_ids": ["-"],
        },
        extra={"token": token, "ttl_minutes": verify_ttl_minutes()},
        created_at=created_at,
    )


def confirm_verification(conn: sqlite3.Connection, *, token: str) -> dict:
    record = repo.get_email_verification(conn, _hash(token))
    if record is None:
        raise unauthenticated("That confirmation link is not valid")
    if record["used_at"] is not None:
        raise unauthenticated("That confirmation link has already been used")
    if record["expires_at"] <= rfc3339(now()):
        raise unauthenticated("That confirmation link has expired")
    repo.mark_email_verification_used(
        conn, record["token_hash"], used_at=rfc3339(now())
    )
    repo.mark_email_verified(conn, record["user_id"], verified_at=rfc3339(now()))
    return {"user_id": record["user_id"], "email_verified": True}


def is_verified(conn: sqlite3.Connection, user_id: str) -> bool:
    return repo.email_verified(conn, user_id) is not None
