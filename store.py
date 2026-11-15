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
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Iterable

import recurrence

from scheduler import START_FORMAT, ScheduleError, parse_when

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# How long an empty lock file is trusted to be "being written right now" rather than abandoned.
LOCK_SETTLE = 2.0

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
    email: str | None = None  # Zoom prejoin email for this meeting; empty uses the global default
    repeat: str | None = None  # legacy: "weekly" / "weekdays"; kept for old schedule files
    rrule: str | None = None  # RFC 5545 recurrence, e.g. "FREQ=WEEKLY;INTERVAL=2;BYDAY=WE"
    tz: str | None = None  # IANA zone name; empty means this machine's zone
    anchor: str | None = None  # first start, so COUNT/UNTIL keep counting from the beginning
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
        self.settings: dict[str, str] = {}
        self.reload()

    # -- io ---------------------------------------------------------------- #

    def reload(self) -> None:
        self.settings = {}
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

        raw_settings = payload.get("settings")
        if isinstance(raw_settings, dict):
            self.settings = {
                str(key): str(value)
                for key, value in raw_settings.items()
                if isinstance(value, (str, int, float)) and str(value).strip()
            }
        self.entries = entries

    def save(self) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "settings": dict(self.settings),
            "entries": [entry.to_dict() for entry in self.entries],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            raise StoreError(f"cannot write {self.path}: {exc}") from exc

    # -- settings ----------------------------------------------------------- #

    def setting(self, key: str, default: str | None = None) -> str | None:
        value = self.settings.get(str(key))
        return value if value else default

    def set_setting(self, key: str, value: str) -> None:
        """Persist one setting next to the entries (re-read first, like every mutation)."""
        self.reload()
        self.settings[str(key)] = str(value)
        self.save()

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
        email: str | None = None,
        repeat: str | None = None,
        rrule: str | None = None,
        tz: str | None = None,
    ) -> Entry:
        repeat = (repeat or "").strip().lower() or None
        if repeat in ("none", "once"):
            repeat = None
        if repeat not in (None, "weekly", "weekdays"):
            raise ScheduleError(f"unknown repeat {repeat!r}; use weekly or weekdays")
        start = parse_when(start_at)
        spec = (rrule or "").strip() or recurrence.legacy_repeat_to_rrule(repeat)
        if spec:
            # Validate now, against a real zone, so a bad rule cannot poison the schedule file.
            recurrence.build(spec, start.strftime(START_FORMAT), tz)
        if tz:
            recurrence.zone(tz)
        clean_email = (email or "").strip() or None
        if clean_email and ("@" not in clean_email or clean_email.endswith("@")):
            raise ScheduleError(f"{clean_email!r} is not an email address")
        self.reload()
        entry = Entry(
            id=uuid.uuid4().hex[:8],
            url=url.strip(),
            start=start.strftime(START_FORMAT),
            output_dir=output_dir,
            note=note.strip(),
            email=clean_email,
            repeat=repeat or None,
            rrule=spec or None,
            tz=(tz or "").strip() or None,
            anchor=start.strftime(START_FORMAT) if spec else None,
            created=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.entries.append(entry)
        self.save()
        return entry

    def update(self, entry_id: str, **changes: object) -> Entry | None:
        """Change fields of an existing entry and re-arm it.

        Only the keys passed are touched; ``None`` means "leave alone" (so a CLI flag that was not given cannot
        blank a value). Validation runs before anything is written, so a bad edit leaves the file untouched.
        """
        allowed = {"url", "start_at", "output_dir", "note", "email", "repeat", "rrule", "tz"}
        unknown = set(changes) - allowed
        if unknown:
            raise ScheduleError(f"cannot edit {sorted(unknown)}")
        self.reload()
        entry = self.get(entry_id)
        if entry is None:
            return None

        start = parse_when(str(changes["start_at"])) if changes.get("start_at") else entry.start_at
        repeat = changes.get("repeat")
        if repeat is not None:
            repeat = (str(repeat) or "").strip().lower() or None
            if repeat in ("none", "once"):
                repeat = None
            if repeat not in (None, "weekly", "weekdays"):
                raise ScheduleError(f"unknown repeat {repeat!r}; use weekly or weekdays")
        else:
            repeat = entry.repeat

        rrule = changes.get("rrule")
        spec = (str(rrule) or "").strip() if rrule is not None else (entry.rrule or "")
        if not spec:
            spec = recurrence.legacy_repeat_to_rrule(repeat) or ""
        tz = changes.get("tz") if changes.get("tz") is not None else entry.tz
        tz = str(tz or "").strip() or None
        if spec:
            recurrence.build(spec, start.strftime(START_FORMAT), tz)
        if tz:
            recurrence.zone(tz)

        email = changes.get("email")
        if email is not None:
            email = str(email).strip() or None
            if email and ("@" not in email or email.endswith("@")):
                raise ScheduleError(f"{email!r} is not an email address")
        else:
            email = entry.email

        entry.start = start.strftime(START_FORMAT)
        entry.repeat = repeat
        entry.rrule = spec or None
        entry.tz = tz
        entry.anchor = start.strftime(START_FORMAT) if spec else None
        entry.email = email
        if changes.get("url") is not None:
            entry.url = str(changes["url"]).strip()
        if changes.get("note") is not None:
            entry.note = str(changes["note"]).strip()
        if changes.get("output_dir") is not None:
            entry.output_dir = str(changes["output_dir"]).strip() or None
        # An edit is a re-arm: a finished or missed entry becomes schedulable again.
        entry.status = STATUS_PENDING
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
        """Advance a repeating entry to its next occurrence and re-arm it.

        Uses the entry's RRULE (legacy ``repeat`` values map onto one). When the rule is exhausted — ``UNTIL`` or
        ``COUNT`` reached — the entry is marked done instead of being re-armed forever.
        """
        self.reload()
        entry = self.get(entry_id)
        if entry is None:
            return None
        spec = entry.rrule or recurrence.legacy_repeat_to_rrule(entry.repeat)
        if not spec:
            return None
        current = now or datetime.now()
        following = recurrence.next_start_local(
            spec, entry.anchor or entry.start, entry.tz, after=current
        )
        if following is None:
            log.info("recurrence for %s is finished (%s); marking done", entry.id, spec)
            entry.status = STATUS_DONE
            self.save()
            return entry
        entry.start = following.strftime(START_FORMAT)
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
        """True when the lock is genuinely held.

        A parseable pid decides on its own: alive → held, dead → stale (that is how a crashed runner is recovered).
        An **empty** file is the one ambiguity — ``acquire`` creates it with O_EXCL and writes the pid a moment
        later, and treating that instant as stale let two runners own the lock (seen live). So an empty lock is
        only reclaimed once it is older than :data:`LOCK_SETTLE`.
        """
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if raw:
            try:
                pid = int(raw)
            except ValueError:
                return False
            if pid <= 0:
                return False
            import psutil

            try:
                return psutil.pid_exists(pid)
            except Exception:  # pragma: no cover - defensive
                return True
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False
        return age < LOCK_SETTLE

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            if self.path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass


