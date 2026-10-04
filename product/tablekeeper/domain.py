"""The rule set: slot grid, opening hours, DST, capacity and occupancy.

One module owns these rules so that `GET /availability`, `POST /reservations`,
`PATCH /reservations/{reference}` and `POST /reservation-moves` can never
disagree about what is bookable — a slot the grid offers must actually be
bookable, and a slot it does not offer must actually be rejected.

Occupancy is the half-open interval ``[starts_at, starts_at + duration)`` in
absolute time, so a 90-minute booking at 19:00 does not overlap one at 20:30.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from . import repo
from .errors import (
    combination_not_allowed,
    invalid_local_time,
    not_found,
    not_on_slot_grid,
    outside_opening_hours,
    party_exceeds_capacity,
    table_unavailable,
)
from .tztime import (
    LocalResolution,
    iter_wall_clock_steps,
    parse_date,
    parse_hhmm,
    parse_local,
    resolve_local,
    rfc3339,
    weekday_name,
    zone,
)

REFERENCE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
REFERENCE_LENGTH = 8


@dataclass(frozen=True)
class Window:
    opens: time
    closes: time


@dataclass(frozen=True)
class Rules:
    """The configuration that decides a booking, for one restaurant on one date.

    Policy 0 is the restaurant's own seeded configuration; every later policy is
    one a manager published. A booking is decided by exactly one of them and keeps
    a snapshot of it, which is why publishing a policy never edits a booking and a
    cancellation is judged by the cutoff the diner actually accepted.
    """

    policy_version: int
    slot_minutes: int
    duration_minutes: int
    cutoff_minutes: int
    hours: dict[str, list[Window]]
    capacities: dict[str, int]

    def windows_on(self, day: date) -> list[Window]:
        return self.hours.get(weekday_name(day), [])

    def capacity_of(self, table_id: str) -> int:
        return int(self.capacities.get(table_id, 0))

    def terms(self) -> dict:
        """The snapshot a reservation accepts: the whole policy, minus its date."""
        return {
            "policy_version": self.policy_version,
            "slot_minutes": self.slot_minutes,
            "reservation_duration_minutes": self.duration_minutes,
            "cancellation_cutoff_minutes": self.cutoff_minutes,
            "opening_hours": [
                {"weekday": name, "opens": w.opens.strftime("%H:%M"),
                 "closes": w.closes.strftime("%H:%M")}
                for name, windows in self.hours.items()
                for w in windows
            ],
            "capacities": {table_id: int(size) for table_id, size in self.capacities.items()},
        }


@dataclass(frozen=True)
class Restaurant:
    id: str
    name: str
    timezone: str
    slot_minutes: int
    duration_minutes: int
    cutoff_minutes: int
    hours: dict[str, list[Window]]
    tables: list[dict]
    combinable: list[tuple[str, str]]
    managers: list[str]

    @property
    def tz(self):
        return zone(self.timezone)

    def windows_on(self, day: date) -> list[Window]:
        """The seeded service windows: policy 0's, and the detail endpoint's."""
        return self.hours.get(weekday_name(day), [])

    def is_manager(self, user_id: str | None) -> bool:
        return bool(user_id) and user_id in self.managers

    def table_by_id(self) -> dict[str, dict]:
        return {table["id"]: table for table in self.tables}

    def declares(self, table_ids: Sequence[str]) -> bool:
        """True when exactly this pair was declared, in either order.

        A pair is unordered: ``[t_1, t_2]`` and ``[t_2, t_1]`` are the same
        combination. Combining is not transitive, so nothing is inferred from two
        declarations sharing a table.
        """
        if len(table_ids) != 2:
            return False
        wanted = frozenset(table_ids)
        return any(frozenset(pair) == wanted for pair in self.combinable)

    def summary(self) -> dict:
        return {"id": self.id, "name": self.name, "timezone": self.timezone}

    def detail(self) -> dict:
        """The restaurant in the fixture's shape."""
        return {
            "id": self.id,
            "name": self.name,
            "timezone": self.timezone,
            "slot_minutes": self.slot_minutes,
            "reservation_duration_minutes": self.duration_minutes,
            "cancellation_cutoff_minutes": self.cutoff_minutes,
            "opening_hours": [
                {"weekday": name, "opens": w.opens.strftime("%H:%M"),
                 "closes": w.closes.strftime("%H:%M")}
                for name, windows in self.hours.items()
                for w in windows
            ],
            "tables": [
                {"id": t["id"], "label": t["label"], "capacity": int(t["capacity"])}
                for t in self.tables
            ],
            "combinable": [[a, b] for a, b in self.combinable],
        }


