"""What the restaurant tells the diner, and how it leaves the building.

A diner books a table and the service writes a message about it. That message is
a **row in the outbox**, written inside the same transaction as the booking
itself, so:

* a confirmation cannot exist for a booking that rolled back;
* a booking cannot exist without its confirmation;
* a retried request does not send a second one, because the idempotent replay
  returns the stored response before any of this runs.

Sending is a separate step (:func:`drain`) that reads the outbox and hands each
message to a transport. Nothing about booking a table therefore depends on a mail
server being reachable, and a transport that is down leaves visible `failed` rows
with the error that stopped them rather than silently dropping the message.

No transport is configured by default: :func:`drain` then reports what is still
queued instead of pretending to have sent it. :class:`RecordingTransport` is what
the tests use, :class:`SmtpTransport` is what a deployment configures, and either
one satisfies the same three-line contract.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Any, Protocol

from . import repo
from .clock import now
from .payments import format_money
from .tztime import rfc3339

# The kinds of message this service writes. A manager digest and a reminder are
# deliberately absent: they are scheduled work rather than consequences of a
# write, and they belong to a worker rather than to the booking path.
CONFIRMED = "booking_confirmed"
CHANGED = "booking_changed"
CANCELLED = "booking_cancelled"
SEATING_CHANGED = "seating_changed"
SERIES_ADOPTED = "series_adopted"
NO_SHOW_CHARGE = "no_show_charge"
PASSWORD_RESET = "password_reset"
EMAIL_VERIFICATION = "email_verification"

MAX_ATTEMPTS = 5


class Transport(Protocol):
    """Anything that can deliver one message, or raise telling us why not."""

    def send(self, message: dict[str, Any]) -> None: ...


class RecordingTransport:
    """Keeps every message in memory. The transport the tests assert against."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    def kinds(self) -> list[str]:
        return [message["kind"] for message in self.sent]

    def to(self, email: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["to_email"] == email]


class SmtpTransport:
    """Delivers over SMTP. Constructed from configuration, never imported blind.

    Kept deliberately small — one connection per drain, plain text only — because
    the interesting product decision is not the protocol but *where* sending
    happens: outside the request that booked the table.
    """

    def __init__(self, host: str, port: int, sender: str, *, username: str | None = None,
                 password: str | None = None, starttls: bool = True) -> None:
        self.host = host
        self.port = port
        self.sender = sender
        self.username = username
        self.password = password
        self.starttls = starttls

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "SmtpTransport | None":
        """A transport when the deployment configured one, and None when it did not.

        Reading configuration is this method's whole job: an unconfigured service
        must not end up with a transport that silently drops mail.
        """
        host = env.get("TABLEKEEPER_SMTP_HOST")
        sender = env.get("TABLEKEEPER_SMTP_FROM")
        if not host or not sender:
            return None
        return cls(
            host=host,
            port=int(env.get("TABLEKEEPER_SMTP_PORT", "587")),
            sender=sender,
            username=env.get("TABLEKEEPER_SMTP_USERNAME"),
            password=env.get("TABLEKEEPER_SMTP_PASSWORD"),
            starttls=env.get("TABLEKEEPER_SMTP_STARTTLS", "1") != "0",
        )

    def send(self, message: dict[str, Any]) -> None:  # pragma: no cover - needs a server
        import smtplib
        from email.message import EmailMessage

        mail = EmailMessage()
        mail["From"] = self.sender
        mail["To"] = message["to_email"]
        mail["Subject"] = message["subject"]
        mail.set_content(message["body"])
        with smtplib.SMTP(self.host, self.port, timeout=10) as server:
            if self.starttls:
                server.starttls()
            if self.username is not None:
                server.login(self.username, self.password or "")
            server.send_message(mail)


# --------------------------------------------------------------------------- #
# writing a message
# --------------------------------------------------------------------------- #
def _new_id() -> str:
    return f"n_{secrets.token_hex(12)}"


def _money(cents: int, currency: str) -> str:
    """Cents as a person reads them. Imported here rather than duplicating the
    rule: there is one way this service writes an amount of money."""
    return format_money(cents, currency)


