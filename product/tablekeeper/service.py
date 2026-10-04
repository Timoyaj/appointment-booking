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
from datetime import timedelta, timezone

from . import domain, history, idempotency, parsing, payments, planning, policies, repo
from . import tztime
from . import notifications
from .clock import now
from .db import Database
from .errors import (
    ApiError,
    already_in_series,
    card_declined,
    cutoff_passed,
    email_not_verified,
    forbidden,
    malformed_request,
    no_feasible_plan,
    not_found,
    payment_required,
    plan_already_applied,
    reservation_cancelled,
    reservation_not_editable,
    stale_plan,
    stale_revision,
    table_unavailable,
    validation_failed,
)
from . import recovery
from .tztime import overlaps, parse_instant, parse_local, rfc3339

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
    count_restaurant_write: bool = True,
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
    # A table an operator has taken out of service cannot be booked into, however
    # free it looks: this is checked before occupancy so that the refusal names the
    # closure rather than whatever happens to be sitting nearby.
    domain.check_not_closed(
        domain.load_closures(
            conn, restaurant,
            start_utc=sitting.starts_at_utc, end_utc=sitting.ends_at_utc,
        ),
        table_ids=table_ids,
        sitting=sitting,
    )

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
    if count_restaurant_write:
        repo.bump_restaurant_revision(conn, restaurant_id)
    return repo.get_reservation_by_reference(conn, record["reference"])  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# POST /reservations
# --------------------------------------------------------------------------- #
def create_reservation(
    db: Database, *, user_id: str, key: str, body: dict, method: str, path: str,
    provider=None,
) -> tuple[int, dict]:
    """Book a table. Idempotent under ``key``; returns (status, body).

    When the restaurant asks for a deposit on this party size, the hold is taken
    as part of the same transaction — after the table is placed and before the
    booking is written into the retry ledger — so a declined card leaves no
    booking behind. The refusal is then recorded outside that transaction, which
    rolls back: a decline written inside it would be undone by the rollback it
    describes, exactly like a failed sign-in.
    """
    # Named before the transaction so the refusal handler below can read what the
    # request was about even when validation is what raised.
    restaurant_id: str | None = None
    party_size: int | None = None
    try:
        with db.transaction() as conn:
            stored = idempotency.lookup(
                conn, user_id=user_id, key=key, method=method, path=path, body=body
            )
            if stored is not None:
                return 200, stored["response_body"]

            # A restaurant that has asked for confirmed addresses will not seat a
            # diner whose address is not confirmed yet. Off unless the deployment
            # turns it on, because an address that cannot receive mail would
            # otherwise lock a paying diner out of a restaurant they can walk into.
            if recovery.require_verified_email() and not recovery.is_verified(conn, user_id):
                raise email_not_verified(
                    "Confirm your email address before booking: check your inbox for "
                    "the message we sent when you signed up"
                )

            # Field validation happens after idempotency resolution, as specified:
            # a used key with a different body is a conflict even when that body is
            # invalid, so the key is judged before the fields are.
            restaurant_id = parsing.id_field(body, "restaurant_id")
            table_ids = parsing.table_set_field(body)
            starts_at_local = parsing.local_time_field(body)
            party_size = parsing.party_size_field(body)
            payment_method_id = parsing.payment_method_field(body)

            record = _place(
                conn,
                user_id=user_id,
                restaurant_id=restaurant_id,  # type: ignore[arg-type]
                table_ids=table_ids,  # type: ignore[arg-type]
                starts_at_local=starts_at_local,
                party_size=party_size,  # type: ignore[arg-type]
            )
            restaurant = domain.require_restaurant(conn, record["restaurant_id"])
            _take_deposit(
                conn,
                provider=provider or payments.FakeProvider(),
                restaurant=restaurant,
                user_id=user_id,
                record=record,
                payment_method_id=payment_method_id,
            )
            response = domain.reservation_body(record)
            _notify(
                conn,
                restaurant=restaurant,
                kind=notifications.CONFIRMED,
                record=record,
            )
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
    except ApiError as error:
        if error.status == 402 and restaurant_id is not None:
            _record_payment_failure(
                db,
                restaurant_id=restaurant_id,
                user_id=user_id,
                outcome=(
                    "no_payment_method" if error.code == "payment_required" else "declined"
                ),
                amount=payments.deposit_for(
                    _settings_of(db, restaurant_id), int(party_size or 0)
                ),
                reason=error.message,
            )
        raise


def _settings_of(db: Database, restaurant_id: str) -> dict | None:
    with db.read() as conn:
        return repo.payment_settings_for(conn, restaurant_id)


def _record_payment_failure(
    db: Database, *, restaurant_id: str, user_id: str, amount: int, reason: str,
    outcome: str,
) -> None:
    """Note a deposit that could not be taken, in a transaction of its own.

    The booking was rolled back, so there is nothing else left to say that a card
    failed on a Friday — and that is a thing a restaurant wants to know.
    """
    with db.transaction() as conn:
        repo.insert_payment_attempt(
            conn,
            {
                "restaurant_id": restaurant_id,
                "reference": None,
                "user_id": user_id,
                "amount_cents": amount,
                "outcome": outcome,
                "reason": reason,
                "created_at": rfc3339(now()),
            },
        )


def _refuse_uneditable(record: dict) -> None:
    """Refuse a booking that is no longer the diner's to change.

    A cancelled booking is nobody's, and a no-show is a fact about the past: the
    party did not come, so there is nothing left to amend, move or adopt. Both are
    409s, with different codes, because a client that treated them the same would
    tell a diner their booking was cancelled when it was not.
    """
    if record["status"] == "cancelled":
        raise reservation_cancelled()
    if record["status"] == "no_show":
        raise reservation_not_editable(
            "That booking was recorded as a no-show and can no longer be changed"
        )


def _take_deposit(
    conn: sqlite3.Connection,
    *,
    provider,
    restaurant: domain.Restaurant,
    user_id: str,
    record: dict,
    payment_method_id: str | None,
) -> dict | None:
    """Hold the deposit this booking owes, inside the booking's own transaction.

    Called after the table is placed and before the booking is acknowledged. A
    hold that cannot be taken **raises**, which rolls the booking back: a table is
    not held by somebody whose card was declined, and the diner is told why before
    they think they have a table.

    Returns the payment block the response carries, or None when this restaurant
    asks for no deposit.
    """
    settings = repo.payment_settings_for(conn, restaurant.id)
    amount = payments.deposit_for(settings, int(record["party_size"]))
    if amount <= 0:
        return None
    if not payment_method_id:
        raise payment_required(
            f"This restaurant holds "
            f"{payments.format_money(amount, settings['currency'])} for a party of "
            f"{record['party_size']} or more; send a 'payment_method_id' to hold "
            f"the table"
        )
    result = provider.authorize(
        amount_cents=amount,
        currency=settings["currency"],
        payment_method_id=payment_method_id,
        # Keyed to the booking attempt, so a retried request holds once.
        idempotency_key=f"booking:{record['reference']}",
    )
    if not result.ok:
        raise card_declined(result.error or "The card was declined")

    created_at = rfc3339(now())
    intent_id = payments.new_intent_id()
    repo.insert_payment_intent(
        conn,
        {
            "id": intent_id,
            "restaurant_id": restaurant.id,
            "reference": record["reference"],
            "user_id": user_id,
            "provider": provider.name,
            "provider_ref": result.provider_ref,
            "currency": settings["currency"],
            "amount_cents": amount,
            "captured_cents": 0,
            "status": payments.AUTHORIZED,
            "created_at": created_at,
            "updated_at": created_at,
        },
    )
    payments.record_event(
        conn, intent_id=intent_id, kind=payments.AUTHORIZED, amount_cents=amount,
        detail={"provider_ref": result.provider_ref},
    )
    return {
        "currency": settings["currency"],
        "amount_cents": amount,
        "status": payments.AUTHORIZED,
        "intent_id": intent_id,
    }


def _release_deposit(
    conn: sqlite3.Connection, *, provider, reference: str, reason: str
) -> dict | None:
    """Give back anything held on a booking. Used on cancel and on completion.

    A hold that the provider refuses to release is left `authorized` rather than
    marked released: the ledger must not claim money was given back when it was
    not, and a manager can see it and ask the provider about it.
    """
    intent = repo.intent_for_reference(conn, reference)
    if intent is None:
        return None
    result = provider.release(provider_ref=intent["provider_ref"])
    if not result.ok:
        return None
    repo.update_intent_status(
        conn, intent["id"], status=payments.RELEASED,
        captured_cents=int(intent["captured_cents"]), updated_at=rfc3339(now()),
    )
    payments.record_event(
        conn, intent_id=intent["id"], kind=payments.RELEASED,
        amount_cents=int(intent["amount_cents"]), detail={"reason": reason},
    )
    return {"intent_id": intent["id"], "status": payments.RELEASED}


def _capture_deposit(
    conn: sqlite3.Connection, *, provider, reference: str, reason: str
) -> dict | None:
    """Keep the deposit: the party did not turn up, so the hold becomes a charge."""
    intent = repo.intent_for_reference(conn, reference)
    if intent is None:
        return None
    amount = int(intent["amount_cents"])
    result = provider.capture(provider_ref=intent["provider_ref"], amount_cents=amount)
    if not result.ok:
        raise card_declined(result.error or "The hold could not be captured")
    repo.update_intent_status(
        conn, intent["id"], status=payments.CAPTURED, captured_cents=amount,
        updated_at=rfc3339(now()),
    )
    payments.record_event(
        conn, intent_id=intent["id"], kind=payments.CAPTURED, amount_cents=amount,
        detail={"reason": reason},
    )
    return {"intent_id": intent["id"], "status": payments.CAPTURED, "captured_cents": amount}


def _payment_block(conn: sqlite3.Connection, reference: str) -> dict | None:
    """What a booking response says about money, when there is anything to say."""
    intent = repo.intent_for_reference(conn, reference)
    if intent is None:
        return None
    return {
        "currency": intent["currency"],
        "amount_cents": int(intent["amount_cents"]),
        "status": intent["status"],
        "intent_id": intent["id"],
    }


def _notify(
    conn: sqlite3.Connection,
    *,
    restaurant: domain.Restaurant,
    kind: str,
    record: dict,
    extra: dict | None = None,
) -> None:
    """Write the diner a message about a booking, inside the caller's transaction.

    Called from the write paths, never from a read, and always *after* the change
    it describes: a cancelled booking's notice cannot exist for a booking that is
    still confirmed. A diner with no email address on file is simply not written
    to — the booking is the promise, the message is the courtesy — so nothing here
    can fail a booking.
    """
    notifications.enqueue(
        conn,
        restaurant_id=restaurant.id,
        user_id=record["user_id"],
        reference=record.get("reference"),
        kind=kind,
        restaurant_name=restaurant.name,
        record=record,
        labels={table["id"]: table["label"] for table in restaurant.tables},
        extra=extra,
    )


def _load_for_manager(
    conn: sqlite3.Connection, *, user_id: str, reference: str
) -> tuple[dict, domain.Restaurant]:
    """A booking for somebody who runs its restaurant, or 404 for everybody else.

    Not a 403: a diner guessing at references must not be able to tell a booking
    that exists from one that does not, and "you may not touch this" is an answer
    about existence. Somebody who does work here gets the booking; anybody else
    gets the same 404 an unknown reference gets.
    """
    from . import onboarding

    record = repo.get_reservation_by_reference(conn, reference)
    if record is None:
        raise not_found(f"No reservation with reference '{reference}'")
    restaurant = domain.require_restaurant(conn, record["restaurant_id"])
    role = onboarding.role_in(
        conn, user_id=user_id, restaurant_id=record["restaurant_id"]
    )
    if role not in (onboarding.OWNER, onboarding.MANAGER):
        raise not_found(f"No reservation with reference '{reference}'")
    return record, restaurant


def mark_no_show(
    db: Database, *, user_id: str, reference: str, provider=None
) -> dict:
    """Record that the party did not come, and keep the deposit they left.

    The deposit is the point of the whole feature: a table for eight on a Friday
    that nobody turns up for is money the restaurant has already lost, and the hold
    taken at booking is what makes some of it back.
    """
    with db.transaction() as conn:
        record, restaurant = _load_for_manager(
            conn, user_id=user_id, reference=reference
        )
        if record["status"] == "no_show":
            return domain.reservation_body(record)
        if record["status"] == "cancelled":
            raise reservation_cancelled()

        captured = _capture_deposit(
            conn, provider=provider or payments.FakeProvider(), reference=reference,
            reason="no show",
        )
        repo.update_reservation(conn, reference, {"status": "no_show"})
        history.record(
            conn,
            reference=reference,
            event=history.NO_SHOW,
            changes=[{"field": "status", "from": record["status"], "to": "no_show"}],
            revision=int(record["revision"]),
            accepted_terms=record["accepted_terms"],
        )
        repo.bump_restaurant_revision(conn, record["restaurant_id"])
        updated = repo.get_reservation_by_reference(conn, reference)
        _notify(
            conn,
            restaurant=restaurant,
            kind=notifications.NO_SHOW_CHARGE,
            record=updated,  # type: ignore[arg-type]
            extra={
                "captured_cents": (captured or {}).get("captured_cents", 0),
                "currency": (
                    repo.get_payment_intent(conn, captured["intent_id"])["currency"]
                    if captured
                    else None
                ),
            },
        )
        return domain.reservation_body(updated)  # type: ignore[arg-type]


