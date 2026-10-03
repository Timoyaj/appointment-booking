"""Test control endpoints: reset, export and import.

``POST /_test/reset`` replaces all state with a fixture. ``GET /_test/export``
returns an atomic read-only snapshot of everything the service holds, and
``POST /_test/import`` swaps that snapshot in atomically — so accounts, hashed
passwords, live bearer tokens, reservations, references, timestamps and completed
idempotent receipts all survive a round trip unchanged, and a failed import
leaves the destination untouched. A snapshot from the preceding stage, which
predates table combinations, imports too: see :func:`_upgrade_snapshot`.

None of these require authentication, and all of them are enabled in the
delivered image.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta, timezone
from typing import Any

from . import FORMAT_VERSION, TRACK, parsing, repo
from .auth import hash_password
from .clock import now
from .db import Database
from .errors import malformed_request, validation_failed
from .tztime import (
    WEEKDAYS,
    UnknownTimezone,
    parse_hhmm,
    parse_local,
    resolve_local,
    rfc3339,
    zone,
)

# A reference is 6 to 12 characters of A-Z0-9 wherever it comes from, including a
# fixture: every reference in the system conforms to one format.
REFERENCE_RE = re.compile(r"^[A-Z0-9]{6,12}$")


# --------------------------------------------------------------------------- #
# reset
# --------------------------------------------------------------------------- #
def _require(condition: bool, message: str) -> None:
    if not condition:
        raise validation_failed(message)


def _as_object(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise malformed_request(f"{where} must be a JSON object")
    return value


def _as_list(value: Any, where: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise malformed_request(f"{where} must be an array")
    return value


def _text(value: Any, where: str, *, max_length: int = parsing.MAX_ID_LENGTH) -> str:
    """A required string: missing is 422, a wrong JSON type is 400."""
    if value is None:
        raise validation_failed(f"{where} is required")
    if not isinstance(value, str):
        raise malformed_request(f"{where} must be a string")
    _require(bool(value), f"{where} must not be empty")
    _require(
        len(value) <= max_length, f"{where} must be at most {max_length} characters"
    )
    return value


def _integer(value: Any, where: str, *, minimum: int = 0) -> int:
    """A required integer: missing is 422, a wrong JSON type is 400."""
    if value is None:
        raise validation_failed(f"{where} is required")
    if not isinstance(value, int) or isinstance(value, bool):
        raise malformed_request(f"{where} must be an integer")
    _require(value >= minimum, f"{where} must be at least {minimum}")
    return int(value)


def reset(db: Database, fixture: Any) -> None:
    """Replace every piece of service state with ``fixture``."""
    body = _as_object(fixture, "reset fixture")
    users = _as_list(body.get("users"), "users")
    restaurants = _as_list(body.get("restaurants"), "restaurants")
    reservations = _as_list(body.get("reservations"), "reservations")

    prepared_users = [_prepare_user(entry, index) for index, entry in enumerate(users)]
    prepared_restaurants = [
        _prepare_restaurant(entry, index) for index, entry in enumerate(restaurants)
    ]
    by_id = {restaurant["row"]["id"]: restaurant for restaurant in prepared_restaurants}
    seen_references: set[str] = set()
    prepared_reservations = [
        _prepare_reservation(entry, index, by_id, seen_references)
        for index, entry in enumerate(reservations)
    ]

    # Everything is validated before anything is written, so a bad fixture
    # leaves the previous state alone rather than half-replacing it.
    with db.transaction() as conn:
        for table in reversed(repo.STATE_TABLES):
            conn.execute(f"DELETE FROM {table}")

        for user in prepared_users:
            repo.insert_user(conn, **user)
        for restaurant in prepared_restaurants:
            repo.insert_restaurant(conn, restaurant["row"])
            for index, hours in enumerate(restaurant["opening_hours"]):
                repo.insert_opening_hours(
                    conn, {**hours, "restaurant_id": restaurant["row"]["id"], "ordinal": index}
                )
            for index, table in enumerate(restaurant["tables"]):
                repo.insert_table(
                    conn, {**table, "restaurant_id": restaurant["row"]["id"], "ordinal": index}
                )
            for index, pair in enumerate(restaurant["combinable"]):
                repo.insert_combinable(
                    conn, {**pair, "restaurant_id": restaurant["row"]["id"], "position": index}
                )
        for reservation in prepared_reservations:
            repo.insert_reservation(conn, reservation)


def _prepare_user(entry: Any, index: int) -> dict:
    where = f"users[{index}]"
    body = _as_object(entry, where)
    email = _text(body.get("email"), f"{where}.email", max_length=320)
    password = body.get("password")
    if not isinstance(password, str):
        raise malformed_request(f"{where}.password must be a string")
    display_name = body.get("display_name")
    if display_name is None:
        display_name = ""
    if not isinstance(display_name, str):
        raise malformed_request(f"{where}.display_name must be a string")
    user_id = _text(body.get("id") if body.get("id") is not None else f"u_seed_{index}",
                    f"{where}.id")
    return {
        "user_id": user_id,
        "email": email,
        # Seeded users must be able to log in with the given password immediately,
        # so it is hashed exactly like a signup password.
        "password": hash_password(password),
        "display_name": display_name,
        "created_at": rfc3339(now()),
    }


def _prepare_restaurant(entry: Any, index: int) -> dict:
    where = f"restaurants[{index}]"
    body = _as_object(entry, where)
    timezone_name = _text(body.get("timezone"), f"{where}.timezone", max_length=64)
    try:
        tz = zone(timezone_name)
    except UnknownTimezone as exc:
        raise validation_failed(
            f"{where}.timezone '{timezone_name}' is not a known IANA zone"
        ) from exc

    opening_hours: list[dict] = []
    for hour_index, hour_entry in enumerate(_as_list(body.get("opening_hours"), f"{where}.opening_hours")):
        hour_where = f"{where}.opening_hours[{hour_index}]"
        hours = _as_object(hour_entry, hour_where)
        weekday = hours.get("weekday")
        if not isinstance(weekday, str) or weekday.lower() not in WEEKDAYS:
            raise validation_failed(
                f"{hour_where}.weekday must be one of {', '.join(WEEKDAYS)}"
            )
        opens = parse_hhmm(str(hours.get("opens")))
        closes = parse_hhmm(str(hours.get("closes")))
        if opens is None or closes is None:
            raise validation_failed(
                f"{hour_where}.opens and .closes must be local HH:MM"
            )
        if closes <= opens:
            raise validation_failed(
                f"{hour_where}.closes must be later than opens on the same local day"
            )
        opening_hours.append(
            {
                "weekday": weekday.lower(),
                "opens": opens.strftime("%H:%M"),
                "closes": closes.strftime("%H:%M"),
            }
        )

    tables: list[dict] = []
    seen_table_ids: set[str] = set()
    for table_index, table_entry in enumerate(_as_list(body.get("tables"), f"{where}.tables")):
        table_where = f"{where}.tables[{table_index}]"
        table = _as_object(table_entry, table_where)
        table_id = _text(table.get("id"), f"{table_where}.id")
        if table_id in seen_table_ids:
            raise validation_failed(f"{table_where}.id '{table_id}' appears more than once")
        seen_table_ids.add(table_id)
        label = table.get("label")
        if label is None:
            label = table_id
        if not isinstance(label, str):
            raise malformed_request(f"{table_where}.label must be a string")
        tables.append(
            {
                "id": table_id,
                "label": label,
                "capacity": _integer(table.get("capacity"), f"{table_where}.capacity", minimum=1),
            }
        )

    # Declared combinations are validated against the tables just parsed: a pair
    # is exactly two distinct tables of *this* restaurant, and combining is not
    # transitive, so nothing here implies a third pair.
    combinable: list[dict] = []
    seen_pairs: set[frozenset] = set()
    for pair_index, pair_entry in enumerate(
        _as_list(body.get("combinable"), f"{where}.combinable")
    ):
        pair_where = f"{where}.combinable[{pair_index}]"
        if not isinstance(pair_entry, list):
            raise malformed_request(f"{pair_where} must be an array of two table ids")
        if len(pair_entry) != 2:
            raise validation_failed(
                f"{pair_where} must hold exactly two table ids: combinations are pairs"
            )
        first, second = pair_entry
        for value in (first, second):
            if not isinstance(value, str):
                raise malformed_request(f"{pair_where} must hold table id strings")
        if first not in seen_table_ids or second not in seen_table_ids:
            raise validation_failed(
                f"{pair_where} names a table that is not in {where}.tables"
            )
        if first == second:
            raise validation_failed(f"{pair_where} names the same table twice")
        pair_key = frozenset((first, second))
        if pair_key in seen_pairs:
            raise validation_failed(f"{pair_where} declares the same pair twice")
        seen_pairs.add(pair_key)
        combinable.append({"table_a": first, "table_b": second})

    return {
        "row": {
            "id": _text(body.get("id"), f"{where}.id"),
            "name": _text(body.get("name"), f"{where}.name", max_length=200),
            "timezone": timezone_name,
            "slot_minutes": _integer(body.get("slot_minutes"), f"{where}.slot_minutes", minimum=1),
            "reservation_duration_minutes": _integer(
                body.get("reservation_duration_minutes"),
                f"{where}.reservation_duration_minutes",
                minimum=1,
            ),
            "cancellation_cutoff_minutes": _integer(
                body.get("cancellation_cutoff_minutes"),
                f"{where}.cancellation_cutoff_minutes",
                minimum=0,
            ),
            "ordinal": index,
        },
        "opening_hours": opening_hours,
        "tables": tables,
        "combinable": combinable,
        "tz": tz,
    }


def _prepare_reservation(
    entry: Any, index: int, restaurants: dict[str, dict], seen_references: set[str]
) -> dict:
    where = f"reservations[{index}]"
    body = _as_object(entry, where)
    restaurant_id = _text(body.get("restaurant_id"), f"{where}.restaurant_id")
    restaurant = restaurants.get(restaurant_id)
    if restaurant is None:
        raise validation_failed(
            f"{where}.restaurant_id '{restaurant_id}' is not in this fixture"
        )

    tz = restaurant["tz"]
    duration = int(restaurant["row"]["reservation_duration_minutes"])
    starts_at_local = body.get("starts_at_local")
    if not isinstance(starts_at_local, str):
        raise malformed_request(f"{where}.starts_at_local must be a string")
    naive = parse_local(starts_at_local)
    if naive is None:
        raise validation_failed(
            f"{where}.starts_at_local must be a bare local YYYY-MM-DDTHH:MM"
        )

    # Seed data is resolved leniently: the first occurrence for an ambiguous
    # local time, and the pre-transition offset for a skipped one, so a fixture
    # can never fail to load.
    resolution = resolve_local(naive, tz)
    starts_utc = resolution.utc
    if starts_utc is None:
        starts_utc = (
            naive.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
        )
    ends_utc = starts_utc + timedelta(minutes=duration)

    status = body.get("status") or "confirmed"
    if not isinstance(status, str) or status not in ("confirmed", "cancelled"):
        raise validation_failed(f"{where}.status must be 'confirmed' or 'cancelled'")

    created_at = body.get("created_at")
    if created_at is None:
        created_at = rfc3339(now())
    elif not isinstance(created_at, str):
        raise malformed_request(f"{where}.created_at must be a string")

    reference = body.get("reference")
    if reference is None:
        reference = f"SEED{index:04d}"
    if not isinstance(reference, str):
        raise malformed_request(f"{where}.reference must be a string")
    if not REFERENCE_RE.match(reference):
        raise validation_failed(
            f"{where}.reference '{reference}' must be 6 to 12 characters of A-Z0-9"
        )
    if reference in seen_references:
        raise validation_failed(
            f"{where}.reference '{reference}' appears more than once"
        )
    seen_references.add(reference)

    # Seeded reservations name their tables either way: `table_id` for one table,
    # `table_ids` for a set. Both are kept as loaded — seed data is lenient about
    # the seating rules, exactly as it is about times, so a fixture can describe a
    # world the API would not have produced.
    seeded_ids = body.get("table_ids")
    seeded_one = body.get("table_id")
    if seeded_ids is not None and seeded_one is not None:
        raise validation_failed(
            f"{where}: hold 'table_id' or 'table_ids', not both"
        )
    if seeded_ids is not None:
        if not isinstance(seeded_ids, list):
            raise malformed_request(f"{where}.table_ids must be an array")
        if not 1 <= len(seeded_ids) <= 2:
            raise validation_failed(
                f"{where}.table_ids must hold one or two table ids"
            )
        for entry in seeded_ids:
            if not isinstance(entry, str):
                raise malformed_request(f"{where}.table_ids must hold strings")
        if len(set(seeded_ids)) != len(seeded_ids):
            raise validation_failed(f"{where}.table_ids names the same table twice")
        table_ids = [
            _text(entry, f"{where}.table_ids[{position}]")
            for position, entry in enumerate(seeded_ids)
        ]
    else:
        table_ids = [_text(seeded_one, f"{where}.table_id")]

    reservation_id = _text(
        body.get("id") if body.get("id") is not None else f"res_seed_{index}",
        f"{where}.id",
    )
    return {
        "id": reservation_id,
        "reference": reference,
        "user_id": _text(body.get("user_id"), f"{where}.user_id"),
        "restaurant_id": restaurant_id,
        "table_id": table_ids[0],
        "table_ids": table_ids,
        "party_size": _integer(body.get("party_size"), f"{where}.party_size", minimum=1),
        "status": status,
        "starts_at_local": naive.strftime("%Y-%m-%dT%H:%M"),
        "starts_at": rfc3339(starts_utc.astimezone(tz)),
        "ends_at": rfc3339(ends_utc.astimezone(tz)),
        "created_at": created_at,
        "starts_at_utc": rfc3339(starts_utc),
        "ends_at_utc": rfc3339(ends_utc),
    }


# --------------------------------------------------------------------------- #
# export / import
# --------------------------------------------------------------------------- #
def export_state(db: Database) -> dict:
    """An atomic, read-only snapshot of the whole service state."""
    with db.read() as conn:
        state = repo.dump_state(conn)
    return {"track": TRACK, "format_version": FORMAT_VERSION, "state": state}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _upgrade_snapshot(state: dict) -> dict:
    """Make a snapshot taken before combinations existed importable here.

    Such a snapshot carries no member rows, because every booking it describes
    holds the single table its own row names. Synthesising those rows is the whole
    upgrade: the rest of the snapshot is validated and stored exactly as it
    arrived, so accounts, live tokens and completed receipts all survive it.
    """
    if state.get("reservation_tables"):
        return state
    reservations = state.get("reservations")
    if not isinstance(reservations, list) or not reservations:
        return state
    members = [
        {"reference": record["reference"], "position": 0, "table_id": record["table_id"]}
        for record in reservations
        if isinstance(record, dict)
        and isinstance(record.get("reference"), str)
        and isinstance(record.get("table_id"), str)
    ]
    if not members:
        return state
    return {**state, "reservation_tables": members}


def import_state(db: Database, payload: Any) -> None:
    """Replace all state with a previously exported snapshot."""
    body = _as_object(payload, "import payload")

    if "track" not in body:
        raise validation_failed("'track' is required")
    if body["track"] != TRACK:
        raise validation_failed(f"'track' must be '{TRACK}'")
    if "format_version" not in body:
        raise validation_failed("'format_version' is required")
    # A wrong version is 422 whatever its JSON type: the spec groups "wrong
    # track/version" with the other import rejections rather than with malformed
    # bodies.
    version = body["format_version"]
    if version != FORMAT_VERSION or isinstance(version, bool):
        raise validation_failed(f"'format_version' must be {FORMAT_VERSION}")
    if "state" not in body:
        raise validation_failed("'state' is required")
    state = body["state"]
    if not isinstance(state, dict):
        raise validation_failed("'state' must be an object")
    state = _upgrade_snapshot(state)

    # Validate before touching anything: a bad snapshot must not wipe the
    # destination on its way to failing.
    with db.read() as conn:
        columns = {table: _columns(conn, table) for table in repo.STATE_TABLES}

    for table in repo.STATE_TABLES:
        records = state.get(table, [])
        if not isinstance(records, list):
            raise validation_failed(f"state.{table} must be an array")
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise validation_failed(f"state.{table}[{index}] must be an object")
            unknown = set(record) - columns[table]
            if unknown:
                raise validation_failed(
                    f"state.{table}[{index}] has unknown column(s): {sorted(unknown)}"
                )
            missing = columns[table] - set(record)
            if missing:
                raise validation_failed(
                    f"state.{table}[{index}] is missing column(s): {sorted(missing)}"
                )

    with db.transaction() as conn:
        repo.replace_state(conn, state)
