"""OBS Studio control over obs-websocket v5, plus the standalone-launch fallback.

Uses ``obs-websocket-py==1.0``, the only release speaking the v5 protocol
bundled with OBS 28+.

Note: ``obsws.call()`` does **not** raise when OBS rejects a request — it returns
the request object with ``status == False``. Every call therefore goes through
:meth:`ObsController._call`, which converts that into :class:`ObsError`.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

log = logging.getLogger(__name__)

DEFAULT_PORT = 4455
RECORD_STARTED = "OBS_WEBSOCKET_OUTPUT_STARTED"
RECORD_STOPPED = "OBS_WEBSOCKET_OUTPUT_STOPPED"

# Profile keys holding the recording directory, in the order OBS versions use them.
_PROFILE_DIR_KEYS = (("SimpleOutput", "FilePath"), ("AdvOut", "RecFilePath"))


class ObsError(RuntimeError):
    """Any failure while talking to OBS."""


@dataclass
class ObsConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    password: str = ""
    record_dir: Path | None = None


class ObsController:
    """Thin, testable wrapper around a single OBS WebSocket session."""

    def __init__(
        self,
        config: ObsConfig,
        *,
        client_factory: Callable[[], Any] | None = None,
        requests_module: Any | None = None,
        request_timeout: float = 10.0,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self._requests_module = requests_module
        self._request_timeout = request_timeout
        self._client: Any | None = None
        self._version: str | None = None
        self._last_output_path: str | None = None
        self._restore_kind: str | None = None
        self._restore_args: tuple[Any, ...] = ()

    # -- plumbing ---------------------------------------------------------- #

    def _requests(self) -> Any:
        if self._requests_module is None:
            from obswebsocket import requests

            self._requests_module = requests
        return self._requests_module

    def _make_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        from obswebsocket import obsws

        return obsws(
            self.config.host,
            self.config.port,
            password=self.config.password,
            legacy=False,
            timeout=self._request_timeout,
        )

    def _call(self, request: Any) -> Any:
        if self._client is None:
            raise ObsError("not connected to OBS")
        try:
            result = self._client.call(request)
        except Exception as exc:  # noqa: BLE001 - every transport failure must look like ObsError
            # obs-websocket raises raw websocket/socket errors when OBS dies (a force-kill gives
            # ConnectionResetError 10054). Callers only guard ObsError, so leaking these ended the process.
            raise ObsError(f"lost the OBS connection: {type(exc).__name__}: {exc}") from exc
        if getattr(result, "status", None) is False:
            name = getattr(request, "name", type(request).__name__)
            raise ObsError(f"OBS rejected {name}: {getattr(result, 'datain', {})}")
        return result

    def call(self, request: Any) -> Any:
        """Send a request, raising :class:`ObsError` when OBS rejects it.

        Public so scene setup (`scene.prepare`) reuses this connection and its error semantics.
        """
        return self._call(request)

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def version(self) -> str | None:
        return self._version

    @property
    def last_output_path(self) -> str | None:
        return self._last_output_path

    # -- session ----------------------------------------------------------- #

    def connect(self) -> str:
        """Connect, authenticate and return the OBS version string."""
        if self._client is not None:
            return self._version or ""

        host, port = self.config.host, self.config.port
        try:
            with socket.create_connection((host, port), timeout=5.0):
                pass
        except OSError as exc:
            raise ObsError(f"OBS WebSocket not reachable at {host}:{port} ({exc})") from exc

        client = self._make_client()
        try:
            client.connect()
            self._client = client
            version = self._call(self._requests().GetVersion()).getObsVersion()
        except ObsError:
            self._client = None
            with suppress(Exception):
                client.disconnect()
            raise
        except Exception as exc:
            self._client = None
            with suppress(Exception):
                client.disconnect()
            raise ObsError(f"cannot connect to OBS WebSocket at {host}:{port}: {exc}") from exc

        self._version = str(version)
        self._subscribe()
        log.info("connected to OBS %s at %s:%s", self._version, host, port)
        return self._version

    def _subscribe(self) -> None:
        """Capture the recorded file path from the stop event (best effort)."""
        client = self._client
        if client is None:
            return
        try:
            from obswebsocket import events
        except Exception:  # pragma: no cover - dependency is declared
            return
        try:
            client.register(self._on_record_state, events.RecordStateChanged)
        except Exception as exc:  # fake clients in tests may not support it
            log.debug("event subscription unavailable: %s", exc)

    def _on_record_state(self, event: Any) -> None:
        data = getattr(event, "datain", None) or {}
        if data.get("outputState") == RECORD_STOPPED:
            path = data.get("outputPath")
            if path:
                self._last_output_path = str(path)
            log.debug("OBS reported recording stopped: %s", path)

    def close(self) -> None:
        """Restore the record directory (if changed) and disconnect; idempotent."""
        if self._client is None:
            return
        try:
            self.restore_record_dir()
        finally:
            client, self._client = self._client, None
            with suppress(Exception):
                client.disconnect()

    # -- record state ------------------------------------------------------ #

    def is_recording(self) -> bool:
        return self._active()

    def _active(self) -> bool:
        status = self._call(self._requests().GetRecordStatus())
        return bool(status.getOutputActive())

    def _wait_active(self, expected: bool, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if self._active() is expected:
                return
            if time.monotonic() >= deadline:
                state = "active" if expected else "stopped"
                raise ObsError(f"OBS never reported the recording as {state} within {timeout:.0f}s")
            time.sleep(0.25)

    def start_record(self, timeout: float = 15.0) -> None:
        log.info("requesting OBS StartRecord")
        try:
            self._call(self._requests().StartRecord())
        except ObsError as exc:
            raise ObsError(f"StartRecord failed: {exc}") from exc
        self._wait_active(True, timeout)
        log.info("OBS confirmed the recording is active")

    def stop_record(self, timeout: float = 20.0) -> str | None:
        """Stop recording and return the file path OBS reported, if any."""
        try:
            self._call(self._requests().StopRecord())
        except ObsError as exc:
            raise ObsError(f"StopRecord failed: {exc}") from exc
        try:
            self._wait_active(False, timeout)
        except ObsError as exc:
            log.warning("%s", exc)
        else:
            log.info("OBS confirmed the recording stopped")
        return self._last_output_path

    # -- recording directory ---------------------------------------------- #

    def ensure_record_dir(self, directory: Path | str) -> bool:
        """Point OBS at *directory*, remembering the previous value for restore.

        Tries ``SetRecordDirectory`` (obs-websocket >= 5.3) first, then the
        ``SimpleOutput.FilePath`` / ``AdvOut.RecFilePath`` profile parameter that
        every v5 build supports. Returns ``False`` when OBS could not be changed.
        """
        target = Path(directory).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        requests = self._requests()

        original: str | None = None
        with suppress(ObsError, KeyError, AttributeError):
            original = str(self._call(requests.GetRecordDirectory()).getRecordDirectory())

        try:
            self._call(requests.SetRecordDirectory(recordDirectory=str(target)))
        except ObsError as exc:
            log.debug("SetRecordDirectory unsupported: %s", exc)
        else:
            self._restore_kind, self._restore_args = "request", (original,)
            log.info("OBS record directory set to %s", target)
            return True

        for category, name in _PROFILE_DIR_KEYS:
            try:
                current = str(
                    self._call(
                        requests.GetProfileParameter(
                            parameterCategory=category, parameterName=name
                        )
                    ).getParameterValue()
                )
                self._call(
                    requests.SetProfileParameter(
                        parameterCategory=category,
                        parameterName=name,
                        parameterValue=str(target),
                    )
                )
            except (ObsError, KeyError, AttributeError) as exc:
                log.debug("cannot use profile key %s.%s: %s", category, name, exc)
                continue
            self._restore_kind, self._restore_args = "profile", (current, category, name)
            log.info(
                "OBS record directory set via profile %s.%s to %s (was %r)",
                category,
                name,
                target,
                current,
            )
            return True

        log.warning("could not point OBS at %s; it keeps its configured directory", target)
        return False

    def restore_record_dir(self) -> None:
        """Undo :meth:`ensure_record_dir`; safe to call more than once."""
        if self._restore_kind is None or self._client is None:
            return
        kind, args = self._restore_kind, self._restore_args
        self._restore_kind, self._restore_args = None, ()
        requests = self._requests()
        try:
            if kind == "request":
                if args[0]:
                    self._call(requests.SetRecordDirectory(recordDirectory=str(args[0])))
                    log.info("restored the OBS record directory to %s", args[0])
            else:
                original, category, name = args
                self._call(
                    requests.SetProfileParameter(
                        parameterCategory=category,
                        parameterName=name,
                        parameterValue=str(original),
                    )
                )
                log.info("restored OBS profile %s.%s to %r", category, name, original)
        except Exception as exc:
            log.warning("could not restore the original OBS record directory: %s", exc)

    def record_directory(self) -> str | None:
        """OBS's configured recording folder, when the build exposes it."""
        try:
            return str(self._call(self._requests().GetRecordDirectory()).getRecordDirectory())
        except Exception as exc:
            log.debug("GetRecordDirectory unavailable: %s", exc)
            return None

    def recording_format(self) -> str | None:
        """Configured recording container (mkv, mp4, ...), if the build exposes it."""
        for category, name in (("SimpleOutput", "RecFormat2"), ("AdvOut", "RecFormat2")):
            try:
                value = str(
                    self._call(
                        self._requests().GetProfileParameter(
                            parameterCategory=category, parameterName=name
                        )
                    ).getParameterValue()
                )
            except Exception:
                continue
            if value:
                return value
        return None

    def ensure_recording_format(self, fmt: str = "mkv") -> str | None:
        """Force the recording container, because only mkv survives a crash.

        Measured: force-killing OBS during an mp4 (even ``hybrid_mp4``) recording leaves a **0-byte** file — the
        footer is written on shutdown. mkv is written incrementally, so the same crash leaves a playable file, and
        ``postprocess`` remuxes it afterwards. Returns the format OBS reports afterwards.
        """
        current = self.recording_format()
        if current == fmt:
            return current
        for category in ("SimpleOutput", "AdvOut"):
            with suppress(Exception):
                self._call(
                    self._requests().SetProfileParameter(
                        parameterCategory=category, parameterName="RecFormat2", parameterValue=fmt
                    )
                )
        after = self.recording_format()
        if after == fmt:
            log.info("OBS recording container set to %s (a crash still leaves a playable file)", fmt)
        else:
            log.warning(
                "OBS refused the %s container (it reports %r); if OBS dies mid-recording the file will be "
                "unusable — set Settings > Output > Recording Format to mkv",
                fmt,
                after,
            )
        return after

    def video_size(self) -> tuple[int, int] | None:
        """Current canvas/output resolution as ``(width, height)``, or ``None`` if OBS will not say."""
        try:
            data = self._call(self._requests().GetVideoSettings()).datain or {}
            return int(data["baseWidth"]), int(data["baseHeight"])
        except Exception as exc:  # noqa: BLE001 - a diagnostic read must not break a recording
            log.debug("could not read OBS video settings: %s", exc)
            return None

    def set_video_size(self, width: int, height: int, fps: int = 30) -> tuple[int, int] | None:
        """Set canvas and output resolution, keeping them equal, and report what OBS ended up with.

        Window capture scales the Zoom window into the canvas, so this is the knob that decides whether shared text
        is legible. Verified by reading the settings back: a refused change is reported, not assumed.
        """
        try:
            self._call(
                self._requests().SetVideoSettings(
                    baseWidth=width,
                    baseHeight=height,
                    outputWidth=width,
                    outputHeight=height,
                    fpsNumerator=fps,
                    fpsDenominator=1,
                )
            )
        except ObsError as exc:
            log.warning("could not set the recording resolution to %dx%d: %s", width, height, exc)
            return None
        actual = self.video_size()
        if actual == (width, height):
            log.info("OBS canvas set to %dx%d @ %d fps", width, height, fps)
        elif actual is not None:
            log.warning(
                "OBS reports %dx%d after asking for %dx%d; the recording keeps OBS's resolution",
                actual[0],
                actual[1],
                width,
                height,
            )
        return actual