def mark_complete(
    db: Database, *, user_id: str, reference: str, provider=None
) -> dict:
    """The party came. Release the hold — the visit is paid for at the table.

    The booking keeps its status: it happened, which is what `confirmed` has meant
    all along, and changing it would free a table the party is still sitting at.
    """
    with db.transaction() as conn:
        record, _restaurant = _load_for_manager(
            conn, user_id=user_id, reference=reference
        )
        if record["status"] != "confirmed":
            raise reservation_not_editable(
                "Only a confirmed booking can be marked as visited"
            )
        released = _release_deposit(
            conn, provider=provider or payments.FakeProvider(), reference=reference,
            reason="the party came",
        )
        repo.insert_audit(
            conn,
            restaurant_id=record["restaurant_id"],
            user_id=user_id,
            action="booking_completed",
            detail=f'{{"reference": "{reference}"}}',
            created_at=rfc3339(now()),
        )
        body = domain.reservation_body(record)
        if released:
            body["deposit_released"] = True
        return body


def reservation_payments(
    db: Database, *, user_id: str, reference: str
) -> dict:
    """What money moved on a booking: its holds and the ledger under them.

    Readable by the diner who made it and by the people who run the restaurant —
    the only audience for whom any of this is their business.
    """
    from . import onboarding

    with db.read() as conn:
        record = repo.get_reservation_by_reference(conn, reference)
        if record is None:
            raise not_found(f"No reservation with reference '{reference}'")
        mine = record["user_id"] == user_id
        staff = onboarding.role_in(
            conn, user_id=user_id, restaurant_id=record["restaurant_id"]
        ) is not None
        if not mine and not staff:
            raise not_found(f"No reservation with reference '{reference}'")
        intents = repo.intents_for_reference(conn, reference)
        return {
            "reference": reference,
            "currency": intents[0]["currency"] if intents else None,
            "intents": [
                {
                    "intent_id": intent["id"],
                    "status": intent["status"],
                    "amount_cents": int(intent["amount_cents"]),
                    "captured_cents": int(intent["captured_cents"]),
                    "created_at": intent["created_at"],
                    "events": repo.payment_events_for(conn, intent["id"]),
                }
                for intent in intents
            ],
        }


def get_payment_settings(db: Database, *, user_id: str, restaurant_id: str) -> dict:
    with db.read() as conn:
        _require_staff(conn, user_id=user_id, restaurant_id=restaurant_id)
        return payments.describe(repo.payment_settings_for(conn, restaurant_id))


def put_payment_settings(
    db: Database, *, user_id: str, restaurant_id: str, body: dict
) -> dict:
    """Publish a deposit. Manager work, audited like every other manager write."""
    from . import onboarding

    with db.transaction() as conn:
        domain.require_restaurant(conn, restaurant_id)
        if not onboarding.is_manager(conn, user_id=user_id, restaurant_id=restaurant_id):
            raise forbidden("Only a manager of this restaurant may set its deposits")
        restaurant = domain.require_restaurant(conn, restaurant_id)
        settings = payments.validate_settings(body, restaurant.tables)
        at = rfc3339(now())
        repo.upsert_payment_settings(
            conn,
            restaurant_id=restaurant_id,
            currency=settings["currency"],
            deposit_per_seat_cents=settings["deposit_per_seat_cents"],
            deposit_from_party_size=settings["deposit_from_party_size"],
            updated_at=at,
            updated_by=user_id,
        )
        repo.insert_audit(
            conn, restaurant_id=restaurant_id, user_id=user_id,
            action="deposit_published",
            detail=(
                f'{{"per_seat_cents": {settings["deposit_per_seat_cents"]}, '
                f'"from_party_size": {settings["deposit_from_party_size"]}, '
                f'"currency": "{settings["currency"]}"}}'
            ),
            created_at=at,
        )
        repo.bump_restaurant_revision(conn, restaurant_id)
        return payments.describe(repo.payment_settings_for(conn, restaurant_id))


def clear_payment_settings(db: Database, *, user_id: str, restaurant_id: str) -> dict:
    from . import onboarding

    with db.transaction() as conn:
        domain.require_restaurant(conn, restaurant_id)
        if not onboarding.is_manager(conn, user_id=user_id, restaurant_id=restaurant_id):
            raise forbidden("Only a manager of this restaurant may set its deposits")
        repo.clear_payment_settings(conn, restaurant_id)
        repo.insert_audit(
            conn, restaurant_id=restaurant_id, user_id=user_id,
            action="deposits_stopped", detail="{}", created_at=rfc3339(now()),
        )
        repo.bump_restaurant_revision(conn, restaurant_id)
        return {"deposits": False}


def _note_series_change(conn: sqlite3.Connection, reference: str, *, exception: bool) -> None:
    """One increment for the agreement a booking belongs to, if it belongs to one.

    A diner's own amendment of an occurrence marks it an exception permanently: the
    sitting has been taken out of the pattern, and putting the date back does not
    put it in again. A cancellation counts, but is not an exception.
    """
    agreement = repo.series_of_reservation(conn, reference)
    if agreement is None:
        return
    if exception:
        repo.mark_exception(conn, reference)
    repo.bump_series_revision(conn, agreement["id"])


def _shift_local(starts_at_local: str, days: int) -> str:
    """The same local clock time on a later calendar date.

    Adding days to a bare local time is exactly what an agreement asks for: the
    calendar date moves and the clock time does not, so a weekly series keeps its
    hour across a daylight-saving change and then meets the same rules as any other
    booking on the date it lands on — including a date where that hour does not
    exist, which refuses the whole adoption.
    """
    naive = parse_local(starts_at_local)
    if naive is None:  # pragma: no cover - the anchor was placed through the API
        raise validation_failed("'starts_at_local' is not a bare local time")
    return (naive + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


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
def cancel_reservation(db: Database, user_id: str, reference: str, *, provider=None) -> dict:
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
        # Cancelling one occurrence of an agreement is the agreement's business, so
        # it counts once there — but it is not a diner reshaping a sitting, so it
        # does not make that occurrence an exception.
        _note_series_change(conn, reference, exception=False)
        repo.bump_restaurant_revision(conn, record["restaurant_id"])
        updated = repo.get_reservation_by_reference(conn, reference)
        # A diner who cancels inside the cutoff gets their deposit back: the
        # table is free and somebody else can have it, so there is nothing to
        # keep. This runs before the message, so the message can say so.
        released = _release_deposit(
            conn, provider=provider or payments.FakeProvider(), reference=reference,
            reason="cancelled inside the cutoff",
        )
        _notify(
            conn,
            restaurant=restaurant,
            kind=notifications.CANCELLED,
            record=updated,  # type: ignore[arg-type]
            extra={"deposit_released": bool(released)},
        )
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
        _refuse_uneditable(record)
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
        domain.check_not_closed(
            domain.load_closures(
                conn, restaurant,
                start_utc=sitting.starts_at_utc, end_utc=sitting.ends_at_utc,
            ),
            table_ids=target_ids,
            sitting=sitting,
        )

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
        # An individual amendment of one occurrence is the diner taking that sitting
        # out of the pattern, permanently.
        _note_series_change(conn, reference, exception=True)
        repo.bump_restaurant_revision(conn, record["restaurant_id"])
        updated = repo.get_reservation_by_reference(conn, reference)
        _notify(
            conn,
            restaurant=restaurant,
            kind=notifications.CHANGED,
            record=updated,  # type: ignore[arg-type]
        )
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
    closures = domain.load_closures(conn, restaurant, start_utc=start, end_utc=end)

    for plan in planned:
        sitting = plan["sitting"]
        # An applied closure is part of the projected state exactly like a booking
        # that is not moving: the resulting seating has to fit around both.
        domain.check_not_closed(closures, table_ids=plan["table_ids"], sitting=sitting)
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
            _refuse_uneditable(record)
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
        affected_series: set[str] = set()
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
            agreement = repo.series_of_reservation(conn, reference)
            if agreement is not None:
                affected_series.add(agreement["id"])
                repo.mark_exception(conn, reference)
            updated = repo.get_reservation_by_reference(conn, reference)
            # Only a booking this batch actually moved hears about it: a booking
            # listed in the batch but left where it was has no news.
            _notify(
                conn,
                restaurant=restaurant,
                kind=notifications.CHANGED,
                record=updated,  # type: ignore[arg-type]
            )
            responses.append(domain.reservation_body(updated))  # type: ignore[arg-type]

        for series_id in sorted(affected_series):
            repo.bump_series_revision(conn, series_id)

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
# POST /series — adopt a booking as occurrence zero of a recurring agreement
# --------------------------------------------------------------------------- #
def create_series(
    db: Database, *, user_id: str, key: str, body: dict, method: str, path: str
) -> tuple[int, dict]:
    """Adopt an existing booking and generate the rest of the agreement.

    Every generated occurrence is an ordinary booking: it selects the policy for its
    own date, obeys the ordinary opening, daylight-saving and occupancy rules, and
    gets its own reference and its own record. That is also why the operation is
    all-or-nothing — the first occurrence that cannot be placed refuses the whole
    adoption, and nothing survives the refusal: no bookings, no records, no
    counters, and no claim on the idempotency key.
    """
    with db.transaction() as conn:
        stored = idempotency.lookup(
            conn, user_id=user_id, key=key, method=method, path=path, body=body
        )
        if stored is not None:
            return 200, stored["response_body"]

        anchor_reference = parsing.string_field(
            body, "anchor_reference", max_length=parsing.MAX_ID_LENGTH
        )
        count = parsing.bounded_int_field(body, "count", 2, 12)
        interval_weeks = parsing.bounded_int_field(body, "interval_weeks", 1, 4)

        anchor = repo.get_reservation_by_reference(conn, anchor_reference)
        # Another diner's booking is indistinguishable from one that does not exist.
        if anchor is None or anchor["user_id"] != user_id:
            raise not_found(f"No reservation with reference '{anchor_reference}'")
        restaurant = domain.require_restaurant(conn, anchor["restaurant_id"])
        _refuse_uneditable(anchor)
        if domain.is_past_cutoff(anchor, restaurant, now()):
            raise cutoff_passed()
        if repo.series_of_reservation(conn, anchor_reference) is not None:
            raise already_in_series()

        series_id = domain.new_series_id()
        created_at = rfc3339(now())
        occurrences: list[dict] = []
        for index in range(count):
            if index == 0:
                # Occurrence zero is the anchor itself. Its reference, identity,
                # revision, terms, record, timestamps and original idempotent
                # response all stay exactly as they were.
                record = anchor
            else:
                record = _place(
                    conn,
                    user_id=user_id,
                    restaurant_id=anchor["restaurant_id"],
                    table_ids=list(anchor["table_ids"]),
                    starts_at_local=_shift_local(
                        anchor["starts_at_local"], index * interval_weeks * 7
                    ),
                    party_size=int(anchor["party_size"]),
                    created_at=created_at,
                    count_restaurant_write=False,
                )
            occurrences.append(
                {
                    "index": index,
                    "reference": record["reference"],
                    "exception": False,
                    "reservation": domain.reservation_body(record),
                }
            )

        repo.insert_series(
            conn,
            {
                "id": series_id,
                "user_id": user_id,
                "restaurant_id": anchor["restaurant_id"],
                "anchor_reference": anchor["reference"],
                "count": count,
                "interval_weeks": interval_weeks,
                "revision": 1,
                "created_at": created_at,
            },
        )
        for occurrence in occurrences:
            repo.insert_occurrence(
                conn,
                {
                    "series_id": series_id,
                    "idx": occurrence["index"],
                    "reference": occurrence["reference"],
                    "exception": False,
                },
            )
        # One increment for the whole adoption, however many bookings it made.
        repo.bump_restaurant_revision(conn, anchor["restaurant_id"])
        _notify(
            conn,
            restaurant=restaurant,
            kind=notifications.SERIES_ADOPTED,
            record=anchor,
            extra={"count": len(occurrences), "interval_weeks": interval_weeks},
        )

        response = {
            "series_id": series_id,
            "revision": 1,
            "interval_weeks": interval_weeks,
            "occurrences": occurrences,
        }
        idempotency.record(
            conn, user_id=user_id, key=key, method=method, path=path,
            body=body, status_code=201, response_body=response,
        )
        return 201, response


def _series_body(conn: sqlite3.Connection, agreement: dict) -> dict:
    """An agreement and the current state of every booking in it."""
    occurrences = []
    for entry in repo.occurrences_for(conn, agreement["id"]):
        record = repo.get_reservation_by_reference(conn, entry["reference"])
        occurrences.append(
            {
                "index": int(entry["idx"]),
                "reference": entry["reference"],
                "exception": bool(entry["exception"]),
                "reservation": domain.reservation_body(record)
                if record is not None
                else None,  # pragma: no cover - an occurrence is a booking
            }
        )
    return {
        "series_id": agreement["id"],
        "revision": int(agreement["revision"]),
        "interval_weeks": int(agreement["interval_weeks"]),
        "occurrences": occurrences,
    }


def series_detail(db: Database, user_id: str | None, series_id: str) -> dict:
    """Only the diner who made the agreement may read it; anybody else gets the
    same 404 as an agreement that does not exist."""
    with db.read() as conn:
        agreement = repo.get_series(conn, series_id)
        if agreement is None or agreement["user_id"] != user_id:
            raise not_found(f"No recurring agreement with id '{series_id}'")
        return _series_body(conn, agreement)


# --------------------------------------------------------------------------- #
# POST /series/{series_id}/amend — one clock time for the rest of an agreement
# --------------------------------------------------------------------------- #
def amend_series(
    db: Database, *, user_id: str, key: str, series_id: str, body: dict,
    method: str, path: str,
) -> tuple[int, dict]:
    """Move the clock time of every remaining occurrence of an agreement.

    The diner names a position in the agreement and a time, and every occurrence
    from that position on takes that time on the date it was already scheduled for.
    Each one is an ordinary amendment of one booking — same cutoff, same policy
    selection, same record entry — but the whole operation is one write: either
    every occurrence that can move does, or nothing at all does, and the agreement
    and the restaurant each count it once.

    Occurrences already taken out of the pattern are left alone. A diner who moved
    one sitting by hand made it an exception, and a cancelled one is nobody's to
    reschedule, so neither is eligible and neither is a reason to refuse the rest.
    """
    with db.transaction() as conn:
        stored = idempotency.lookup(
            conn, user_id=user_id, key=key, method=method, path=path, body=body
        )
        if stored is not None:
            return 200, stored["response_body"]

        agreement = repo.get_series(conn, series_id)
        # Somebody else's agreement is indistinguishable from one that does not
        # exist, exactly as an agreement read is.
        if agreement is None or agreement["user_id"] != user_id:
            raise not_found(f"No recurring agreement with id '{series_id}'")

        count = int(agreement["count"])
        if count < 1:  # pragma: no cover - an agreement always has an occurrence
            raise validation_failed("'from_index' has no occurrence to name")
        expected_revision = parsing.positive_int_field(body, "expected_revision")
        # An index into this agreement, so its upper bound is the agreement's own.
        from_index = parsing.bounded_int_field(body, "from_index", 0, count - 1)
        local_time = parsing.clock_time_field(body, "local_time")

        # The revision is the agreement's, not a booking's, and it is judged before
        # any occurrence is looked at: two clients that read the same agreement and
        # both write to it cannot both be writing to the one they read.
        if expected_revision != int(agreement["revision"]):
            raise stale_revision(
                f"Agreement '{series_id}' is at revision {agreement['revision']}, "
                f"not {expected_revision}"
            )

        restaurant = domain.require_restaurant(conn, agreement["restaurant_id"])
        eligible: list[tuple[dict, dict]] = []
        for entry in repo.occurrences_for(conn, series_id):
            if int(entry["idx"]) < from_index or entry["exception"]:
                continue
            record = repo.get_reservation_by_reference(conn, entry["reference"])
            if record is None or record["status"] == "cancelled":
                # A cancelled occurrence is not rescheduled and does not refuse the
                # agreement: it is simply not part of what this changes.
                continue
            eligible.append((entry, record))

        # Non-occupancy problems first, in occurrence order, one occurrence at a
        # time — each real change meets its own accepted cutoff and the policy for
        # the date it lands on, exactly as an individual amendment would.
        planned: list[dict] = []
        for entry, record in eligible:
            target_local = f"{record['starts_at_local'][:10]}T{local_time}"
            if target_local == record["starts_at_local"]:
                # Nothing about this sitting would change, so nothing is checked and
                # nothing is written: it keeps its terms, its revision and its
                # record, and stays exactly where it is for everybody else.
                continue
            if domain.is_past_cutoff(record, restaurant, now()):
                raise cutoff_passed()
            table_ids = list(record["table_ids"])
            rules = domain.rules_for(conn, restaurant, target_local)
            sitting = domain.resolve_sitting(restaurant, rules, target_local)
            domain.require_tables(restaurant, table_ids)
            domain.check_combination(restaurant, table_ids)
            domain.check_capacity(rules, table_ids, int(record["party_size"]))
            planned.append(
                {
                    "record": record,
                    "table_ids": table_ids,
                    "sitting": sitting,
                    "rules": rules,
                    "index": int(entry["idx"]),
                }
            )

        # The whole operation is judged against the state it would produce: the
        # occurrences it does not touch, every other booking, and every closure an
        # operator has applied, all stay where they are.
        if planned:
            _check_projected_occupancy(conn, restaurant, planned)

        for plan in planned:
            record = plan["record"]
            reference = record["reference"]
            sitting = plan["sitting"]
            revision = int(record["revision"]) + 1
            terms = plan["rules"].terms()
            # The tables it holds are retained, so only the times and the terms that
            # go with the new date are written.
            repo.update_reservation(
                conn, reference,
                {"revision": revision, "accepted_terms": terms, **sitting.stored_fields()},
            )
            history.record(
                conn, reference=reference, event=history.CHANGED,
                changes=history.changes_between(
                    {
                        "table_ids": record["table_ids"],
                        "starts_at_local": record["starts_at_local"],
                        "party_size": int(record["party_size"]),
                    },
                    {
                        "table_ids": list(record["table_ids"]),
                        "starts_at_local": sitting.starts_at_local,
                        "party_size": int(record["party_size"]),
                    },
                    restaurant.combinable,
                ),
                revision=revision, accepted_terms=terms,
            )

        # Rescheduling the pattern is the agreement's business, so it counts once
        # there and once at the restaurant — and it makes no occurrence an
        # exception, because nothing has been taken out of the pattern: the pattern
        # itself moved.
        if planned:
            repo.bump_series_revision(conn, series_id)
            repo.bump_restaurant_revision(conn, restaurant.id)

        response = _series_body(conn, repo.get_series(conn, series_id))  # type: ignore[arg-type]
        idempotency.record(
            conn, user_id=user_id, key=key, method=method, path=path, body=body,
            status_code=201, response_body=response,
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
        created = auth.signup(
            conn, email=email, password=password, display_name=display_name
        )
        # The confirmation link is written with the account, in the same
        # transaction: an account that exists is an account whose address we are
        # waiting to hear about.
        recovery.send_verification(conn, user_id=created["user_id"])
        return created


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

    try:
        # A failed login raises inside the transaction, which rolls back cleanly.
        with db.transaction() as conn:
            return auth.login(conn, email=email, password=password)
    except ApiError as error:
        # The failure is counted after that rollback, in a transaction of its own,
        # because anything written before it would be undone by the failure it
        # describes and the address would never lock out. A refusal that *was* the
        # throttle is deliberately not counted: recording it would push the
        # lockout further away every time somebody waited it out.
        if error.status == 401:
            with db.transaction() as conn:
                auth.record_failure(conn, email=email)
        raise


def session_info(db: Database, user_id: str, token: str) -> dict:
    """What the caller's own session is: who they are, and when it stops working."""
    with db.read() as conn:
        user = repo.get_user(conn, user_id)
        record = repo.get_session(conn, token)
        # Read inside the same snapshot as everything else here: one answer about
        # one caller, taken at one moment.
        verified = recovery.is_verified(conn, user_id)

    return {
        "user_id": user_id,
        "display_name": user["display_name"] if user else None,
        "email": user["email"] if user else None,
        "email_verified": verified,
        # A token issued before sessions existed has no expiry, which is a fact
        # about it rather than a missing field.
        "expires_at": record["expires_at"] if record else None,
        "restaurants": [
            {"id": row["id"], "name": row["name"], "role": row["role"]}
            for row in _restaurants_for(db, user_id)
        ],
    }


def logout(db: Database, *, token: str) -> dict:
    """Sign this device out. Signing out twice is not an error."""
    from . import auth

    with db.transaction() as conn:
        auth.logout(conn, token)
    return {"signed_out": True}


def logout_everywhere(db: Database, *, user_id: str) -> dict:
    """Sign out of every device, including tokens issued before sessions existed."""
    from . import auth

    with db.transaction() as conn:
        revoked = auth.logout_everywhere(conn, user_id)
    return {"signed_out": True, "tokens_revoked": revoked}


def _restaurants_for(db: Database, user_id: str) -> list[dict]:
    with db.read() as conn:
        return repo.restaurants_for_user(conn, user_id)


# --------------------------------------------------------------------------- #
# the outbox a restaurant can see and retry
# --------------------------------------------------------------------------- #
def _require_staff(conn: sqlite3.Connection, *, user_id: str, restaurant_id: str) -> None:
    """Anything about a restaurant's messages is between it and its staff."""
    from . import onboarding

    domain.require_restaurant(conn, restaurant_id)
    if onboarding.role_in(conn, user_id=user_id, restaurant_id=restaurant_id) is None:
        raise not_found(f"No restaurant with id '{restaurant_id}'")


def restaurant_reservations(
    db: Database, *, user_id: str, restaurant_id: str, params: dict
) -> dict:
    """A restaurant's bookings for a window, as its own staff see them.

    The report says how many covers were served; this says who they are, which is
    what a host standing at the door needs. `from` and `to` default to today and
    the next six days in the restaurant's own calendar, because the console asks
    for "this week" without knowing the timezone arithmetic.
    """
    from . import reports as reports_module

    with db.read() as conn:
        _require_staff(conn, user_id=user_id, restaurant_id=restaurant_id)
        restaurant = domain.require_restaurant(conn, restaurant_id)
        if params.get("from") is None and params.get("to") is None:
            today = now().astimezone(tztime.zone(restaurant.timezone)).date()
            start, end = today, today + timedelta(days=6)
        else:
            start, end = reports_module.window(params)
        records = repo.reservations_for_restaurant(
            conn,
            restaurant_id,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
        )
        return {
            "restaurant_id": restaurant_id,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "reservations": [
                {
                    "reference": record["reference"],
                    "status": record["status"],
                    "starts_at_local": record["starts_at_local"],
                    "party_size": int(record["party_size"]),
                    "table_ids": record["table_ids"],
                    "table_id": record["table_id"],
                    "created_at": record["created_at"],
                    "revision": int(record["revision"]),
                    "diner_name": record.get("diner_name"),
                    "diner_email": record.get("diner_email"),
                    "cancellation_cutoff_minutes": int(
                        (record.get("accepted_terms") or {}).get(
                            "cancellation_cutoff_minutes", 0
                        )
                    ),
                }
                for record in records
            ],
        }


def list_notifications(db: Database, *, user_id: str, restaurant_id: str) -> dict:
    with db.read() as conn:
        _require_staff(conn, user_id=user_id, restaurant_id=restaurant_id)
        records = repo.list_notifications(conn, restaurant_id)
        return {
            "summary": notifications.summary(conn, restaurant_id),
            "notifications": [
                {
                    "id": record["id"],
                    "kind": record["kind"],
                    "to_email": record["to_email"],
                    "subject": record["subject"],
                    "status": record["status"],
                    "attempts": int(record["attempts"]),
                    "last_error": record["last_error"],
                    "reference": record["reference"],
                    "created_at": record["created_at"],
                    "sent_at": record["sent_at"],
                }
                for record in records
            ],
        }


def retry_notification(
    db: Database, *, user_id: str, restaurant_id: str, notification_id: str
) -> dict:
    with db.transaction() as conn:
        _require_staff(conn, user_id=user_id, restaurant_id=restaurant_id)
        found = next(
            (
                record
                for record in repo.list_notifications(conn, restaurant_id, limit=1000)
                if record["id"] == notification_id
            ),
            None,
        )
        if found is None:
            raise not_found(f"No notification with id '{notification_id}'")
        requeued = notifications.retry(db, notification_id)
        return {"id": notification_id, "requeued": requeued, "status": "queued"}


def drain_notifications(
    db: Database, *, user_id: str, restaurant_id: str, transport=None
) -> dict:
    """Send what is waiting for this restaurant. Reports honestly when nothing can.

    Scoped to the restaurant the caller works at: a manager sending their guest
    list is sending their own post, not the platform's address confirmations or
    another restaurant's cancellations.
    """
    with db.read() as conn:
        _require_staff(conn, user_id=user_id, restaurant_id=restaurant_id)
    result = notifications.drain(db, transport, restaurant_id=restaurant_id)
    return result


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
# POST /restaurants/{id}/replans — propose a seating plan for a closed table
# --------------------------------------------------------------------------- #
def _closure_request(
    restaurant: domain.Restaurant, body: dict
) -> planning.ClosureInterval:
    """A manager's proposed closure: one table, and an interval in absolute time.

    The instants carry their own offsets, so the interval means the same thing to
    the manager in the dining room and to the service comparing it with a sitting
    in UTC. An interval that does not end after it starts is not an interval.
    """
    table_id = parsing.id_field(body, "table_id")
    starts_at = parsing.instant_field(body, "from")
    ends_at = parsing.instant_field(body, "to")
    start = parse_instant(starts_at)
    end = parse_instant(ends_at)
    assert start is not None and end is not None  # the parsing layer accepted both
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    if end_utc <= start_utc:
        raise validation_failed(
            "'to' must be later than 'from': a closure covers an interval"
        )
    # Another restaurant's table is as missing as one that was never there.
    domain.require_table(restaurant, table_id)  # type: ignore[arg-type]
    return planning.ClosureInterval(
        table_id=table_id,  # type: ignore[arg-type]
        starts_at=starts_at,
        ends_at=ends_at,
        starts_at_utc=start_utc,
        ends_at_utc=end_utc,
    )


def _plan_response(
    plan_id: str, revision: int, closure: planning.ClosureInterval,
    assignments: Sequence[dict], moved_count: int, unused_seats: int,
) -> dict:
    """A proposed plan as the manager reads it: the closure, and every booking."""
    return {
        "plan_id": plan_id,
        "restaurant_revision": int(revision),
        "closure": {
            "table_id": closure.table_id,
            "from": closure.starts_at,
            "to": closure.ends_at,
        },
        "assignments": [
            {
                "reference": assignment["reference"],
                "table_ids": list(assignment["table_ids"]),
                "changed": bool(assignment["changed"]),
            }
            for assignment in assignments
        ],
        "moved_count": int(moved_count),
        "unused_seats": int(unused_seats),
    }


def propose_replan(
    db: Database, *, user_id: str, key: str, restaurant_id: str, body: dict,
    method: str, path: str,
) -> tuple[int, dict]:
    """Work out what a closure would cost, and store the answer without acting on it.

    A preview is a read that remembers itself: it chooses a seating for every
    booking the closure puts in question and keeps the choice, but writes no
    closure, moves no booking, records nothing in any ledger and does not even
    count as a change to the restaurant. What it does keep is the restaurant's
    revision at the time, which is what makes the plan stale the moment anything
    else happens.
    """
    with db.transaction() as conn:
        restaurant = domain.require_restaurant(conn, restaurant_id)
        if not restaurant.is_manager(user_id):
            raise forbidden(
                "Only a manager of this restaurant may plan its seating"
            )

        stored = idempotency.lookup(
            conn, user_id=user_id, key=key, method=method, path=path, body=body
        )
        if stored is not None:
            return 200, stored["response_body"]

        closure = _closure_request(restaurant, body)
        subjects, occupants, closures = planning.consider(conn, restaurant, closure)
        planning.check_limits(restaurant, subjects)
        plan = planning.best_plan(subjects, occupants, closures)
        if plan is None:
            raise no_feasible_plan(
                f"Table '{closure.table_id}' cannot be closed for that interval "
                f"while all {len(subjects)} bookings it affects keep their seats"
            )

        record = repo.get_restaurant(conn, restaurant_id)
        assert record is not None  # require_restaurant just loaded it
        revision = int(record["revision"])
        plan_id = domain.new_plan_id()
        repo.insert_replan(
            conn,
            {
                "id": plan_id,
                "restaurant_id": restaurant_id,
                "table_id": closure.table_id,
                "closed_from": closure.starts_at,
                "closed_to": closure.ends_at,
                "closed_from_utc": rfc3339(closure.starts_at_utc),
                "closed_to_utc": rfc3339(closure.ends_at_utc),
                "restaurant_revision": revision,
                "status": "proposed",
                "moved_count": plan.moved_count,
                "unused_seats": plan.unused_seats,
                "created_at": rfc3339(now()),
            },
        )
        for position, assignment in enumerate(plan.assignments):
            table_ids = assignment["table_ids"]
            repo.insert_assignment(
                conn,
                {
                    "plan_id": plan_id,
                    "position": position,
                    "reference": assignment["reference"],
                    "table_a": table_ids[0],
                    "table_b": table_ids[1] if len(table_ids) > 1 else None,
                    "changed": assignment["changed"],
                },
            )

        response = _plan_response(
            plan_id, revision, closure, plan.assignments,
            plan.moved_count, plan.unused_seats,
        )
        idempotency.record(
            conn, user_id=user_id, key=key, method=method, path=path, body=body,
            status_code=201, response_body=response,
        )
        return 201, response


# --------------------------------------------------------------------------- #
# POST /restaurants/{id}/replans/{plan_id}/apply — seat the room as proposed
# --------------------------------------------------------------------------- #
def apply_replan(
    db: Database, *, user_id: str, key: str, restaurant_id: str, plan_id: str,
    body: dict, method: str, path: str,
) -> tuple[int, dict]:
    """Apply a proposed plan: the closure and every reassignment, or neither.

    The plan is guarded by the restaurant's revision rather than re-checked: if
    anything at all has happened here since it was proposed — a booking, an
    amendment, a cancellation, a policy, another plan — the plan describes a room
    that no longer exists and is refused as stale. A closure at another restaurant
    changes nothing here, so it cannot invalidate this one.
    """
    with db.transaction() as conn:
        restaurant = domain.require_restaurant(conn, restaurant_id)
        if not restaurant.is_manager(user_id):
            raise forbidden(
                "Only a manager of this restaurant may plan its seating"
            )

        stored = idempotency.lookup(
            conn, user_id=user_id, key=key, method=method, path=path, body=body
        )
        if stored is not None:
            # A replay is the original answer, however the room has moved on since:
            # re-running it would apply the plan twice or report it stale.
            return 200, stored["response_body"]

        plan = repo.get_replan(conn, plan_id)
        # A plan belongs to the restaurant it was proposed for, so one proposed
        # elsewhere is as absent as one that was never proposed.
        if plan is None or plan["restaurant_id"] != restaurant_id:
            raise not_found(f"No seating plan with id '{plan_id}'")
        if plan["status"] == "applied":
            raise plan_already_applied()

        record = repo.get_restaurant(conn, restaurant_id)
        assert record is not None  # require_restaurant just loaded it
        current = int(record["revision"])
        if current != int(plan["restaurant_revision"]):
            raise stale_plan(
                f"Plan '{plan_id}' was proposed at revision "
                f"{plan['restaurant_revision']}; this restaurant is at {current}"
            )

        assignments = repo.assignments_for(conn, plan_id)
        applied_at = rfc3339(now())
        repo.insert_closure(
            conn,
            {
                "plan_id": plan_id,
                "restaurant_id": restaurant_id,
                "table_id": plan["table_id"],
                "closed_from": plan["closed_from"],
                "closed_to": plan["closed_to"],
                "closed_from_utc": plan["closed_from_utc"],
                "closed_to_utc": plan["closed_to_utc"],
                "created_at": applied_at,
            },
        )

        responses: list[dict] = []
        affected_series: set[str] = set()
        for assignment in assignments:
            reference = assignment["reference"]
            record = repo.get_reservation_by_reference(conn, reference)
            if record is None:  # pragma: no cover - the revision guard rules it out
                raise not_found(f"No reservation with reference '{reference}'")
            if assignment["changed"]:
                table_ids = list(assignment["table_ids"])
                revision = int(record["revision"]) + 1
                # A repair moves the tables under a booking and nothing else: its
                # times, its party size, its reference and the terms it accepted all
                # stay exactly as they were, so no policy is re-selected and no
                # cutoff is consulted.
                repo.update_reservation(
                    conn, reference,
                    {"table_id": table_ids[0], "revision": revision},
                )
                repo.set_reservation_tables(conn, reference, table_ids)
                history.record(
                    conn, reference=reference, event=history.REASSIGNED,
                    changes=history.reassignment_changes(
                        record["table_ids"], table_ids, restaurant.combinable
                    ),
                    revision=revision, accepted_terms=record["accepted_terms"],
                    at=applied_at, plan_id=plan_id,
                )
                # A moved occurrence keeps its place in its agreement, its scheduled
                # date and its exception flag; the agreement still counts the repair
                # once, however many of its occurrences moved.
                agreement = repo.series_of_reservation(conn, reference)
                if agreement is not None:
                    affected_series.add(agreement["id"])
                record = repo.get_reservation_by_reference(conn, reference)
                # The diner is told their table moved, which is the whole point of
                # the operation: a repair nobody hears about is a diner standing at
                # a table that is not theirs.
                _notify(
                    conn,
                    restaurant=restaurant,
                    kind=notifications.SEATING_CHANGED,
                    record=record,  # type: ignore[arg-type]
                )
            responses.append(domain.reservation_body(record))  # type: ignore[arg-type]

        for series_id in sorted(affected_series):
            repo.bump_series_revision(conn, series_id)
        repo.mark_replan_applied(conn, plan_id, applied_at)
        # One increment for the whole plan, however many bookings it moved.
        revision = repo.bump_restaurant_revision(conn, restaurant_id)

        response = {
            "plan_id": plan_id,
            "restaurant_revision": revision,
            "reservations": responses,
        }
        idempotency.record(
            conn, user_id=user_id, key=key, method=method, path=path, body=body,
            status_code=201, response_body=response,
        )
        return 201, response


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


# --------------------------------------------------------------------------- #
# reports
# --------------------------------------------------------------------------- #
def restaurant_summary(
    db: Database, *, user_id: str, restaurant_id: str, params: dict
) -> dict:
    """How the room has been doing, over a window of the restaurant's calendar."""
    from . import reports

    with db.read() as conn:
        _require_staff(conn, user_id=user_id, restaurant_id=restaurant_id)
        restaurant = domain.require_restaurant(conn, restaurant_id)
        start, end = reports.window(params)
        return reports.summary(conn, restaurant, start=start, end=end, params=params)


def restaurant_bookings_csv(
    db: Database, *, user_id: str, restaurant_id: str, params: dict
) -> str:
    from . import reports

    with db.read() as conn:
        _require_staff(conn, user_id=user_id, restaurant_id=restaurant_id)
        restaurant = domain.require_restaurant(conn, restaurant_id)
        start, end = reports.window(params)
        return reports.bookings_csv(conn, restaurant, start=start, end=end)


# --------------------------------------------------------------------------- #
# getting back in
# --------------------------------------------------------------------------- #
def request_password_reset(db: Database, *, body: dict) -> dict:
    """Ask for a reset link.

    Always the same answer, whether or not that address has an account here: an
    endpoint that says "no such user" is an endpoint for finding out who a
    restaurant's guests are.
    """
    email = body.get("email")
    if email is None:
        raise validation_failed("'email' is required")
    if not isinstance(email, str):
        raise malformed_request("'email' must be a string")
    with db.transaction() as conn:
        recovery.request_reset(conn, email=email)
    return {
        "requested": True,
        "message": (
            "If that address has an account, a reset link is on its way to it."
        ),
    }


def confirm_password_reset(db: Database, *, body: dict) -> dict:
    token = body.get("token")
    new_password = body.get("new_password")
    if not isinstance(token, str) or not token:
        raise validation_failed("'token' is required")
    if new_password is None:
        raise validation_failed("'new_password' is required")
    with db.transaction() as conn:
        return recovery.confirm_reset(conn, token=token, new_password=new_password)


def request_email_verification(db: Database, *, user_id: str) -> dict:
    """Send another confirmation link, or report that the address is already confirmed."""
    with db.transaction() as conn:
        if recovery.is_verified(conn, user_id):
            return {"sent": False, "email_verified": True}
        recovery.send_verification(conn, user_id=user_id)
        return {"sent": True, "email_verified": False}


def confirm_email_verification(db: Database, *, body: dict) -> dict:
    token = body.get("token")
    if not isinstance(token, str) or not token:
        raise validation_failed("'token' is required")
    with db.transaction() as conn:
        return recovery.confirm_verification(conn, token=token)

