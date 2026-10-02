"""Reservations: book, list, read, cancel, amend, and move several at once.

Each function owns its transaction. Reads use a read transaction; writes use
``BEGIN IMMEDIATE`` so that concurrent requests serialise and a batch of moves
commits or rolls back as one unit.

Validation order is deliberate and follows the spec:

* a reservation is placed only after the restaurant and **every** table of the
  requested set are known (404), the set is one table or a pair the restaurant
  declared (422 ``combination_not_allowed``), the local time exists
  (422 ``invalid_local_time``), the sitting fits the day's service
  (422 ``outside_opening_hours``), it is aligned to the slot grid
  (422 ``not_on_slot_grid``), the party fits the tables it holds — a pair on
  their summed capacity (422 ``party_exceeds_capacity``) — and every one of those
  tables is free (409 ``table_unavailable``);
* cancel and amend are refused once the cancellation cutoff has passed
  (409 ``cutoff_passed``), and an amendment of a cancelled booking is
  409 ``reservation_cancelled``;
* for a batch of moves, non-occupancy errors take precedence in input order,
  with each booking's cutoff error preceding its other problems, and occupancy
  clashes are judged against the state the batch would produce.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from . import domain, idempotency, parsing, repo
from .clock import now
from .db import Database
from .errors import (
    cutoff_passed,
    malformed_request,
    not_found,
    reservation_cancelled,
    table_unavailable,
    validation_failed,
)
from .tztime import overlaps, rfc3339

MAX_MOVES = 8


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _load_owned(
    conn: sqlite3.Connection, user_id: str, reference: str
) -> tuple[dict, domain.Restaurant]:
    """A reservation by reference, or 404 — including when it belongs to someone else.

    Another diner's booking is indistinguishable from a booking that does not
    exist, so existence is never leaked.
    """
    record = repo.get_reservation_by_reference(conn, reference)
    if record is None or record["user_id"] != user_id:
        raise not_found(f"No reservation with reference '{reference}'")
    restaurant = domain.require_restaurant(conn, record["restaurant_id"])
    return record, restaurant


def _place(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    restaurant_id: str,
    table_ids: Sequence[str],
    starts_at_local: str,
    party_size: int,
    created_at: str | None = None,
    reference: str | None = None,
    reservation_id: str | None = None,
    exclude_reference: str | None = None,
) -> dict:
    """Validate a placement and write it. Returns the stored row."""
    restaurant = domain.require_restaurant(conn, restaurant_id)
    # Every table of the set must exist before anything is said about the set:
    # an unknown table is a missing resource, not a seating rule.
    tables = domain.require_tables(restaurant, table_ids)
    domain.check_combination(restaurant, table_ids)
    sitting = domain.resolve_sitting(restaurant, starts_at_local)
    domain.check_capacity(tables, party_size)

    occupancy = domain.load_occupancy(
        conn,
        restaurant,
        start_utc=sitting.starts_at_utc,
        end_utc=sitting.ends_at_utc,
        exclude_references=(exclude_reference,) if exclude_reference else (),
    )
    domain.check_free(
        occupancy,
        table_ids=table_ids,
        sitting=sitting,
        exclude_reference=exclude_reference,
    )

    record = {
        "id": reservation_id or domain.new_reservation_id(),
        "reference": reference or domain.new_reference(conn),
        "user_id": user_id,
        "restaurant_id": restaurant_id,
        "table_id": table_ids[0],
        "table_ids": list(table_ids),
        "party_size": party_size,
        "status": "confirmed",
        "created_at": created_at or rfc3339(now()),
        **sitting.stored_fields(),
    }
    repo.insert_reservation(conn, record)
    return repo.get_reservation_by_reference(conn, record["reference"])  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# POST /reservations
# --------------------------------------------------------------------------- #
def create_reservation(
    db: Database, *, user_id: str, key: str, body: dict, method: str, path: str
) -> tuple[int, dict]:
    """Book a table. Idempotent under ``key``; returns (status, body)."""
    with db.transaction() as conn:
        stored = idempotency.lookup(
            conn, user_id=user_id, key=key, method=method, path=path, body=body
        )
        if stored is not None:
            return 200, stored["response_body"]

        # Field validation happens after idempotency resolution, as specified.
        restaurant_id = parsing.id_field(body, "restaurant_id")
        table_ids = parsing.table_set_field(body)
        starts_at_local = parsing.local_time_field(body)
        party_size = parsing.party_size_field(body)

        record = _place(
            conn,
            user_id=user_id,
            restaurant_id=restaurant_id,  # type: ignore[arg-type]
            table_ids=table_ids,  # type: ignore[arg-type]
            starts_at_local=starts_at_local,
            party_size=party_size,  # type: ignore[arg-type]
        )
        response = domain.reservation_body(record)
        idempotency.record(
            conn,
            user_id=user_id,
            key=key,
            method=method,
            path=path,
            body=body,
            status_code=201,
            response_body=response,
        )
        return 201, response


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def list_reservations(db: Database, user_id: str) -> dict:
    with db.read() as conn:
        records = repo.list_reservations_for_user(conn, user_id)
        return {"reservations": [domain.reservation_body(r) for r in records]}


def get_reservation(db: Database, user_id: str, reference: str) -> dict:
    with db.read() as conn:
        record, _restaurant = _load_owned(conn, user_id, reference)
        return domain.reservation_body(record)


# --------------------------------------------------------------------------- #
# POST /reservations/{reference}/cancel
# --------------------------------------------------------------------------- #
def cancel_reservation(db: Database, user_id: str, reference: str) -> dict:
    with db.transaction() as conn:
        record, restaurant = _load_owned(conn, user_id, reference)
        if record["status"] == "cancelled":
            # Cancelling twice is not an error: report the current state.
            return domain.reservation_body(record)
        if domain.is_past_cutoff(record, restaurant, now()):
            raise cutoff_passed()
        updated = repo.update_reservation(conn, reference, {"status": "cancelled"})
        return domain.reservation_body(updated)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# PATCH /reservations/{reference}
# --------------------------------------------------------------------------- #
def amend_reservation(db: Database, user_id: str, reference: str, body: dict) -> dict:
    table_ids = parsing.table_set_field(body, required=False)
    starts_at_local = parsing.optional_local_time_field(body)
    party_size = parsing.optional_party_size_field(body)

    with db.transaction() as conn:
        record, restaurant = _load_owned(conn, user_id, reference)
        if record["status"] == "cancelled":
            raise reservation_cancelled()
        # The cutoff is measured against the current start time, before any change.
        if domain.is_past_cutoff(record, restaurant, now()):
            raise cutoff_passed()

        # An amendment that does not name tables keeps the ones it already holds —
        # which may be a pair, so the current set is carried over whole.
        target_ids = list(table_ids) if table_ids else list(record["table_ids"])
        target_local = starts_at_local or record["starts_at_local"]
        target_party = party_size if party_size is not None else int(record["party_size"])

        tables = domain.require_tables(restaurant, target_ids)
        domain.check_combination(restaurant, target_ids)
        sitting = domain.resolve_sitting(restaurant, target_local)
        domain.check_capacity(tables, target_party)

        occupancy = domain.load_occupancy(
            conn,
            restaurant,
            start_utc=sitting.starts_at_utc,
            end_utc=sitting.ends_at_utc,
            exclude_references=(reference,),
        )
        domain.check_free(
            occupancy,
            table_ids=target_ids,
            sitting=sitting,
            exclude_reference=reference,
        )

        # Release the old slot and take the new one in the same transaction.
        # Identity, owner and creation time never change.
        repo.update_reservation(
            conn,
            reference,
            {
                "table_id": target_ids[0],
                "party_size": target_party,
                **sitting.stored_fields(),
            },
        )
        repo.set_reservation_tables(conn, reference, target_ids)
        updated = repo.get_reservation_by_reference(conn, reference)
        return domain.reservation_body(updated)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# POST /reservation-moves
# --------------------------------------------------------------------------- #
def _parse_moves(body: dict) -> list[dict]:
    """Validate the shape of a move set. Structure problems are 422, per spec."""
    if "moves" not in body:
        raise validation_failed("'moves' is required")
    moves = body["moves"]
    if not isinstance(moves, list):
        raise validation_failed("'moves' must be an array of move objects")
    if not 1 <= len(moves) <= MAX_MOVES:
        raise validation_failed(
            f"'moves' must contain between 1 and {MAX_MOVES} entries"
        )

    seen: set[str] = set()
    items: list[dict] = []
    for index, entry in enumerate(moves):
        if not isinstance(entry, dict):
            raise validation_failed(f"moves[{index}] must be an object")
        reference = entry.get("reference")
        if not isinstance(reference, str) or not reference:
            raise validation_failed(f"moves[{index}].reference must be a non-empty string")
        if len(reference) > parsing.MAX_ID_LENGTH:
            raise validation_failed(
                f"moves[{index}].reference must be at most {parsing.MAX_ID_LENGTH} characters"
            )
        if reference in seen:
            raise validation_failed(
                f"moves[{index}].reference '{reference}' appears more than once"
            )
        seen.add(reference)
        items.append(
            {
                "reference": reference,
                # Ordinary PATCH fields, with the ordinary type rules.
                "table_ids": parsing.table_set_field(entry, required=False),
                "starts_at_local": parsing.optional_local_time_field(entry),
                "party_size": parsing.optional_party_size_field(entry),
            }
        )
    return items


def _check_projected_occupancy(
    conn: sqlite3.Connection, restaurant: domain.Restaurant, planned: Sequence[dict]
) -> None:
    """No table may belong to two resulting bookings at overlapping times.

    Judged against the projected state: bookings outside the batch as they are
    stored, bookings inside the batch at their new placements. That is what makes
    a two-booking swap possible while still refusing a genuine clash. A booking
    that holds a pair is checked under each of its tables, and two bookings clash
    when they share *any* table.
    """
    references = [plan["record"]["reference"] for plan in planned]
    start = min(plan["sitting"].starts_at_utc for plan in planned)
    end = max(plan["sitting"].ends_at_utc for plan in planned)
    static = domain.load_occupancy(
        conn, restaurant, start_utc=start, end_utc=end, exclude_references=references
    )

    for plan in planned:
        sitting = plan["sitting"]
        for table_id in plan["table_ids"]:
            clash = domain.find_conflict(
                static,
                table_id=table_id,
                starts_at_utc=sitting.starts_at_utc,
                ends_at_utc=sitting.ends_at_utc,
            )
            if clash is not None:
                raise table_unavailable(
                    f"Table '{table_id}' is already booked by reference "
                    f"'{clash['reference']}' at that time"
                )

    for index, left in enumerate(planned):
        for right in planned[index + 1 :]:
            shared = [t for t in left["table_ids"] if t in right["table_ids"]]
            if not shared:
                continue
            if overlaps(
                left["sitting"].starts_at_utc,
                left["sitting"].ends_at_utc,
                right["sitting"].starts_at_utc,
                right["sitting"].ends_at_utc,
            ):
                raise table_unavailable(
                    f"Moves for '{left['record']['reference']}' and "
                    f"'{right['record']['reference']}' would both occupy table "
                    f"'{shared[0]}' at overlapping times"
                )


def move_reservations(
    db: Database, *, user_id: str, key: str, body: dict, method: str, path: str
) -> tuple[int, dict]:
    """Change several bookings at once: every move commits, or nothing does."""
    with db.transaction() as conn:
        stored = idempotency.lookup(
            conn, user_id=user_id, key=key, method=method, path=path, body=body
        )
        if stored is not None:
            return 200, stored["response_body"]

        items = _parse_moves(body)

        resolved: list[tuple[dict, dict]] = []
        for item in items:
            record = repo.get_reservation_by_reference(conn, item["reference"])
            if record is None or record["user_id"] != user_id:
                raise not_found(f"No reservation with reference '{item['reference']}'")
            resolved.append((item, record))

        restaurant_ids = {record["restaurant_id"] for _item, record in resolved}
        if len(restaurant_ids) > 1:
            raise validation_failed(
                "Every booking in one move request must belong to the same restaurant"
            )
        restaurant = domain.require_restaurant(conn, resolved[0][1]["restaurant_id"])

        # Non-occupancy problems, in input order, cutoff first for each booking.
        planned: list[dict] = []
        for item, record in resolved:
            if record["status"] == "cancelled":
                raise reservation_cancelled()
            if domain.is_past_cutoff(record, restaurant, now()):
                raise cutoff_passed()

            table_ids = (
                list(item["table_ids"]) if item["table_ids"] else list(record["table_ids"])
            )
            starts_at_local = item["starts_at_local"] or record["starts_at_local"]
            party_size = (
                item["party_size"]
                if item["party_size"] is not None
                else int(record["party_size"])
            )
            tables = domain.require_tables(restaurant, table_ids)
            domain.check_combination(restaurant, table_ids)
            sitting = domain.resolve_sitting(restaurant, starts_at_local)
            domain.check_capacity(tables, party_size)
            planned.append(
                {
                    "item": item,
                    "record": record,
                    "table_ids": table_ids,
                    "party_size": party_size,
                    "sitting": sitting,
                }
            )

        _check_projected_occupancy(conn, restaurant, planned)

        responses: list[dict] = []
        for plan in planned:
            reference = plan["record"]["reference"]
            repo.update_reservation(
                conn,
                reference,
                {
                    "table_id": plan["table_ids"][0],
                    "party_size": plan["party_size"],
                    **plan["sitting"].stored_fields(),
                },
            )
            repo.set_reservation_tables(conn, reference, plan["table_ids"])
            updated = repo.get_reservation_by_reference(conn, reference)
            responses.append(domain.reservation_body(updated))  # type: ignore[arg-type]

        response = {"reservations": responses}
        idempotency.record(
            conn,
            user_id=user_id,
            key=key,
            method=method,
            path=path,
            body=body,
            status_code=201,
            response_body=response,
        )
        return 201, response


# --------------------------------------------------------------------------- #
# auth-facing helpers used by the routes
# --------------------------------------------------------------------------- #
def signup(db: Database, body: dict) -> dict:
    email = parsing.string_field(body, "email", max_length=320)
    password = body.get("password")
    display_name = body.get("display_name")
    if not isinstance(password, str):
        if password is None:
            raise validation_failed("'password' is required")
        raise malformed_request("'password' must be a string")
    if display_name is None:
        raise validation_failed("'display_name' is required")
    if not isinstance(display_name, str):
        raise malformed_request("'display_name' must be a string")

    from . import auth

    auth.validate_credentials(email=email, password=password, display_name=display_name)
    with db.transaction() as conn:
        return auth.signup(
            conn, email=email, password=password, display_name=display_name
        )


def login(db: Database, body: dict) -> dict:
    email = body.get("email")
    password = body.get("password")
    if email is None:
        raise validation_failed("'email' is required")
    if not isinstance(email, str):
        raise malformed_request("'email' must be a string")
    if password is None:
        raise validation_failed("'password' is required")
    if not isinstance(password, str):
        raise malformed_request("'password' must be a string")

    from . import auth

    # A failed login raises inside the transaction, which rolls back cleanly.
    with db.transaction() as conn:
        return auth.login(conn, email=email, password=password)


def public_restaurants(db: Database) -> dict:
    with db.read() as conn:
        records = repo.list_restaurants(conn)
        return {
            "restaurants": [
                {"id": r["id"], "name": r["name"], "timezone": r["timezone"]}
                for r in records
            ]
        }


def public_restaurant(db: Database, restaurant_id: str) -> dict:
    with db.read() as conn:
        restaurant = domain.require_restaurant(conn, restaurant_id)
        return restaurant.detail()


def public_availability(db: Database, params: dict) -> dict:
    restaurant_id = parsing.query_string(params, "restaurant_id")
    date_value = parsing.query_string(params, "date")
    party_size = parsing.query_int(params, "party_size")

    from .tztime import parse_date

    day = parse_date(date_value)  # type: ignore[arg-type]
    if day is None:
        raise validation_failed("'date' must be a local calendar date YYYY-MM-DD")
    if party_size is not None and party_size < 1:
        raise validation_failed("'party_size' must be at least 1")

    with db.read() as conn:
        restaurant = domain.require_restaurant(conn, restaurant_id)  # type: ignore[arg-type]
        return domain.availability(conn, restaurant, day, int(party_size or 1))
