"""Recurrence with a real timezone and real RRULEs (replaces ``weekly``/``weekdays``).

The store keeps comparing **naive local** datetimes, so everything here takes and returns naive machine-local time
and converts internally:

* ``start_local`` is the wall-clock start the user typed (``"2026-11-09 10:00"``).
* ``tz`` is the meeting's zone (``"Europe/London"``). An empty tz keeps the legacy behaviour: the machine's zone.
* Occurrences are computed with ``dateutil.rrule`` on an aware ``dtstart``, so **10:00 stays 10:00 across a DST
  change** instead of drifting by an hour.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrule, rrulestr

from scheduler import ScheduleError, parse_start

_UNTIL = re.compile(r"UNTIL=([^;]+)", re.IGNORECASE)

LEGACY_RULES = {
    "weekly": "FREQ=WEEKLY",
    "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
}


def zone(tz: str | None) -> ZoneInfo:
    """Resolve an IANA zone name; empty means the machine's zone."""
    if not tz:
        return datetime.now().astimezone().tzinfo  # type: ignore[return-value]
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"unknown timezone {tz!r}") from exc


def legacy_repeat_to_rrule(repeat: str | None) -> str | None:
    """Map the old ``repeat`` field onto an RRULE so old schedule files keep working."""
    if not repeat:
        return None
    key = repeat.strip().lower()
    if key in LEGACY_RULES:
        return LEGACY_RULES[key]
    return None


def normalize_spec(spec: str, tz_name: str | None) -> str:
    """Make ``UNTIL`` UTC when the start is zone-aware (RFC 5545 requires it, dateutil enforces it)."""
    spec = spec.strip().upper()
    if not spec:
        raise ScheduleError("empty recurrence rule")
    if "FREQ=" not in spec:
        raise ScheduleError(f"recurrence rule needs FREQ=: {spec!r}")

    def _fix(match: re.Match[str]) -> str:
        value = match.group(1)
        if value.endswith("Z"):
            return f"UNTIL={value}"
        try:
            local = datetime.strptime(value, "%Y%m%dT%H%M%S")
        except ValueError as exc:
            raise ScheduleError(f"invalid UNTIL in {spec!r}") from exc
        aware = local.replace(tzinfo=zone(tz_name))
        return "UNTIL=" + aware.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")

    return _UNTIL.sub(_fix, spec)


def build(spec: str, start_local: str, tz_name: str | None = None) -> rrule:
    """A dateutil rrule anchored at the meeting's start, in the meeting's zone."""
    tz = zone(tz_name)
    start = parse_start(start_local)
    dtstart = start.replace(tzinfo=tz)
    try:
        return rrulestr(normalize_spec(spec, tz_name), dtstart=dtstart)
    except (ValueError, TypeError) as exc:
        raise ScheduleError(f"invalid recurrence rule {spec!r}: {exc}") from exc


def occurrences(
    spec: str,
    start_local: str,
    tz_name: str | None = None,
    count: int = 5,
    after: datetime | None = None,
) -> list[datetime]:
    """The next ``count`` starts, expressed as naive machine-local datetimes.

    ``after=None`` begins at the start of the series; pass a datetime for "what comes next from here".
    """
    rule = build(spec, start_local, tz_name)
    tz = zone(tz_name)
    if after is None:
        first = rule[0] if rule else datetime.now(tz)
        anchor = first - timedelta(seconds=1)
    else:
        anchor = after.astimezone(tz)
    found: list[datetime] = []
    for moment in rule.xafter(anchor, count=count, inc=False):
        found.append(moment.astimezone().replace(tzinfo=None))
    return found


def next_start_local(
    spec: str,
    start_local: str,
    tz_name: str | None = None,
    after: datetime | None = None,
) -> datetime | None:
    """The single next start after ``after`` (default: now), or ``None`` when the rule is exhausted."""
    found = occurrences(
        spec, start_local, tz_name, count=1, after=after or datetime.now()
    )
    return found[0] if found else None


def describe(spec: str, tz_name: str | None) -> str:
    """Short human summary for the CLI/GUI: ``FREQ=WEEKLY;INTERVAL=2`` + zone."""
    parts = []
    freq = re.search(r"FREQ=([A-Z]+)", spec.upper())
    if freq:
        parts.append(freq.group(1).capitalize())
    interval = re.search(r"INTERVAL=(\d+)", spec.upper())
    if interval and interval.group(1) != "1":
        parts.append(f"every {interval.group(1)}")
    days = re.search(r"BYDAY=([A-Z,]+)", spec.upper())
    if days:
        parts.append(days.group(1))
    return " ".join(parts) + (f" ({tz_name})" if tz_name else "")
