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
    "restaurant_combinable",
    "restaurant_managers",
    "restaurant_policies",
    "reservations",
    "reservation_tables",
    "reservation_history",
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


def insert_manager(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        "INSERT INTO restaurant_managers (restaurant_id, position, user_id) "
        "VALUES (?, ?, ?)",
        (data["restaurant_id"], int(data["position"]), data["user_id"]),
    )


def managers_for(conn: sqlite3.Connection, restaurant_id: str) -> list[str]:
    """The users who may publish this restaurant's policies, in fixture order."""
    return [
        record["user_id"]
        for record in rows(
            conn,
            "SELECT user_id FROM restaurant_managers WHERE restaurant_id = ? "
            "ORDER BY position",
            (restaurant_id,),
        )
    ]


def insert_policy(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        "INSERT INTO restaurant_policies (restaurant_id, policy_version, effective_from, "
        " slot_minutes, reservation_duration_minutes, cancellation_cutoff_minutes, "
        " opening_hours, capacities, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            data["restaurant_id"],
            int(data["policy_version"]),
            data["effective_from"],
            int(data["slot_minutes"]),
            int(data["reservation_duration_minutes"]),
            int(data["cancellation_cutoff_minutes"]),
            json.dumps(data["opening_hours"], separators=(",", ":")),
            json.dumps(data["capacities"], separators=(",", ":")),
            data["created_at"],
        ),
    )


def policies_for(conn: sqlite3.Connection, restaurant_id: str) -> list[dict]:
    """Every published policy, in publication order, with its parts decoded."""
    found = rows(
        conn,
        "SELECT * FROM restaurant_policies WHERE restaurant_id = ? ORDER BY policy_version",
        (restaurant_id,),
    )
    for policy in found:
        policy["opening_hours"] = json.loads(policy["opening_hours"])
        policy["capacities"] = json.loads(policy["capacities"])
    return found


def next_policy_version(conn: sqlite3.Connection, restaurant_id: str) -> int:
    """One more than the highest version this restaurant has, so versions never
    repeat and a failed write allocates nothing."""
    found = conn.execute(
        "SELECT COALESCE(MAX(policy_version), 0) AS highest FROM restaurant_policies "
        "WHERE restaurant_id = ?",
        (restaurant_id,),
    ).fetchone()
    return int(found["highest"]) + 1


def insert_combinable(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        "INSERT INTO restaurant_combinable (restaurant_id, position, table_a, table_b) "
        "VALUES (?, ?, ?, ?)",
        (
            data["restaurant_id"],
            int(data["position"]),
            data["table_a"],
            data["table_b"],
        ),
    )


def combinable_for(conn: sqlite3.Connection, restaurant_id: str) -> list[dict]:
    """Declared pairs in declaration order — the order `available_options` uses."""
    return rows(
        conn,
        "SELECT table_a, table_b FROM restaurant_combinable "
        "WHERE restaurant_id = ? ORDER BY position",
        (restaurant_id,),
    )


# --------------------------------------------------------------------------- #
# reservations
# --------------------------------------------------------------------------- #
def insert_reservation(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        "INSERT INTO reservations (id, reference, user_id, restaurant_id, table_id, "
        " party_size, status, starts_at_local, starts_at, ends_at, created_at, "
        " starts_at_utc, ends_at_utc, revision, accepted_terms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            int(data.get("revision", 1)),
            json.dumps(data.get("accepted_terms") or {}, separators=(",", ":"), sort_keys=True),
        ),
    )
    set_reservation_tables(conn, data["reference"], data.get("table_ids") or [data["table_id"]])


def set_reservation_tables(
    conn: sqlite3.Connection, reference: str, table_ids: Sequence[str]
) -> None:
    """The tables a reservation holds, in order. Replaces any previous set."""
    conn.execute("DELETE FROM reservation_tables WHERE reference = ?", (reference,))
    conn.executemany(
        "INSERT INTO reservation_tables (reference, position, table_id) VALUES (?, ?, ?)",
        [(reference, position, table_id) for position, table_id in enumerate(table_ids)],
    )


def tables_for_references(
    conn: sqlite3.Connection, references: Sequence[str]
) -> dict[str, list[str]]:
    """Member tables per reservation, each in position order."""
    found: dict[str, list[str]] = {reference: [] for reference in references}
    if not references:
        return found
    placeholders = ", ".join("?" for _ in references)
    for record in conn.execute(
        f"SELECT reference, table_id FROM reservation_tables "
        f"WHERE reference IN ({placeholders}) ORDER BY reference, position",
        tuple(references),
    ):
        found[record["reference"]].append(record["table_id"])
    return found


def _decorate(record: dict | None, members: dict[str, list[str]]) -> dict | None:
    """A reservation row as the layers above read it.

    `table_ids` is attached, and the accepted terms are decoded from the text they
    are stored as. A row with no member rows — only possible in a snapshot taken
    before combinations existed — holds the single table its row names.
    """
    if record is None:
        return None
    table_ids = members.get(record["reference"]) or []
    record["table_ids"] = list(table_ids) or [record["table_id"]]
    terms = record.get("accepted_terms")
    if isinstance(terms, str):
        record["accepted_terms"] = json.loads(terms or "{}")
    return record


