"""Choosing a seating plan after a table closes.

A closure puts every confirmed booking that overlaps it in question, and the
question is not "where can each one go" but "what is the smallest change that
keeps all of them". Diners keep their times, their party sizes and the terms they
accepted; only the tables under them may move, and only as far as they must.

The plan is chosen by minimising three things in a fixed order:

1. how many bookings change tables at all;
2. the seats left empty across all of them, so a party of two is not given the
   six-top when the four-top would do;
3. the vector of option ranks in ascending reference order, which is what makes
   the answer unique once the first two agree — the option list is the same one
   availability offers, singles in fixture order then declared pairs in declared
   order, so "the first option that works" is a rule a manager can see.

Counting changes first, and only then seats, is what makes the result a *repair*:
the room absorbs the closure rather than reshuffling diners who were never on the
closed table. Because the first criterion dominates, the search walks the subsets
of bookings that may move, smallest first, and stops at the first subset that can
be seated — every plan with fewer moves has already been tried and refused.

Everything here is pure: it is given the bookings, the tables they could take and
the things already in the way, and returns the choice or nothing. Reading the
restaurant is the caller's job, which is what lets the whole plan be proposed
inside one transaction and applied inside another.
"""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from . import domain, repo
from .errors import planning_limit

# The sizes a plan is required to support. Beyond them the search is refused
# rather than attempted, because a manager is better told the room is too large to
# re-plan automatically than kept waiting for an answer that may never come.
MAX_TABLES = 6
MAX_PAIRS = 4
MAX_CONSIDERED = 6


@dataclass(frozen=True)
class ClosureInterval:
    """The proposed closure, in the shape the planner compares against."""

    table_id: str
    starts_at: str
    ends_at: str
    starts_at_utc: datetime
    ends_at_utc: datetime

    def as_closure(self) -> domain.Closure:
        """The same interval as an applied closure would be, for the shared checks."""
        return domain.Closure(
            table_id=self.table_id,
            starts_at_utc=self.starts_at_utc,
            ends_at_utc=self.ends_at_utc,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            plan_id="",  # not applied yet: nothing to name
        )


@dataclass(frozen=True)
class Occupant:
    """Something already holding tables over an interval: a fixed booking."""

    reference: str
    tables: frozenset[str]
    starts_at_utc: datetime
    ends_at_utc: datetime


@dataclass
class Subject:
    """One booking the closure puts in question, and the seatings it could take."""

    record: dict
    reference: str
    party_size: int
    starts_at_utc: datetime
    ends_at_utc: datetime
    rules: domain.Rules
    held: tuple[str, ...]
    options: list[tuple[str, ...]]
    held_rank: int | None


@dataclass
class Plan:
    """The chosen seating for every considered booking, in reference order."""

    assignments: list[dict]
    moved_count: int
    unused_seats: int


# --------------------------------------------------------------------------- #
# reading the room
# --------------------------------------------------------------------------- #
def terms_rules(
    conn: sqlite3.Connection, restaurant: domain.Restaurant, record: dict
) -> domain.Rules:
    """The rules a booking is re-seated under: the ones it accepted.

    A repair does not re-decide a booking, so the capacities it is offered are the
    capacities of the policy it was made under, not today's. A row with no usable
    terms — only possible in a snapshot that predates them — falls back to the
    policy selected for its date, which is the closest thing to what it accepted.
    """
    terms = record.get("accepted_terms") or {}
    if isinstance(terms.get("capacities"), dict) and "policy_version" in terms:
        return domain.rules_from_policy(terms)
    return domain.rules_for(conn, restaurant, record["starts_at_local"])  # pragma: no cover


def options_for(
    restaurant: domain.Restaurant, rules: domain.Rules, party_size: int
) -> list[tuple[str, ...]]:
    """Every seating this booking could take, in the order availability offers them.

    Single tables in fixture order, then declared pairs in declaration order, each
    one large enough for the party under the booking's own terms. The position in
    this list is the option's rank, which is the last thing two equally-cheap
    plans are compared by.
    """
    options: list[tuple[str, ...]] = [
        (table["id"],)
        for table in restaurant.tables
        if rules.capacity_of(table["id"]) >= party_size
    ]
    for first, second in restaurant.combinable:
        if rules.capacity_of(first) + rules.capacity_of(second) >= party_size:
            options.append((first, second))
    return options


