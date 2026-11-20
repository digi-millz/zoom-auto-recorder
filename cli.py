"""Click entry point: schedule files, countdown, drive Zoom, record with OBS."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, NoReturn
from xml.sax.saxutils import escape as xml_escape

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

import invite
import notify
import zoom_account

from obs_controller import (
    ObsConfig,
    ObsController,
    ObsError,
    clear_crash_flag,
    find_obs_exe,
    find_recent_recording,
    RECORDING_SUFFIXES,
    is_obs_running,
    is_websocket_reachable,
    launch_obs,
    wait_for_websocket,
)
from scheduler import (
    START_FORMAT,
    ScheduleError,
    format_delta,
    parse_start,
    parse_when,
    render_countdown,
    runner_needed,
    seconds_until,
    wait_with_countdown,
)
from store import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_MISSED,
    STATUS_PENDING,
    STATUS_RUNNING,
    Entry,
    RunnerLock,
    ScheduleStore,
    StoreError,
    append_run,
    clear_stop,
    next_action,
    read_runs,
    request_stop,
    split_entry_spec,
    stop_requested,
)
from window_manager import (
    WindowNotFound,
    close_window,
    current_meeting_window,
    desktop_locked,
    display_awake,
    find_meeting_window,
    find_process,
    is_windows,
    iter_windows,
    launch_meeting,
    maximize,
    restore_window,
    to_zoommtg,
)

import dialogs
import ocr
import postprocess
import report
import scene
import win_capture
from watcher import DialogWatcher

EXIT_OK = 0
EXIT_OBS = 3
EXIT_NO_WINDOW = 4
EXIT_RECORD = 5
EXIT_LOST = 6
EXIT_NO_FILE = 7
EXIT_DISK = 8
EXIT_SESSION = 9

DEFAULT_SCHEDULE = "./schedule.json"
DEFAULT_OUTPUT = "./recordings"

# Shared with the UI so the value shown and the value typed cannot drift apart.

console = Console()
log = logging.getLogger("recorder")


def _setup_logging(verbose: bool, log_file: str | None) -> None:
    rich_handler = RichHandler(
        console=console, show_path=False, rich_tracebacks=True, markup=False
    )
    # RichHandler draws its own time column; adding asctime here duplicates it.
    rich_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    handlers: list[logging.Handler] = [rich_handler]
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        handlers.append(file_handler)

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=handlers,
        force=True,
    )
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("obsws").setLevel(logging.WARNING)


def _fail(code: int, message: str) -> NoReturn:
    log.error("%s", message)
    raise click.exceptions.Exit(code)


def _obs_password(flag_value: str | None) -> str:
    """``--obs-password``/``OBS_PASSWORD`` minus the line ending a Windows environment value carries.

    Set as a registry value the password arrives as ``"secret\r"``; OBS answers the handshake with an empty response
    and the only message is "password may be incorrect", so the CR is trimmed where the value enters.
    """
    return (flag_value or "").rstrip("\r\n")



def _recording_title(window_title: str, url: str, preferred: str | None = None) -> str:
    """A name for the file: the meeting topic when Zoom exposes one, else the meeting id.

    The waiting room and webinar windows are titled "Zoom Workplace" / "Zoom Webinar", which would name every
    recording the same thing. Before falling back to the meeting id, *preferred* is used — the topic the prejoin
    window showed ("Day 1 of Wealth Workshop"), which is what a person would have named the file.
    """
    title = (window_title or "").strip()
    if title.lower() not in dialogs.GENERIC_TITLES:
        return title
    hint = (preferred or "").strip()
    if hint.lower() not in dialogs.GENERIC_TITLES:
        return hint
    target = to_zoommtg(url) or ""
    match = re.search(r"confno=(\d+)", target)
    return f"zoom-{match.group(1)}" if match else (title or "meeting")


def _part_title(title: str, part: int) -> str:
    """``"Weekly sync"`` for the first part, ``"Weekly sync (part 2)"`` after an OBS resume."""
    return title if part <= 1 else f"{title} (part {part})"


def free_gb(path: Path) -> float | None:
    """Free space in GB for *path*, or ``None`` when it cannot be read."""
    try:
        return shutil.disk_usage(path).free / 1_000_000_000
    except OSError as exc:
        log.debug("cannot read free space for %s: %s", path, exc)
        return None


def disk_is_low(path: Path, min_free_gb: float) -> bool:
    """True when the volume holding *path* has less than *min_free_gb* left.

    An unreadable path is **not** treated as low: a transient stat failure must not end a recording.
    """
    if not min_free_gb:
        return False
    available = free_gb(path)
    return available is not None and available < min_free_gb

DISK_CHECK_INTERVAL = 30.0

# First capture check a few seconds in (OBS needs a frame or two to start rendering), then periodically.
BLACK_FIRST_CHECK = 10.0
BLACK_RECHECK = 60.0

# How many times a crashed OBS is relaunched mid-meeting. Two is enough to survive a bad driver update; beyond
# that the machine is unhealthy and the parts on disk are the recording.
MAX_OBS_RESUMES = 2

# Canvas + output resolution presets. "auto" leaves whatever OBS has configured, which is what earlier runs used.
QUALITY_SIZES = {"720p": (1280, 720), "1080p": (1920, 1080)}

# How often the runner re-reads a configured calendar feed. Google refreshes its iCal export with some delay, so
# polling faster than this buys nothing.
CALENDAR_SYNC_INTERVAL = 900.0

WAITING_ROOM_CLASS = "zWaitingRoomWndClass"


def waiting_room_expired(entered: float | None, now: float, timeout: float) -> bool:
    """True when we have been in Zoom's waiting room longer than *timeout* (0 = never)."""
    return bool(timeout) and entered is not None and now - entered >= timeout


def _monitor(
    controller: ObsController,
    max_duration: float,
    close_grace: float,
    should_stop: Callable[[], bool] = lambda: False,
    extra_classes: tuple[str, ...] = (),
    title_pattern=None,
    on_window_change: Callable[[object], None] | None = None,
    waiting_room_timeout: float = 0.0,
    output_path: Path | None = None,
    min_free_gb: float = 0.0,
    capture_luma: Callable[[], int | None] | None = None,
    on_black: Callable[[str], None] | None = None,
    repoint_capture: Callable[[], None] | None = None,
) -> int:
    """Record until the meeting window goes away for *close_grace* seconds.

    Re-enumerates every second instead of trusting one HWND: Zoom recreates its meeting window on
    screen-share/fullscreen changes, and the waiting room is replaced by the real meeting window on admission.
    Both would otherwise leave OBS pointed at a window that no longer exists (black frames for the rest of the
    recording), so a change is reported through *on_window_change*.

    There is no time limit by default: the recording ends when the host closes the meeting. ``max_duration`` and
    ``waiting_room_timeout`` are the two opt-in exceptions — the second one exists because a host who never starts
    the meeting leaves us recording a waiting-room screen indefinitely.

    ``capture_luma`` is sampled shortly after the start and then periodically: a black frame means a minimised
    window or a locked session, which produce a file of pure black. That is reported through *on_black*, and one
    re-point of the capture is attempted before giving up.
    """
    started = time.monotonic()
    gone_since: float | None = None
    waiting_since: float | None = None
    current: tuple[int, str, str] | None = None
    next_disk_check = started
    next_luma_check = started + BLACK_FIRST_CHECK if capture_luma else None
    black_streak = 0
    while True:
        now = time.monotonic()
        if should_stop():
            log.info("stop requested; ending the recording")
            return EXIT_OK

        window = current_meeting_window(
            extra_classes=extra_classes, title_pattern=title_pattern
        )
        if window is None:
            current = None
            waiting_since = None
            if find_process() is None:
                log.info("Zoom process exited")
                return EXIT_OK
            if gone_since is None:
                gone_since = now
                log.info(
                    "no Zoom meeting window; stopping in %.0fs unless it returns",
                    close_grace,
                )
            elif now - gone_since >= close_grace:
                log.info("Zoom meeting window closed")
                return EXIT_OK
        else:
            gone_since = None
            identity = (window.hwnd, window.cls, window.title)
            if identity != current:
                current = identity
                if on_window_change is not None:
                    try:
                        on_window_change(window)
                    except Exception as exc:  # noqa: BLE001 - never end a recording over this
                        log.warning("could not re-point the capture: %s", exc)

            if window.cls == WAITING_ROOM_CLASS:
                if waiting_since is None:
                    waiting_since = now
                    if waiting_room_timeout:
                        log.info(
                            "in Zoom's waiting room; giving the host up to %.0f min to admit us",
                            waiting_room_timeout / 60,
                        )
                elif waiting_room_expired(waiting_since, now, waiting_room_timeout):
                    log.warning(
                        "still in the waiting room after %s; stopping the recording "
                        "(the waiting-room window is left open — closing it opens Zoom's "
                        "'End meeting or leave meeting?' dialog, which nobody should click blind)",
                        format_delta(now - waiting_since),
                    )
                    return EXIT_OK
            else:
                waiting_since = None

        if output_path is not None and now >= next_disk_check:
            next_disk_check = now + DISK_CHECK_INTERVAL
            if disk_is_low(output_path, min_free_gb):
                log.error(
                    "free space in %s dropped below %.1f GB; stopping the recording to keep the file usable",
                    output_path,
                    min_free_gb,
                )
                return EXIT_DISK

        if next_luma_check is not None and now >= next_luma_check:
            next_luma_check = now + BLACK_RECHECK
            luma = capture_luma()  # type: ignore[misc]
            if scene.looks_black(luma):
                black_streak += 1
                log.warning(
                    "capture is black (max luminance %s, streak %d) — the Zoom window is probably "
                    "minimised or the session locked",
                    luma,
                    black_streak,
                )
                if black_streak == 1 and repoint_capture is not None:
                    try:
                        repoint_capture()
                        log.info("re-pointed the capture once; will check again in %.0fs", BLACK_RECHECK)
                    except Exception as exc:  # noqa: BLE001 - never end a recording over a retry
                        log.warning("could not re-point the capture: %s", exc)
                if on_black is not None:
                    on_black(f"capture black for {black_streak} checks (max luminance {luma})")
            else:
                if black_streak:
                    log.info("capture is producing frames again (max luminance %s)", luma)
                black_streak = 0

        if max_duration and now - started >= max_duration:
            log.warning("maximum recording duration (%.0fs) reached", max_duration)
            return EXIT_OK

        try:
            recording = controller.is_recording()
        except ObsError as exc:
            log.error("lost the OBS WebSocket connection: %s", exc)
            return EXIT_LOST
        if not recording:
            log.error("OBS stopped recording by itself")
            return EXIT_RECORD

        time.sleep(1.0)


def _wait_until_ready(
    schedule: ScheduleStore, entry: Entry, poll: float, max_late: float
) -> None:
    """Wait for *entry* to start, re-reading the schedule every *poll* seconds.

    Returns early when the schedule changes, so a meeting added during a long
    wait is picked up immediately instead of being recorded late.
    """
    from rich.live import Live

    target = entry.start_at
    remaining = seconds_until(target)
    if remaining <= 0:
        return

    next_reload = time.monotonic() + poll
    with Live(
        render_countdown(remaining, target), console=console, refresh_per_second=4
    ) as live:
        while True:
            remaining = seconds_until(target)
            if remaining <= 0:
                return
            if stop_requested(schedule.path):
                log.info("stop requested while waiting for %s", entry.id)
                return
            live.update(render_countdown(remaining, target))
            time.sleep(min(1.0, remaining))
            if stop_requested(schedule.path):
                log.info("stop requested while waiting for %s", entry.id)
                return
            if time.monotonic() < next_reload:
                continue
            next_reload = time.monotonic() + poll
            try:
                schedule.reload()
            except StoreError as exc:
                log.warning("cannot re-read the schedule: %s", exc)
                continue
            _, candidate = next_action(schedule.entries, datetime.now(), max_late)
            if candidate is not None and candidate.id != entry.id:
                log.info("schedule changed; re-evaluating before %s", entry.id)
                return