def load_restaurant(conn: sqlite3.Connection, restaurant_id: str) -> Restaurant | None:
    record = repo.get_restaurant(conn, restaurant_id)
    if record is None:
        return None
    hours: dict[str, list[Window]] = {}
    for entry in repo.opening_hours_for(conn, restaurant_id):
        opens = parse_hhmm(entry["opens"])
        closes = parse_hhmm(entry["closes"])
        if opens is None or closes is None:  # pragma: no cover - validated at reset
            continue
        hours.setdefault(entry["weekday"], []).append(Window(opens, closes))
    tables = repo.tables_for(conn, restaurant_id)
    return Restaurant(
        id=record["id"],
        name=record["name"],
        timezone=record["timezone"],
        slot_minutes=int(record["slot_minutes"]),
        duration_minutes=int(record["reservation_duration_minutes"]),
        cutoff_minutes=int(record["cancellation_cutoff_minutes"]),
        hours=hours,
        tables=tables,
        combinable=[
            (entry["table_a"], entry["table_b"])
            for entry in repo.combinable_for(conn, restaurant_id)
        ],
        managers=repo.managers_for(conn, restaurant_id),
    )


def require_restaurant(conn: sqlite3.Connection, restaurant_id: str) -> Restaurant:
    restaurant = load_restaurant(conn, restaurant_id)
    if restaurant is None:
        raise not_found(f"No restaurant with id '{restaurant_id}'")
    return restaurant


def policy_zero(restaurant: Restaurant) -> Rules:
    """The rules a restaurant was seeded with, which apply before any publication."""
    return Rules(
        policy_version=0,
        slot_minutes=restaurant.slot_minutes,
        duration_minutes=restaurant.duration_minutes,
        cutoff_minutes=restaurant.cutoff_minutes,
        hours=restaurant.hours,
        capacities={table["id"]: int(table["capacity"]) for table in restaurant.tables},
    )


def rules_from_policy(policy: dict) -> Rules:
    """A published policy row as the rule set that decides a booking."""
    hours: dict[str, list[Window]] = {}
    for entry in policy["opening_hours"]:
        opens = parse_hhmm(entry["opens"])
        closes = parse_hhmm(entry["closes"])
        if opens is None or closes is None:  # pragma: no cover - validated on publication
            continue
        hours.setdefault(entry["weekday"], []).append(Window(opens, closes))
    return Rules(
        policy_version=int(policy["policy_version"]),
        slot_minutes=int(policy["slot_minutes"]),
        duration_minutes=int(policy["reservation_duration_minutes"]),
        cutoff_minutes=int(policy["cancellation_cutoff_minutes"]),
        hours=hours,
        capacities={table_id: int(size) for table_id, size in policy["capacities"].items()},
    )


def rules_on(conn: sqlite3.Connection, restaurant: Restaurant, day: date) -> Rules:
    """The rules that decide a booking whose local start falls on ``day``.

    The greatest ``effective_from`` not later than that date wins, and a tie is
    broken by the greater policy version — so a policy published later for the same
    date supersedes the earlier one for future decisions, without touching any
    booking that already accepted it. Publication order and effective-date order
    are independent; only the dates are compared here.
    """
    wanted = day.isoformat()
    selected: dict | None = None
    for policy in repo.policies_for(conn, restaurant.id):
        if policy["effective_from"] > wanted:
            continue
        if selected is None or (policy["effective_from"], policy["policy_version"]) > (
            selected["effective_from"], selected["policy_version"]
        ):
            selected = policy
    return rules_from_policy(selected) if selected is not None else policy_zero(restaurant)


def rules_for(conn: sqlite3.Connection, restaurant: Restaurant, starts_at_local: str) -> Rules:
    """The rules for a booking, chosen by the booking's own local start date."""
    day = parse_date(starts_at_local[:10]) if len(starts_at_local) >= 10 else None
    if day is None:  # pragma: no cover - the parsing layer rejects these first
        return policy_zero(restaurant)
    return rules_on(conn, restaurant, day)


def require_table(restaurant: Restaurant, table_id: str) -> dict:
    """A table of *this* restaurant — another restaurant's table is also 404."""
    for table in restaurant.tables:
        if table["id"] == table_id:
            return table
    raise not_found(f"Restaurant '{restaurant.id}' has no table '{table_id}'")


def require_tables(restaurant: Restaurant, table_ids: Sequence[str]) -> list[dict]:
    """Every table of a requested set, in the order it was requested."""
    return [require_table(restaurant, table_id) for table_id in table_ids]


def check_combination(restaurant: Restaurant, table_ids: Sequence[str]) -> None:
    """A set of more than two tables, or an undeclared pair, cannot be booked.

    Both are the same refusal: the restaurant decides what may be joined, and a
    set it did not declare is not a seating option however well the party fits.
    """
    if len(table_ids) > 2:
        raise combination_not_allowed(
            f"A booking holds at most two tables; {len(table_ids)} were requested"
        )
    if len(table_ids) == 2 and not restaurant.declares(table_ids):
        raise combination_not_allowed(
            f"Restaurant '{restaurant.id}' does not combine tables "
            f"'{table_ids[0]}' and '{table_ids[1]}'"
        )


# --------------------------------------------------------------------------- #
# slot grid
# --------------------------------------------------------------------------- #
def slot_starts(restaurant: Restaurant, rules: Rules, day: date) -> list[datetime]:
    """Local slot starts for one day, in order, without duplicates.

    A slot exists for every ``slot_minutes`` step from ``opens`` whose sitting
    finishes by ``closes``. Local times skipped by a spring-forward transition
    never appear; a fall-back hour appears once, resolved to its first
    occurrence.
    """
    tz = restaurant.tz
    seen: set[str] = set()
    starts: list[datetime] = []
    for window in rules.windows_on(day):
        for candidate in iter_wall_clock_steps(
            day, window.opens, window.closes, rules.slot_minutes,
            rules.duration_minutes,
        ):
            resolution = resolve_local(candidate, tz)
            if not resolution.exists:
                continue  # inside the skipped hour
            label = candidate.strftime("%Y-%m-%dT%H:%M")
            if label in seen:
                continue  # a repeated local hour is offered once
            seen.add(label)
            starts.append(candidate)
    return starts


@dataclass(frozen=True)
class Sitting:
    """A resolved request to occupy a table."""

    starts_at_local: str
    starts_at: datetime  # aware, restaurant-local offset
    ends_at: datetime    # aware, restaurant-local offset
    starts_at_utc: datetime
    ends_at_utc: datetime

    def stored_fields(self) -> dict:
        """Every time column a reservation row keeps, in one place."""
        return {
            "starts_at_local": self.starts_at_local,
            "starts_at": rfc3339(self.starts_at),
            "ends_at": rfc3339(self.ends_at),
            "starts_at_utc": rfc3339(self.starts_at_utc),
            "ends_at_utc": rfc3339(self.ends_at_utc),
        }


def resolve_sitting(
    restaurant: Restaurant, rules: Rules, starts_at_local: str
) -> Sitting:
    """Validate a requested local start against the calendar and the grid.

    Order matters and follows the spec's own wording: a local time that does not
    exist cannot be on a grid or inside opening hours, so DST comes first; then
    whether the sitting fits the day's service; then whether it is aligned to the
    slot grid.
    """
    naive = parse_local(starts_at_local)
    if naive is None:  # pragma: no cover - parsing layer rejects these first
        raise invalid_local_time(f"'{starts_at_local}' is not a bare local YYYY-MM-DDTHH:MM")

    tz = restaurant.tz
    resolution: LocalResolution = resolve_local(naive, tz)
    if not resolution.exists:
        raise invalid_local_time(
            f"{starts_at_local} does not exist in {restaurant.timezone} "
            "(the clocks spring forward past it)"
        )

    day = naive.date()
    duration = timedelta(minutes=rules.duration_minutes)
    fitting = [
        window
        for window in rules.windows_on(day)
        if datetime.combine(day, window.opens) <= naive
        and naive + duration <= datetime.combine(day, window.closes)
    ]
    if not fitting:
        raise outside_opening_hours()

    on_grid = any(
        int((naive - datetime.combine(day, window.opens)).total_seconds() // 60)
        % rules.slot_minutes
        == 0
        for window in fitting
    )
    if not on_grid:
        raise not_on_slot_grid()

    starts_utc = resolution.utc
    assert starts_utc is not None  # guaranteed by resolution.exists
    ends_utc = starts_utc + duration
    return Sitting(
        starts_at_local=naive.strftime("%Y-%m-%dT%H:%M"),
        starts_at=starts_utc.astimezone(tz),
        ends_at=ends_utc.astimezone(tz),
        starts_at_utc=starts_utc,
        ends_at_utc=ends_utc,
    )


# --------------------------------------------------------------------------- #
# occupancy
# --------------------------------------------------------------------------- #
def parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)



