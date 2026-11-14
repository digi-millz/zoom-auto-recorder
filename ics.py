"""Minimal iCalendar (.ics) import: VEVENT -> scheduled Zoom meeting.

Only what matters here is parsed: DTSTART, SUMMARY, DTEND is ignored, the first
Zoom link in DESCRIPTION/URL/LOCATION/SUMMARY, and a weekly RRULE. Recurring
rules with COUNT/UNTIL/BYDAY are reduced to "weekly" — the runner rolls the entry
forward itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

ZOOM_URL = re.compile(
    r"(https?://[^\s\"<>]*zoom\.us/[^\s\"<>]+|zoommtg://[^\s\"<>]+)", re.IGNORECASE
)
DEFAULT_TIME = (10, 0)


@dataclass(frozen=True)
class IcsEvent:
    when: datetime
    url: str
    summary: str = ""
    repeat: str | None = None


def unfold(text: str) -> list[str]:
    """Undo RFC 5545 line folding."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_dt(value: str) -> datetime | None:
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
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt == "%Y%m%d":
            parsed = parsed.replace(hour=DEFAULT_TIME[0], minute=DEFAULT_TIME[1])
        return parsed
    return None


def _build(block: dict[str, str]) -> IcsEvent | None:
    when = _parse_dt(block.get("DTSTART", ""))
    if when is None:
        return None
    haystack = " ".join(
        block.get(key, "") for key in ("DESCRIPTION", "URL", "LOCATION", "SUMMARY")
    )
    match = ZOOM_URL.search(haystack)
    if match is None:
        return None
    url = match.group(1).rstrip(".,;)")
    repeat = "weekly" if "FREQ=WEEKLY" in block.get("RRULE", "").upper() else None
    return IcsEvent(
        when=when,
        url=url,
        summary=block.get("SUMMARY", "").strip(),
        repeat=repeat,
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
        block.setdefault(name.split(";", 1)[0].upper(), value)
    return events