def _check_orphan_recording(config: ObsConfig, stop_orphan: bool) -> None:
    """Warn about, or optionally stop, an OBS recording no entry owns."""
    controller = ObsController(config)
    try:
        controller.connect()
    except ObsError as exc:
        log.debug("skipping the orphan-recording check: %s", exc)
        return
    try:
        if not controller.is_recording():
            return
        if stop_orphan:
            path = controller.stop_record(timeout=15.0)
            log.warning(
                "stopped an untracked OBS recording (file: %s)", path or "unknown"
            )
        else:
            log.warning(
                "OBS is already recording but no schedule entry owns it; leaving it "
                "running (pass --stop-orphan-recording to stop it)"
            )
    except ObsError as exc:
        log.warning("could not inspect the active OBS recording: %s", exc)
    finally:
        controller.close()


def _preflight(
    url: str,
    zoom_target: str | None,
    target: datetime,
    output_dir: Path,
    config: ObsConfig,
    obs_exe: str | None,
) -> int:
    console.rule("[bold]preflight")
    rows = [
        ("zoom url", url),
        ("zoommtg url", zoom_target or "(browser fallback)"),
        ("start", f"{target:%Y-%m-%d %H:%M}"),
        ("output dir", str(output_dir)),
        ("obs endpoint", f"{config.host}:{config.port}"),
        ("obs password", "set" if config.password else "(empty)"),
        ("obs process", "running" if is_obs_running() else "not running"),
        ("obs exe", str(find_obs_exe(obs_exe) or "(not found)")),
        ("windows", "yes" if is_windows() else "no (recording drive unavailable)"),
    ]
    for label, value in rows:
        console.print(f"{label:<14}: {value}")

    controller = ObsController(config)
    try:
        version = controller.connect()
        console.print(f"{'obs version':<14}: {version}")
        console.print(f"{'obs recording':<14}: {controller.is_recording()}")
    except ObsError as exc:
        console.print(f"[red]OBS check failed: {exc}[/]")
        return EXIT_OBS
    finally:
        controller.close()

    console.print("[green]preflight OK[/]")
    return EXIT_OK


@dataclass(frozen=True)
class SessionResult:
    exit_code: int
    path: str | None = None
    interrupted: bool = False
    shots_dir: Path | None = None


def _sweep_empty_recordings(directory: Path, since: datetime) -> int:
    """Delete zero-byte recordings this session produced.

    A crash between ``StartRecord`` and the first flushed frame leaves a 0-byte file (an mp4 that never got its
    footer, or an mkv killed inside its first second). They are unusable, they clutter the folder, and a later
    ``find_recent_recording`` could otherwise pick one as "the" recording.
    """
    removed = 0
    cutoff = since.timestamp() - 5
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if entry.suffix.lower() not in RECORDING_SUFFIXES:
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        if stat.st_size == 0 and stat.st_mtime >= cutoff:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def _verify_recording(
    controller: ObsController,
    reported: str | None,
    output_path: Path,
    since: datetime,
) -> str | None:
    """Confirm a non-empty file exists, waiting briefly for OBS to flush it.

    Falls back to OBS's own configured folder when it ignored --output-dir.
    """
    directories = [output_path]
    try:
        obs_directory = controller.record_directory()
    except ObsError:
        # OBS died (the usual reason we are here); its own folder is simply unknown.
        obs_directory = None
    if obs_directory:
        directories.append(obs_directory)

    deadline = time.monotonic() + 5.0
    while True:
        if reported:
            candidate = Path(reported)
            try:
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return str(candidate)
            except OSError:
                pass
        found = find_recent_recording(directories, since)
        if found is not None:
            return str(found)
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def _record_session(
    *,
    url: str,
    output_path: Path,
    config: ObsConfig,
    obs_exe: str | None,
    launch_mode: str,
    join_timeout: float,
    join_delay: float,
    max_duration: float,
    close_grace: float,
    keep_recording_on_interrupt: bool,
    min_free_gb: float = 2.0,
    should_stop: Callable[[], bool] | None = None,
    window_classes: tuple[str, ...] = (),
    window_title: str | None = None,
    passcode: str | None = None,
    email: str | None = None,
    auto_accept: bool = False,
    auto_accept_seconds: float = 20.0,
    waiting_room_timeout: float = 0.0,
    notify_url: str | None = None,
    steal_meeting: bool = True,
    quality: str = "auto",
    mic: bool = False,
) -> SessionResult:
    """Open the meeting, record it, stop when the window closes. Never raises Exit."""
    should_stop = should_stop or (lambda: False)
    title_pattern = None
    if window_title:
        try:
            title_pattern = re.compile(window_title, re.IGNORECASE)
        except re.error as exc:
            log.error("invalid --window-title %r: %s", window_title, exc)
            return SessionResult(1)

    session_started = datetime.now()
    if min_free_gb:
        try:
            output_path.mkdir(parents=True, exist_ok=True)
            free_gb = shutil.disk_usage(output_path).free / 1_000_000_000
        except OSError as exc:
            log.error("cannot check free space in %s: %s", output_path, exc)
            return SessionResult(EXIT_DISK)
        if free_gb < min_free_gb:
            log.error(
                "only %.1f GB free in %s; need %.1f GB (lower --min-free-gb to override)",
                free_gb,
                output_path,
                min_free_gb,
            )
            return SessionResult(EXIT_DISK)

    if not is_windows():
        log.error("this tool drives the Windows Zoom client; Windows is required")
        return SessionResult(1)

    # A locked session makes every window capture black, so this is caught before anything is launched rather
    # than ten seconds into the file. Unknown failures report "not locked": a diagnostic must not block a meeting.
    if desktop_locked():
        log.error(
            "the Windows session is locked: window capture would record a black rectangle with audio; "
            "unlock and run again (the entry stays pending)"
        )
        return SessionResult(EXIT_SESSION)

    # A pasted invite carries the passcode in prose; canonicalise once so the URI has ?pwd= and no dialog is
    # needed. An unparseable URL is left exactly as given (the browser fallback may still handle it).
    room = invite.parse(url)
    join_url = room.url or url
    effective_passcode = passcode or room.passcode
    if join_url != url.strip():
        log.info("using join link %s (from the pasted invitation)", join_url)

    # A stale .sentinel makes OBS open a "Crash Detected / safe mode" modal that blocks the WebSocket server.
    clear_crash_flag()
    log.info("launching Zoom: %s", join_url)
    try:
        launch_meeting(join_url, mode=launch_mode, passcode=effective_passcode)
    except (OSError, RuntimeError) as exc:
        log.error("could not launch Zoom: %s", exc)
        return SessionResult(1)

    shots_dir = output_path / "_shots" / session_started.strftime("%Y%m%d-%H%M%S")
    def on_alert(alert) -> None:
        log.error(
            "needs_attention: unhandled Zoom dialog %s %r (shot: %s)",
            alert.cls,
            alert.title,
            alert.screenshot or "none",
        )
        notify.send(
            notify_url,
            notify.Event(
                notify.NEEDS_ATTENTION,
                meeting=alert.title or alert.cls,
                detail=(alert.text or "")[:300],
            ),
        )

    rules = dialogs.load_rules()
    if not steal_meeting:
        rules = rules.without("leave-and-join")
        log.info("--no-steal-meeting: leaving a meeting Zoom is already in will raise needs_attention")

    watcher = DialogWatcher(
        rules=rules,
        dry_run=not auto_accept,
        shots_dir=shots_dir,
        email=email or zoom_account.discover(),
        on_alert=on_alert,
    )
    watcher.start()

    try:
        window = find_meeting_window(
            timeout=join_timeout,
            extra_classes=window_classes,
            title_pattern=title_pattern,
        )
    except WindowNotFound as exc:
        log.error("%s", exc)
        return SessionResult(EXIT_NO_WINDOW)

    try:
        maximize(window.hwnd)
    except OSError as exc:
        log.warning("could not maximise the Zoom window: %s", exc)
    else:
        log.info("Zoom window hwnd=%#x is maximised", window.hwnd)

    if join_delay > 0:
        log.info("waiting %.0fs for Zoom to settle before recording", join_delay)
        try:
            time.sleep(join_delay)
        except KeyboardInterrupt:
            console.print("[yellow]cancelled before the recording started[/]")
            return SessionResult(EXIT_OK, interrupted=True)

    controller = ObsController(config)
    adopted = False
    try:
        controller.connect()
    except ObsError as exc:
        log.warning("%s", exc)
        if is_obs_running():
            # The process exists but the server is not listening *yet* — Windows is slow to start OBS, and a
            # scheduled run often begins seconds after login. Give it the same grace as a launch we do ourselves.
            log.info("OBS is running but not listening yet; waiting up to %.0fs for its WebSocket", 45.0)
            try:
                wait_for_websocket(controller)
            except ObsError:
                controller.close()
                log.error(
                    "OBS is running but its WebSocket server never came up; enable "
                    "Tools > WebSocket Server Settings (port 4455) and pass --obs-password "
                    "when authentication is on"
                )
                return SessionResult(EXIT_OBS)
            log.info("OBS is reachable now; continuing")
        else:
            exe = find_obs_exe(obs_exe)
            if exe is None:
                controller.close()
                log.error("OBS is not running and obs64.exe could not be found; pass --obs-exe")
                return SessionResult(EXIT_OBS)
            log.info("OBS is not running; launching %s", exe)
            process = launch_obs(exe, start_recording=True)
            try:
                wait_for_websocket(controller)
            except ObsError as late:
                controller.close()
                log.error(
                    "%s; OBS pid %s was launched and may already be recording "
                    "— stop it manually",
                    late,
                    process.pid,
                )
                return SessionResult(EXIT_OBS)
            adopted = True
            log.warning(
                "recording was started by the OBS launch parameters; --output-dir does not "
                "apply to this file"
            )

    recording_format = controller.recording_format()
    if recording_format and recording_format.lower() in {"mp4", "mov"}:
        log.warning(
            "OBS is recording to %s; a crash or forced kill can leave the file "
            "unplayable — set OBS's recording format to mkv",
            recording_format,
        )

    recording_started = adopted
    keep_recording = False
    saved_path: str | None = None
    interrupted = False
    exit_code = EXIT_OK
    try:
        if not adopted:
            if controller.is_recording():
                log.error("OBS is already recording; refusing to take over an existing recording")
                return SessionResult(1)
            if not controller.ensure_record_dir(output_path):
                log.warning("OBS keeps its configured recording directory")
            # mkv is written incrementally, so a crash still leaves a playable file (an mp4 does not — measured
            # 0 bytes); postprocess remuxes it to mp4 after the stop.
            controller.ensure_recording_format("mkv")
            if quality in QUALITY_SIZES:
                controller.set_video_size(*QUALITY_SIZES[quality])
            try:
                scene.prepare(controller.call, window, mic=mic)
            except Exception as exc:  # noqa: BLE001 - never block a recording on scene setup
                log.warning(
                    "could not configure the OBS scene (%s); recording whatever OBS has selected", exc
                )
            try:
                controller.start_record()
            except ObsError as exc:
                log.error("could not start the OBS recording: %s", exc)
                return SessionResult(EXIT_RECORD)
            except KeyboardInterrupt:
                # OBS may have accepted StartRecord before the interrupt landed.
                with suppress(ObsError):
                    controller.stop_record(timeout=10.0)
                console.print("[yellow]interrupted while starting the recording[/]")
                return SessionResult(EXIT_OK, interrupted=True)
            recording_started = True

        if max_duration:
            log.info("recording until the meeting ends (safety cap %.0fs)", max_duration)
        else:
            log.info("recording until the host closes the meeting (no time limit)")

        def repoint(new_window) -> None:
            """Keep OBS pointed at the current meeting window (waiting room → meeting, recreations)."""
            log.info(
                "meeting window changed to %s (%r); re-pointing the OBS capture",
                new_window.cls,
                new_window.title,
            )
            scene.prepare(controller.call, new_window, mic=mic)

        def capture_luma() -> int | None:
            return scene.source_luma(controller.call)

        def report_black(detail: str) -> None:
            log.error("needs_attention: %s", detail)
            notify.send(
                notify_url,
                notify.Event(
                    notify.NEEDS_ATTENTION,
                    meeting=_recording_title(window.title, url),
                    detail=detail,
                ),
            )

        def repair_capture() -> None:
            """A black frame usually means a minimised or recreated window: un-minimise it, then re-point.

            Re-pointing at a minimised window would capture black again, so the restore comes first and the
            window is looked up again afterwards (restoring can recreate the capture handle).
            """
            target = current_meeting_window(
                extra_classes=window_classes, title_pattern=title_pattern
            ) or window
            if target is None:
                return
            if restore_window(target.hwnd):
                log.info("the meeting window was minimised; restored it")
                time.sleep(0.4)
                target = (
                    current_meeting_window(
                        extra_classes=window_classes, title_pattern=title_pattern
                    )
                    or target
                )
            scene.prepare(controller.call, target, mic=mic)

        def monitor_once() -> int:
            return _monitor(
                controller,
                max_duration,
                close_grace,
                should_stop,
                window_classes,
                title_pattern,
                on_window_change=repoint,
                waiting_room_timeout=waiting_room_timeout,
                output_path=output_path,
                min_free_gb=min_free_gb,
                capture_luma=capture_luma,
                on_black=report_black,
                # Re-point at the *current* window, not the one captured at start: a black frame usually means
                # the window was recreated or minimised, and re-preparing a stale handle would not help.
                repoint_capture=repair_capture,
            )

        exit_code = monitor_once()
        part_index = 1
        resumes = 0
        # A clean "OBS stopped the recording" and a dead WebSocket look different from the outside, but both mean
        # the same thing: the recording ended while the meeting did not. A force-killed OBS is always EXIT_LOST.
        def attempt_resume() -> int | None:
            """Relaunch OBS and restart the recording. Returns the new exit code, or ``None`` to stop trying."""
            nonlocal controller, part_index
            alive = current_meeting_window(
                extra_classes=window_classes, title_pattern=title_pattern
            )
            if alive is None:
                log.error("OBS stopped and the meeting window is gone; nothing left to record")
                return None
            log.warning(
                "OBS stopped recording on its own (exit %s); resuming as part %d (OBS will be relaunched)",
                exit_code,
                part_index + 1,
            )
            notify.send(
                notify_url,
                notify.Event(
                    notify.NEEDS_ATTENTION,
                    meeting=_recording_title(alive.title, url),
                    detail=f"OBS stopped mid-meeting; resumed as part {part_index + 1}",
                ),
            )
            # Close the orphaned part properly and file it before starting a new one.
            with suppress(ObsError):
                controller.stop_record(timeout=5.0)
            partial = _verify_recording(controller, None, output_path, session_started)
            with suppress(ObsError):
                controller.close()
            if partial:
                part_recording = postprocess.finalize(
                    Path(partial),
                    output_path,
                    session_started,
                    _part_title(_recording_title(alive.title, url, watcher.prejoin_title), part_index),
                )
                log.info(
                    "part %d kept: %s (%.0fs)",
                    part_index,
                    part_recording.path,
                    part_recording.duration_sec,
                )
            else:
                swept = _sweep_empty_recordings(output_path, session_started)
                if swept:
                    log.warning(
                        "part %d was too short to keep (killed before OBS flushed a frame); "
                        "removed %d empty file(s)",
                        part_index,
                        swept,
                    )
            part_index += 1

            exe = find_obs_exe(obs_exe)
            if exe is None:
                log.error("OBS is not installed any more; the rest of the meeting will not be recorded")
                return None
            clear_crash_flag()
            launch_obs(exe, start_recording=False)
            controller = ObsController(config)
            try:
                wait_for_websocket(controller)
            except ObsError as exc:
                log.error("relaunched OBS but its WebSocket never came up: %s", exc)
                return None
            try:
                if not controller.ensure_record_dir(output_path):
                    log.warning("OBS keeps its configured recording directory")
                controller.ensure_recording_format("mkv")
                scene.prepare(controller.call, alive, mic=mic)
                controller.start_record()
            except (ObsError, scene.SceneError) as exc:
                log.error("could not restart the recording after the OBS crash: %s", exc)
                return None
            log.info("recording resumed with a new OBS instance (part %d)", part_index)
            return monitor_once()

        while exit_code in (EXIT_RECORD, EXIT_LOST) and resumes < MAX_OBS_RESUMES:
            resumes += 1
            try:
                resumed = attempt_resume()
            except Exception as exc:  # noqa: BLE001 - a failed resume must not skip the finalize below
                log.error("could not resume after the OBS failure: %s: %s", type(exc).__name__, exc)
                break
            if resumed is None:
                break
            exit_code = resumed
    except KeyboardInterrupt:
        interrupted = True
        if keep_recording_on_interrupt:
            keep_recording = True
            log.warning("interrupted; leaving the OBS recording running")
        else:
            log.info("interrupted; stopping the recording")
    finally:
        watcher.stop()
        log.info("dialog watcher: %s", watcher.summary())
        if not keep_recording and recording_started:
            reported: str | None = None
            try:
                reported = controller.stop_record()
            except ObsError as exc:
                log.error("could not stop the OBS recording: %s", exc)
                exit_code = exit_code or EXIT_LOST
            saved_path = _verify_recording(
                controller, reported, output_path, session_started
            )
            if saved_path:
                recording = postprocess.finalize(
                    Path(saved_path),
                    output_path,
                    session_started,
                    _part_title(_recording_title(window.title, url, watcher.prejoin_title), part_index),
                )
                saved_path = str(recording.path)
                picture = postprocess.sample_luma(recording.path)
                log.info(
                    "recording ready: %s (%.0fs, %.1f MB, video=%s audio=%s, picture=%s%s)",
                    recording.path,
                    recording.duration_sec,
                    recording.size_bytes / 1_000_000,
                    recording.has_video,
                    recording.has_audio,
                    "unknown" if picture is None else picture,
                    ", remuxed" if recording.remuxed else "",
                )
                if not recording.ok:
                    exit_code = exit_code or EXIT_NO_FILE
                elif picture is not None and picture <= postprocess.BLACK_FRAME_LUMA:
                    # Valid streams, valid duration, black picture: exactly the file that slipped through on
                    # 2026-11-15 because nothing looked at the frames after the session.
                    log.error(
                        "needs_attention: the finished file is black (max luma %s): %s",
                        picture,
                        recording.path,
                    )
                    notify.send(
                        notify_url,
                        notify.Event(
                            notify.NEEDS_ATTENTION,
                            meeting=_recording_title(window.title, url, watcher.prejoin_title),
                            path=str(recording.path),
                            detail=f"recording is black (max luma {picture})",
                        ),
                    )
            else:
                log.error(
                    "no recording file found in %s or OBS's folder", output_path
                )
                exit_code = exit_code or EXIT_NO_FILE
            # A crash before OBS flushed a frame leaves a 0-byte file; never leave it behind.
            _sweep_empty_recordings(output_path, session_started)
        controller.close()

    return SessionResult(exit_code, saved_path, interrupted, shots_dir)


