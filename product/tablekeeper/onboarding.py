"""Restaurants a real person can create, and the staff who run them.

Up to here a restaurant could only exist by having a fixture POSTed at the test
hooks: the only caller of ``repo.insert_restaurant`` was the reset endpoint. That
is fine for a reference implementation and impossible for a product — nobody signs
up by handing an operator a JSON file.

So this module is the second way a restaurant comes into being: an authenticated
person describes their room, and gets it, with themselves attached as its owner.
It validates the description the same way the fixture path does, because a
restaurant is a restaurant however it arrived, and it writes everything in one
transaction so a half-built restaurant is never left behind for a diner to find.

Roles are deliberately small. An **owner** may do anything including deciding who
else works there; a **manager** may publish policies and plan the seating; a
**host** may see the room and its bookings but change none of its rules. The
permission that already existed — publishing policies, previewing and applying a
seating plan — is still decided by ``restaurant_managers``, so every rule the
earlier stages wrote still reads the table it was written against; this module
keeps that table in step with the richer one.
"""

from __future__ import annotations

import secrets
import sqlite3
from typing import Any

from . import domain, repo
from .clock import now
from .errors import (
    already_staff,
    forbidden,
    last_owner,
    not_found,
    validation_failed,
)
from .tztime import WEEKDAYS, UnknownTimezone, parse_hhmm, rfc3339, zone

OWNER = "owner"
MANAGER = "manager"
HOST = "host"
ROLES = (OWNER, MANAGER, HOST)

# The roles that may do what the earlier stages called "manager work": publishing
# a policy, and proposing or applying a seating plan.
MANAGER_ROLES = (OWNER, MANAGER)

NAME_MAX = 120
LABEL_MAX = 40
MAX_TABLES = 200
MAX_OPENING_HOURS = 7 * 8


def _string(value: Any, field: str, *, max_length: int) -> str:
    if value is None:
        raise validation_failed(f"'{field}' is required")
    if not isinstance(value, str):
        raise validation_failed(f"'{field}' must be a string")
    text = value.strip()
    if not text:
        raise validation_failed(f"'{field}' must not be empty")
    if len(text) > max_length:
        raise validation_failed(f"'{field}' must be at most {max_length} characters")
    return text


def _bounded_int(value: Any, field: str, low: int, high: int) -> int:
    if value is None:
        raise validation_failed(f"'{field}' is required")
    if isinstance(value, bool) or not isinstance(value, int):
        raise validation_failed(f"'{field}' must be an integer")
    if not low <= value <= high:
        raise validation_failed(f"'{field}' must be between {low} and {high}")
    return int(value)


def new_restaurant_id() -> str:
    return f"r_{secrets.token_hex(6)}"


def _timezone(value: Any) -> str:
    name = _string(value, "timezone", max_length=64)
    try:
        zone(name)
    except (UnknownTimezone, KeyError, ValueError):
        raise validation_failed(f"'{name}' is not a known IANA timezone")
    return name


def _opening_hours(value: Any) -> list[dict]:
    if value is None:
        raise validation_failed("'opening_hours' is required")
    if not isinstance(value, list):
        raise validation_failed("'opening_hours' must be a list")
    if len(value) > MAX_OPENING_HOURS:
        raise validation_failed("'opening_hours' has too many entries")
    hours: list[dict] = []
    for index, entry in enumerate(value):
        where = f"opening_hours[{index}]"
        if not isinstance(entry, dict):
            raise validation_failed(f"{where} must be an object")
        weekday = entry.get("weekday")
        if weekday not in WEEKDAYS:
            raise validation_failed(
                f"{where}.weekday must be one of {', '.join(WEEKDAYS)}"
            )
        opens = parse_hhmm(entry.get("opens")) if isinstance(entry.get("opens"), str) else None
        closes = parse_hhmm(entry.get("closes")) if isinstance(entry.get("closes"), str) else None
        if opens is None:
            raise validation_failed(f"{where}.opens must be a time written HH:MM")
        if closes is None:
            raise validation_failed(f"{where}.closes must be a time written HH:MM")
        if closes <= opens:
            raise validation_failed(f"{where}.closes must be after {where}.opens")
        hours.append(
            {
                "weekday": weekday,
                "opens": opens.strftime("%H:%M"),
                "closes": closes.strftime("%H:%M"),
            }
        )
    if not hours:
        raise validation_failed("'opening_hours' must describe at least one service")
    return hours


