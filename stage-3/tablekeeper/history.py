"""A reservation's own record.

Every real change a booking goes through is written here as one entry: what
happened, which fields changed and from what to what, the revision that resulted,
and the complete terms the booking accepted as a result. Old entries never acquire
newer terms — the ledger is the booking's memory, not a view of its present.

Two kinds of write record nothing, because neither changes the booking: an
amendment that sets every field to the value it already has, and a replay of an
idempotent request, which re-runs no operation at all.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from . import repo
from .clock import now
from .tztime import rfc3339

# The order a `changed` entry names its fields in.
CREATED = "created"
CHANGED = "changed"
CANCELLED = "cancelled"


def declared_order(table_ids: Sequence[str], combinable: Sequence[tuple[str, str]]) -> list[str]:
    """A table set in the order the restaurant declared the combination.

    A pair is one seating however it is written, so the ledger always names it the
    same way: `[t_2,t_1]` and `[t_1,t_2]` are recorded as the declared pair, and
    that is also what makes a reversed request comparable to what is stored.
    """
    ids = list(table_ids)
    if len(ids) == 2:
        for first, second in combinable:
            if {first, second} == set(ids):
                return [first, second]
    return ids


def _table_change(before: Sequence[str] | None, after: Sequence[str],
                  combinable: Sequence[tuple[str, str]]) -> dict | None:
    """The seating change, if the seating changed.

    A single table moving to a single table is reported as `table_id`; anything
    involving a pair is reported as `table_ids`, with both lists complete.
    """
    held = declared_order(after, combinable)
    if before is None:
        field, value = ("table_id", held[0]) if len(held) == 1 else ("table_ids", held)
        return {"field": field, "from": None, "to": value}

    was = declared_order(before, combinable)
    if was == held:
        return None
    if len(was) == 1 and len(held) == 1:
        return {"field": "table_id", "from": was[0], "to": held[0]}
    return {"field": "table_ids", "from": was, "to": held}


def creation_changes(table_ids: Sequence[str], starts_at_local: str, party_size: int,
                     combinable: Sequence[tuple[str, str]]) -> list[dict]:
    """A `created` entry names all three fields, each from nothing."""
    changes = [_table_change(None, table_ids, combinable)]
    changes.append({"field": "starts_at_local", "from": None, "to": starts_at_local})
    changes.append({"field": "party_size", "from": None, "to": int(party_size)})
    return [change for change in changes if change is not None]


def changes_between(before: dict, after: dict,
                    combinable: Sequence[tuple[str, str]]) -> list[dict]:
    """Only the fields that actually changed, in the order the spec names them."""
    changes: list[dict] = []
    tables = _table_change(before["table_ids"], after["table_ids"], combinable)
    if tables is not None:
        changes.append(tables)
    if before["starts_at_local"] != after["starts_at_local"]:
        changes.append({
            "field": "starts_at_local",
            "from": before["starts_at_local"],
            "to": after["starts_at_local"],
        })
    if int(before["party_size"]) != int(after["party_size"]):
        changes.append({
            "field": "party_size",
            "from": int(before["party_size"]),
            "to": int(after["party_size"]),
        })
    return changes


def record(conn: sqlite3.Connection, *, reference: str, event: str, changes: list[dict],
           revision: int, accepted_terms: dict, at: str | None = None) -> dict:
    """One entry in the booking's record. `seq` makes the order total."""
    entry = {
        "reference": reference,
        "seq": repo.next_history_seq(conn, reference),
        "at": at or rfc3339(now()),
        "event": event,
        "changes": changes,
        "revision": int(revision),
        "accepted_terms": accepted_terms,
    }
    repo.insert_history(conn, entry)
    return entry


def ledger(conn: sqlite3.Connection, reference: str) -> dict:
    """The whole record, oldest first."""
    return {
        "reference": reference,
        "entries": [
            {
                "seq": int(entry["seq"]),
                "at": entry["at"],
                "event": entry["event"],
                "changes": entry["changes"],
                "revision": int(entry["revision"]),
                "accepted_terms": entry["accepted_terms"],
            }
            for entry in repo.history_for(conn, reference)
        ],
    }