def _table_labels(record: dict, labels: dict[str, str]) -> str:
    table_ids = list(record.get("table_ids") or [record["table_id"]])
    return ", ".join(labels.get(table_id, table_id) for table_id in table_ids)


def _when(record: dict) -> str:
    """The diner's own wall clock, which is what they will read it against."""
    return f"{record['starts_at_local'].replace('T', ' ')} (local time)"


def _compose(
    *, kind: str, restaurant_name: str, record: dict, labels: dict[str, str],
    extra: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Subject and body for one kind of message.

    Plain text on purpose: it is what every client can read, it is what the tests
    can assert on, and a booking confirmation is not a place to be clever.
    """
    reference = record["reference"]
    party = int(record["party_size"])
    when = _when(record)
    tables = _table_labels(record, labels)
    size = f"{party} {'guest' if party == 1 else 'guests'}"

    if kind == CANCELLED:
        context = extra or {}
        money = ""
        if context.get("deposit_released"):
            money = (
                "\n\nThe deposit you paid to hold the table has been released back "
                "to your card."
            )
        return (
            f"Your booking at {restaurant_name} is cancelled",
            f"Booking {reference} at {restaurant_name} on {when} for {size} has been "
            f"cancelled.{money}\n\nIf this was not you, reply to this message and we "
            f"will look into it.",
        )
    if kind in (PASSWORD_RESET, EMAIL_VERIFICATION):
        # These two are not about a booking at all, so they are written plainly
        # and carry the link the message is for.
        context = extra or {}
        token = context.get("token", "")
        minutes = int(context.get("ttl_minutes") or 60)
        if kind == PASSWORD_RESET:
            return (
                "Reset your Tablekeeper password",
                f"Somebody asked to reset the password for this address.\n\n"
                f"Use this code to choose a new one:\n\n    {token}\n\n"
                f"It works for {minutes} minutes and only once. If this was not you, "
                f"nothing has changed and you can ignore this message.",
            )
        return (
            "Confirm your Tablekeeper email address",
            f"Use this code to confirm this address:\n\n    {token}\n\n"
            f"It works for {minutes} minutes and only once.",
        )
    if kind == NO_SHOW_CHARGE:
        context = extra or {}
        amount = int(context.get("captured_cents") or 0)
        money = (
            f"\n\nThe deposit of "
            f"{_money(amount, str(context.get('currency') or ''))} has been kept, as "
            f"the terms of this booking said it would be."
            if amount
            else "\n\nNo deposit was held on this booking."
        )
        return (
            f"We missed you at {restaurant_name}",
            f"Booking {reference} for {when} for {size} was recorded as a no-show, "
            f"because the table was still waiting for you.{money}\n\nIf we have this "
            f"wrong, reply to this message and we will put it right.",
        )
    if kind == SEATING_CHANGED:
        return (
            f"Your table at {restaurant_name} has changed",
            f"Booking {reference} at {restaurant_name} on {when} for {size} has been "
            f"moved to table {tables}. Your time and party size are unchanged, and "
            f"nothing else about your booking has changed.\n\nWe are sorry for the "
            f"shuffle; a table came out of service and we have reseated you.",
        )
    if kind == CHANGED:
        return (
            f"Your booking at {restaurant_name} has changed",
            f"Booking {reference} at {restaurant_name} is now {when} for {size}, "
            f"at table {tables}.\n\nIf you did not ask for this change, reply to "
            f"this message.",
        )
    if kind == SERIES_ADOPTED:
        context = extra or {}
        count = int(context.get("count") or 1)
        interval = int(context.get("interval_weeks") or 1)
        cadence = "every week" if interval == 1 else f"every {interval} weeks"
        return (
            f"Your recurring booking at {restaurant_name} is confirmed",
            f"Booking {reference} at {restaurant_name} on {when} for {size} is now the "
            f"first of a recurring agreement: {count} bookings in total, {cadence}.\n\n"
            f"Every booking in the agreement is a normal booking with its own "
            f"reference, and this message covers the whole agreement rather than "
            f"one per sitting.",
        )
    return (
        f"Booking confirmed at {restaurant_name}",
        f"Booking {reference} is confirmed: {when}, {size}, table {tables}.\n\n"
        f"Keep this reference — it is how you amend or cancel the booking.\n\n"
        f"Please cancel if your plans change so the table can go to somebody else.",
    )


def enqueue(
    conn: sqlite3.Connection,
    *,
    restaurant_id: str,
    user_id: str,
    reference: str | None,
    kind: str,
    restaurant_name: str,
    record: dict,
    labels: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> str | None:
    """Write one message to the outbox. Returns its id, or None if we cannot.

    A booking still succeeds when its diner has no email address on file — the
    booking is the product's promise, the message is a courtesy — so an account
    without an address simply gets no row rather than failing the write that
    mattered.
    """
    user = repo.get_user(conn, user_id)
    if user is None or not user.get("email"):
        return None
    subject, body = _compose(
        kind=kind, restaurant_name=restaurant_name, record=record, labels=labels or {},
        extra=extra,
    )
    notification_id = _new_id()
    repo.insert_notification(
        conn,
        {
            "id": notification_id,
            "restaurant_id": restaurant_id,
            "user_id": user_id,
            "reference": reference,
            "kind": kind,
            "to_email": user["email"],
            "subject": subject,
            "body": body,
            "status": "queued",
            "attempts": 0,
            "created_at": created_at or rfc3339(now()),
        },
    )
    return notification_id


def for_reference(conn: sqlite3.Connection, reference: str) -> list[dict]:
    return repo.notifications_for_reference(conn, reference)


# --------------------------------------------------------------------------- #
# sending
# --------------------------------------------------------------------------- #
def drain(
    db, transport: Transport | None, *, limit: int = 50, restaurant_id: str | None = None
) -> dict[str, Any]:
    """Hand queued messages to the transport. Never raises for one bad message.

    Returns what happened: how many were delivered, how many failed, how many are
    still queued, and — when no transport is configured — nothing was sent at all
    rather than everything being marked sent.
    """
    if transport is None:
        with db.read() as conn:
            queued = repo.pending_notifications(
                conn, limit=limit, restaurant_id=restaurant_id
            )
        return {
            "configured": False,
            "sent": 0,
            "failed": 0,
            "queued": len(queued),
        }

    sent = 0
    failed = 0
    with db.transaction() as conn:
        for message in repo.pending_notifications(
            conn, limit=limit, restaurant_id=restaurant_id
        ):
            try:
                transport.send(message)
            except Exception as error:  # a transport failure is data, not a crash
                repo.mark_notification_failed(
                    conn, message["id"], error=f"{type(error).__name__}: {error}"
                )
                failed += 1
                continue
            repo.mark_notification_sent(conn, message["id"], sent_at=rfc3339(now()))
            if message["kind"] in (PASSWORD_RESET, EMAIL_VERIFICATION):
                # The link was a credential while it was waiting to be delivered;
                # now that it has been, the database does not need to hold it.
                repo.scrub_notification_body(conn, message["id"])
            sent += 1
        remaining = repo.pending_notifications(
            conn, limit=limit, restaurant_id=restaurant_id
        )
    return {
        "configured": True,
        "sent": sent,
        "failed": failed,
        "queued": len(remaining),
    }


def retry(db, notification_id: str) -> bool:
    """Put a failed message back in the queue, once somebody has looked at why."""
    with db.transaction() as conn:
        return repo.requeue_notification(conn, notification_id)


def summary(conn: sqlite3.Connection, restaurant_id: str) -> dict[str, int]:
    """Counts a manager can act on: what is waiting, what got through, what did not."""
    queued = repo.count_notifications(conn, restaurant_id, status="queued")
    sent = repo.count_notifications(conn, restaurant_id, status="sent")
    failed = repo.count_notifications(conn, restaurant_id, status="failed")
    return {"queued": queued, "sent": sent, "failed": failed}


def as_json(message: dict) -> str:  # pragma: no cover - debugging aid
    return json.dumps(message, default=str, sort_keys=True)