# --------------------------------------------------------------------------- #
# OBS discovery, recording verification and the standalone launcher
# --------------------------------------------------------------------------- #

RECORDING_SUFFIXES = {".mkv", ".mp4", ".mov", ".flv", ".ts", ".m4v"}


def find_recent_recording(
    directories: Sequence[str | Path], since: datetime
) -> Path | None:
    """Newest non-empty recording in *directories* written since *since*.

    OBS does not reliably report a path (the event may be missed, or the build
    is too old), so the file is also located on disk.
    """
    threshold = since.timestamp() - 5.0
    best: tuple[float, Path] | None = None
    for directory in directories:
        try:
            entries = Path(directory).expanduser().iterdir()
        except OSError:
            continue
        for candidate in entries:
            try:
                if not candidate.is_file() or candidate.suffix.lower() not in RECORDING_SUFFIXES:
                    continue
                stat = candidate.stat()
            except OSError:
                continue
            if stat.st_size <= 0 or stat.st_mtime < threshold:
                continue
            if best is None or stat.st_mtime > best[0]:
                best = (stat.st_mtime, candidate)
    return best[1] if best else None


def is_websocket_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _obs_from_registry() -> Path | None:
    if sys.platform != "win32":
        return None
    import winreg

    candidates = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\OBS Studio"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\OBS Studio"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\OBS Studio"),
    )
    for root, key_path in candidates:
        try:
            with winreg.OpenKey(root, key_path) as key:
                icon, _ = winreg.QueryValueEx(key, "DisplayIcon")
        except OSError:
            continue
        path = Path(str(icon).split(",")[0].strip().strip('"'))
        if path.is_file():
            return path
    return None


