"""Data access.

Every function takes an explicit connection so the caller owns the transaction
boundary — that is what lets a single booking and a whole batch of moves share
one atomic unit of work. Rows are converted to dicts here; nothing above this
module writes SQL.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

# Every table whose contents make up the service state, in dependency order.
# Export dumps them; import replaces them in reverse then re-inserts in order.
STATE_TABLES: tuple[str, ...] = (
    "users",
    "tokens",
    "restaurants",
    "opening_hours",
    "dining_tables",
    "reservations",
    "idempotency",
)


def rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def row(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> dict | None:
    found = conn.execute(sql, params).fetchone()
    return dict(found) if found is not None else None


# --------------------------------------------------------------------------- #
# users and tokens
# --------------------------------------------------------------------------- #
def insert_user(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    email: str,
    password: str,
    display_name: str,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO users (id, email, password, display_name, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, email, password, display_name, created_at),
    )


def get_user_by_email(conn: sqlite3.Connection, email: str) -> dict | None:
    return row(conn, "SELECT * FROM users WHERE email = ?", (email,))


def get_user(conn: sqlite3.Connection, user_id: str) -> dict | None:
    return row(conn, "SELECT * FROM users WHERE id = ?", (user_id,))


def count_users(conn: sqlite3.Connection) -> int:
    found = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return int(found["n"]) if found else 0


def insert_token(conn: sqlite3.Connection, *, token: str, user_id: str, created_at: str) -> None:
    conn.execute(
        "INSERT INTO tokens (token, user_id, created_at) VALUES (?, ?, ?)",
        (token, user_id, created_at),
    )


def get_token(conn: sqlite3.Connection, token: str) -> dict | None:
    return row(
        conn,
        "SELECT t.token, t.user_id, t.created_at, u.display_name "
        "FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.token = ?",
        (token,),
    )


# --------------------------------------------------------------------------- #
# restaurants, opening hours, tables
# --------------------------------------------------------------------------- #
def insert_restaurant(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        "INSERT INTO restaurants (id, name, timezone, slot_minutes, "
        " reservation_duration_minutes, cancellation_cutoff_minutes, ordinal) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            data["id"],
            data["name"],
            data["timezone"],
            int(data["slot_minutes"]),
            int(data["reservation_duration_minutes"]),
            int(data["cancellation_cutoff_minutes"]),
            int(data["ordinal"]),
        ),
    )


def list_restaurants(conn: sqlite3.Connection) -> list[dict]:
    return rows(conn, "SELECT * FROM restaurants ORDER BY ordinal, id")


def get_restaurant(conn: sqlite3.Connection, restaurant_id: str) -> dict | None:
    return row(conn, "SELECT * FROM restaurants WHERE id = ?", (restaurant_id,))


def insert_opening_hours(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        "INSERT INTO opening_hours (restaurant_id, weekday, opens, closes, ordinal) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            data["restaurant_id"],
            data["weekday"],
            data["opens"],
            data["closes"],
            int(data["ordinal"]),
        ),
    )


def opening_hours_for(
    conn: sqlite3.Connection, restaurant_id: str
) -> list[dict]:
    return rows(
        conn,
        "SELECT weekday, opens, closes FROM opening_hours "
        "WHERE restaurant_id = ? ORDER BY ordinal, opens",
        (restaurant_id,),
    )


def insert_table(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        "INSERT INTO dining_tables (restaurant_id, id, label, capacity, ordinal) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            data["restaurant_id"],
            data["id"],
            data["label"],
            int(data["capacity"]),
            int(data["ordinal"]),
        ),
    )


def tables_for(conn: sqlite3.Connection, restaurant_id: str) -> list[dict]:
    """In fixture order — the order `available_table_ids` must be reported in."""
    return rows(
        conn,
        "SELECT id, label, capacity FROM dining_tables "
        "WHERE restaurant_id = ? ORDER BY ordinal, id",
        (restaurant_id,),
    )


def get_table(conn: sqlite3.Connection, restaurant_id: str, table_id: str) -> dict | None:
    return row(
        conn,
        "SELECT id, label, capacity FROM dining_tables "
        "WHERE restaurant_id = ? AND id = ?",
        (restaurant_id, table_id),
    )


# --------------------------------------------------------------------------- #
# reservations
# --------------------------------------------------------------------------- #
def insert_reservation(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        "INSERT INTO reservations (id, reference, user_id, restaurant_id, table_id, "
        " party_size, status, starts_at_local, starts_at, ends_at, created_at, "
        " starts_at_utc, ends_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            data["id"],
            data["reference"],
            data["user_id"],
            data["restaurant_id"],
            data["table_id"],
            int(data["party_size"]),
            data["status"],
            data["starts_at_local"],
            data["starts_at"],
            data["ends_at"],
            data["created_at"],
            data["starts_at_utc"],
            data["ends_at_utc"],
        ),
    )


def get_reservation_by_reference(
    conn: sqlite3.Connection, reference: str
) -> dict | None:
    return row(conn, "SELECT * FROM reservations WHERE reference = ?", (reference,))


def reference_exists(conn: sqlite3.Connection, reference: str) -> bool:
    found = conn.execute(
        "SELECT 1 AS hit FROM reservations WHERE reference = ?", (reference,)
    ).fetchone()
    return found is not None


def list_reservations_for_user(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    """The caller's bookings, `starts_at` descending, confirmed and cancelled alike."""
    return rows(
        conn,
        "SELECT * FROM reservations WHERE user_id = ? "
        "ORDER BY starts_at_utc DESC, created_at DESC, id DESC",
        (user_id,),
    )


