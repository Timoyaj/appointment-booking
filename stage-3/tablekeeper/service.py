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
* cancel and amend are refused once the **accepted** cancellation cutoff has
  passed (409 ``cutoff_passed``), and an amendment of a cancelled booking is
  409 ``reservation_cancelled``;
* an amendment whose ``expected_revision`` is not the booking's current one is
  409 ``stale_revision``, before the cutoff and before any field is validated;
* an amendment is validated against the policy that applies to its resulting
  start date, and a real change replaces the accepted terms and the end time
  together and adds one revision and one entry to the booking's record, while a
  change that changes nothing does none of those;
* for a batch of moves, non-occupancy errors take precedence in input order,
  with each booking's cutoff error preceding its other problems, and occupancy
  clashes are judged against the state the batch would produce.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from . import domain, history, idempotency, parsing, policies, repo
from .clock import now
from .db import Database
from .errors import (
    cutoff_passed,
    forbidden,
    malformed_request,
    not_found,
    reservation_cancelled,
    stale_revision,
    table_unavailable,
    validation_failed,
)
from .tztime import overlaps, rfc3339

MAX_MOVES = 8


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _load_owned(
    conn: sqlite3.Connection, user_id: str | None, reference: str
) -> tuple[dict, domain.Restaurant]:
    """A reservation by reference, or 404 — including when it belongs to someone else.

    Another diner's booking is indistinguishable from a booking that does not
    exist, so existence is never leaked. A caller with no identity at all gets the
    same answer, which is what the record and decision endpoints report instead of
    the general 401.
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
    domain.require_tables(restaurant, table_ids)
    domain.check_combination(restaurant, table_ids)
    # The booking's own local start date chooses the policy that decides it — its
    # grid, its service windows, its sitting length and its capacities.
    rules = domain.rules_for(conn, restaurant, starts_at_local)
    sitting = domain.resolve_sitting(restaurant, rules, starts_at_local)
    domain.check_capacity(rules, table_ids, party_size)

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

    terms = rules.terms()
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
        # A booking starts at revision 1 under the terms it was decided with.
        "revision": 1,
        "accepted_terms": terms,
        **sitting.stored_fields(),
    }
    repo.insert_reservation(conn, record)
    history.record(
        conn,
        reference=record["reference"],
        event=history.CREATED,
        changes=history.creation_changes(
            table_ids, sitting.starts_at_local, party_size, restaurant.combinable
        ),
        revision=1,
        accepted_terms=terms,
        at=record["created_at"],
    )
    repo.bump_restaurant_revision(conn, restaurant_id)
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
        # Cancelling is a real change: one revision, one entry, and the terms the
        # diner accepted stay exactly as they were. Cancelling twice is not.
        revision = int(record["revision"]) + 1
        repo.update_reservation(
            conn, reference, {"status": "cancelled", "revision": revision}
        )
        history.record(
            conn,
            reference=reference,
            event=history.CANCELLED,
            changes=[],
            revision=revision,
            accepted_terms=record["accepted_terms"],
        )
        repo.bump_restaurant_revision(conn, record["restaurant_id"])
        updated = repo.get_reservation_by_reference(conn, reference)
        return domain.reservation_body(updated)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# PATCH /reservations/{reference}
# --------------------------------------------------------------------------- #
def amend_reservation(db: Database, user_id: str, reference: str, body: dict) -> dict:
    table_ids = parsing.table_set_field(body, required=False)
    starts_at_local = parsing.optional_local_time_field(body)
    party_size = parsing.optional_party_size_field(body)
    expected_revision = parsing.expected_revision_field(body)

    with db.transaction() as conn:
        record, restaurant = _load_owned(conn, user_id, reference)
        # A revision the client did not expect is refused before anything else is
        # judged: the change it describes is not the change the client meant to make.
        if expected_revision is not None and expected_revision != int(record["revision"]):
            raise stale_revision(
                f"Reservation '{reference}' is at revision {record['revision']}, "
                f"not {expected_revision}"
            )
        if record["status"] == "cancelled":
            raise reservation_cancelled()
        # The cutoff is measured against the current start time, before any change,
        # and it is the cutoff the diner accepted when this booking was decided.
        if domain.is_past_cutoff(record, restaurant, now()):
            raise cutoff_passed()

        # An amendment that does not name tables keeps the ones it already holds —
        # which may be a pair, so the current set is carried over whole.
        target_ids = list(table_ids) if table_ids else list(record["table_ids"])
        target_local = starts_at_local or record["starts_at_local"]
        target_party = party_size if party_size is not None else int(record["party_size"])

        domain.require_tables(restaurant, target_ids)
        domain.check_combination(restaurant, target_ids)
        # Every resulting field is validated against the policy that applies to the
        # resulting start date — not the policy the booking was made under, and not
        # the restaurant's seeded configuration.
        rules = domain.rules_for(conn, restaurant, target_local)
        sitting = domain.resolve_sitting(restaurant, rules, target_local)
        domain.check_capacity(rules, target_ids, target_party)

        occupancy = domain.load_occupancy(
            conn, restaurant,
            start_utc=sitting.starts_at_utc,
            end_utc=sitting.ends_at_utc,
            exclude_references=(reference,),
        )
        domain.check_free(
            occupancy, table_ids=target_ids, sitting=sitting, exclude_reference=reference
        )

        before = {
            "table_ids": record["table_ids"],
            "starts_at_local": record["starts_at_local"],
            "party_size": int(record["party_size"]),
        }
        after = {
            "table_ids": target_ids,
            "starts_at_local": sitting.starts_at_local,
            "party_size": target_party,
        }
        changes = history.changes_between(before, after, restaurant.combinable)
        if not changes:
            # A no-op amendment keeps its terms, its end time and its revision, and
            # records nothing at all. It still required a confirmed, editable
            # booking, so those refusals above stand.
            return domain.reservation_body(record)

        # The stored order of an unchanged set is left alone: a reversed pair names
        # the same seating and is no reason to rewrite the booking.
        moved_tables = set(target_ids) != set(record["table_ids"])
        stored_ids = list(target_ids) if moved_tables else list(record["table_ids"])
        revision = int(record["revision"]) + 1
        terms = rules.terms()
        # Terms and end time are replaced together with the change that earned them.
        repo.update_reservation(
            conn,
            reference,
            {
                "table_id": stored_ids[0],
                "party_size": target_party,
                "revision": revision,
                "accepted_terms": terms,
                **sitting.stored_fields(),
            },
        )
        if moved_tables:
            repo.set_reservation_tables(conn, reference, stored_ids)
        history.record(
            conn, reference=reference, event=history.CHANGED, changes=changes,
            revision=revision, accepted_terms=terms,
        )
        repo.bump_restaurant_revision(conn, record["restaurant_id"])
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
                "expected_revision": parsing.expected_revision_field(entry),
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
            # Each real change uses the individual amendment's semantics, in the same
            # order: the revision the client expected, then the accepted cutoff
            # against the current start, then the resulting fields against the
            # policy that applies to the resulting date.
            expected = item["expected_revision"]
            if expected is not None and expected != int(record["revision"]):
                raise stale_revision(
                    f"Reservation '{record['reference']}' is at revision "
                    f"{record['revision']}, not {expected}"
                )
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
            domain.require_tables(restaurant, table_ids)
            domain.check_combination(restaurant, table_ids)
            rules = domain.rules_for(conn, restaurant, starts_at_local)
            sitting = domain.resolve_sitting(restaurant, rules, starts_at_local)
            domain.check_capacity(rules, table_ids, party_size)
            planned.append(
                {
                    "item": item,
                    "record": record,
                    "table_ids": table_ids,
                    "party_size": party_size,
                    "sitting": sitting,
                    "rules": rules,
                }
            )

        _check_projected_occupancy(conn, restaurant, planned)

        responses: list[dict] = []
        changed_any = False
        for plan in planned:
            record = plan["record"]
            reference = record["reference"]
            changes = history.changes_between(
                {
                    "table_ids": record["table_ids"],
                    "starts_at_local": record["starts_at_local"],
                    "party_size": int(record["party_size"]),
                },
                {
                    "table_ids": plan["table_ids"],
                    "starts_at_local": plan["sitting"].starts_at_local,
                    "party_size": plan["party_size"],
                },
                restaurant.combinable,
            )
            if not changes:
                # Listed but unchanged: it keeps its terms, its revision and its
                # record, and still occupied its table for the checks above.
                responses.append(domain.reservation_body(record))
                continue

            moved_tables = set(plan["table_ids"]) != set(record["table_ids"])
            stored_ids = (
                list(plan["table_ids"]) if moved_tables else list(record["table_ids"])
            )
            revision = int(record["revision"]) + 1
            terms = plan["rules"].terms()
            repo.update_reservation(
                conn,
                reference,
                {
                    "table_id": stored_ids[0],
                    "party_size": plan["party_size"],
                    "revision": revision,
                    "accepted_terms": terms,
                    **plan["sitting"].stored_fields(),
                },
            )
            if moved_tables:
                repo.set_reservation_tables(conn, reference, stored_ids)
            history.record(
                conn, reference=reference, event=history.CHANGED, changes=changes,
                revision=revision, accepted_terms=terms,
            )
            changed_any = True
            updated = repo.get_reservation_by_reference(conn, reference)
            responses.append(domain.reservation_body(updated))  # type: ignore[arg-type]

        # One increment for the whole batch, and none at all if it changed nothing.
        if changed_any:
            repo.bump_restaurant_revision(conn, restaurant.id)

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
    explain = _explain_flag(params)

    from .tztime import parse_date

    day = parse_date(date_value)  # type: ignore[arg-type]
    if day is None:
        raise validation_failed("'date' must be a local calendar date YYYY-MM-DD")
    if party_size is not None and party_size < 1:
        raise validation_failed("'party_size' must be at least 1")

    with db.read() as conn:
        restaurant = domain.require_restaurant(conn, restaurant_id)  # type: ignore[arg-type]
        return domain.availability(
            conn, restaurant, day, int(party_size or 1), explain=explain
        )


def _explain_flag(params: dict) -> bool:
    """`explain` is optional, and its only accepted value is the string `true`.

    Anything else — `false`, `1`, the empty string — is a refusal rather than a
    quiet no, because a client that asked for an explanation and got none would
    read the answer as "nothing is wrong".
    """
    if "explain" not in params:
        return False
    if params["explain"] != "true":
        raise validation_failed("'explain' accepts only the value 'true'")
    return True


# --------------------------------------------------------------------------- #
# policies
# --------------------------------------------------------------------------- #
def publish_policy(
    db: Database, *, user_id: str, key: str, restaurant_id: str, body: dict,
    method: str, path: str,
) -> tuple[int, dict]:
    """Publish a complete policy. Idempotent under ``key``; versions are never reused."""
    with db.transaction() as conn:
        restaurant = domain.require_restaurant(conn, restaurant_id)
        # Only this restaurant's managers may publish, and being one grants nothing
        # else: another diner's bookings stay as private as they were.
        if not restaurant.is_manager(user_id):
            raise forbidden("Only a manager of this restaurant may publish its policies")

        stored = idempotency.lookup(
            conn, user_id=user_id, key=key, method=method, path=path, body=body
        )
        if stored is not None:
            return 200, stored["response_body"]

        policy = policies.validate(body, restaurant)
        # Allocated only now: a refused policy, and a replayed one, take no version.
        version = repo.next_policy_version(conn, restaurant_id)
        record = {
            **policy,
            "restaurant_id": restaurant_id,
            "policy_version": version,
            "created_at": rfc3339(now()),
        }
        repo.insert_policy(conn, record)
        repo.bump_restaurant_revision(conn, restaurant_id)

        response = policies.published(record)
        idempotency.record(
            conn, user_id=user_id, key=key, method=method, path=path, body=body,
            status_code=201, response_body=response,
        )
        return 201, response


def list_policies(db: Database, restaurant_id: str) -> dict:
    """Every published policy in publication order. Policy 0 is not among them: it
    was never published, it is the restaurant's own configuration."""
    with db.read() as conn:
        restaurant = domain.require_restaurant(conn, restaurant_id)
        return {
            "policies": [
                policies.published(policy)
                for policy in repo.policies_for(conn, restaurant.id)
            ]
        }


# --------------------------------------------------------------------------- #
# a reservation's own record, and the decision that produced it
# --------------------------------------------------------------------------- #
def reservation_history(db: Database, user_id: str | None, reference: str) -> dict:
    with db.read() as conn:
        record, _restaurant = _load_owned(conn, user_id, reference)
        return history.ledger(conn, record["reference"])


def reservation_decision(db: Database, user_id: str | None, reference: str) -> dict:
    """The terms the booking currently holds, including after cancellation."""
    with db.read() as conn:
        record, _restaurant = _load_owned(conn, user_id, reference)
        return {
            "reference": record["reference"],
            "revision": int(record["revision"]),
            "accepted_terms": record["accepted_terms"],
        }