def _record_session_awake(**kwargs) -> SessionResult:
    """`_record_session` with the display held awake; Windows clears the flag when this process ends."""
    with display_awake() as granted:
        if not granted:
            log.warning("could not keep the display awake; a screen blank would record black")
        return _record_session(**kwargs)


def _write_report(
    schedule_path: str | Path, record: dict[str, object], shots_dir: Path | None
) -> Path | None:
    """Write ``logs/reports/<stamp>-<entry>.html`` for this session, or ``None`` without a shots folder.

    A session that never saw a dialog has no screenshots — the recorder only creates that folder when it writes
    one — and that used to raise here, so every clean join logged "could not write the run report".
    """
    if shots_dir is None:
        return None
    shots: list[Path] = []
    if Path(shots_dir).is_dir():
        shots = sorted(path for path in Path(shots_dir).iterdir() if path.is_file())
    try:
        directory = Path(schedule_path).expanduser().resolve().with_name("logs") / "reports"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = str(record["ts"]).replace(":", "").replace("-", "")
        target = directory / f"{stamp}-{record.get('entry') or 'manual'}.html"
        target.write_text(report.render(record, shots), encoding="utf-8")
        return target
    except OSError as exc:
        log.warning("could not write the run report: %s", exc)
        return None


def _log_session(
    schedule_path: str | Path,
    *,
    entry_id: str | None,
    meeting: str,
    scheduled: str,
    result: SessionResult,
    seconds: int,
) -> None:
    """Append the outcome to ``logs/runs.jsonl``. Bookkeeping never fails a recording."""
    record: dict[str, object] = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "entry": entry_id,
        "meeting": meeting,
        "scheduled": scheduled,
        "status": "done" if result.exit_code == EXIT_OK else "failed",
        "exit_code": result.exit_code,
        "seconds": seconds,
        "path": str(result.path) if result.path else "",
        "detail": "interrupted" if result.interrupted else "",
    }
    written = _write_report(schedule_path, record, result.shots_dir)
    if written is not None:
        record["report"] = str(written)
    try:
        append_run(schedule_path, record)
    except OSError as exc:
        log.warning("could not append to the run log: %s", exc)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Schedule Zoom meetings and record them with OBS Studio."""


def _session_options(func):
    """Options shared by the single-shot `record` and the `run` runner."""
    for option in (
        click.option("--obs-port", default=4455, show_default=True, type=click.IntRange(1, 65535)),
        click.option(
            "--obs-password",
            default=None,
            envvar="OBS_PASSWORD",
            help="OBS WebSocket password (or the OBS_PASSWORD environment variable).",
        ),
        click.option(
            "--obs-exe",
            default=None,
            type=click.Path(dir_okay=False),
            help="obs64.exe to launch when OBS is not running.",
        ),
        click.option(
            "--launch-mode",
            type=click.Choice(["zoommtg", "https"]),
            default="zoommtg",
            show_default=True,
            help="zoommtg: hand the protocol link to the desktop client; https: browser.",
        ),
        click.option(
            "--join-timeout",
            default=300.0,
            show_default=True,
            type=click.FloatRange(min=1.0),
            help="Seconds to wait for the Zoom meeting window.",
        ),
        click.option(
            "--join-delay",
            default=0.0,
            show_default=True,
            type=click.FloatRange(min=0.0),
            help="Seconds between maximising Zoom and starting the recording.",
        ),
        click.option(
            "--max-duration",
            default=0.0,
            show_default="no limit",
            type=click.FloatRange(min=0.0),
            help=(
                "Optional safety cap in seconds; 0 (the default) means the recording ends only when the "
                "meeting does."
            ),
        ),
        click.option(
            "--close-grace",
            default=5.0,
            show_default="5s",
            type=click.FloatRange(min=0.0),
            help="Seconds without a Zoom meeting window before OBS is stopped.",
        ),
        click.option(
            "--waiting-room-timeout",
            default=1800.0,
            show_default="30min",
            type=click.FloatRange(min=0.0),
            help=(
                "Stop the recording if still in Zoom's waiting room after this many seconds; 0 waits forever. "
                "In-meeting recordings are unaffected."
            ),
        ),
        click.option(
            "--notify",
            "notify_url",
            default=None,
            metavar="URL",
            help=(
                "Webhook called on done/failed/needs_attention (Slack, Discord or any JSON endpoint). "
                "Defaults to the URL in the UI settings."
            ),
        ),
        click.option(
            "--steal-meeting/--no-steal-meeting",
            "steal_meeting",
            default=True,
            show_default="on",
            help=(
                "When Zoom is already in another meeting, answer 'Leave & join' so the scheduled meeting "
                "starts. --no-steal-meeting leaves that dialog alone and raises needs_attention instead."
            ),
        ),
        click.option(
            "--quality",
            type=click.Choice(["auto", "720p", "1080p"]),
            default="auto",
            show_default=True,
            help="Recording resolution; auto keeps whatever OBS has configured.",
        ),
        click.option(
            "--retention-days",
            default=0,
            show_default="keep forever",
            type=click.IntRange(min=0),
            help="Delete recordings older than this many days after a run; 0 keeps everything.",
        ),
        click.option(
            "--min-free-gb",
            default=2.0,
            show_default="2 GB",
            type=click.FloatRange(min=0.0),
            help="Refuse to start a recording below this much free space; 0 disables.",
        ),
        click.option(
            "--keep-recording-on-interrupt",
            is_flag=True,
            help="Leave OBS recording when Ctrl+C is pressed.",
        ),
        click.option("--passcode", default=None, help="Meeting passcode if the link lacks ?pwd=."),
        click.option(
            "--auto-accept/--no-auto-accept",
            "auto_accept",
            default=True,
            show_default="on",
            help="Read blocking Zoom dialogs (consent/acknowledge/join audio) and click the matching label.",
        ),
        click.option(
            "--auto-accept-seconds",
            default=20.0,
            show_default="20s",
            type=click.FloatRange(min=0.0),
            help="Deprecated: the dialog watcher now runs for the whole session.",
            hidden=True,
        ),
        click.option(
            "--window-class",
            "window_classes",
            multiple=True,
            help="Extra Zoom window class treated as the meeting window.",
        ),
        click.option(
            "--window-title",
            default=None,
            help="Regex for the meeting window title (case-insensitive).",
        ),
    ):
        func = option(func)
    return func


@cli.command()
@click.option("--url", required=True, help="Zoom join link (https://.../j/... or zoommtg://...).")
@click.option("--start", "start_at", required=True, metavar="'YYYY-MM-DD HH:MM'")
@click.option(
    "--output-dir",
    default=DEFAULT_OUTPUT,
    show_default=True,
    type=click.Path(file_okay=False),
    help="Directory for the recording; OBS is pointed here when possible.",
)
@_session_options
@click.option("--log-file", default=None, type=click.Path(dir_okay=False))
@click.option("--dry-run", is_flag=True, help="Check prerequisites and OBS, then exit.")
@click.option(
    "--fail-if-past",
    is_flag=True,
    help="Exit instead of starting immediately when --start is in the past.",
)
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
@click.option(
    "--email",
    "email",
    default=None,
    metavar="you@example.com",
    help="Zoom prejoin email; defaults to the schedule setting, then the built-in one.",
)
@click.option(
    "--mic/--no-mic",
    default=False,
    show_default=True,
    help="Also record this machine's microphone (remote audio is recorded either way).",
)
def record(
    url: str,
    start_at: str,
    output_dir: str,
    obs_port: int,
    obs_password: str | None,
    obs_exe: str | None,
    launch_mode: str,
    join_timeout: float,
    join_delay: float,
    max_duration: float,
    close_grace: float,
    waiting_room_timeout: float,
    keep_recording_on_interrupt: bool,
    min_free_gb: float,
    notify_url: str | None,
    steal_meeting: bool,
    quality: str,
    mic: bool,
    retention_days: int,
    window_classes: tuple[str, ...],
    window_title: str | None,
    passcode: str | None,
    email: str | None,
    auto_accept: bool,
    auto_accept_seconds: float,
    log_file: str | None,
    dry_run: bool,
    fail_if_past: bool,
    verbose: bool,
) -> None:
    """Record one meeting now or at --start (no schedule file involved)."""
    _setup_logging(verbose, log_file)

    try:
        target = parse_start(start_at)
    except ScheduleError as exc:
        raise click.BadParameter(str(exc), param_hint="--start") from exc

    output_path = Path(output_dir).expanduser().resolve()
    try:
        output_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _fail(1, f"cannot create output directory {output_path}: {exc}")

    config = ObsConfig(port=obs_port, password=_obs_password(obs_password), record_dir=output_path)

    if dry_run:
        zoom_target = to_zoommtg(url) if launch_mode == "zoommtg" else None
        raise click.exceptions.Exit(
            _preflight(url, zoom_target, target, output_path, config, obs_exe)
        )

    remaining = seconds_until(target)
    if remaining > 0:
        log.info("meeting scheduled for %s (%s from now)", target, format_delta(remaining))
        try:
            wait_with_countdown(target, console=console)
        except KeyboardInterrupt:
            console.print("[yellow]cancelled before launch[/]")
            raise click.exceptions.Exit(EXIT_OK) from None
    elif remaining < 0:
        message = f"--start {start_at} is {format_delta(-remaining)} in the past"
        if fail_if_past:
            _fail(1, message)
        log.warning("%s; starting immediately", message)

    started_mono = time.monotonic()
    result = _record_session_awake(
        url=url,
        output_path=output_path,
        config=config,
        obs_exe=obs_exe,
        launch_mode=launch_mode,
        join_timeout=join_timeout,
        join_delay=join_delay,
        max_duration=max_duration,
        close_grace=close_grace,
        waiting_room_timeout=waiting_room_timeout,
        keep_recording_on_interrupt=keep_recording_on_interrupt,
        min_free_gb=min_free_gb,
        window_classes=window_classes,
        window_title=window_title,
        passcode=passcode,
        email=email,
        auto_accept=auto_accept,
        auto_accept_seconds=auto_accept_seconds,
        notify_url=notify_url,
        steal_meeting=steal_meeting,
        quality=quality,
        mic=mic,
    )
    _log_session(
        DEFAULT_SCHEDULE,
        entry_id=None,
        meeting=_recording_title("", url),
        scheduled=target.strftime("%Y-%m-%d %H:%M"),
        result=result,
        seconds=int(time.monotonic() - started_mono),
    )
    postprocess.prune(output_path, retention_days)
    notify.send(
        notify_url,
        notify.Event(
            notify.DONE if result.exit_code == EXIT_OK else notify.FAILED,
            meeting=url,
            path=result.path,
            exit_code=result.exit_code,
        ),
    )
    raise click.exceptions.Exit(result.exit_code)


@cli.command("add")
@click.argument("spec", required=False)
@click.option("--url", default=None, help="Zoom join link (alternative to SPEC).")
@click.option(
    "--start",
    "start_at",
    default=None,
    metavar="'YYYY-MM-DD HH:MM'",
    help="Start time (alternative to SPEC).",
)
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(file_okay=False),
    help="Per-entry folder; defaults to ./recordings when the runner starts.",
)
@click.option("--note", default="", help="Free-text label shown by `list`.")
@click.option(
    "--email",
    default=None,
    metavar="you@example.com",
    help="Zoom prejoin email for this meeting (defaults to the schedule setting).",
)
@click.option(
    "--repeat",
    type=click.Choice(["none", "weekly", "weekdays"]),
    default="none",
    show_default=True,
    help="Re-arm the event after each recording.",
)
@click.option(
    "--rrule",
    default=None,
    metavar="'FREQ=WEEKLY;INTERVAL=2;BYDAY=WE'",
    help="Full RFC 5545 recurrence; overrides --repeat.",
)
@click.option(
    "--tz",
    "tz_name",
    default=None,
    metavar="'Europe/London'",
    help="Meeting timezone; keeps 10:00 at 10:00 across DST. Defaults to this machine's zone.",
)
@click.option(
    "--schedule",
    "schedule_path",
    default=DEFAULT_SCHEDULE,
    show_default=True,
    type=click.Path(dir_okay=False),
)
@click.option(
    "--mic/--no-mic",
    default=False,
    show_default=True,
    help="Also record this machine's microphone; remote audio is recorded either way.",
)
def add_meeting(
    spec: str | None,
    url: str | None,
    start_at: str | None,
    output_dir: str | None,
    note: str,
    email: str | None,
    repeat: str,
    tz_name: str | None,
    rrule: str | None,
    schedule_path: str,
    mic: bool,
) -> None:
    """Add a meeting: `add "11/9/26 10am https://zoom.us/j/123"`.

    SPEC accepts the date/time and the link in one quoted line; the URL may be
    first or last. `--url`/`--start` do the same thing for scripts.
    """
    if spec:
        if url or start_at:
            raise click.UsageError("use either a single SPEC or --url with --start")
        try:
            start_at, url = split_entry_spec(spec)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="SPEC") from exc
    elif not (url and start_at):
        raise click.UsageError(
            "provide a single 'WHEN URL' argument, or both --url and --start"
        )

    try:
        target = parse_when(start_at)
    except ScheduleError as exc:
        raise click.ClickException(str(exc)) from exc
    if target < datetime.now():
        console.print(
            "[yellow]warning:[/] start time is in the past; "
            "the runner records it only within --max-late"
        )

    try:
        store = ScheduleStore(schedule_path)
        canonical = target.strftime(START_FORMAT)
        # Two shapes of the same invite (link vs. "Meeting ID: … Passcode: …") name one meeting; compare the
        # parsed identity, not the string.
        duplicate = next(
            (
                item
                for item in store.pending()
                if invite.same_meeting(item.url, url) and item.start == canonical
            ),
            None,
        )
        if duplicate is not None:
            console.print(
                f"[yellow]warning:[/] already scheduled as {duplicate.id}"
            )
        entry = store.add(
            url=url,
            start_at=canonical,
            output_dir=output_dir,
            note=note,
            email=email,
            repeat=None if repeat == "none" else repeat,
            rrule=rrule,
            tz=tz_name,
            mic=mic,
        )
    except (ScheduleError, StoreError) as exc:
        raise click.ClickException(str(exc)) from exc

    if to_zoommtg(url) is None:
        console.print(
            "[yellow]warning:[/] link is not a recognisable Zoom join link; "
            "the browser fallback will be used"
        )
    console.print(f"[green]added[/] [cyan]{entry.id}[/]  {entry.start}  {entry.url}")


@cli.command("list")
@click.option("--all", "show_all", is_flag=True, help="Include finished, failed and missed entries.")
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON instead of a table.")
@click.option(
    "--schedule",
    "schedule_path",
    default=DEFAULT_SCHEDULE,
    show_default=True,
    type=click.Path(dir_okay=False),
)
def list_meetings(show_all: bool, as_json: bool, schedule_path: str) -> None:
    """Show scheduled meetings (pending only by default)."""
    try:
        store = ScheduleStore(schedule_path)
    except StoreError as exc:
        raise click.ClickException(str(exc)) from exc

    entries = store.sorted_entries()
    if not show_all:
        entries = [entry for entry in entries if entry.status == STATUS_PENDING]
    if as_json:
        click.echo(json.dumps([entry.to_dict() for entry in entries], indent=2))
        return
    if not entries:
        console.print("no entries" if show_all else "no pending entries")
        return

    table = Table(show_header=True, header_style="bold")
    for column in ("id", "start", "status", "url", "note", "exit", "file"):
        table.add_column(column)
    for entry in entries:
        table.add_row(
            entry.id,
            entry.start,
            entry.status,
            entry.url,
            entry.note,
            "" if entry.last_exit_code is None else str(entry.last_exit_code),
            entry.last_path or "",
        )
    console.print(table)


@cli.command("remove")
@click.argument("entry_id")
@click.option(
    "--schedule",
    "schedule_path",
    default=DEFAULT_SCHEDULE,
    show_default=True,
    type=click.Path(dir_okay=False),
)
def remove_meeting(entry_id: str, schedule_path: str) -> None:
    """Remove a meeting from the schedule by id."""
    try:
        store = ScheduleStore(schedule_path)
        removed = store.remove(entry_id)
    except StoreError as exc:
        raise click.ClickException(str(exc)) from exc
    if not removed:
        raise click.ClickException(f"no entry {entry_id!r} in {store.path}")
    console.print(f"[green]removed[/] {entry_id}")


@cli.command("edit")
@click.argument("entry_id")
@click.option(
    "--schedule",
    "schedule_path",
    default=DEFAULT_SCHEDULE,
    show_default=True,
    type=click.Path(dir_okay=False),
)
@click.option("--start", "start_at", default=None, help="New local start, e.g. '2026-11-20 10:00'.")
@click.option("--url", default=None, help="New Zoom link or pasted invitation.")
@click.option(
    "--note",
    "--title",
    "note",
    default=None,
    help="Meeting title used for the file name and notifications.",
)
@click.option("--output-dir", default=None, type=click.Path(file_okay=False), help="Absolute folder for this meeting.")
@click.option("--rrule", default=None, help="RFC 5545 rule, e.g. 'FREQ=WEEKLY;INTERVAL=2;BYDAY=WE'.")
@click.option("--tz", "tz_name", default=None, help="IANA time zone for the recurrence, e.g. Europe/London.")
@click.option(
    "--mic/--no-mic",
    default=None,
    help="Record this machine's microphone for this entry (or stop doing so).",
)
def edit_meeting(
    entry_id: str,
    schedule_path: str,
    start_at: str | None,
    url: str | None,
    note: str | None,
    output_dir: str | None,
    rrule: str | None,
    tz_name: str | None,
    mic: bool | None,
) -> None:
    """Change one entry; only the flags you pass change. Same validation as the UI."""
    changes = {
        "start_at": start_at,
        "url": url,
        "note": note,
        "output_dir": output_dir,
        "rrule": rrule,
        "tz": tz_name,
        "mic": mic,
    }
    given = {key: value for key, value in changes.items() if value is not None}
    if not given:
        raise click.UsageError("nothing to change: pass at least one of --start/--url/--title/…")
    try:
        store = ScheduleStore(schedule_path)
        updated = store.update(entry_id, **given)
    except (ScheduleError, StoreError) as exc:
        raise click.ClickException(str(exc)) from exc
    if updated is None:
        raise click.ClickException(f"no entry {entry_id!r} in {store.path}")
    # An edit re-arms the entry, so say the new state rather than the old one.
    console.print(
        f"[green]updated[/] [cyan]{updated.id}[/]  {updated.start}  {updated.status}  {updated.url}"
    )
    if updated.rrule:
        console.print(f"  rule: {updated.rrule}" + (f" ({updated.tz})" if updated.tz else ""))
    if updated.output_dir:
        console.print(f"  into: {updated.output_dir}")


def _should_roll(entry: Entry, interrupted: bool, stopping: bool) -> bool:
    """Re-arm a repeating entry after a run.

    Any rule counts — the legacy ``repeat`` string *or* an RRULE. Checking only ``repeat`` meant an entry created by
    the UI's repeat builder (which writes ``rrule`` and leaves ``repeat`` empty) recorded once and then never again;
    the daily "Road to Payouts" meeting was not re-armed after its failed 09:00 run (2026-11-17).
    """
    return bool(entry.rrule or entry.repeat) and not interrupted and not stopping

@dataclass(frozen=True)
class EntryOutcome:
    """What the runner should do after one entry: count a failure, stop, or carry on."""

    failed: bool = False
    stop: bool = False


def _record_entry(
    schedule: ScheduleStore,
    entry: Entry,
    *,
    default_output: Path,
    obs_port: int,
    obs_password: str | None,
    obs_exe: str | None,
    launch_mode: str,
    join_timeout: float,
    join_delay: float,
    max_duration: float,
    close_grace: float,
    waiting_room_timeout: float,
    keep_recording_on_interrupt: bool,
    min_free_gb: float,
    notify_url: str | None,
    steal_meeting: bool,
    quality: str,
    retention_days: int,
    window_classes: tuple[str, ...],
    window_title: str | None,
    passcode: str | None,
    auto_accept: bool,
    auto_accept_seconds: float,
) -> EntryOutcome:
    """Record one due entry: pick its folder, run the session, mark, notify, prune, roll forward."""
    # Re-read the saved folder per entry so changing it in the UI takes effect without restarting the runner;
    # an entry's own folder still wins.
    configured = schedule.setting("output_dir")
    base_output = Path(configured).expanduser().resolve() if configured else default_output
    output = (
        Path(entry.output_dir).expanduser().resolve() if entry.output_dir else base_output
    )
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error("entry %s: cannot create %s: %s", entry.id, output, exc)
        schedule.mark(entry.id, STATUS_FAILED, exit_code=1)
        return EntryOutcome(failed=True)

    log.info("recording entry %s (%s) into %s", entry.id, entry.start, output)
    schedule.mark(entry.id, STATUS_RUNNING)
    started_mono = time.monotonic()
    result = _record_session_awake(
        url=entry.url,
        output_path=output,
        config=ObsConfig(port=obs_port, password=_obs_password(obs_password), record_dir=output),
        obs_exe=obs_exe,
        launch_mode=launch_mode,
        join_timeout=join_timeout,
        join_delay=join_delay,
        max_duration=max_duration,
        close_grace=close_grace,
        waiting_room_timeout=waiting_room_timeout,
        keep_recording_on_interrupt=keep_recording_on_interrupt,
        min_free_gb=min_free_gb,
        window_classes=window_classes,
        window_title=window_title,
        passcode=passcode,
        email=entry.email or schedule.setting("email") or zoom_account.discover(),
        auto_accept=auto_accept,
        auto_accept_seconds=auto_accept_seconds,
        should_stop=lambda: stop_requested(schedule.path),
        notify_url=notify_url or schedule.setting("notify_url"),
        steal_meeting=steal_meeting,
        quality=quality,
        mic=entry.mic,
    )
    _log_session(
        schedule.path,
        entry_id=entry.id,
        meeting=entry.note or _recording_title("", entry.url),
        scheduled=entry.start,
        result=result,
        seconds=int(time.monotonic() - started_mono),
    )

    status = STATUS_DONE if result.exit_code == EXIT_OK else STATUS_FAILED
    schedule.mark(entry.id, status, exit_code=result.exit_code, path=result.path)
    notify.send(
        notify_url or schedule.setting("notify_url"),
        notify.Event(
            notify.DONE if status == STATUS_DONE else notify.FAILED,
            meeting=entry.note or entry.url,
            path=result.path,
            exit_code=result.exit_code,
        ),
    )
    log.info(
        "entry %s finished: exit %s%s",
        entry.id,
        result.exit_code,
        f", saved {result.path}" if result.path else "",
    )
    postprocess.prune(output, retention_days)
    postprocess.prune_shots(output, retention_days)
    if _should_roll(entry, result.interrupted, stop_requested(schedule.path)):
        rolled = schedule.roll_repeat(entry.id)
        if rolled is not None:
            log.info("next occurrence of %s: %s", entry.id, rolled.start)

    return EntryOutcome(
        failed=result.exit_code != EXIT_OK,
        stop=result.interrupted or stop_requested(schedule.path),
    )


@cli.command("run")
@click.option(
    "--schedule",
    "schedule_path",
    default=DEFAULT_SCHEDULE,
    show_default=True,
    type=click.Path(dir_okay=False),
)
@click.option(
    "--output-dir",
    default=DEFAULT_OUTPUT,
    show_default=True,
    type=click.Path(file_okay=False),
    help="Default folder for entries without their own --output-dir.",
)
@click.option("--once", is_flag=True, help="Record at most one due entry, then exit.")
@click.option(
    "--drain",
    is_flag=True,
    help="Exit when nothing is pending instead of waiting for new entries.",
)
@click.option(
    "--poll",
    default=30.0,
    show_default=True,
    type=click.FloatRange(min=1.0),
    help="Seconds between schedule re-checks while waiting for the next meeting.",
)
@click.option(
    "--max-late",
    default=600.0,
    show_default="10m",
    type=click.FloatRange(min=0.0),
    help="Record entries at most this many seconds late; older ones become missed.",
)
@click.option(
    "--stop-orphan-recording",
    is_flag=True,
    help="Stop an OBS recording that no schedule entry owns when starting.",
)
@click.option(
    "--calendar-url",
    default=None,
    metavar="URL",
    help=(
        "iCal feed to import meetings from every 15 min (Google Calendar's 'secret address in iCal format'); "
        "defaults to the URL saved in the UI settings."
    ),
)
@_session_options
@click.option("--log-file", default=None, type=click.Path(dir_okay=False))
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def run(
    schedule_path: str,
    output_dir: str,
    once: bool,
    drain: bool,
    poll: float,
    max_late: float,
    stop_orphan_recording: bool,
    calendar_url: str | None,
    obs_port: int,
    obs_password: str | None,
    obs_exe: str | None,
    launch_mode: str,
    join_timeout: float,
    join_delay: float,
    max_duration: float,
    close_grace: float,
    waiting_room_timeout: float,
    keep_recording_on_interrupt: bool,
    min_free_gb: float,
    notify_url: str | None,
    steal_meeting: bool,
    quality: str,
    retention_days: int,
    window_classes: tuple[str, ...],
    window_title: str | None,
    passcode: str | None,
    auto_accept: bool,
    auto_accept_seconds: float,
    log_file: str | None,
    verbose: bool,
) -> None:
    """Watch the schedule and record each meeting; stays resident by default."""
    _setup_logging(verbose, log_file)

    try:
        schedule = ScheduleStore(schedule_path)
    except StoreError as exc:
        _fail(1, str(exc))

    lock = RunnerLock(schedule.path)
    if not lock.acquire():
        _fail(1, f"another runner holds {lock.path}")

    default_output = Path(output_dir).expanduser().resolve()
    failures = 0
    try:
        clear_stop(schedule.path)
        idle_logged = False
        abandoned = schedule.recover_abandoned()
        if abandoned:
            log.warning("marked %d interrupted entry/entries as failed", abandoned)
        _check_orphan_recording(
            ObsConfig(
                port=obs_port, password=_obs_password(obs_password), record_dir=default_output
            ),
            stop_orphan_recording,
        )

        next_calendar_sync = 0.0

        while True:
            feed = calendar_url or schedule.setting("calendar_url")
            if feed and time.monotonic() >= next_calendar_sync:
                next_calendar_sync = time.monotonic() + CALENDAR_SYNC_INTERVAL
                _sync_calendar(schedule, feed, default_output)

            try:
                schedule.reload()
            except StoreError as exc:
                log.error("%s", exc)
                failures += 1
                break

            action, entry = next_action(schedule.entries, datetime.now(), max_late)

            if stop_requested(schedule.path):
                log.info("stop requested; runner exiting")
                break

            if action == "idle":
                if once or drain:
                    log.info("nothing pending; runner exiting")
                    break
                if not idle_logged:
                    log.info(
                        "nothing pending; waiting for new entries (add one with `add`)"
                    )
                    idle_logged = True
                time.sleep(min(poll, 1.0))
                continue

            if action == "wait":
                if once or drain:
                    assert entry is not None
                    log.info(
                        "nothing due yet; next is %s at %s (exiting)", entry.id, entry.start
                    )
                    break
                assert entry is not None
                log.info(
                    "next meeting %s at %s (%s away)",
                    entry.id,
                    entry.start,
                    format_delta(seconds_until(entry.start_at)),
                )
                try:
                    _wait_until_ready(schedule, entry, poll, max_late)
                except KeyboardInterrupt:
                    console.print("[yellow]runner stopped[/]")
                    break
                continue

            if action == "missed":
                assert entry is not None
                log.warning(
                    "skipping %s (%s): more than %.0fs late", entry.id, entry.start, max_late
                )
                schedule.mark(entry.id, STATUS_MISSED)
                notify.send(
                    notify_url or schedule.setting("notify_url"),
                    notify.Event(notify.MISSED, meeting=entry.note or entry.url),
                )
                continue

            assert entry is not None
            outcome = _record_entry(
                schedule,
                entry,
                default_output=default_output,
                obs_port=obs_port,
                obs_password=obs_password,
                obs_exe=obs_exe,
                launch_mode=launch_mode,
                join_timeout=join_timeout,
                join_delay=join_delay,
                max_duration=max_duration,
                close_grace=close_grace,
                waiting_room_timeout=waiting_room_timeout,
                keep_recording_on_interrupt=keep_recording_on_interrupt,
                min_free_gb=min_free_gb,
                notify_url=notify_url,
                steal_meeting=steal_meeting,
                quality=quality,
                retention_days=retention_days,
                window_classes=window_classes,
                window_title=window_title,
                passcode=passcode,
                auto_accept=auto_accept,
                auto_accept_seconds=auto_accept_seconds,
            )
            if outcome.failed:
                failures += 1
            if once or outcome.stop:
                break
    except KeyboardInterrupt:
        console.print("[yellow]runner stopped[/]")
    finally:
        clear_stop(schedule.path)
        lock.release()

    raise click.exceptions.Exit(1 if failures else EXIT_OK)


def _pythonw() -> str:
    """Prefer pythonw.exe so the scheduled runner has no console window."""
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    return str(windowless if windowless.is_file() else executable)


def build_task_command(
    *,
    schedule_path: str | Path,
    output_dir: str | Path,
    log_file: str | Path | None = None,
    python_exe: str | Path | None = None,
    drain: bool = False,
) -> list[str]:
    """The argv a scheduled task runs: interpreter, script, and the runner's flags.

    ``drain`` catches up what is already due and exits instead of staying resident — for a machine that should
    not keep a recorder alive all day.
    """
    script = Path(__file__).resolve().with_name("main.py")
    command = [
        str(python_exe or _pythonw()),
        str(script),
        "run",
        "--schedule",
        str(Path(schedule_path).expanduser().resolve()),
        "--output-dir",
        str(Path(output_dir).expanduser().resolve()),
    ]
    if log_file:
        command += ["--log-file", str(Path(log_file).expanduser().resolve())]
    if drain:
        command.append("--drain")
    return command


def build_schtasks_args(
    *,
    name: str,
    schedule_path: str | Path,
    output_dir: str | Path,
    log_file: str | Path | None = None,
    python_exe: str | Path | None = None,
    drain: bool = False,
) -> list[str]:
    """``schtasks /Create`` command that starts the runner at logon."""
    command = build_task_command(
        schedule_path=schedule_path,
        output_dir=output_dir,
        log_file=log_file,
        python_exe=python_exe,
        drain=drain,
    )
    return [
        "schtasks",
        "/Create",
        "/TN",
        name,
        "/SC",
        "ONLOGON",
        "/RL",
        "LIMITED",
        "/F",
        "/TR",
        subprocess.list2cmdline(command),
    ]


WATCHDOG_TASK_SUFFIX = "-watch"

# The watchdog arrives with `run --drain`, which records anything at most --max-late (600 s) late. Firing less often
# than that would let an entry age past the window it exists to catch.
WATCHDOG_MAX_MINUTES = 10


def shortcut_dir(kind: str = "start") -> Path:
    """Where Windows keeps shortcuts a user can click (``desktop`` / ``start``)."""
    if kind == "desktop":
        return Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop"
    return (
        Path(os.environ.get("APPDATA") or Path.home())
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
    )


def create_shortcut(link: Path, target: Path, arguments: str, workdir: Path) -> bool:
    """Create a ``.lnk`` through WScript.Shell; a failure is logged, never raised."""
    link.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
        "$s.TargetPath='{target}';"
        "$s.Arguments='{args}';"
        "$s.WorkingDirectory='{wd}';"
        "$s.Save()"
    ).format(
        link=str(link).replace("'", "''"),
        target=str(target).replace("'", "''"),
        args=arguments.replace("'", "''"),
        wd=str(workdir).replace("'", "''"),
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True
        )
    except OSError as exc:
        log.warning("could not create %s: %s", link.name, exc)
        return False
    if done.returncode != 0:
        log.warning(
            "could not create %s: %s", link.name, (done.stderr or done.stdout).strip()[:200]
        )
        return False
    return True


def build_watchdog_xml(
    *,
    schedule_path: str | Path,
    output_dir: str | Path,
    log_file: str | Path | None = None,
    python_exe: str | Path | None = None,
    minutes: int = 5,
    start_boundary: str | None = None,
) -> str:
    """Task Scheduler XML for the periodic ``run --drain`` that covers a dead resident runner.

    XML rather than ``schtasks /SC MINUTE /TR``: ``/TR`` is capped at 261 characters and the absolute drain command
    with interpreter, script, schedule, output and log paths exceeds it — measured live on 2026-11-20
    (``ERROR: Value for '/TR' option cannot be more than 261 character(s)``), which left the logon task registered
    with no watchdog. The task takes the runner lock when it fires, so a live resident runner makes it exit with
    "another runner holds …"; otherwise it records what is due and marks the rest ``missed``.
    """
    if not 1 <= minutes <= WATCHDOG_MAX_MINUTES:
        raise ValueError(
            f"watchdog interval must be 1-{WATCHDOG_MAX_MINUTES} minutes to stay inside --max-late (600 s)"
        )
    command = build_task_command(
        schedule_path=schedule_path,
        output_dir=output_dir,
        log_file=log_file,
        python_exe=python_exe,
        drain=True,
    )
    executable, *arguments = command
    boundary = start_boundary or (datetime.now() + timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <Triggers>\n    <TimeTrigger>\n"
        f"      <StartBoundary>{boundary}</StartBoundary>\n"
        "      <Repetition>\n"
        f"        <Interval>PT{int(minutes)}M</Interval>\n"
        "        <StopAtDurationEnd>false</StopAtDurationEnd>\n"
        "      </Repetition>\n"
        "      <Enabled>true</Enabled>\n"
        "    </TimeTrigger>\n  </Triggers>\n"
        '  <Principals>\n    <Principal id="Author">\n'
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>\n"
        "    <Enabled>true</Enabled>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n    <Exec>\n'
        f"      <Command>{xml_escape(executable)}</Command>\n"
        f"      <Arguments>{xml_escape(subprocess.list2cmdline(arguments))}</Arguments>\n"
        f"      <WorkingDirectory>{xml_escape(str(Path(__file__).resolve().parent))}</WorkingDirectory>\n"
        "    </Exec>\n  </Actions>\n"
        "</Task>\n"
    )


def build_task_xml(
    *,
    schedule_path: str | Path,
    output_dir: str | Path,
    log_file: str | Path | None = None,
    python_exe: str | Path | None = None,
    restart_interval: str = "PT1M",
    restart_count: int = 3,
    drain: bool = False,
) -> str:
    """Task Scheduler XML for the logon runner.

    Needed because ``schtasks`` has no flag for ``RestartOnFailure`` (a crashed runner stays dead until the next
    logon) and its default ``ExecutionTimeLimit`` is 72 hours (a resident runner would be killed at day 3);
    ``PT0S`` means unlimited. ``MultipleInstancesPolicy=IgnoreNew`` plus the runner's own lock makes a double
    start harmless.
    """
    command = build_task_command(
        schedule_path=schedule_path,
        output_dir=output_dir,
        log_file=log_file,
        python_exe=python_exe,
        drain=drain,
    )
    executable, *arguments = command
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        '  <Principals>\n    <Principal id="Author">\n'
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        f"    <RestartOnFailure>\n      <Interval>{restart_interval}</Interval>\n"
        f"      <Count>{int(restart_count)}</Count>\n    </RestartOnFailure>\n"
        "    <Enabled>true</Enabled>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n    <Exec>\n'
        f"      <Command>{xml_escape(executable)}</Command>\n"
        f"      <Arguments>{xml_escape(subprocess.list2cmdline(arguments))}</Arguments>\n"
        f"      <WorkingDirectory>{xml_escape(str(Path(__file__).resolve().parent))}</WorkingDirectory>\n"
        "    </Exec>\n  </Actions>\n"
        "</Task>\n"
    )


@cli.command("status")
@click.option(
    "--schedule",
    "schedule_path",
    default=DEFAULT_SCHEDULE,
    show_default=True,
    type=click.Path(dir_okay=False),
)
@click.option("--obs-port", default=4455, show_default=True, type=click.IntRange(1, 65535))
@click.option("--obs-password", default=None, envvar="OBS_PASSWORD")
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON.")
def status(
    schedule_path: str, obs_port: int, obs_password: str | None, as_json: bool
) -> None:
    """Show runner state, entry counts, the next meeting and OBS reachability."""
    try:
        store = ScheduleStore(schedule_path)
    except StoreError as exc:
        raise click.ClickException(str(exc)) from exc

    pid = RunnerLock(store.path).owner_pid()
    counts: dict[str, int] = {}
    for entry in store.entries:
        counts[entry.status] = counts.get(entry.status, 0) + 1

    reachable = is_websocket_reachable("127.0.0.1", obs_port)
    pending = store.pending()
    needs_runner = bool(pending) and runner_needed(
        pending[0].start_at, datetime.now(), pid
    )
    if as_json:
        click.echo(
            json.dumps(
                {
                    "schedule": str(store.path),
                    "runner_pid": pid,
                    "counts": counts,
                    "next": pending[0].to_dict() if pending else None,
                    "obs_reachable": reachable,
                    "obs_port": obs_port,
                    "runner_needed": needs_runner,
                },
                indent=2,
            )
        )
        return

    console.print(f"schedule   : {store.path}")
    console.print(f"runner     : {'running (pid %d)' % pid if pid else 'not running'}")
    console.print(
        "entries    : "
        + (", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "none")
    )
    if pending:
        nxt = pending[0]
        console.print(
            f"next       : {nxt.id} at {nxt.start} "
            f"({format_delta(seconds_until(nxt.start_at))}) {nxt.url}"
        )
    else:
        console.print("next       : (nothing pending)")
    console.print(
        f"obs socket : {'reachable' if reachable else 'unreachable'} at 127.0.0.1:{obs_port}"
    )
    if needs_runner:
        console.print(
            f"[red]warning    : nothing will record {pending[0].id}[/] — no runner is alive and it starts in "
            f"{format_delta(seconds_until(pending[0].start_at))}. Start `main.py run`, or register one that "
            f"cannot be forgotten: `main.py install --restart-on-failure --watchdog`."
        )



@cli.command("stop")
@click.option(
    "--schedule",
    "schedule_path",
    default=DEFAULT_SCHEDULE,
    show_default=True,
    type=click.Path(dir_okay=False),
)
@click.option(
    "--wait",
    default=30.0,
    show_default="30s",
    type=click.FloatRange(min=0.0),
    help="Seconds to wait for the runner to exit; 0 returns immediately.",
)
def stop(schedule_path: str, wait: float) -> None:
    """Ask the resident runner to stop after its current step.

    This writes a marker file rather than killing the process: a killed runner skips OBS's StopRecord, leaving the
    recording open and the entry stuck in ``running``. The runner checks the marker while waiting, between entries
    and during a recording, so it can stop OBS cleanly first.
    """
    try:
        store = ScheduleStore(schedule_path)
    except StoreError as exc:
        raise click.ClickException(str(exc)) from exc

    pid = RunnerLock(store.path).owner_pid()
    if not pid:
        console.print("[yellow]no runner is running for this schedule[/]")
        raise click.exceptions.Exit(1)

    request_stop(store.path)
    console.print(f"[green]stop requested[/] of runner pid {pid}")
    if wait <= 0:
        return
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if RunnerLock(store.path).owner_pid() is None:
            console.print("[green]runner stopped[/]")
            return
        time.sleep(0.5)
    console.print(
        "[yellow]still running[/] — it stops after the current meeting; `status` shows the pid"
    )


@cli.command("runs")
@click.option(
    "--schedule",
    "schedule_path",
    default=DEFAULT_SCHEDULE,
    show_default=True,
    type=click.Path(dir_okay=False),
)
@click.option("--limit", default=10, show_default=True, type=click.IntRange(1, 1000))
@click.option("--json", "as_json", is_flag=True, help="Print one JSON object per line.")
def runs(schedule_path: str, limit: int, as_json: bool) -> None:
    """Show recent session outcomes from logs/runs.jsonl (newest first)."""
    records = read_runs(schedule_path, limit=limit)
    if as_json:
        for record in records:
            click.echo(json.dumps(record, sort_keys=True))
        return
    if not records:
        console.print("no runs recorded yet (logs/runs.jsonl)")
        return
    for record in records:
        status = str(record.get("status") or "?")
        elapsed = format_delta(float(record.get("seconds") or 0))
        meeting = record.get("meeting") or record.get("entry") or "?"
        line = f"{record.get('ts') or '?'}  {status:<7} {elapsed:>8}  {meeting}"
        if record.get("detail"):
            line += f"  ({record['detail']})"
        console.print(line)
        if record.get("path"):
            console.print(f"    -> {record['path']}")
        if record.get("report"):
            console.print(f"    report: {record['report']}")


@cli.command("install")
@click.option("--name", default="ZoomRecorder", show_default=True, help="Task Scheduler task name.")
@click.option(
    "--schedule",
    "schedule_path",
    default=DEFAULT_SCHEDULE,
    show_default=True,
    type=click.Path(dir_okay=False),
)
@click.option(
    "--output-dir",
    default=DEFAULT_OUTPUT,
    show_default=True,
    type=click.Path(file_okay=False),
)
@click.option(
    "--log-file",
    default="./recordings/runner.log",
    show_default=True,
    type=click.Path(dir_okay=False),
)
@click.option("--python", "python_exe", default=None, help="Python interpreter for the task.")
@click.option(
    "--restart-on-failure/--no-restart-on-failure",
    "restart_on_failure",
    default=False,
    help="Register via Task XML so a crashed runner is restarted (1 min apart, 3 tries).",
)
@click.option(
    "--drain",
    is_flag=True,
    help="Record what is already due at logon, then exit instead of staying resident.",
)
@click.option(
    "--watchdog/--no-watchdog",
    "watchdog",
    default=False,
    help="Also register a periodic `run --drain` that records what is due when the resident runner is dead.",
)
@click.option(
    "--watchdog-minutes",
    default=5,
    show_default=True,
    type=click.IntRange(1, WATCHDOG_MAX_MINUTES),
    help="How often the watchdog task fires (must stay inside --max-late).",
)
@click.option(
    "--shortcuts/--no-shortcuts",
    "shortcuts",
    default=False,
    help="Also put a 'Zoom Recorder' shortcut on the Desktop and in the Start menu.",
)
def install(
    name: str,
    schedule_path: str,
    output_dir: str,
    log_file: str | None,
    python_exe: str | None,
    restart_on_failure: bool,
    drain: bool,
    watchdog: bool,
    watchdog_minutes: int,
    shortcuts: bool,
) -> None:
    """Register the resident runner to start at logon (Windows Task Scheduler)."""
    if not is_windows():
        _fail(1, "Task Scheduler registration is Windows-only")

    if restart_on_failure:
        # NamedTemporaryFile closes the handle; mkstemp would leak it and schtasks cannot read a locked file.
        with tempfile.NamedTemporaryFile(
            "w",
            suffix=".xml",
            prefix="zoom-recorder-task-",
            encoding="utf-16",
            delete=False,
        ) as handle:
            handle.write(
                build_task_xml(
                    schedule_path=schedule_path,
                    output_dir=output_dir,
                    log_file=log_file,
                    python_exe=python_exe,
                    drain=drain,
                )
            )
            xml_path = Path(handle.name)
        try:
            args = ["schtasks", "/Create", "/TN", name, "/XML", str(xml_path), "/F"]
            log.info("running: %s", subprocess.list2cmdline(args))
            completed = subprocess.run(args, capture_output=True, text=True)
        finally:
            with suppress(OSError):
                xml_path.unlink()
    else:
        args = build_schtasks_args(
            name=name,
            schedule_path=schedule_path,
            output_dir=output_dir,
            log_file=log_file,
            python_exe=python_exe,
            drain=drain,
        )
        log.info("running: %s", subprocess.list2cmdline(args))
        completed = subprocess.run(args, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        _fail(1, f"schtasks failed ({completed.returncode}): {detail}")
    console.print(f"[green]installed[/] task [cyan]{name}[/] (starts at logon)")
    started = subprocess.run(
        ["schtasks", "/Run", "/TN", name], capture_output=True, text=True
    )
    if started.returncode != 0:
        log.warning(
            "task registered but could not be started: %s",
            (started.stderr or started.stdout).strip(),
        )
    queried = subprocess.run(
        ["schtasks", "/Query", "/TN", name, "/FO", "LIST"],
        capture_output=True,
        text=True,
    )
    if queried.returncode == 0:
        for line in queried.stdout.splitlines():
            if line.strip().startswith(("TaskName", "Status", "Next Run Time")):
                console.print(f"  {line.strip()}")
    if watchdog:
        watch_name = name + WATCHDOG_TASK_SUFFIX
        # From XML, not `schtasks /SC MINUTE /TR`: that switch rejects a command line longer than 261 characters and
        # the absolute drain command is longer (measured live 2026-11-20).
        with tempfile.NamedTemporaryFile(
            "w", suffix=".xml", prefix="zoom-recorder-watch-", encoding="utf-16", delete=False
        ) as handle:
            handle.write(
                build_watchdog_xml(
                    schedule_path=schedule_path,
                    output_dir=output_dir,
                    log_file=log_file,
                    python_exe=python_exe,
                    minutes=watchdog_minutes,
                )
            )
            watch_xml = Path(handle.name)
        try:
            watch_args = ["schtasks", "/Create", "/TN", watch_name, "/XML", str(watch_xml), "/F"]
            log.info("running: %s", subprocess.list2cmdline(watch_args))
            watched = subprocess.run(watch_args, capture_output=True, text=True)
        finally:
            with suppress(OSError):
                watch_xml.unlink()
        if watched.returncode != 0:
            detail = (watched.stderr or watched.stdout).strip()
            _fail(
                1,
                f"the logon task is registered but the watchdog failed "
                f"({watched.returncode}): {detail}",
            )
        console.print(
            f"[green]installed[/] watchdog [cyan]{watch_name}[/] "
        )

    if shortcuts:
        script = Path(__file__).resolve().with_name("main.py")
        made = 0
        for kind in ("start", "desktop"):
            link = shortcut_dir(kind) / "Zoom Recorder.lnk"
            if create_shortcut(link, Path(_pythonw()), f'"{script}" gui', script.parent):
                made += 1
                console.print(f"[green]shortcut[/] {link}")
        if not made:
            console.print(
                "[yellow]no shortcut created[/] — create one by hand pointing at "
                f"{_pythonw()} \"{script}\" gui"
            )


@cli.command("uninstall")
@click.option("--name", default="ZoomRecorder", show_default=True, help="Task Scheduler task name.")
def uninstall(name: str) -> None:
    """Remove the logon task registered by `install`."""
    if not is_windows():
        _fail(1, "Task Scheduler registration is Windows-only")

    completed = subprocess.run(
        ["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True, text=True
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        _fail(1, f"schtasks failed ({completed.returncode}): {detail}")
    console.print(f"[green]removed[/] task [cyan]{name}[/]")

    watched = subprocess.run(
        ["schtasks", "/Delete", "/TN", name + WATCHDOG_TASK_SUFFIX, "/F"],
        capture_output=True,
        text=True,
    )
    if watched.returncode == 0:
        console.print(f"[green]removed[/] watchdog [cyan]{name + WATCHDOG_TASK_SUFFIX}[/]")
    else:
        log.debug(
            "no watchdog task %s to remove: %s",
            name + WATCHDOG_TASK_SUFFIX,
            (watched.stderr or watched.stdout).strip(),
        )


@cli.command("import-ics")
@click.argument("source")
@click.option(
    "--schedule",
    "schedule_path",
    default=DEFAULT_SCHEDULE,
    show_default=True,
    type=click.Path(dir_okay=False),
)
@click.option("--output-dir", default=None, type=click.Path(file_okay=False))
@click.option("--dry-run", is_flag=True, help="List what would be imported.")
def import_ics(
    source: str, schedule_path: str, output_dir: str | None, dry_run: bool
) -> None:
    """Import Zoom meetings from an .ics file, or from an http(s) calendar URL.

    Google Calendar's per-calendar "secret address in iCal format" is such a URL — no OAuth involved.
    """
    from ics import fetch, parse_events

    try:
        if source.lower().startswith(("http://", "https://")):
            text = fetch(source)
        else:
            text = Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        _fail(1, f"cannot read {source}: {exc}")

    events = parse_events(text)
    if not events:
        console.print("no Zoom events with a link found in the calendar")
        return

    try:
        store = ScheduleStore(schedule_path)
    except StoreError as exc:
        raise click.ClickException(str(exc)) from exc

    added, skipped, failed = _import_events(store, events, output_dir, dry_run)

    verb = "would add" if dry_run else "added"
    console.print(
        f"{verb} {added} event(s), skipped {skipped} duplicate(s)"
        + (f", {failed} with an unusable rule" if failed else "")
    )


def _import_events(store, events, output_dir: str | None, dry_run: bool) -> tuple[int, int, int]:
    """Add the events that are not already scheduled. Dedupe by meeting identity + start time.

    Returns ``(added, skipped, failed)``. A rule the schedule rejects leaves the occurrence imported once rather
    than aborting the whole calendar.
    """
    existing = [(entry.url, entry.start) for entry in store.entries]
    added = skipped = failed = 0
    for event in events:
        canonical = event.when.strftime(START_FORMAT)
        if any(
            invite.same_meeting(url, event.url) and start == canonical
            for url, start in existing
        ):
            skipped += 1
            continue
        label = f"{canonical} {event.url} ({event.summary or 'no summary'}"
        label += f", rule={event.rrule})" if event.rrule else ")"
        if dry_run:
            console.print(f"[cyan]would add[/] {label}")
            added += 1
            existing.append((event.url, canonical))
            continue
        try:
            store.add(
                url=event.url,
                start_at=canonical,
                output_dir=output_dir,
                note=event.summary,
                rrule=event.rrule,
                tz=event.tz,
            )
        except ScheduleError as exc:
            log.warning("calendar event %r could not be scheduled: %s", event.summary, exc)
            try:
                store.add(
                    url=event.url,
                    start_at=canonical,
                    output_dir=output_dir,
                    note=event.summary,
                )
            except (ScheduleError, StoreError) as again:
                log.error("skipping calendar event %r: %s", event.summary, again)
                failed += 1
                continue
            failed += 1
        existing.append((event.url, canonical))
        added += 1
    return added, skipped, failed


def _sync_calendar(schedule: ScheduleStore, url: str, output_dir: Path | None) -> int:
    """Fetch a calendar feed and import any Zoom meeting that is not scheduled yet.

    Failures are logged and swallowed: a calendar that is temporarily unreachable must not stop the runner from
    recording what it already knows about.
    """
    import ics

    try:
        text = ics.fetch(url)
    except (OSError, ValueError) as exc:
        log.warning("calendar sync failed (%s): %s", url, exc)
        return 0
    events = ics.parse_events(text)
    if not events:
        return 0
    try:
        added, _skipped, _failed = _import_events(
            schedule, events, str(output_dir) if output_dir else None, dry_run=False
        )
    except StoreError as exc:
        log.warning("calendar sync could not write the schedule: %s", exc)
        return 0
    if added:
        log.info("calendar sync: added %d meeting(s) from the feed", added)
    return added


def _slug(text: str) -> str:
    """Filesystem-safe fragment of a window class."""
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:40] or "window"


@cli.command("verify")
@click.argument("recording", type=click.Path(exists=True))
@click.option(
    "--expected-minutes",
    default=None,
    type=click.FloatRange(min=0.1),
    help="How long the meeting actually ran; the file must be within 10% (5 s minimum).",
)
@click.option("--json", "as_json", is_flag=True, help="Print the verdict as JSON.")
def verify(recording: str, expected_minutes: float | None, as_json: bool) -> None:
    """Check a finished recording: one video stream, audio present, duration sane (P4 acceptance)."""
    path = Path(recording)
    if path.is_dir():
        media = sorted(
            (item for item in path.iterdir() if item.suffix.lower() in (".mp4", ".mkv")),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if not media:
            _fail(1, f"no .mp4/.mkv files in {path}")
        path = media[0]
    payload = postprocess.probe_payload(path)
    if payload is None:
        _fail(1, f"ffprobe could not read {path}")
    verdict = postprocess.assess(
        payload, expected_seconds=expected_minutes * 60 if expected_minutes else None
    )
    if as_json:
        click.echo(
            json.dumps(
                {
                    "file": str(path),
                    "ok": verdict.ok,
                    "duration": verdict.duration,
                    "video_streams": verdict.video_streams,
                    "audio_streams": verdict.audio_streams,
                    "width": verdict.width,
                    "height": verdict.height,
                    "problems": list(verdict.problems),
                },
                indent=2,
            )
        )
    else:
        console.print(f"file     : {path}")
        console.print(f"duration : {verdict.duration:.1f}s")
        console.print(
            f"video    : {verdict.video_streams} stream(s)"
            + (f", {verdict.width}x{verdict.height}" if verdict.width else "")
        )
        console.print(f"audio    : {verdict.audio_streams} stream(s)")
        if verdict.ok:
            console.print("[green]verdict  : PASS[/]")
        else:
            console.print("[red]verdict  : FAIL[/]")
            for problem in verdict.problems:
                console.print(f"  - {problem}")
    raise click.exceptions.Exit(EXIT_OK if verdict.ok else EXIT_NO_FILE)


@cli.command("inspect")
@click.option("--url", default=None, help="Join link; launches the Zoom client when given.")
@click.option("--passcode", default=None, help="Meeting passcode if the link lacks ?pwd=.")
@click.option(
    "--process",
    "process_name",
    default="zoom.exe",
    show_default=True,
    help="Executable whose windows are inspected.",
)
@click.option(
    "--seconds",
    default=20.0,
    show_default=True,
    type=click.FloatRange(min=0.0),
    help="How long to keep watching for new or changing windows.",
)
@click.option(
    "--out",
    "out_dir",
    default="./shots",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Directory for frames and word dumps.",
)
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
@click.option("--log-file", default=None, type=click.Path(dir_okay=False), help="Also write logs to a file.")
def inspect(
    url: str | None,
    passcode: str | None,
    process_name: str,
    seconds: float,
    out_dir: str,
    verbose: bool,
    log_file: str | None,
) -> None:
    """Dump each Zoom window's frame, OCR words and click decision; no OBS needed."""
    _setup_logging(verbose, log_file)
    if not is_windows():
        _fail(1, "window inspection requires Windows")
    if not ocr.available():
        log.warning("OCR unavailable: %s", ocr.unavailable_reason())

    if url:
        log.info("launching %s", url)
        try:
            launch_meeting(url, mode="zoommtg", passcode=passcode)
        except (OSError, RuntimeError) as exc:
            _fail(1, f"could not launch Zoom: {exc}")

    rules = dialogs.load_rules()
    target = Path(out_dir) / datetime.now().strftime("%Y%m%d-%H%M%S")
    target.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(seconds, 0.1)
    seen: dict[int, str] = {}
    dumps = 0

    while time.monotonic() < deadline:
        for window in iter_windows(process_name):
            if not window.visible or not window.title.strip():
                continue
            if window.width < 40 or window.height < 40:
                # The floating meeting toolbar reports 0x0; PrintWindow returns nothing for it.
                continue
            capture = win_capture.grab(window.hwnd)
            if capture is None:
                log.warning("hwnd=%#x %r could not be captured", window.hwnd, window.title)
                continue
            words = ocr.words(capture.image)
            digest = dialogs.signature(words) or f"blank {capture.image.size}"
            if seen.get(window.hwnd) == digest:
                continue
            seen[window.hwnd] = digest

            scope = dialogs.scope_for_class(window.cls)
            click = dialogs.pick(words, rules, scope=scope)
            stem = f"{window.hwnd:08x}_{_slug(window.cls)}"
            capture.image.save(target / f"{stem}.png")
            record = {
                "hwnd": window.hwnd,
                "class": window.cls,
                "title": window.title,
                "scope": scope,
                "capture_method": capture.method,
                "origin": list(capture.origin),
                "size": list(capture.image.size),
                "signature": digest,
                "text": ocr.text(capture.image),
                "words": [
                    {
                        "text": word.text,
                        "x": word.x,
                        "y": word.y,
                        "width": word.width,
                        "height": word.height,
                        "line": word.line,
                    }
                    for word in words
                ],
                "would_click": None
                if click is None
                else {
                    "rule": click.rule,
                    "label": click.label,
                    "score": round(click.score, 3),
                    "frame_xy": [click.x, click.y],
                    "screen_xy": list(capture.to_screen(click.x, click.y)),
                },
            }
            (target / f"{stem}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            dumps += 1

            if click is None:
                console.print(f"[yellow]{window.cls}[/] {window.title!r}: no matching rule")
            else:
                screen_xy = capture.to_screen(click.x, click.y)
                console.print(
                    f"[green]{window.cls}[/] {window.title!r}: would click {click.label!r} "
                    f"at {screen_xy} (rule {click.rule}, score {click.score:.2f})"
                )
        time.sleep(0.7)

    console.print(f"[green]{dumps} window dump(s) -> {target}[/]")


@cli.command("probe")
@click.option("--url", required=True, help="Zoom join link to test.")
@click.option("--passcode", default=None, help="Meeting passcode if the link lacks ?pwd=.")
@click.option(
    "--launch-mode",
    type=click.Choice(["zoommtg", "https"]),
    default="zoommtg",
    show_default=True,
)
@click.option("--join-timeout", default=120.0, show_default=True, type=click.FloatRange(min=1.0))
@click.option("--observe", default=10.0, show_default=True, type=click.FloatRange(min=0.0))
@click.option("--auto-accept", is_flag=True, help="Press Enter on blocking Zoom prompts.")
@click.option("--leave", is_flag=True, help="Close the meeting window when done.")
def probe(
    url: str,
    passcode: str | None,
    launch_mode: str,
    join_timeout: float,
    observe: float,
    auto_accept: bool,
    leave: bool,
) -> None:
    """Join a link, dump every Zoom window/prompt, then exit (no OBS needed)."""
    if not is_windows():
        _fail(1, "Zoom window probing requires Windows")

    log.info("launching %s", url)
    try:
        launch_meeting(url, mode=launch_mode, passcode=passcode)
    except (OSError, RuntimeError) as exc:
        _fail(1, f"could not launch Zoom: {exc}")

    try:
        window = find_meeting_window(timeout=join_timeout)
    except WindowNotFound as exc:
        _fail(EXIT_NO_WINDOW, str(exc))

    log.info(
        "meeting window hwnd=%#x class=%r title=%r",
        window.hwnd,
        window.cls,
        window.title,
    )
    watcher = DialogWatcher(
        dry_run=not auto_accept,
        shots_dir=Path("./shots") / datetime.now().strftime("%Y%m%d-%H%M%S"),
        email=zoom_account.discover(),
    )
    watcher.start()
    deadline = time.monotonic() + observe
    while time.monotonic() < deadline:
        for candidate in iter_windows():
            if candidate.visible and candidate.title.strip():
                log.info(
                    "zoom hwnd=%#x class=%r title=%r area=%d",
                    candidate.hwnd,
                    candidate.cls,
                    candidate.title,
                    candidate.area,
                )
        time.sleep(1.0)

    watcher.stop()
    log.info("dialog watcher: %s", watcher.summary())

    if leave:
        close_window(window.hwnd)
        console.print("[green]probe done; meeting window closed[/]")
    else:
        console.print("[green]probe done[/]")


@cli.command("gui")
@click.option(
    "--schedule",
    "schedule_path",
    default=DEFAULT_SCHEDULE,
    show_default=True,
    type=click.Path(dir_okay=False),
)
@click.option(
    "--output-dir",
    default=DEFAULT_OUTPUT,
    show_default=True,
    type=click.Path(file_okay=False),
)
@click.option(
    "--log-file",
    default="./recordings/runner.log",
    show_default=True,
    type=click.Path(dir_okay=False),
)
@click.option("--obs-port", default=4455, show_default=True, type=click.IntRange(1, 65535))
@click.option("--port", default=8765, show_default=True, type=click.IntRange(1024, 65535))
@click.option("--open/--no-open", "open_browser", default=True, show_default=True)
def gui(
    schedule_path: str,
    output_dir: str,
    log_file: str,
    obs_port: int,
    port: int,
    open_browser: bool,
) -> None:
    """Open the local calendar UI (127.0.0.1 only) for adding and tracking events."""
    from webui import serve

    _setup_logging(False, None)
    serve(
        schedule_path=schedule_path,
        output_dir=output_dir,
        log_file=log_file,
        obs_port=obs_port,
        port=port,
        open_browser=open_browser,
    )


if __name__ == "__main__":  # pragma: no cover
    cli()
