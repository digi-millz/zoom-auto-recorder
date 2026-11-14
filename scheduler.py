"""Scheduling maths and the pre-launch countdown.

Everything here is pure or injectable (``sleep``/``clock``/``console``) so the
waiting logic can be unit-tested without waiting.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta
from typing import Callable

START_FORMAT = "%Y-%m-%d %H:%M"

# Accepted in `add` specs and in the JSON store; output is always START_FORMAT.
_DATE_FORMATS = (
    ("%Y-%m-%d %H:%M", True),
    ("%Y-%m-%dT%H:%M", True),
    ("%Y-%m-%d %H:%M:%S", True),
    ("%m/%d/%Y %H:%M", True),
    ("%m/%d/%y %H:%M", True),
    ("%m/%d %H:%M", False),
    ("%Y-%m-%d %I:%M %p", True),
    ("%Y-%m-%d %I %p", True),
    ("%m/%d/%Y %I:%M %p", True),
    ("%m/%d/%Y %I %p", True),
    ("%m/%d/%y %I:%M %p", True),
    ("%m/%d/%y %I %p", True),
    ("%m/%d %I:%M %p", False),
    ("%m/%d %I %p", False),
)
_MERIDIEM = re.compile(r"([ap])\.?\s*m\.?$", re.IGNORECASE)


class ScheduleError(ValueError):
    """Raised when a date/time cannot be interpreted."""


def parse_start(value: str, fmt: str = START_FORMAT) -> datetime:
    """Parse the canonical ``YYYY-MM-DD HH:MM`` form."""
    try:
        return datetime.strptime(value.strip(), fmt)
    except ValueError as exc:
        raise ScheduleError(f"invalid start {value!r}, expected {fmt!r}") from exc


def parse_when(value: str, now: datetime | None = None) -> datetime:
    """Parse a human date/time: ``2026-11-09 10:00``, ``11/9/26 10am``, ``11/9 10:00``.

    Formats without a year use the current year. Naive local time throughout.
    """
    text = " ".join(value.strip().split())
    if not text:
        raise ScheduleError("empty date/time")
    now = now or datetime.now()

    meridiem = _MERIDIEM.search(text)
    if meridiem:
        text = text[: meridiem.start()].strip()
        text = f"{text} {'AM' if meridiem.group(1).lower() == 'a' else 'PM'}"

    for fmt, has_year in _DATE_FORMATS:
        try:
            if has_year:
                parsed = datetime.strptime(text, fmt)
            else:
                # %m/%d without a year triggers a DeprecationWarning on 3.13+,
                # so anchor it to a fixed year and substitute afterwards.
                parsed = datetime.strptime(f"2000 {text}", f"%Y {fmt}").replace(
                    year=now.year
                )
        except ValueError:
            continue
        return parsed

    raise ScheduleError(
        f"cannot parse {value!r}; try 'YYYY-MM-DD HH:MM' or 'M/D/YY h:MM am/pm'"
    )


def seconds_until(target: datetime, now: datetime | None = None) -> float:
    """Seconds from *now* (default: wall clock) until *target*; negative if past."""
    return (target - (now or datetime.now())).total_seconds()


def format_delta(seconds: float) -> str:
    """Render a duration as ``HH:MM:SS``, prefixed with ``Nd`` past 24 hours.

    Rounds up, so a countdown shows ``00:00:01`` until the deadline is actually
    reached instead of sitting on ``00:00:00``.
    """
    total = math.ceil(max(seconds, 0.0))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    clock = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}d {clock}" if days else clock


def next_occurrence(start: datetime, repeat: str, after: datetime) -> datetime:
    """Next weekly/weekday occurrence strictly after *after*."""
    if repeat == "weekly":
        step = timedelta(days=7)
    elif repeat == "weekdays":
        step = timedelta(days=1)
    else:
        raise ScheduleError(f"unknown repeat {repeat!r}; use weekly or weekdays")

    candidate = start
    while candidate <= after:
        candidate += step
        if repeat == "weekdays":
            while candidate.weekday() >= 5:
                candidate += timedelta(days=1)
    return candidate


def render_countdown(remaining: float, target: datetime):
    """Build the live countdown line (Rich ``Text``)."""
    from rich.text import Text

    text = Text()
    text.append("recording starts in ", style="dim")
    text.append(format_delta(remaining), style="bold green" if remaining < 60 else "bold cyan")
    text.append(f"  (at {target:%Y-%m-%d %H:%M:%S})", style="dim")
    return text


def wait_with_countdown(
    target: datetime,
    *,
    console=None,
    tick: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = datetime.now,
) -> None:
    """Block until *target*, rendering a live countdown.

    Sleeps in short slices so Ctrl+C is handled promptly, and recomputes the
    remaining time from the wall clock every iteration so the deadline does not
    drift. Returns immediately when *target* is already in the past.
    """
    remaining = seconds_until(target, clock())
    if remaining <= 0:
        return

    if console is None:
        from rich.console import Console

        console = Console()

    from rich.live import Live

    with Live(
        render_countdown(remaining, target), console=console, refresh_per_second=4
    ) as live:
        while True:
            remaining = seconds_until(target, clock())
            if remaining <= 0:
                break
            live.update(render_countdown(remaining, target))
            sleep(min(tick, remaining))

    console.print(f"[bold green]start time reached[/] ({clock():%Y-%m-%d %H:%M:%S})")
