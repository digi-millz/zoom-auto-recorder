"""Persistent schedule of Zoom meetings to record.

The store is a plain JSON file so it can be edited by hand. Every mutation
re-reads the file first, which keeps a running recorder from clobbering entries
added from another shell.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Iterable

from scheduler import START_FORMAT, ScheduleError, next_occurrence, parse_when

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_MISSED = "missed"


class StoreError(RuntimeError):
    """Raised when the schedule file cannot be read or written."""



def split_entry_spec(spec: str) -> tuple[str, str]:
    """Split ``"11/9/26 10am https://zoom.us/j/123"`` into ``(when, url)``.

    The URL may appear anywhere in the line; everything else is the date/time.
    """
    tokens = spec.split()
    for index, token in enumerate(tokens):
        if "://" in token or token.lower().startswith("zoom.us/"):
            when = " ".join(tokens[:index] + tokens[index + 1 :]).strip()
            if not when:
                raise ValueError(f"no date/time in {spec!r}")
            return when, token
    raise ValueError(f"no meeting URL in {spec!r}")


@dataclass
class Entry:
    id: str
    url: str
    start: str  # "YYYY-MM-DD HH:MM", local wall clock
    output_dir: str | None = None
    note: str = ""
    repeat: str | None = None
    status: str = STATUS_PENDING
    created: str = ""
    last_exit_code: int | None = None
    last_path: str | None = None

    @property
    def start_at(self) -> datetime:
        return parse_when(self.start)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Entry:
        known = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


class ScheduleStore:
    """JSON-backed list of scheduled meetings."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.entries: list[Entry] = []
        self.reload()

    # -- io ---------------------------------------------------------------- #

    def reload(self) -> None:
        if not self.path.exists():
            self.entries = []
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError(f"cannot read {self.path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise StoreError(f"{self.path}: expected a JSON object")

        entries: list[Entry] = []
        for item in payload.get("entries", []):
            if not isinstance(item, dict):
                raise StoreError(f"{self.path}: entry is not an object: {item!r}")
            entry = Entry.from_dict(item)
            try:
                entry.start_at
            except ScheduleError as exc:
                raise StoreError(f"{self.path}: entry {entry.id} has an invalid start: {exc}") from exc
            entries.append(entry)
        self.entries = entries

    def save(self) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "entries": [entry.to_dict() for entry in self.entries],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            raise StoreError(f"cannot write {self.path}: {exc}") from exc

    # -- queries ------------------------------------------------------------ #

    def sorted_entries(self) -> list[Entry]:
        return sorted(self.entries, key=lambda entry: (entry.start_at, entry.created, entry.id))

    def pending(self) -> list[Entry]:
        return [entry for entry in self.sorted_entries() if entry.status == STATUS_PENDING]

    def get(self, entry_id: str) -> Entry | None:
        return next((entry for entry in self.entries if entry.id == entry_id), None)

    # -- mutations (always re-read first) ----------------------------------- #

    def add(
        self,
        *,
        url: str,
        start_at: str,
        output_dir: str | None = None,
        note: str = "",
        repeat: str | None = None,
    ) -> Entry:
        repeat = (repeat or "").strip().lower() or None
        if repeat in ("none", "once"):
            repeat = None
        if repeat not in (None, "weekly", "weekdays"):
            raise ScheduleError(f"unknown repeat {repeat!r}; use weekly or weekdays")
        start = parse_when(start_at)
        self.reload()
        entry = Entry(
            id=uuid.uuid4().hex[:8],
            url=url.strip(),
            start=start.strftime(START_FORMAT),
            output_dir=output_dir,
            note=note.strip(),
            repeat=repeat or None,
            created=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.entries.append(entry)
        self.save()
        return entry

    def remove(self, entry_id: str) -> bool:
        self.reload()
        remaining = [entry for entry in self.entries if entry.id != entry_id]
        if len(remaining) == len(self.entries):
            return False
        self.entries = remaining
        self.save()
        return True

    def mark(
        self,
        entry_id: str,
        status: str,
        *,
        exit_code: int | None = None,
        path: str | None = None,
    ) -> Entry | None:
        self.reload()
        entry = self.get(entry_id)
        if entry is None:
            return None
        entry.status = status
        if exit_code is not None:
            entry.last_exit_code = exit_code
        if path is not None:
            entry.last_path = path
        self.save()
        return entry

    def roll_repeat(self, entry_id: str, now: datetime | None = None) -> Entry | None:
        """Advance a repeating entry to its next occurrence and re-arm it."""
        self.reload()
        entry = self.get(entry_id)
        if entry is None or not entry.repeat:
            return None
        current = now or datetime.now()
        entry.start = next_occurrence(
            entry.start_at, entry.repeat, current
        ).strftime(START_FORMAT)
        entry.status = STATUS_PENDING
        self.save()
        return entry

    def recover_abandoned(self) -> int:
        """Mark entries left ``running`` by a crashed runner as failed.

        Only safe after the runner lock is held.
        """
        self.reload()
        abandoned = [entry for entry in self.entries if entry.status == STATUS_RUNNING]
        for entry in abandoned:
            entry.status = STATUS_FAILED
        if abandoned:
            self.save()
        return len(abandoned)


def next_action(
    entries: Iterable[Entry], now: datetime, max_late: float
) -> tuple[str, Entry | None]:
    """Decide what the runner should do next.

    Returns ``("idle", None)`` when nothing is pending, ``("wait", entry)`` when
    the earliest entry is still in the future, ``("missed", entry)`` when it is
    too far in the past to be worth recording, else ``("due", entry)``.
    """
    pending = sorted(
        (entry for entry in entries if entry.status == STATUS_PENDING),
        key=lambda entry: (entry.start_at, entry.created, entry.id),
    )
    if not pending:
        return "idle", None

    earliest = pending[0]
    delta = (earliest.start_at - now).total_seconds()
    if delta > 0:
        return "wait", earliest
    if -delta > max_late:
        return "missed", earliest
    return "due", earliest



def stop_marker_path(schedule_path: str | Path) -> Path:
    path = Path(schedule_path).expanduser().resolve()
    return path.with_name(path.name + ".stop")


def request_stop(schedule_path: str | Path) -> Path:
    """Ask a running runner to stop after the current step (cross-process)."""
    marker = stop_marker_path(schedule_path)
    marker.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    return marker


def stop_requested(schedule_path: str | Path) -> bool:
    return stop_marker_path(schedule_path).exists()


def clear_stop(schedule_path: str | Path) -> None:
    with suppress(OSError):
        stop_marker_path(schedule_path).unlink()


class RunnerLock:
    """Single-runner guard so two processes never record the same entry."""

    def __init__(self, schedule_path: str | Path) -> None:
        self.path = Path(schedule_path).expanduser().resolve().with_name(
            Path(schedule_path).name + ".lock"
        )
        self._held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._owner_alive():
                    return False
                with suppress(OSError):
                    self.path.unlink()
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            self._held = True
            return True
        return False

    def owner_pid(self) -> int | None:
        """Pid of the live process holding the lock, if any."""
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return None
        if pid <= 0:
            return None
        import psutil

        try:
            return pid if psutil.pid_exists(pid) else None
        except Exception:  # pragma: no cover - defensive
            return None

    def _owner_alive(self) -> bool:
        return self.owner_pid() is not None

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            if self.path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass


