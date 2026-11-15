"""Minimal iCalendar (.ics) import: VEVENT -> scheduled Zoom meeting.

Only what matters here is parsed: DTSTART (with its TZID), SUMMARY, the first Zoom link in
DESCRIPTION/URL/LOCATION/SUMMARY, and the RRULE. The rule is passed through rather than flattened — the schedule
stores RFC 5545 rules and ``recurrence.py`` does the maths — so "every 2 weeks on Wednesday" survives the import.

A rule this recorder cannot express (``BYMONTHDAY``, ``WKST``, …) is dropped to a single occurrence instead of
being mis-scheduled, and that is logged.

``fetch()`` takes the same text from an ``http(s)`` URL, which is what a Google Calendar "secret address in iCal
format" is.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import recurrence
from scheduler import ScheduleError

log = logging.getLogger(__name__)

ZOOM_URL = re.compile(
    r"(https?://[^\s\"<>]*zoom\.us/[^\s\"<>]+|zoommtg://[^\s\"<>]+)", re.IGNORECASE
)
DEFAULT_TIME = (10, 0)

# RRULE parts this recorder can schedule. Anything else means "import one occurrence".
SUPPORTED_RULE_PARTS = frozenset({"FREQ", "INTERVAL", "BYDAY", "UNTIL", "COUNT"})


@dataclass(frozen=True)
class IcsEvent:
    when: datetime
    url: str
    summary: str = ""
    rrule: str | None = None
    tz: str | None = None


def unfold(text: str) -> list[str]:
    """Undo RFC 5545 line folding."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_dt(value: str, tz_name: str | None = None) -> datetime | None:
    """DTSTART -> naive machine-local time.

    Handles ``…Z`` (UTC), a bare local stamp, and a local stamp qualified by ``TZID`` (what Google and Outlook
    emit). The schedule compares naive local times, so the conversion happens here, once.
    """
    value = value.strip()
    if not value:
        return None
    if value.endswith("Z"):
        for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%MZ"):
            try:
                return (
                    datetime.strptime(value, fmt)
                    .replace(tzinfo=timezone.utc)
                    .astimezone()
                    .replace(tzinfo=None)
                )
            except ValueError:
                continue
        return None

    parsed: datetime | None = None
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt == "%Y%m%d":
            parsed = parsed.replace(hour=DEFAULT_TIME[0], minute=DEFAULT_TIME[1])
        break
    if parsed is None:
        return None
    if tz_name:
        try:
            zone = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            log.debug("unknown TZID %r on DTSTART; treating it as local time", tz_name)
            return parsed
        return parsed.replace(tzinfo=zone).astimezone().replace(tzinfo=None)
    return parsed


def _rule(value: str, when: datetime, tz_name: str | None) -> str | None:
    """The RRULE if this recorder can schedule it, else ``None`` (one occurrence)."""
    spec = (value or "").strip().upper()
    if not spec:
        return None
    parts = [part for part in spec.split(";") if part]
    names = {part.split("=", 1)[0] for part in parts}
    unsupported = names - SUPPORTED_RULE_PARTS
    if unsupported:
        log.info(
            "recurrence %r uses %s, which this recorder does not schedule; importing a single occurrence",
            spec,
            ", ".join(sorted(unsupported)),
        )
        return None
    try:
        recurrence.build(spec, when.strftime("%Y-%m-%d %H:%M"), tz_name)
    except ScheduleError as exc:
        log.info("dropping unsupported recurrence %r (%s)", spec, exc)
        return None
    return spec


def _build(block: dict[str, str]) -> IcsEvent | None:
    tz_name = block.get("DTSTART_TZID") or None
    when = _parse_dt(block.get("DTSTART", ""), tz_name)
    if when is None:
        return None
    haystack = " ".join(
        block.get(key, "") for key in ("DESCRIPTION", "URL", "LOCATION", "SUMMARY")
    )
    match = ZOOM_URL.search(haystack)
    if match is None:
        return None
    url = match.group(1).rstrip(".,;)")
    return IcsEvent(
        when=when,
        url=url,
        summary=block.get("SUMMARY", "").strip(),
        rrule=_rule(block.get("RRULE", ""), when, tz_name),
        tz=tz_name,
    )


def parse_events(text: str) -> list[IcsEvent]:
    """Every VEVENT carrying a Zoom link, in file order."""
    events: list[IcsEvent] = []
    block: dict[str, str] | None = None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            block = {}
            continue
        if line == "END:VEVENT":
            if block is not None:
                event = _build(block)
                if event is not None:
                    events.append(event)
            block = None
            continue
        if block is None or ":" not in line:
            continue
        name, value = line.split(":", 1)
        parameters = name.split(";")
        key = parameters[0].upper()
        block.setdefault(key, value)
        if key == "DTSTART":
            for parameter in parameters[1:]:
                if "=" in parameter:
                    param_name, _, param_value = parameter.partition("=")
                    if param_name.strip().upper() == "TZID":
                        block.setdefault("DTSTART_TZID", param_value.strip())
    return events


def fetch(url: str, timeout: float = 20.0) -> str:
    """Download an ``http(s)`` iCalendar feed (a calendar's "secret address"). Raises ``OSError`` on failure."""
    request = urllib.request.Request(
        url.strip(),
        headers={"User-Agent": "zoom-auto-recorder", "Accept": "text/calendar, text/plain, */*"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")