def find_conflict(
    reservations: Iterable[dict],
    *,
    table_id: str,
    starts_at_utc: datetime,
    ends_at_utc: datetime,
    exclude_reference: str | None = None,
) -> dict | None:
    """The first confirmed booking on ``table_id`` overlapping the half-open interval."""
    for candidate in reservations:
        if candidate["table_id"] != table_id:
            continue
        if candidate["status"] != "confirmed":
            continue
        if exclude_reference is not None and candidate["reference"] == exclude_reference:
            continue
        existing_start = parse_utc(candidate["starts_at_utc"])
        existing_end = parse_utc(candidate["ends_at_utc"])
        if starts_at_utc < existing_end and existing_start < ends_at_utc:
            return candidate
    return None


@dataclass(frozen=True)
class Closure:
    """A table taken out of service over a half-open interval in absolute time.

    A closure is not a booking and has no diner attached to it, but it excludes a
    table exactly as a confirmed booking on it would — which is why availability
    reports it under the same `no_overlap` rule, and why a pair is unavailable
    when either of its tables is closed.
    """

    table_id: str
    starts_at_utc: datetime
    ends_at_utc: datetime
    starts_at: str
    ends_at: str
    plan_id: str

    def covers(self, table_id: str, start_utc: datetime, end_utc: datetime) -> bool:
        return (
            table_id == self.table_id
            and start_utc < self.ends_at_utc
            and self.starts_at_utc < end_utc
        )


def load_closures(
    conn: sqlite3.Connection,
    restaurant: Restaurant,
    *,
    start_utc: datetime,
    end_utc: datetime,
) -> list[Closure]:
    """Applied closures that could overlap ``[start_utc, end_utc)``."""
    return [
        Closure(
            table_id=record["table_id"],
            starts_at_utc=parse_utc(record["closed_from_utc"]),
            ends_at_utc=parse_utc(record["closed_to_utc"]),
            starts_at=record["closed_from"],
            ends_at=record["closed_to"],
            plan_id=record["plan_id"],
        )
        for record in repo.closures_in_range(
            conn, restaurant.id, rfc3339(start_utc), rfc3339(end_utc)
        )
    ]


def find_closure(
    closures: Iterable[Closure],
    *,
    table_id: str,
    starts_at_utc: datetime,
    ends_at_utc: datetime,
) -> Closure | None:
    for closure in closures:
        if closure.covers(table_id, starts_at_utc, ends_at_utc):
            return closure
    return None


def check_not_closed(
    closures: Iterable[Closure],
    *,
    table_ids: Sequence[str],
    sitting: Sitting,
) -> None:
    """No table of the set may be closed for any part of the sitting.

    This is an operator's repair, not a diner's mistake, so it is refused the same
    way an occupied table is: 409 `table_unavailable`. A diner's cancellation
    cutoff does not apply — the diner is not the one changing anything.
    """
    for table_id in table_ids:
        closure = find_closure(
            closures,
            table_id=table_id,
            starts_at_utc=sitting.starts_at_utc,
            ends_at_utc=sitting.ends_at_utc,
        )
        if closure is not None:
            raise table_unavailable(
                f"Table '{table_id}' is closed from {closure.starts_at} to {closure.ends_at}"
            )


def load_occupancy(
    conn: sqlite3.Connection,
    restaurant: Restaurant,
    *,
    start_utc: datetime,
    end_utc: datetime,
    exclude_references: Sequence[str] = (),
) -> list[dict]:
    """Confirmed bookings that could clash with ``[start_utc, end_utc)``.

    Padded by a day on each side so a DST shift or an unusual offset can never
    hide a real overlap from the range query.
    """
    slack = timedelta(days=1)
    return repo.confirmed_for_restaurant_in_range(
        conn,
        restaurant.id,
        rfc3339(start_utc - slack),
        rfc3339(end_utc + slack),
        exclude_references=exclude_references,
    )