def _obs_install_candidates() -> list[Path]:
    """Every place an OBS install hides on Windows, in the order worth checking.

    Besides the official installer's ``Program Files`` layout, this covers the two per-user installs that do *not*
    touch PATH or the registry: ``winget``'s package directory and the "install for me only" location. Without
    these, a machine where OBS is closed looks like "OBS is not installed".
    """
    relative = Path("bin") / "64bit" / "obs64.exe"
    roots = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
    ]
    candidates = [Path(root) / "obs-studio" / relative for root in roots if root]

    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "obs-studio" / relative)
        # winget keeps per-user packages under this prefix and never adds a Start Menu entry.
        winget = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if winget.is_dir():
            candidates.extend(
                sorted(winget.glob("OBSProject.OBSStudio*/" + str(relative)), reverse=True)
            )
    registry = _obs_from_registry()
    if registry:
        candidates.append(registry)
    return candidates


def find_obs_exe(explicit: str | Path | None = None) -> Path | None:
    """Locate ``obs64.exe``: explicit flag, env var, PATH, then the known install locations."""
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return candidate
    env = os.environ.get("OBS_EXE")
    if env and Path(env).is_file():
        return Path(env)
    for name in ("obs64.exe", "obs.exe"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for candidate in _obs_install_candidates():
        if candidate.is_file():
            return candidate
    return None


def obs_launch_args(
    exe: str | Path,
    *,
    start_recording: bool = True,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Build the OBS command line. ``--stoprecording`` does not exist, so the
    only way back out of a standalone launch is the WebSocket API."""
    args = [str(exe)]
    if start_recording:
        args.append("--startrecording")
    args.append("--minimize-to-tray")
    args.extend(extra_args)
    return args


def launch_obs(
    exe: str | Path,
    *,
    start_recording: bool = True,
    extra_args: Sequence[str] = (),
) -> subprocess.Popen:
    """Launch OBS standalone, recording immediately.

    OBS must be started with its own directory as the working directory, or it
    fails to find its data files.
    """
    exe = Path(exe)
    args = obs_launch_args(exe, start_recording=start_recording, extra_args=extra_args)
    log.info("launching OBS: %s (cwd=%s)", subprocess.list2cmdline(args), exe.parent)
    return subprocess.Popen(args, cwd=str(exe.parent), close_fds=True)


def is_obs_running() -> bool:
    import psutil

    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").lower() in {"obs.exe", "obs64.exe"}:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def crash_flag_path(appdata: str | Path | None = None) -> Path:
    """OBS's unclean-shutdown marker (``%APPDATA%/obs-studio/.sentinel``).

    Created at launch, removed only on a clean exit. If it survives, every later start shows "Crash Detected —
    run in safe mode?", which blocks the WebSocket server while nobody is watching the screen.
    """
    base = Path(appdata) if appdata is not None else Path(os.environ.get("APPDATA", ""))
    return base / "obs-studio" / ".sentinel"


def clear_crash_flag(appdata: str | Path | None = None) -> bool:
    """Delete a stale crash marker so the next launch goes straight to the normal UI."""
    if is_obs_running():
        log.debug("OBS is running; leaving .sentinel alone")
        return False
    path = crash_flag_path(appdata)
    if not path.exists():
        return False
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        log.warning("could not clear %s: %s", path, exc)
        return False
    log.info("cleared the OBS crash marker; the next launch skips the safe-mode prompt")
    return True


def wait_for_websocket(
    controller: ObsController, *, timeout: float = 45.0, interval: float = 1.0
) -> str:
    """Retry :meth:`ObsController.connect` until *timeout* expires."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while True:
        try:
            return controller.connect()
        except ObsError as exc:
            last = exc
        if time.monotonic() >= deadline:
            raise ObsError(f"OBS WebSocket did not become reachable within {timeout:.0f}s: {last}")
        time.sleep(interval)