def _tables(value: Any) -> list[dict]:
    if value is None:
        raise validation_failed("'tables' is required")
    if not isinstance(value, list):
        raise validation_failed("'tables' must be a list")
    if not value:
        raise validation_failed("'tables' must name at least one table")
    if len(value) > MAX_TABLES:
        raise validation_failed(f"'tables' must name at most {MAX_TABLES} tables")
    tables: list[dict] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        where = f"tables[{index}]"
        if not isinstance(entry, dict):
            raise validation_failed(f"{where} must be an object")
        table_id = _string(entry.get("id"), f"{where}.id", max_length=64)
        if table_id in seen:
            raise validation_failed(f"{where}.id repeats the table id '{table_id}'")
        seen.add(table_id)
        tables.append(
            {
                "id": table_id,
                "label": _string(entry.get("label"), f"{where}.label", max_length=LABEL_MAX),
                "capacity": _bounded_int(entry.get("capacity"), f"{where}.capacity", 1, 100),
                "ordinal": index,
            }
        )
    return tables


def _combinable(value: Any, tables: list[dict]) -> list[tuple[str, str]]:
    """Declared pairs, each naming two of this restaurant's own tables."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise validation_failed("'combinable' must be a list")
    known = {table["id"] for table in tables}
    pairs: list[tuple[str, str]] = []
    seen: set[frozenset[str]] = set()
    for index, entry in enumerate(value):
        where = f"combinable[{index}]"
        if not isinstance(entry, list) or len(entry) != 2:
            raise validation_failed(f"{where} must be a pair of two table ids")
        first, second = entry
        if not isinstance(first, str) or not isinstance(second, str):
            raise validation_failed(f"{where} must name two table ids as strings")
        if first == second:
            raise validation_failed(f"{where} names the same table twice")
        for table_id in (first, second):
            if table_id not in known:
                raise validation_failed(f"{where} names unknown table '{table_id}'")
        key = frozenset((first, second))
        if key in seen:
            # The same pair twice is the same declaration; keeping both would make
            # the grid draw one seating twice.
            continue
        seen.add(key)
        pairs.append((first, second))
    return pairs


def create_restaurant(db, *, user_id: str, body: dict, key: str | None = None,
                      method: str = "POST", path: str = "/restaurants",
                      ) -> tuple[int, dict]:
    """Create a restaurant, with its creator as its owner.

    Idempotent under ``key`` when one is supplied, so a client that retries a
    signup over a flaky connection does not end up running two restaurants.
    """
    from . import idempotency

    name = _string(body.get("name"), "name", max_length=NAME_MAX)
    timezone_name = _timezone(body.get("timezone"))
    slot_minutes = _bounded_int(body.get("slot_minutes"), "slot_minutes", 1, 1440)
    duration = _bounded_int(
        body.get("reservation_duration_minutes"), "reservation_duration_minutes", 1, 1440
    )
    cutoff = _bounded_int(
        body.get("cancellation_cutoff_minutes"), "cancellation_cutoff_minutes", 0, 10_080
    )
    hours = _opening_hours(body.get("opening_hours"))
    tables = _tables(body.get("tables"))
    combinable = _combinable(body.get("combinable"), tables)

    with db.transaction() as conn:
        if key is not None:
            stored = idempotency.lookup(
                conn, user_id=user_id, key=key, method=method, path=path, body=body
            )
            if stored is not None:
                return 200, stored["response_body"]

        restaurant_id = new_restaurant_id()
        while repo.get_restaurant(conn, restaurant_id) is not None:  # pragma: no cover
            restaurant_id = new_restaurant_id()

        created_at = rfc3339(now())
        repo.insert_restaurant(
            conn,
            {
                "id": restaurant_id,
                "name": name,
                "timezone": timezone_name,
                "slot_minutes": slot_minutes,
                "reservation_duration_minutes": duration,
                "cancellation_cutoff_minutes": cutoff,
                "ordinal": len(repo.list_restaurants(conn)),
            },
        )
        for index, window in enumerate(hours):
            repo.insert_opening_hours(
                conn, {**window, "restaurant_id": restaurant_id, "ordinal": index}
            )
        for table in tables:
            repo.insert_table(conn, {**table, "restaurant_id": restaurant_id})
        for index, (first, second) in enumerate(combinable):
            repo.insert_combinable(
                conn,
                {
                    "restaurant_id": restaurant_id,
                    "position": index,
                    "table_a": first,
                    "table_b": second,
                },
            )
        # The creator runs the restaurant. Owner is a manager for every rule the
        # earlier stages wrote, so both tables are written and stay in step.
        repo.insert_manager(
            conn,
            {"restaurant_id": restaurant_id, "position": 0, "user_id": user_id},
        )
        repo.insert_staff(
            conn,
            restaurant_id=restaurant_id,
            user_id=user_id,
            role=OWNER,
            created_at=created_at,
        )
        repo.insert_audit(
            conn,
            restaurant_id=restaurant_id,
            user_id=user_id,
            action="restaurant_created",
            detail=f'{{"name": "{name}"}}',
            created_at=created_at,
        )

        response = {
            **_detail(conn, restaurant_id),
            "role": OWNER,
            "owner_user_id": user_id,
        }
        if key is not None:
            idempotency.record(
                conn, user_id=user_id, key=key, method=method, path=path, body=body,
                status_code=201, response_body=response,
            )
        return 201, response


def _detail(conn: sqlite3.Connection, restaurant_id: str) -> dict:
    """The restaurant as its own staff read it: the public detail plus its people."""
    restaurant = domain.require_restaurant(conn, restaurant_id)
    detail = restaurant.detail()
    detail["staff"] = [
        {
            "user_id": record["user_id"],
            "role": record["role"],
            "display_name": record["display_name"],
            "email": record["email"],
        }
        for record in repo.staff_for(conn, restaurant_id)
    ]
    return detail


def restaurant_for_staff(db, *, user_id: str, restaurant_id: str) -> dict:
    """A restaurant as its staff see it, and 404 to everybody else.

    A diner asking about somebody else's restaurant gets the public answer from
    the ordinary endpoint; this one is for the people who run it, so a stranger
    is told nothing rather than shown a redacted version.
    """
    with db.read() as conn:
        if role_in(conn, user_id=user_id, restaurant_id=restaurant_id) is None:
            raise not_found(f"No restaurant with id '{restaurant_id}'")
        detail = _detail(conn, restaurant_id)
        record = repo.get_restaurant(conn, restaurant_id)
        detail["revision"] = int(record["revision"]) if record else 0
        detail["your_role"] = role_in(
            conn, user_id=user_id, restaurant_id=restaurant_id
        )
    return detail


def role_of(db, *, user_id: str, restaurant_id: str) -> str | None:
    """This person's role here, or None when they have nothing to do with it.

    A manager who predates roles reads as a manager, because that is what they
    are: the older table is still the record of who may publish and plan.
    """
    with db.read() as conn:
        return role_in(conn, user_id=user_id, restaurant_id=restaurant_id)


def role_in(conn: sqlite3.Connection, *, user_id: str, restaurant_id: str) -> str | None:
    role = repo.staff_role(conn, restaurant_id, user_id)
    if role is not None:
        return role
    if user_id in repo.managers_for(conn, restaurant_id):
        return MANAGER
    return None


def is_manager(conn: sqlite3.Connection, *, user_id: str, restaurant_id: str) -> bool:
    """Whether this person may do manager work — the older question, still asked
    of the older table so that nothing the earlier stages decided has changed."""
    return user_id in repo.managers_for(conn, restaurant_id)


def my_restaurants(db, *, user_id: str) -> dict:
    with db.read() as conn:
        records = repo.restaurants_for_user(conn, user_id)
    return {
        "restaurants": [
            {"id": r["id"], "name": r["name"], "timezone": r["timezone"],
             "role": r["role"]}
            for r in records
        ]
    }


# --------------------------------------------------------------------------- #
# who works here
# --------------------------------------------------------------------------- #
def add_staff(db, *, actor_id: str, restaurant_id: str, body: dict) -> tuple[int, dict]:
    """Invite somebody to work here. Owners only: this is who may do what."""
    email = _string(body.get("email"), "email", max_length=320).lower()
    role = body.get("role")
    if role not in ROLES:
        raise validation_failed(f"'role' must be one of {', '.join(ROLES)}")

    with db.transaction() as conn:
        _require_owner(conn, actor_id=actor_id, restaurant_id=restaurant_id)
        person = repo.get_user_by_email(conn, email)
        if person is None:
            # Deliberately not an invitation email yet: this service has no way to
            # verify an address it was merely told about, and adding somebody to a
            # restaurant is not a reason to start guessing.
            raise validation_failed(
                "That person needs an account before they can be added; ask them to "
                "sign up first"
            )
        if role_in(conn, user_id=person["id"], restaurant_id=restaurant_id) is not None:
            raise already_staff()
        created_at = rfc3339(now())
        repo.insert_staff(
            conn,
            restaurant_id=restaurant_id,
            user_id=person["id"],
            role=role,
            created_at=created_at,
        )
        if role in MANAGER_ROLES:
            repo.insert_manager(
                conn,
                {
                    "restaurant_id": restaurant_id,
                    "position": len(repo.managers_for(conn, restaurant_id)),
                    "user_id": person["id"],
                },
            )
        repo.bump_restaurant_revision(conn, restaurant_id)
        _audit(conn, restaurant_id=restaurant_id, user_id=actor_id,
               action="staff_added", detail=f'{{"user_id": "{person["id"]}", "role": "{role}"}}')
        return 201, {
            "user_id": person["id"],
            "display_name": person["display_name"],
            "email": person["email"],
            "role": role,
        }


def change_role(db, *, actor_id: str, restaurant_id: str, target_id: str,
                body: dict) -> dict:
    """Change what somebody may do. An owner cannot be demoted by another owner."""
    role = body.get("role")
    if role not in ROLES:
        raise validation_failed(f"'role' must be one of {', '.join(ROLES)}")

    with db.transaction() as conn:
        _require_owner(conn, actor_id=actor_id, restaurant_id=restaurant_id)
        current = role_in(conn, user_id=target_id, restaurant_id=restaurant_id)
        if current is None:
            raise not_found("That person does not work at this restaurant")
        if current == OWNER and role != OWNER:
            # Ownership is not something one owner takes from another; the last
            # owner leaving would leave a restaurant nobody can administer.
            if _owner_count(conn, restaurant_id) <= 1:
                raise last_owner()
        repo.insert_staff(
            conn, restaurant_id=restaurant_id, user_id=target_id, role=role,
            created_at=rfc3339(now()),
        )
        if role in MANAGER_ROLES:
            if target_id not in repo.managers_for(conn, restaurant_id):
                repo.insert_manager(
                    conn,
                    {
                        "restaurant_id": restaurant_id,
                        "position": len(repo.managers_for(conn, restaurant_id)),
                        "user_id": target_id,
                    },
                )
        else:
            repo.remove_manager(conn, restaurant_id, target_id)
        repo.bump_restaurant_revision(conn, restaurant_id)
        _audit(conn, restaurant_id=restaurant_id, user_id=actor_id,
               action="role_changed",
               detail=f'{{"user_id": "{target_id}", "role": "{role}"}}')
        return {"user_id": target_id, "role": role}


def remove_staff(db, *, actor_id: str, restaurant_id: str, target_id: str) -> dict:
    """Remove somebody from the restaurant. The last owner cannot be removed."""
    with db.transaction() as conn:
        _require_owner(conn, actor_id=actor_id, restaurant_id=restaurant_id)
        current = role_in(conn, user_id=target_id, restaurant_id=restaurant_id)
        if current is None:
            raise not_found("That person does not work at this restaurant")
        if current == OWNER and _owner_count(conn, restaurant_id) <= 1:
            raise last_owner()
        repo.remove_staff(conn, restaurant_id, target_id)
        repo.remove_manager(conn, restaurant_id, target_id)
        repo.bump_restaurant_revision(conn, restaurant_id)
        _audit(conn, restaurant_id=restaurant_id, user_id=actor_id,
               action="staff_removed", detail=f'{{"user_id": "{target_id}"}}')
        return {"user_id": target_id, "removed": True}


def _owner_count(conn: sqlite3.Connection, restaurant_id: str) -> int:
    return sum(
        1 for record in repo.staff_for(conn, restaurant_id) if record["role"] == OWNER
    )


def _require_owner(conn: sqlite3.Connection, *, actor_id: str, restaurant_id: str) -> None:
    """Only an owner decides who works here.

    The restaurant is resolved first, so an unknown restaurant is a 404 to
    everybody and a known one is a 403 to somebody who merely dines there.
    """
    domain.require_restaurant(conn, restaurant_id)
    if role_in(conn, user_id=actor_id, restaurant_id=restaurant_id) != OWNER:
        raise forbidden("Only an owner of this restaurant may change its staff")


def _audit(conn: sqlite3.Connection, *, restaurant_id: str, user_id: str,
           action: str, detail: str) -> None:
    repo.insert_audit(
        conn,
        restaurant_id=restaurant_id,
        user_id=user_id,
        action=action,
        detail=detail,
        created_at=rfc3339(now()),
    )


def audit_trail(db, *, user_id: str, restaurant_id: str, limit: int = 50) -> dict:
    """What has happened here, to whom, and when — for anybody who works here."""
    with db.read() as conn:
        domain.require_restaurant(conn, restaurant_id)
        if role_in(conn, user_id=user_id, restaurant_id=restaurant_id) is None:
            raise not_found(f"No restaurant with id '{restaurant_id}'")
        return {"entries": repo.audit_for(conn, restaurant_id, limit=limit)}