def consider(
    conn: sqlite3.Connection,
    restaurant: domain.Restaurant,
    closure: ClosureInterval,
) -> tuple[list[Subject], list[Occupant], list[domain.Closure]]:
    """Split the restaurant's confirmed bookings into the ones in question and the rest.

    A booking is *considered* when its sitting overlaps the proposed closure at
    all, whether or not it is on the closed table: the plan may have to move it to
    make room for somebody who is. Everything else is fixed and keeps its
    assignment, but still has to be seated around.
    """
    in_question = repo.confirmed_reservations_in_range(
        conn,
        restaurant.id,
        domain.rfc3339(closure.starts_at_utc),
        domain.rfc3339(closure.ends_at_utc),
    )
    subjects: list[Subject] = []
    for record in in_question:
        rules = terms_rules(conn, restaurant, record)
        tables = tuple(record["table_ids"])
        options = options_for(restaurant, rules, int(record["party_size"]))
        subjects.append(
            Subject(
                record=record,
                reference=record["reference"],
                party_size=int(record["party_size"]),
                starts_at_utc=domain.parse_utc(record["starts_at_utc"]),
                ends_at_utc=domain.parse_utc(record["ends_at_utc"]),
                rules=rules,
                held=tables,
                options=options,
                # A booking that holds something the restaurant no longer offers —
                # only seed data can produce one — has no rank, and so has to move.
                held_rank=next(
                    (index for index, option in enumerate(options)
                     if frozenset(option) == frozenset(tables)),
                    None,
                ),
            )
        )
    subjects.sort(key=lambda subject: subject.reference)

    # The bookings that stay where they are still have to be seated around, and a
    # considered booking's sitting can reach past the closure on either side — so
    # the fixed ones are loaded over the whole span the plan has to cover, not
    # over the closure alone.
    span_start = min(
        (subject.starts_at_utc for subject in subjects), default=closure.starts_at_utc
    )
    span_end = max(
        (subject.ends_at_utc for subject in subjects), default=closure.ends_at_utc
    )
    considered = {subject.reference for subject in subjects}
    occupants = [
        Occupant(
            record["reference"],
            frozenset(record["table_ids"]),
            domain.parse_utc(record["starts_at_utc"]),
            domain.parse_utc(record["ends_at_utc"]),
        )
        for record in repo.confirmed_reservations_in_range(
            conn, restaurant.id, domain.rfc3339(span_start), domain.rfc3339(span_end)
        )
        if record["reference"] not in considered
    ]
    closures = [closure.as_closure()] + domain.load_closures(
        conn, restaurant, start_utc=span_start, end_utc=span_end
    )
    return subjects, occupants, closures


def check_limits(
    restaurant: domain.Restaurant, subjects: Sequence[Subject]
) -> None:
    """Refuse a room too large to re-plan automatically."""
    if len(restaurant.tables) > MAX_TABLES:
        raise planning_limit(
            f"A plan supports at most {MAX_TABLES} tables; this restaurant has "
            f"{len(restaurant.tables)}"
        )
    if len(restaurant.combinable) > MAX_PAIRS:
        raise planning_limit(
            f"A plan supports at most {MAX_PAIRS} declared pairs; this restaurant "
            f"has {len(restaurant.combinable)}"
        )
    if len(subjects) > MAX_CONSIDERED:
        raise planning_limit(
            f"A plan considers at most {MAX_CONSIDERED} bookings; this closure puts "
            f"{len(subjects)} in question"
        )


# --------------------------------------------------------------------------- #
# feasibility
# --------------------------------------------------------------------------- #
def _blocked(
    subject: Subject,
    option: Sequence[str],
    occupants: Iterable[Occupant],
    closures: Iterable[domain.Closure],
) -> bool:
    """True when this seating is already impossible for this booking alone.

    Ignoring the other considered bookings, which are judged as they are assigned:
    a closure over any of the tables, or a fixed booking holding one of them
    during this sitting.
    """
    for table_id in option:
        for closure in closures:
            if closure.covers(table_id, subject.starts_at_utc, subject.ends_at_utc):
                return True
    for occupant in occupants:
        if not (
            subject.starts_at_utc < occupant.ends_at_utc
            and occupant.starts_at_utc < subject.ends_at_utc
        ):
            continue
        if occupant.tables.intersection(option):
            return True
    return False


def _clashes(left: Subject, left_option: Sequence[str],
             right: Subject, right_option: Sequence[str]) -> bool:
    """Two considered bookings cannot share a table at overlapping times."""
    if not set(left_option).intersection(right_option):
        return False
    return (
        left.starts_at_utc < right.ends_at_utc
        and right.starts_at_utc < left.ends_at_utc
    )


def _unused(subject: Subject, option_index: int) -> int:
    """Seats left empty: the option's capacity under this booking's terms."""
    capacity = sum(
        subject.rules.capacity_of(table_id)
        for table_id in subject.options[option_index]
    )
    return capacity - subject.party_size


