"""What a restaurant can see about its own room.

A booking system a restaurant pays for is one that answers questions the owner
actually asks: how many covers did we do, when do people come, how many of them
pay and do not turn up, and which tables earn the floor space they take up.

Everything here is computed from what the service already stores — reservations,
the history of their changes, the tables they hold and the money captured on them
— so nothing new is written and nothing is estimated. Where a number would need a
guess, it is not reported: a made-up occupancy figure is worse than no occupancy
figure, because somebody will staff a shift against it.

Two things are deliberately *not* here. Revenue is money **actually captured**,
not the menu price of the covers, because the service never sees the bill. And
utilisation is measured against the hours the restaurant published, not against
somebody's idea of a full house.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta

from . import repo
from .errors import validation_failed
from .tztime import parse_date

MAX_RANGE_DAYS = 366


def window(params: dict) -> tuple[date, date]:
    """The window asked for: ``from`` and ``to``, inclusive, as calendar dates."""
    raw_from = params.get("from")
    raw_to = params.get("to")
    if raw_from is None or raw_to is None:
        raise validation_failed("'from' and 'to' are required, as YYYY-MM-DD dates")
    start = parse_date(raw_from)
    end = parse_date(raw_to)
    if start is None or end is None:
        raise validation_failed("'from' and 'to' must be dates written YYYY-MM-DD")
    if end < start:
        raise validation_failed("'to' must not be before 'from'")
    if (end - start).days > MAX_RANGE_DAYS:
        raise validation_failed(f"A reporting window is at most {MAX_RANGE_DAYS} days")
    return start, end


def _rows(conn: sqlite3.Connection, restaurant_id: str, start: date, end: date) -> list[dict]:
    """Every booking whose **local** start date falls in the window.

    Filtered on the local date rather than on a UTC instant: a report says "the
    covers we did on Saturday", and Saturday is a fact about the restaurant's own
    calendar, not about Greenwich.

    The member tables are attached the same way every other reader gets them: a
    party seated across t_1 and t_2 counts for both, which is the whole reason a
    restaurant looks at table use.
    """
    found = repo.rows(
        conn,
        "SELECT r.* FROM reservations r WHERE r.restaurant_id = ? "
        "AND r.starts_at_local >= ? AND r.starts_at_local < ? "
        "ORDER BY r.starts_at_local",
        (restaurant_id, start.isoformat(), (end + timedelta(days=1)).isoformat()),
    )
    members = repo.tables_for_references(conn, [record["reference"] for record in found])
    for record in found:
        record["table_ids"] = members.get(record["reference"]) or [record["table_id"]]
    return found


def _seats(restaurant_tables: list[dict]) -> int:
    return sum(int(table["capacity"]) for table in restaurant_tables)


def summary(
    conn: sqlite3.Connection, restaurant, *, start: date, end: date, params: dict
) -> dict:
    """The numbers a restaurant asks for, over a window of its own calendar."""
    records = _rows(conn, restaurant.id, start, end)
    tables = restaurant.tables
    seats = _seats(tables)

    confirmed = [r for r in records if r["status"] == "confirmed"]
    cancelled = [r for r in records if r["status"] == "cancelled"]
    no_shows = [r for r in records if r["status"] == "no_show"]

    covers = sum(int(r["party_size"]) for r in confirmed)
    no_show_covers = sum(int(r["party_size"]) for r in no_shows)
    cancelled_covers = sum(int(r["party_size"]) for r in cancelled)

    # Covers by the hour a diner actually sits down: the question a kitchen
    # roster is built from.
    by_hour: Counter[int] = Counter()
    by_weekday: Counter[str] = Counter()
    by_party: Counter[str] = Counter()
    for record in confirmed:
        local = record["starts_at_local"]
        hour = int(local[11:13]) if len(local) >= 13 else 0
        by_hour[hour] += int(record["party_size"])
        by_weekday[local[:10]] += int(record["party_size"])
        party = int(record["party_size"])
        bucket = "1-2" if party <= 2 else "3-4" if party <= 4 else "5-6" if party <= 6 else "7+"
        by_party[bucket] += 1

    # Lead time: how far ahead people book. Between the day it was made and the
    # day it is for, measured in days, because that is the unit a restaurant plans
    # in.
    lead_times: list[int] = []
    for record in records:
        made = record.get("created_at") or ""
        booked_for = record.get("starts_at_local") or ""
        try:
            made_day = datetime.fromisoformat(made).date()
            booked_day = date.fromisoformat(booked_for[:10])
        except ValueError:
            continue
        lead_times.append(max(0, (booked_day - made_day).days))

    # Table use: which tables earn their floor space. Bookings and covers only —
    # a table that is always free is the finding, and it does not need an index
    # invented for it.
    use: dict[str, dict] = {
        table["id"]: {
            "table_id": table["id"],
            "label": table["label"],
            "capacity": int(table["capacity"]),
            "bookings": 0,
            "covers": 0,
        }
        for table in tables
    }
    for record in confirmed:
        for table_id in record.get("table_ids") or [record["table_id"]]:
            entry = use.get(table_id)
            if entry is None:
                continue
            entry["bookings"] += 1
            entry["covers"] += int(record["party_size"])

    # Capacity: how many covers the room could have served in the window had it
    # been full for every sitting it published, and what fraction it served.
    # Derived from the published hours, so a restaurant that shortens service
    # sees its own decision, not a benchmark somebody else chose.
    from . import domain

    sittings_per_day: dict[str, int] = {}
    day = start
    available_covers = 0
    while day <= end:
        # The rules for that date, so a report over a period where the restaurant
        # published new hours or a new sitting length measures each day against
        # what it actually promised that day.
        rules = domain.rules_on(conn, restaurant, day)
        minutes = 0
        for window in rules.hours.get(domain.weekday_name(day), []):
            minutes += (window.closes.hour * 60 + window.closes.minute) - (
                window.opens.hour * 60 + window.opens.minute
            )
        duration = max(1, int(rules.duration_minutes))
        sittings = minutes // duration if minutes else 0
        sittings_per_day[day.isoformat()] = sittings
        available_covers += sittings * seats
        day += timedelta(days=1)

    captured_cents = repo.captured_cents_for_restaurant(
        conn, restaurant.id, from_date=start.isoformat(), to_date=(end + timedelta(days=1)).isoformat()
    )

    deposits = repo.payment_settings_for(conn, restaurant.id)
    attempts = repo.payment_attempts_for(conn, restaurant.id)
    declined = [a for a in attempts if a["outcome"] == "declined"]

    total_bookings = len(records)
    return {
        "restaurant_id": restaurant.id,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "bookings": {
            "total": total_bookings,
            "confirmed": len(confirmed),
            "cancelled": len(cancelled),
            "no_show": len(no_shows),
        },
        "covers": {
            "served": covers,
            "cancelled": cancelled_covers,
            "no_show": no_show_covers,
            "available": available_covers,
            # Zero rather than null when the window had no service at all: a
            # restaurant closed for a week served 0% of nothing, and a division
            # by zero is not a report.
            "utilisation": round(covers / available_covers, 4) if available_covers else 0.0,
        },
        "no_show_rate": round(len(no_shows) / total_bookings, 4) if total_bookings else 0.0,
        "cancellation_rate": round(len(cancelled) / total_bookings, 4) if total_bookings else 0.0,
        "covers_by_hour": {str(hour): by_hour[hour] for hour in sorted(by_hour)},
        "covers_by_day": {day: by_weekday[day] for day in sorted(by_weekday)},
        "bookings_by_party_size": {k: by_party[k] for k in ("1-2", "3-4", "5-6", "7+") if by_party[k]},
        "lead_time_days": {
            "bookings_measured": len(lead_times),
            "average": round(sum(lead_times) / len(lead_times), 1) if lead_times else None,
            "same_day": sum(1 for lead in lead_times if lead == 0),
            "within_a_week": sum(1 for lead in lead_times if lead <= 7),
        },
        "table_use": sorted(use.values(), key=lambda entry: (-entry["covers"], entry["label"])),
        "money": {
            "deposits_published": deposits is not None,
            "currency": deposits["currency"] if deposits else None,
            "captured_cents": captured_cents,
            "attempts": len(attempts),
            "declined": len(declined),
        },
        "sittings_published": sum(sittings_per_day.values()),
    }


def bookings_csv(
    conn: sqlite3.Connection, restaurant, *, start: date, end: date
) -> str:
    """The window's bookings as CSV, for whoever keeps the spreadsheet.

    Every booking, whatever became of it: a report that quietly dropped the
    cancellations would answer "how busy were we" with the wrong number.
    """
    from . import domain

    records = _rows(conn, restaurant.id, start, end)
    labels = {table["id"]: table["label"] for table in restaurant.tables}
    header = [
        "reference", "date", "time", "tables", "party_size", "status",
        "created_at", "cancellation_cutoff_minutes",
    ]
    lines = [",".join(header)]
    for record in records:
        tables = record.get("table_ids") or [record["table_id"]]
        local = record["starts_at_local"]
        terms = record.get("accepted_terms") or "{}"
        cutoff = ""
        try:
            import json

            cutoff = str(json.loads(terms).get("cancellation_cutoff_minutes", ""))
        except (ValueError, TypeError):  # pragma: no cover - stored by us
            cutoff = ""
        values = [
            record["reference"],
            local[:10],
            local[11:16],
            " ".join(labels.get(table_id, table_id) for table_id in tables),
            str(int(record["party_size"])),
            record["status"],
            record.get("created_at") or "",
            cutoff,
        ]
        lines.append(",".join(_csv_cell(value) for value in values))
    return "\n".join(lines) + "\n"


def _csv_cell(value: str) -> str:
    """A CSV cell that cannot break the row, and cannot be read as a formula.

    A leading =, +, - or @ is what a spreadsheet treats as a formula, and this
    file is opened in one. Prefixing it with an apostrophe is the standard
    defence and costs nothing.
    """
    text = str(value)
    if text[:1] in ("=", "+", "-", "@"):
        text = "'" + text
    if any(character in text for character in (",", '"', "\n", "\r")):
        return '"' + text.replace('"', '""') + '"'
    return text
