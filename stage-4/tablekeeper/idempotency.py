"""Idempotency.

Two write paths require a key: ``POST /reservations`` and
``POST /reservation-moves``. The rules implemented here:

* the key is scoped to the authenticated user — two users may use the same key
  string with no interaction;
* a replay is the same user, method, path and body. The same key on a different
  path is a different request and succeeds normally;
* first use executes and returns **201**; a replay returns **200** with a body
  identical to the original as a JSON value, even after the resource has changed
  or been cancelled, and makes no further state changes;
* the same key with a different body is ``409 idempotency_key_reuse``, and this
  is decided *before* field validation — a used key with an otherwise invalid
  body still returns 409;
* a key whose original request failed with 4xx was never recorded, so reusing it
  is a first use;
* for concurrent identical requests with an unused key, exactly one returns 201
  and the others return 200 with the same body, because the lookup and the write
  share one ``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import repo
from .clock import now
from .errors import idempotency_key_reuse
from .tztime import rfc3339


def lookup(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    key: str,
    method: str,
    path: str,
    body: Any,
) -> dict | None:
    """Return the stored receipt for a replay, or ``None`` for a first use.

    Raises ``409 idempotency_key_reuse`` when the key was already used by this
    caller on this method+path with a different body.
    """
    record = repo.get_idempotency(
        conn, user_id=user_id, key=key, method=method.upper(), path=path
    )
    if record is None:
        return None
    if record["body_fingerprint"] != repo.fingerprint(body):
        raise idempotency_key_reuse()
    return record


def record(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    key: str,
    method: str,
    path: str,
    body: Any,
    status_code: int,
    response_body: Any,
) -> None:
    """Store a successful outcome so later retries replay it."""
    repo.insert_idempotency(
        conn,
        user_id=user_id,
        key=key,
        method=method.upper(),
        path=path,
        body_fingerprint=repo.fingerprint(body),
        request_body=body,
        status_code=status_code,
        response_body=response_body,
        created_at=rfc3339(now()),
    )