# --------------------------------------------------------------------------- #
# the search
# --------------------------------------------------------------------------- #
def _choices(
    subjects: Sequence[Subject],
    occupants: Iterable[Occupant],
    closures: Iterable[domain.Closure],
    movers: frozenset[int],
) -> list[list[int]] | None:
    """The option indices each booking may take, or None if one has none.

    A booking outside ``movers`` keeps its tables, so it has one choice — and if
    that choice is impossible, this subset of movers cannot work. A booking inside
    ``movers`` must genuinely change tables; an assignment that left it where it
    was belongs to a smaller subset, which has already been tried.
    """
    choices: list[list[int]] = []
    for index, subject in enumerate(subjects):
        if index in movers:
            allowed = [
                option_index
                for option_index in range(len(subject.options))
                if frozenset(subject.options[option_index]) != frozenset(subject.held)
            ]
        elif subject.held_rank is None:
            return None  # holds something that is not an option: it has to move
        else:
            allowed = [subject.held_rank]
        feasible = [
            option_index
            for option_index in allowed
            if not _blocked(subject, subject.options[option_index], occupants, closures)
        ]
        if not feasible:
            return None
        feasible.sort(key=lambda option_index: (_unused(subject, option_index),
                                                option_index))
        choices.append(feasible)
    return choices


def _search(
    subjects: Sequence[Subject],
    occupants: Iterable[Occupant],
    closures: Iterable[domain.Closure],
    movers: frozenset[int],
) -> list[int] | None:
    """The best seating for this set of movers, or None if they cannot all be seated.

    Depth-first in reference order with two admissible bounds: the least seats any
    completion could waste, and the least rank vector any completion could have.
    A branch is dropped as soon as it cannot beat what has already been found,
    which is what keeps the worst case — six bookings, ten options each — inside
    the request rather than outside it.
    """
    choices = _choices(subjects, occupants, closures, movers)
    if choices is None:
        return None
    count = len(subjects)
    least_unused = [min(_unused(s, i) for i in choices[i_index])
                    for i_index, s in enumerate(subjects)]
    least_rank = [min(choices[i_index]) for i_index in range(count)]
    # Suffix sums, so the bound at a position is one lookup.
    unused_after = [0] * (count + 1)
    for index in range(count - 1, -1, -1):
        unused_after[index] = unused_after[index + 1] + least_unused[index]

    best: tuple[int, tuple[int, ...], list[int]] | None = None
    chosen: list[int] = [-1] * count

    def walk(position: int, unused_so_far: int, ranks: tuple[int, ...]) -> None:
        nonlocal best
        if best is not None:
            best_unused, best_ranks, _ = best
            projected = unused_so_far + unused_after[position]
            if projected > best_unused:
                return
            if projected == best_unused and ranks + tuple(least_rank[position:]) >= best_ranks:
                return
        if position == count:
            best = (unused_so_far, ranks, list(chosen))
            return
        subject = subjects[position]
        for option_index in choices[position]:
            option = subject.options[option_index]
            # Everything before this position is already seated, so a clash with
            # any of them is a clash with the plan being built.
            if any(
                _clashes(subject, option, subjects[earlier],
                         subjects[earlier].options[chosen[earlier]])
                for earlier in range(position)
            ):
                continue
            chosen[position] = option_index
            walk(position + 1, unused_so_far + _unused(subject, option_index),
                 ranks + (option_index,))
            chosen[position] = -1

    walk(0, 0, ())
    return best[2] if best is not None else None


def best_plan(
    subjects: Sequence[Subject],
    occupants: Iterable[Occupant],
    closures: Iterable[domain.Closure],
) -> Plan | None:
    """The plan that changes the fewest bookings, or None if there is no plan.

    Subsets of movers are tried smallest first, so the first subset that can be
    seated is the one with the fewest changes — and within it the search has
    already minimised wasted seats and then option ranks.
    """
    occupants = list(occupants)
    closures = list(closures)
    count = len(subjects)
    for size in range(count + 1):
        for movers in itertools.combinations(range(count), size):
            chosen = _search(subjects, occupants, closures, frozenset(movers))
            if chosen is None:
                continue
            assignments = []
            moved = 0
            unused = 0
            for index, subject in enumerate(subjects):
                option = subject.options[chosen[index]]
                changed = frozenset(option) != frozenset(subject.held)
                moved += 1 if changed else 0
                unused += _unused(subject, chosen[index])
                assignments.append(
                    {
                        "reference": subject.reference,
                        # A booking that stays where it was is reported as it holds
                        # its tables, in the order it asked for them; a booking that
                        # moves is reported as the room offers the option, which is
                        # the declared order for a pair.
                        "table_ids": list(option) if changed else list(subject.held),
                        "changed": changed,
                        "rank": chosen[index],
                        "record": subject.record,
                    }
                )
            return Plan(assignments=assignments, moved_count=moved, unused_seats=unused)
    return None