def confirmed_for_restaurant_in_range(
    conn: sqlite3.Connection,
    restaurant_id: str,
    window_start: str,
    window_end: str,
    exclude_references: Iterable[str] = (),
) -> list[dict]:
    """Confirmed bookings that could overlap a UTC window.

    Both bounds are compared against the UTC-normalised columns, whose string
    order is chronological. Exact overlap is still decided in Python, where the
    half-open intervals of both bookings are known.
    """
    excluded = tuple(exclude_references)
    sql = (
        "SELECT * FROM reservations WHERE restaurant_id = ? AND status = 'confirmed' "
        "AND starts_at_utc < ? AND ends_at_utc > ?"
    )
    params: list[Any] = [restaurant_id, window_end, window_start]
    if excluded:
        sql += f" AND reference NOT IN ({', '.join('?' for _ in excluded)})"
        params.extend(excluded)
    return rows(conn, sql, params)


def update_reservation(
    conn: sqlite3.Connection, reference: str, fields: dict
) -> dict | None:
    allowed = {
        "table_id",
        "party_size",
        "status",
        "starts_at_local",
        "starts_at",
        "ends_at",
        "starts_at_utc",
        "ends_at_utc",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot update fields: {sorted(unknown)}")
    if not fields:
        return get_reservation_by_reference(conn, reference)
    assignments = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(
        f"UPDATE reservations SET {assignments} WHERE reference = ?",
        (*fields.values(), reference),
    )
    return get_reservation_by_reference(conn, reference)


# --------------------------------------------------------------------------- #
# idempotency
# --------------------------------------------------------------------------- #
def get_idempotency(
    conn: sqlite3.Connection, *, user_id: str, key: str, method: str, path: str
) -> dict | None:
    found = row(
        conn,
        "SELECT * FROM idempotency WHERE user_id = ? AND key = ? AND method = ? AND path = ?",
        (user_id, key, method, path),
    )
    if found is None:
        return None
    found["response_body"] = json.loads(found["response_body"])
    found["request_body"] = json.loads(found["request_body"])
    return found


def insert_idempotency(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    key: str,
    method: str,
    path: str,
    body_fingerprint: str,
    request_body: Any,
    status_code: int,
    response_body: Any,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO idempotency (user_id, key, method, path, body_fingerprint, "
        " request_body, status_code, response_body, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            key,
            method,
            path,
            body_fingerprint,
            json.dumps(request_body, sort_keys=True, separators=(",", ":")),
            int(status_code),
            json.dumps(response_body, sort_keys=True, separators=(",", ":")),
            created_at,
        ),
    )


def fingerprint(body: Any) -> str:
    """Canonical JSON hash: key order and whitespace do not matter."""
    import hashlib

    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# state export / import
# --------------------------------------------------------------------------- #
def dump_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """A read-only snapshot of everything, as plain JSON-compatible rows."""
    state: dict[str, Any] = {}
    for table in STATE_TABLES:
        state[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
    return state


def replace_state(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    """Atomically swap in exported state. Caller owns the transaction."""
    for table in reversed(STATE_TABLES):
        conn.execute(f"DELETE FROM {table}")
    for table in STATE_TABLES:
        records = state.get(table) or []
        if not records:
            continue
        columns = list(records[0].keys())
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        )
        conn.executemany(sql, [tuple(record[c] for c in columns) for record in records])