def combined_capacity(rules: Rules, table_ids: Sequence[str]) -> int:
    """A combination's capacity is the sum of its tables' capacities, at the
    capacities the selected policy gives them."""
    return sum(rules.capacity_of(table_id) for table_id in table_ids)


def check_capacity(rules: Rules, table_ids: Sequence[str], party_size: int) -> None:
    """One table, or a combination of them: the party must fit the whole set."""
    if party_size > combined_capacity(rules, table_ids):
        raise party_exceeds_capacity()


def check_free(
    occupancy: Iterable[dict],
    *,
    table_ids: Sequence[str],
    sitting: Sitting,
    exclude_reference: str | None = None,
) -> None:
    """Every table of the set must be free: one taken member refuses the booking."""
    for table_id in table_ids:
        clash = find_conflict(
            occupancy,
            table_id=table_id,
            starts_at_utc=sitting.starts_at_utc,
            ends_at_utc=sitting.ends_at_utc,
            exclude_reference=exclude_reference,
        )
        if clash is not None:
            raise table_unavailable(
                f"Table '{table_id}' already has a confirmed booking overlapping "
                f"{sitting.starts_at_local}"
            )


# --------------------------------------------------------------------------- #
# availability
# --------------------------------------------------------------------------- #
def availability(
    conn: sqlite3.Connection,
    restaurant: Restaurant,
    day: date,
    party_size: int,
    explain: bool = False,
) -> dict:
    """What the day offers, decided by the policy selected for that date.

    With ``explain``, every slot also accounts for every table of the restaurant —
    available or not, in fixture order — naming both rules that decide it, so a
    diner can see *why* a table is not offered rather than only that it is not.
    """
    tz = restaurant.tz
    rules = rules_on(conn, restaurant, day)
    eligible = [t for t in restaurant.tables if rules.capacity_of(t["id"]) >= party_size]
    by_id = restaurant.table_by_id()
    starts = slot_starts(restaurant, rules, day)

    slots: list[dict] = []
    occupancy: list[dict] = []
    closures: list[Closure] = []
    if starts:
        first_utc = resolve_local(starts[0], tz).utc
        last_utc = resolve_local(starts[-1], tz).utc
        assert first_utc is not None and last_utc is not None
        occupancy = load_occupancy(
            conn,
            restaurant,
            start_utc=first_utc,
            end_utc=last_utc + timedelta(minutes=rules.duration_minutes),
        )
        closures = load_closures(
            conn,
            restaurant,
            start_utc=first_utc,
            end_utc=last_utc + timedelta(minutes=rules.duration_minutes),
        )

    def is_free(table_id: str, start_utc: datetime, end_utc: datetime) -> bool:
        """Nothing else holds this table then: no booking, and no closure.

        A closure is reported under `no_overlap` exactly like a conflicting
        booking, because from a diner's point of view it is the same fact — the
        table cannot be sat at during this sitting.
        """
        if find_closure(
            closures, table_id=table_id, starts_at_utc=start_utc, ends_at_utc=end_utc
        ) is not None:
            return False
        return (
            find_conflict(
                occupancy,
                table_id=table_id,
                starts_at_utc=start_utc,
                ends_at_utc=end_utc,
            )
            is None
        )

    for naive in starts:
        resolution = resolve_local(naive, tz)
        start_utc = resolution.utc
        assert start_utc is not None  # nonexistent slots were filtered out
        end_utc = start_utc + timedelta(minutes=rules.duration_minutes)
        free_tables = [t for t in eligible if is_free(t["id"], start_utc, end_utc)]

        # Every seating option the party could take: single tables in fixture
        # order, then declared pairs in `combinable` order with their tables named
        # in that same order. A pair is offered on its summed capacity, so it can
        # be the only option that fits — and it is offered only when *both* of its
        # tables are free, since booking it occupies the two of them.
        options = [
            {"table_ids": [t["id"]], "capacity": rules.capacity_of(t["id"])}
            for t in free_tables
        ]
        for first, second in restaurant.combinable:
            left, right = by_id.get(first), by_id.get(second)
            if left is None or right is None:  # pragma: no cover - validated at reset
                continue
            capacity = rules.capacity_of(first) + rules.capacity_of(second)
            if capacity < party_size:
                continue
            if not is_free(first, start_utc, end_utc) or not is_free(second, start_utc, end_utc):
                continue
            options.append({"table_ids": [first, second], "capacity": capacity})

        slot: dict = {
            "starts_at_local": naive.strftime("%Y-%m-%dT%H:%M"),
            "starts_at": rfc3339(start_utc.astimezone(tz)),
            # Single tables only, exactly as in stage 1.
            "available_table_ids": [t["id"] for t in free_tables],
            "available_options": options,
        }
        if explain:
            # Every table appears exactly once, in fixture order, and both rules are
            # reported for it — including a table another rule already excluded, so
            # nothing is silently dropped. A rule that holds is reported holding.
            slot["explain"] = [
                {
                    "table_id": table["id"],
                    "policy_version": rules.policy_version,
                    "available": party_size <= rules.capacity_of(table["id"])
                    and is_free(table["id"], start_utc, end_utc),
                    "rules": [
                        {"rule": "capacity",
                         "holds": party_size <= rules.capacity_of(table["id"])},
                        {"rule": "no_overlap",
                         "holds": is_free(table["id"], start_utc, end_utc)},
                    ],
                }
                for table in restaurant.tables
            ]
        slots.append(slot)

    return {
        "restaurant_id": restaurant.id,
        "date": day.isoformat(),
        "timezone": restaurant.timezone,
        "slots": slots,
    }


