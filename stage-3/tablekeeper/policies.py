"""Published booking policies.

A policy is **complete**, not a patch: every field is required, and what a manager
supplies is what the restaurant decides by from the date it takes effect. Policies
are immutable, so publishing is a new version rather than an edit, and a booking
keeps the terms it accepted whichever policy comes after it.

Everything wrong about a policy — a missing field, a wrong type, a date that is not
a date, an hour range that does not end after it starts, a capacity map that does
not name exactly this restaurant's tables — is one refusal: 422
``validation_failed``, allocating no version and changing no state. The spec states
that rule for the policy as a whole, so it is applied to the policy as a whole
rather than splitting wrong types out into 400 the way a request body's fields are.

Table ids, labels, the timezone and declared combinations cannot be changed by a
policy: they are the restaurant's identity, not its rules.
"""

from __future__ import annotations

from typing import Any

from .errors import validation_failed
from .tztime import WEEKDAYS, parse_date, parse_hhmm

GRID_RANGE = (1, 1440)
CUTOFF_RANGE = (0, 10_080)
CAPACITY_RANGE = (1, 100)

REQUIRED = (
    "effective_from",
    "slot_minutes",
    "reservation_duration_minutes",
    "cancellation_cutoff_minutes",
    "opening_hours",
    "capacities",
)


def _integer(value: Any, field: str, bounds: tuple[int, int]) -> int:
    """A required integer within bounds. A boolean is not an integer."""
    if value is None:
        raise validation_failed(f"'{field}' is required")
    if not isinstance(value, int) or isinstance(value, bool):
        raise validation_failed(f"'{field}' must be an integer")
    if not bounds[0] <= value <= bounds[1]:
        raise validation_failed(
            f"'{field}' must be between {bounds[0]} and {bounds[1]}"
        )
    return int(value)


def _date(value: Any) -> str:
    """An actual calendar date, written YYYY-MM-DD."""
    if value is None:
        raise validation_failed("'effective_from' is required")
    if not isinstance(value, str):
        raise validation_failed("'effective_from' must be a date written YYYY-MM-DD")
    if parse_date(value) is None:
        raise validation_failed(
            f"'effective_from' must be an actual calendar date, not '{value}'"
        )
    return value


def _opening_hours(value: Any) -> list[dict]:
    """Stage 1's service windows, with no weekday named twice."""
    if value is None:
        raise validation_failed("'opening_hours' is required")
    if not isinstance(value, list):
        raise validation_failed("'opening_hours' must be an array of weekday windows")
    # An empty list is allowed: as in stage 1, a weekday with no window is closed,
    # so a policy may close the room entirely. What it may not do is name one day twice.

    hours: list[dict] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        where = f"opening_hours[{index}]"
        if not isinstance(entry, dict):
            raise validation_failed(f"'{where}' must be an object")
        weekday = entry.get("weekday")
        if not isinstance(weekday, str) or weekday.lower() not in WEEKDAYS:
            raise validation_failed(
                f"'{where}.weekday' must be one of {', '.join(WEEKDAYS)}"
            )
        name = weekday.lower()
        if name in seen:
            raise validation_failed(f"'{where}.weekday' names '{name}' more than once")
        seen.add(name)
        opens = parse_hhmm(str(entry.get("opens")))
        closes = parse_hhmm(str(entry.get("closes")))
        if opens is None or closes is None:
            raise validation_failed(f"'{where}.opens' and '.closes' must be local HH:MM")
        if closes <= opens:
            raise validation_failed(
                f"'{where}.closes' must be later than opens on the same local day"
            )
        hours.append(
            {
                "weekday": name,
                "opens": opens.strftime("%H:%M"),
                "closes": closes.strftime("%H:%M"),
            }
        )
    return hours


def _capacities(value: Any, table_ids: list[str]) -> dict[str, int]:
    """Exactly this restaurant's tables, each with a capacity that fits a room."""
    if value is None:
        raise validation_failed("'capacities' is required")
    if not isinstance(value, dict):
        raise validation_failed("'capacities' must be an object of table id to capacity")
    if set(value) != set(table_ids):
        missing = sorted(set(table_ids) - set(value))
        unknown = sorted(set(value) - set(table_ids))
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if unknown:
            detail.append(f"unknown {', '.join(unknown)}")
        raise validation_failed(
            "'capacities' must name exactly this restaurant's tables "
            f"({'; '.join(detail)})"
        )
    return {
        table_id: _integer(value[table_id], f"capacities.{table_id}", CAPACITY_RANGE)
        for table_id in table_ids
    }


def validate(body: dict, restaurant) -> dict:
    """A complete policy for *this* restaurant, ready to store.

    Unknown fields are ignored, as everywhere else in the API: a policy carries the
    six things a restaurant decides by, and nothing a client adds changes that.
    """
    for field in REQUIRED:
        if field not in body:
            raise validation_failed(f"'{field}' is required")

    return {
        "effective_from": _date(body.get("effective_from")),
        "slot_minutes": _integer(body.get("slot_minutes"), "slot_minutes", GRID_RANGE),
        "reservation_duration_minutes": _integer(
            body.get("reservation_duration_minutes"),
            "reservation_duration_minutes",
            GRID_RANGE,
        ),
        "cancellation_cutoff_minutes": _integer(
            body.get("cancellation_cutoff_minutes"),
            "cancellation_cutoff_minutes",
            CUTOFF_RANGE,
        ),
        "opening_hours": _opening_hours(body.get("opening_hours")),
        "capacities": _capacities(
            body.get("capacities"), [table["id"] for table in restaurant.tables]
        ),
    }


def published(policy: dict) -> dict:
    """A policy as the API reports it: what was supplied, plus its version."""
    return {
        "policy_version": int(policy["policy_version"]),
        "effective_from": policy["effective_from"],
        "slot_minutes": int(policy["slot_minutes"]),
        "reservation_duration_minutes": int(policy["reservation_duration_minutes"]),
        "cancellation_cutoff_minutes": int(policy["cancellation_cutoff_minutes"]),
        "opening_hours": policy["opening_hours"],
        "capacities": policy["capacities"],
    }