def get_reservation_by_reference(
    conn: sqlite3.Connection, reference: str
) -> dict | None:
    record = row(conn, "SELECT * FROM reservations WHERE reference = ?", (reference,))
    return _decorate(record, tables_for_references(conn, [reference]))


def reference_exists(conn: sqlite3.Connection, reference: str) -> bool:
    found = conn.execute(
        "SELECT 1 AS hit FROM reservations WHERE reference = ?", (reference,)
    ).fetchone()
    return found is not None


def list_reservations_for_user(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    """The caller's bookings, `starts_at` descending, confirmed and cancelled alike."""
    records = rows(
        conn,
        "SELECT * FROM reservations WHERE user_id = ? "
        "ORDER BY starts_at_utc DESC, created_at DESC, id DESC",
        (user_id,),
    )
    members = tables_for_references(conn, [r["reference"] for r in records])
    return [_decorate(r, members) for r in records]  # type: ignore[return-value]


def confirmed_for_restaurant_in_range(
    conn: sqlite3.Connection,
    restaurant_id: str,
    window_start: str,
    window_end: str,
    exclude_references: Iterable[str] = (),
) -> list[dict]:
    """Confirmed bookings that could overlap a UTC window — one row per table held.

    A booking of two tables appears twice, once under each table's id, so a
    caller asking "is this table taken?" needs no idea that combinations exist.

    Both bounds are compared against the UTC-normalised columns, whose string
    order is chronological. Exact overlap is still decided in Python, where the
    half-open intervals of both bookings are known.
    """
    excluded = tuple(exclude_references)
    sql = (
        "SELECT r.id, r.reference, r.user_id, r.restaurant_id, m.table_id AS table_id, "
        " r.party_size, r.status, r.starts_at_local, r.starts_at, r.ends_at, "
        " r.created_at, r.starts_at_utc, r.ends_at_utc, m.position AS position "
        "FROM reservations r JOIN reservation_tables m ON m.reference = r.reference "
        "WHERE r.restaurant_id = ? AND r.status = 'confirmed' "
        "AND r.starts_at_utc < ? AND r.ends_at_utc > ?"
    )
    params: list[Any] = [restaurant_id, window_end, window_start]
    if excluded:
        sql += f" AND r.reference NOT IN ({', '.join('?' for _ in excluded)})"
        params.extend(excluded)
    return rows(conn, sql, params)


def update_reservation(
    conn: sqlite3.Connection, reference: str, fields: dict
) -> dict | None:
    allowed = {
        "table_id",
        "party_size",
        "status",
        "revision",
        "accepted_terms",
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
    values = [
        # Accepted terms are stored as the same canonical text they are exported
        # as, so a snapshot restores them byte for byte.
        json.dumps(value, separators=(",", ":"), sort_keys=True)
        if name == "accepted_terms" and not isinstance(value, str)
        else value
        for name, value in fields.items()
    ]
    conn.execute(
        f"UPDATE reservations SET {assignments} WHERE reference = ?",
        (*values, reference),
    )
    return get_reservation_by_reference(conn, reference)



# --------------------------------------------------------------------------- #
# reservation history
# --------------------------------------------------------------------------- #
def next_history_seq(conn: sqlite3.Connection, reference: str) -> int:
    """`seq` starts at 1 and increases by exactly 1, so the order is total even
    when two writes land in the same second."""
    found = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS highest FROM reservation_history WHERE reference = ?",
        (reference,),
    ).fetchone()
    return int(found["highest"]) + 1


def insert_history(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        "INSERT INTO reservation_history (reference, seq, at, event, changes, revision, "
        " accepted_terms) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            data["reference"],
            int(data["seq"]),
            data["at"],
            data["event"],
            json.dumps(data["changes"], separators=(",", ":")),
            int(data["revision"]),
            json.dumps(data["accepted_terms"], separators=(",", ":"), sort_keys=True),
        ),
    )


def history_for(conn: sqlite3.Connection, reference: str) -> list[dict]:
    """The reservation's own record, oldest first, with its parts decoded."""
    entries = rows(
        conn,
        "SELECT * FROM reservation_history WHERE reference = ? ORDER BY seq",
        (reference,),
    )
    for entry in entries:
        entry["changes"] = json.loads(entry["changes"])
        entry["accepted_terms"] = json.loads(entry["accepted_terms"])
    return entries


# --------------------------------------------------------------------------- #
# the restaurant's own revision
# --------------------------------------------------------------------------- #
def bump_restaurant_revision(conn: sqlite3.Connection, restaurant_id: str) -> int:
    """One increment for one successful operation, whatever it touched."""
    conn.execute(
        "UPDATE restaurants SET revision = revision + 1 WHERE id = ?", (restaurant_id,)
    )
    found = conn.execute(
        "SELECT revision FROM restaurants WHERE id = ?", (restaurant_id,)
    ).fetchone()
    return int(found["revision"]) if found else 0


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