# --------------------------------------------------------------------------- #
# identities and rendering
# --------------------------------------------------------------------------- #
def new_reservation_id() -> str:
    return f"res_{secrets.token_hex(12)}"


def new_series_id() -> str:
    """An opaque identifier for a recurring agreement."""
    return f"ser_{secrets.token_hex(12)}"


def new_plan_id() -> str:
    """An opaque identifier for a proposed seating plan."""
    return f"pln_{secrets.token_hex(12)}"


def new_reference(conn: sqlite3.Connection) -> str:
    """6-12 characters of A-Z0-9, unique across all reservations, never reused."""
    for _ in range(12):
        candidate = "".join(
            secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH)
        )
        if not repo.reference_exists(conn, candidate):
            return candidate
    raise RuntimeError("could not allocate a unique reference")  # pragma: no cover


def reservation_body(record: dict) -> dict:
    """The reservation shape used by every endpoint that returns one.

    ``table_ids`` is always present. ``table_id`` is present only when the booking
    holds exactly one table, so a single-table booking reads exactly as it did
    before combinations existed and a combination is never reduced to one of its
    members.
    """
    table_ids = list(record.get("table_ids") or [record["table_id"]])
    body: dict = {
        "reservation_id": record["id"],
        "reference": record["reference"],
        "restaurant_id": record["restaurant_id"],
    }
    if len(table_ids) == 1:
        body["table_id"] = table_ids[0]
    body["table_ids"] = table_ids
    body.update(
        {
            "party_size": int(record["party_size"]),
            "status": record["status"],
            "starts_at_local": record["starts_at_local"],
            "starts_at": record["starts_at"],
            "ends_at": record["ends_at"],
            "created_at": record["created_at"],
            # The booking's own state: how many real changes it has been through,
            # and the whole policy it was decided under, frozen with it.
            "revision": int(record.get("revision", 1)),
            "accepted_terms": record.get("accepted_terms") or {},
        }
    )
    return body


def cutoff_deadline(record: dict, cutoff_minutes: int) -> datetime:
    """The instant from which the booking can no longer be changed."""
    return parse_utc(record["starts_at_utc"]) - timedelta(minutes=cutoff_minutes)


def accepted_cutoff(record: dict, restaurant: Restaurant) -> int:
    """The cutoff the diner accepted, which is the one a change is judged by.

    Publishing a tighter policy afterwards does not shorten a booking already
    made, and a looser one does not lengthen it.
    """
    terms = record.get("accepted_terms") or {}
    if "cancellation_cutoff_minutes" in terms:
        return int(terms["cancellation_cutoff_minutes"])
    return restaurant.cutoff_minutes  # pragma: no cover - a row that predates terms


def is_past_cutoff(record: dict, restaurant: Restaurant, now_utc: datetime) -> bool:
    """True when ``now`` is within the accepted cutoff of the current start, or later."""
    return now_utc >= cutoff_deadline(record, accepted_cutoff(record, restaurant))
