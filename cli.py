"""Click entry point: schedule files, countdown, drive Zoom, record with OBS."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, NoReturn

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from obs_controller import (
    ObsConfig,
    ObsController,
    ObsError,
    find_obs_exe,
    find_recent_recording,
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
    clear_stop,
    next_action,
    split_entry_spec,
    stop_requested,
)
from window_manager import (
    WindowNotFound,
    accept_prompt,
    close_window,
    current_meeting_window,
    iter_windows,
    find_meeting_window,
    find_process,
    is_windows,
    launch_meeting,
    maximize,
    to_zoommtg,
)

EXIT_OK = 0
EXIT_OBS = 3
EXIT_NO_WINDOW = 4
EXIT_RECORD = 5
EXIT_LOST = 6
EXIT_NO_FILE = 7
EXIT_DISK = 8

DEFAULT_SCHEDULE = "./schedule.json"
DEFAULT_OUTPUT = "./recordings"

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


def _monitor(
    controller: ObsController,
    max_duration: float,
    close_grace: float,
    should_stop: Callable[[], bool] = lambda: False,
    extra_classes: tuple[str, ...] = (),
    title_pattern=None,
) -> int:
    """Record until no Zoom meeting window remains for *close_grace* seconds.

    Re-enumerates every second instead of trusting one HWND: Zoom recreates its
    meeting window on screen-share/fullscreen changes, which would otherwise be
    mistaken for the meeting ending.
    """
    started = time.monotonic()
    gone_since: float | None = None
    while True:
        now = time.monotonic()
        if should_stop():
            log.info("stop requested; ending the recording")
            return EXIT_OK
        if current_meeting_window(
            extra_classes=extra_classes, title_pattern=title_pattern
        ) is None:
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
    obs_directory = controller.record_directory()
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
    auto_accept: bool = False,
    auto_accept_seconds: float = 20.0,
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

    log.info("launching Zoom: %s", url)
    try:
        launch_meeting(url, mode=launch_mode, passcode=passcode)
    except (OSError, RuntimeError) as exc:
        log.error("could not launch Zoom: %s", exc)
        return SessionResult(1)

    if auto_accept:
        def _accept_loop() -> None:
            deadline = time.monotonic() + auto_accept_seconds
            while time.monotonic() < deadline:
                prompt = accept_prompt()
                if prompt is not None:
                    log.info(
                        "dismissed Zoom prompt %r (%s)", prompt.title, prompt.cls
                    )
                time.sleep(0.5)

        threading.Thread(target=_accept_loop, daemon=True).start()

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
            controller.close()
            log.error(
                "OBS is running but its WebSocket server is unreachable; enable "
                "Tools > WebSocket Server Settings (port 4455) and pass --obs-password "
                "when authentication is on"
            )
            return SessionResult(EXIT_OBS)
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
            log.info("recording until the meeting ends")

        exit_code = _monitor(
            controller,
            max_duration,
            close_grace,
            should_stop,
            window_classes,
            title_pattern,
        )
    except KeyboardInterrupt:
        interrupted = True
        if keep_recording_on_interrupt:
            keep_recording = True
            log.warning("interrupted; leaving the OBS recording running")
        else:
            log.info("interrupted; stopping the recording")
    finally:
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
                log.info("recording verified: %s", saved_path)
            else:
                log.error(
                    "no recording file found in %s or OBS's folder", output_path
                )
                exit_code = exit_code or EXIT_NO_FILE
        controller.close()

    return SessionResult(exit_code, saved_path, interrupted)


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
            default=28800.0,
            show_default="8h",
            type=click.FloatRange(min=0.0),
            help="Safety cap in seconds; 0 disables it.",
        ),
        click.option(
            "--close-grace",
            default=5.0,
            show_default="5s",
            type=click.FloatRange(min=0.0),
            help="Seconds without a Zoom meeting window before OBS is stopped.",
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
            "--auto-accept",
            is_flag=True,
            help="Press Enter on blocking Zoom prompts (disclosure/confirmation).",
        ),
        click.option(
            "--auto-accept-seconds",
            default=20.0,
            show_default="20s",
            type=click.FloatRange(min=0.0),
            help="How long to keep watching for blocking Zoom prompts.",
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
    keep_recording_on_interrupt: bool,
    min_free_gb: float,
    window_classes: tuple[str, ...],
    window_title: str | None,
    passcode: str | None,
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

    config = ObsConfig(port=obs_port, password=obs_password or "", record_dir=output_path)

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

    result = _record_session(
        url=url,
        output_path=output_path,
        config=config,
        obs_exe=obs_exe,
        launch_mode=launch_mode,
        join_timeout=join_timeout,
        join_delay=join_delay,
        max_duration=max_duration,
        close_grace=close_grace,
        keep_recording_on_interrupt=keep_recording_on_interrupt,
        min_free_gb=min_free_gb,
        window_classes=window_classes,
        window_title=window_title,
        passcode=passcode,
        auto_accept=auto_accept,
        auto_accept_seconds=auto_accept_seconds,
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
    "--repeat",
    type=click.Choice(["none", "weekly", "weekdays"]),
    default="none",
    show_default=True,
    help="Re-arm the event after each recording.",
)
@click.option(
    "--schedule",
    "schedule_path",
    default=DEFAULT_SCHEDULE,
    show_default=True,
    type=click.Path(dir_okay=False),
)
def add_meeting(
    spec: str | None,
    url: str | None,
    start_at: str | None,
    output_dir: str | None,
    note: str,
    repeat: str,
    schedule_path: str,
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
        duplicate = next(
            (
                item
                for item in store.pending()
                if item.url == url.strip() and item.start == canonical
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
            repeat=None if repeat == "none" else repeat,
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
    obs_port: int,
    obs_password: str | None,
    obs_exe: str | None,
    launch_mode: str,
    join_timeout: float,
    join_delay: float,
    max_duration: float,
    close_grace: float,
    keep_recording_on_interrupt: bool,
    min_free_gb: float,
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
                port=obs_port, password=obs_password or "", record_dir=default_output
            ),
            stop_orphan_recording,
        )

        while True:
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
                continue

            assert entry is not None
            output = (
                Path(entry.output_dir).expanduser().resolve()
                if entry.output_dir
                else default_output
            )
            try:
                output.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.error("entry %s: cannot create %s: %s", entry.id, output, exc)
                schedule.mark(entry.id, STATUS_FAILED, exit_code=1)
                failures += 1
                if once:
                    break
                continue

            log.info("recording entry %s (%s) into %s", entry.id, entry.start, output)
            schedule.mark(entry.id, STATUS_RUNNING)
            result = _record_session(
                url=entry.url,
                output_path=output,
                config=ObsConfig(
                    port=obs_port, password=obs_password or "", record_dir=output
                ),
                obs_exe=obs_exe,
                launch_mode=launch_mode,
                join_timeout=join_timeout,
                join_delay=join_delay,
                max_duration=max_duration,
                close_grace=close_grace,
                keep_recording_on_interrupt=keep_recording_on_interrupt,
                min_free_gb=min_free_gb,
                window_classes=window_classes,
                window_title=window_title,
                passcode=passcode,
                auto_accept=auto_accept,
                auto_accept_seconds=auto_accept_seconds,
                should_stop=lambda: stop_requested(schedule.path),
            )

            status = STATUS_DONE if result.exit_code == EXIT_OK else STATUS_FAILED
            if result.exit_code != EXIT_OK:
                failures += 1
            schedule.mark(entry.id, status, exit_code=result.exit_code, path=result.path)
            log.info(
                "entry %s finished: exit %s%s",
                entry.id,
                result.exit_code,
                f", saved {result.path}" if result.path else "",
            )
            if entry.repeat and not result.interrupted and not stop_requested(schedule.path):
                rolled = schedule.roll_repeat(entry.id)
                if rolled is not None:
                    log.info("next occurrence of %s: %s", entry.id, rolled.start)

            if once or result.interrupted or stop_requested(schedule.path):
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


def build_schtasks_args(
    *,
    name: str,
    schedule_path: str | Path,
    output_dir: str | Path,
    log_file: str | Path | None = None,
    python_exe: str | Path | None = None,
) -> list[str]:
    """``schtasks /Create`` command that starts the resident runner at logon."""
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
def install(
    name: str,
    schedule_path: str,
    output_dir: str,
    log_file: str | None,
    python_exe: str | None,
) -> None:
    """Register the resident runner to start at logon (Windows Task Scheduler)."""
    if not is_windows():
        _fail(1, "Task Scheduler registration is Windows-only")

    args = build_schtasks_args(
        name=name,
        schedule_path=schedule_path,
        output_dir=output_dir,
        log_file=log_file,
        python_exe=python_exe,
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


@cli.command("import-ics")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
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
    path: str, schedule_path: str, output_dir: str | None, dry_run: bool
) -> None:
    """Import Zoom meetings from an .ics calendar file."""
    from ics import parse_events

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        _fail(1, f"cannot read {path}: {exc}")

    events = parse_events(text)
    if not events:
        console.print("no Zoom events with a link found in the calendar")
        return

    try:
        store = ScheduleStore(schedule_path)
    except StoreError as exc:
        raise click.ClickException(str(exc)) from exc

    existing = {(entry.url, entry.start) for entry in store.entries}
    added = skipped = 0
    for event in events:
        canonical = event.when.strftime(START_FORMAT)
        if (event.url, canonical) in existing:
            skipped += 1
            continue
        if dry_run:
            console.print(
                f"[cyan]would add[/] {canonical} {event.url} "
                f"({event.summary or 'no summary'}, repeat={event.repeat or 'none'})"
            )
        else:
            store.add(
                url=event.url,
                start_at=canonical,
                output_dir=output_dir,
                note=event.summary,
                repeat=event.repeat,
            )
            existing.add((event.url, canonical))
        added += 1

    verb = "would add" if dry_run else "added"
    console.print(f"{verb} {added} event(s), skipped {skipped} duplicate(s)")


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
        if auto_accept:
            prompt = accept_prompt(window.hwnd)
            if prompt is not None:
                log.info("dismissed prompt %r (%s)", prompt.title, prompt.cls)
        time.sleep(1.0)

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
