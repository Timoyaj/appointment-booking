"""Local time, offsets and DST.

The spec fixes three behaviours, and all of them live here:

* ``starts_at_local`` is a bare wall-clock ``YYYY-MM-DDTHH:MM`` at the
  restaurant, resolved against its IANA zone;
* a local time in a spring-forward gap does not exist and must be rejected,
  while a local time in a fall-back fold occurs twice and always resolves to the
  **first** occurrence (before the clocks change);
* ``reservation_duration_minutes`` is absolute time, so ``ends_at`` is the start
  instant plus the duration, rendered back into local time with whatever offset
  applies at that moment.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_LOCAL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$")
# An instant with an explicit offset: `Z`, `+02:00` or `-0530` all count, and a
# bare local time does not -- a closure is an interval in absolute time, so it has
# to say which one it means.
_INSTANT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?"
    r"(?:[Zz]|[+-]\d{2}:?\d{2})$"
)
_TIME_RE = re.compile(r"^(\d{2}):(\d{2})$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class UnknownTimezone(ValueError):
    pass


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnknownTimezone(name) from exc


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def rfc3339(moment: datetime) -> str:
    """RFC 3339 with an explicit offset and second precision, e.g. +02:00."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat(timespec="seconds")


def format_local(moment: datetime, tz: ZoneInfo) -> str:
    """Wall-clock ``YYYY-MM-DDTHH:MM`` at ``tz``."""
    local = moment.astimezone(tz)
    return local.strftime("%Y-%m-%dT%H:%M")


def parse_local(value: str) -> datetime | None:
    """Parse a bare ``YYYY-MM-DDTHH:MM``; ``None`` when the format is wrong.

    No offset and no ``Z`` are accepted here on purpose — an offset makes the
    string a different thing than the spec allows.
    """
    match = _LOCAL_RE.match(value or "")
    if not match:
        return None
    year, month, day, hour, minute = (int(part) for part in match.groups())
    try:
        # Naive on purpose: this is a bare wall-clock time whose zone is the
        # restaurant's, decided later by resolve_local().
        return datetime(year, month, day, hour, minute)  # noqa: DTZ001
    except ValueError:
        return None


def parse_instant(value: str) -> datetime | None:
    """An RFC 3339 instant **with an explicit offset**; None when it has none.

    A closure is an interval in absolute time, so the string that names it has to
    say which instant it means: `2026-09-28T18:00:00+02:00` and the same moment
    written `16:00:00Z` are one interval, while `2026-09-28T18:00` is a wall
    clock somewhere and is refused. Seconds and a fractional part are optional
    because the API returns second precision and a client may not.
    """
    if not isinstance(value, str) or not _INSTANT_RE.match(value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        # A well-formed but impossible instant, like February the 30th.
        return None


def parse_date(value: str) -> date | None:
    match = _DATE_RE.match(value or "")
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_hhmm(value: str) -> time | None:
    match = _TIME_RE.match(value or "")
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def weekday_name(day: date) -> str:
    return WEEKDAYS[day.weekday()]


# --------------------------------------------------------------------------- #
# DST resolution
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LocalResolution:
    """What a bare local wall-clock time means in a given zone."""

    kind: str  # "normal" | "ambiguous" | "nonexistent"
    naive: datetime
    utc: datetime | None = None
    aware: datetime | None = None

    @property
    def exists(self) -> bool:
        return self.kind != "nonexistent"

    @property
    def ambiguous(self) -> bool:
        return self.kind == "ambiguous"


def resolve_local(naive: datetime, tz: ZoneInfo) -> LocalResolution:
    """Classify a bare local time and resolve it to an instant.

    Ambiguous times resolve to the first occurrence (``fold=0``), which is the
    one before the clocks change; nonexistent times are reported as such.
    """
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    utc_first = first.astimezone(timezone.utc)
    utc_second = second.astimezone(timezone.utc)

    if utc_first == utc_second:
        # Only one candidate instant. It is a real local time only if converting
        # it back reproduces the wall clock we started from; otherwise the time
        # falls in a spring-forward gap.
        if utc_first.astimezone(tz).replace(tzinfo=None) == naive:
            return LocalResolution("normal", naive, utc_first, first)
        return LocalResolution("nonexistent", naive)

    # The two folds disagree, so this wall clock is either repeated or skipped.
    if utc_first.astimezone(tz).replace(tzinfo=None) == naive:
        return LocalResolution("ambiguous", naive, utc_first, first)
    return LocalResolution("nonexistent", naive)


def to_utc(naive: datetime, tz: ZoneInfo) -> datetime | None:
    resolution = resolve_local(naive, tz)
    return resolution.utc if resolution.exists else None


def local_date_bounds_utc(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC bounds wide enough to cover every instant of one local day.

    Used to load only the reservations that could overlap a day's slots, with a
    day of slack on each side for DST shifts.
    """
    start = datetime.combine(day, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(day, time(23, 59, 59), tzinfo=tz).astimezone(timezone.utc)
    return start - timedelta(days=1), end + timedelta(days=1)


def iter_wall_clock_steps(
    day: date, opens: time, closes: time, step_minutes: int, duration_minutes: int
) -> Sequence[datetime]:
    """Local slot starts: every ``step_minutes`` from ``opens`` that still fits.

    Wall-clock arithmetic on purpose — the grid the diner sees is labelled in
    local time, and the rule is ``slot + duration <= closes``.
    """
    if step_minutes <= 0:
        return []
    step = timedelta(minutes=step_minutes)
    cursor = datetime.combine(day, opens)
    limit = datetime.combine(day, closes)
    out: list[datetime] = []
    while cursor + timedelta(minutes=duration_minutes) <= limit:
        out.append(cursor)
        cursor = cursor + step
    return out


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Half-open interval overlap: a 19:00/90-minute sitting does not touch 20:30."""
    return a_start < b_end and b_start < a_end
