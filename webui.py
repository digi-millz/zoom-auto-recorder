"""Local web UI for the recorder: calendar, add form, entry list, runner control.

Stdlib only (``http.server``), bound to localhost, no authentication — it is a
desktop control panel, not a network service. The page talks to this module's
JSON API; everything goes through the same ``schedule.json`` the CLI uses.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from obs_controller import is_websocket_reachable
from scheduler import ScheduleError
from store import RunnerLock, ScheduleStore, StoreError, request_stop

log = logging.getLogger(__name__)

UI_FILE = Path(__file__).with_name("ui.html")
_PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass
class UiState:
    schedule_path: Path
    output_dir: Path
    log_file: Path
    obs_port: int
    token: str = ""
    obs_cache: tuple[float, bool] | None = None


def _obs_reachable(state: UiState) -> bool:
    if state.obs_cache and time.monotonic() - state.obs_cache[0] < 5.0:
        return state.obs_cache[1]
    reachable = is_websocket_reachable("127.0.0.1", state.obs_port)
    state.obs_cache = (time.monotonic(), reachable)
    return reachable


def status_payload(state: UiState) -> dict:
    store = ScheduleStore(state.schedule_path)
    pending = store.pending()
    return {
        "schedule": str(store.path),
        "output_dir": str(state.output_dir),
        "runner_pid": RunnerLock(store.path).owner_pid(),
        "obs_reachable": _obs_reachable(state),
        "obs_port": state.obs_port,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "next": pending[0].to_dict() if pending else None,
        "entries": [entry.to_dict() for entry in store.sorted_entries()],
    }


def start_runner(state: UiState) -> int:
    owner = RunnerLock(state.schedule_path).owner_pid()
    if owner:
        raise RuntimeError(f"runner already running (pid {owner})")

    command = [
        sys.executable,
        str(_PROJECT_ROOT / "main.py"),
        "run",
        "--schedule",
        str(state.schedule_path),
        "--output-dir",
        str(state.output_dir),
        "--log-file",
        str(state.log_file),
        "--obs-port",
        str(state.obs_port),
    ]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=str(_PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    log.info("started runner pid %s", process.pid)
    return process.pid


def stop_runner(state: UiState) -> bool:
    """Write the stop marker; the runner stops after the current step.

    A marker is used instead of a signal: a hard kill would skip OBS's
    ``StopRecord`` and leave the lock file behind. The runner polls it while
    waiting, between entries and during a recording.
    """
    pid = RunnerLock(state.schedule_path).owner_pid()
    if not pid:
        return False
    request_stop(state.schedule_path)
    log.info("asked runner pid %s to stop", pid)
    return True


class UiHandler(BaseHTTPRequestHandler):
    server_version = "ZoomRecorderUI/1.0"

    @property
    def state(self) -> UiState:
        return self.server.state  # type: ignore[attr-defined]

    @property
    def _server(self) -> "UiServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        log.debug("%s - %s", self.address_string(), format % args)

    # -- responses --------------------------------------------------------- #

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_ui(self) -> None:
        try:
            body = (
                UI_FILE.read_text(encoding="utf-8")
                .replace("__API_TOKEN__", self.state.token)
                .encode("utf-8")
            )
        except OSError as exc:
            self._send_json(500, {"error": f"ui.html missing: {exc}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in self._server.allowed_hosts

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        allowed = {f"http://{host}" for host in self._server.allowed_hosts}
        return origin.lower() in allowed

    def _token_ok(self) -> bool:
        supplied = self.headers.get("X-Auth-Token") or ""
        return secrets.compare_digest(supplied, self.state.token)

    def _guard(self, *, need_token: bool = True) -> bool:
        if not self._host_ok() or not self._origin_ok():
            self._send_json(403, {"error": "forbidden"})
            return False
        if need_token and not self._token_ok():
            self._send_json(401, {"error": "missing or invalid token"})
            return False
        return True

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        return payload

    # -- routes ------------------------------------------------------------ #

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not self._guard(need_token=False):
                return
            self._send_ui()
        elif path == "/api/status":
            if not self._guard():
                return
            try:
                self._send_json(200, status_payload(self.state))
            except StoreError as exc:
                self._send_json(500, {"error": str(exc)})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        path = urlparse(self.path).path
        if not self._guard():
            return
        content_type = (self.headers.get("Content-Type") or "").lower()
        if not content_type.startswith("application/json"):
            self._send_json(415, {"error": "expected application/json"})
            return
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        if path == "/api/entries":
            self._add_entry(payload)
        elif path == "/api/runner/start":
            try:
                self._send_json(200, {"pid": start_runner(self.state)})
            except RuntimeError as exc:
                self._send_json(409, {"error": str(exc)})
        elif path == "/api/runner/stop":
            self._send_json(200, {"stopped": stop_runner(self.state)})
        else:
            self._send_json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib signature
        path = urlparse(self.path).path
        if not self._guard():
            return
        prefix = "/api/entries/"
        if not path.startswith(prefix):
            self._send_json(404, {"error": "not found"})
            return
        entry_id = path[len(prefix) :]
        try:
            removed = ScheduleStore(self.state.schedule_path).remove(entry_id)
        except StoreError as exc:
            self._send_json(500, {"error": str(exc)})
            return
        if removed:
            self._send_json(200, {"removed": entry_id})
        else:
            self._send_json(404, {"error": f"no entry {entry_id}"})

    # -- helpers ----------------------------------------------------------- #

    def _add_entry(self, payload: dict) -> None:
        url = str(payload.get("url") or "").strip()
        when = str(payload.get("when") or "").strip()
        note = str(payload.get("note") or "").strip()
        output_dir = str(payload.get("output_dir") or "").strip() or None
        repeat = (str(payload.get("repeat") or "").strip().lower() or None)
        if repeat in ("none", "once"):
            repeat = None

        if not url:
            self._send_json(400, {"error": "url is required"})
            return
        if not when:
            self._send_json(400, {"error": "date and time are required"})
            return
        try:
            entry = ScheduleStore(self.state.schedule_path).add(
                url=url,
                start_at=when,
                output_dir=output_dir,
                note=note,
                repeat=repeat,
            )
        except (ScheduleError, StoreError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(201, {"entry": entry.to_dict()})


class UiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: UiState):
        self.state = state
        super().__init__(address, UiHandler)
        port = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}


def make_server(
    *,
    schedule_path: str | Path,
    output_dir: str | Path,
    log_file: str | Path | None = None,
    obs_port: int = 4455,
    host: str = "127.0.0.1",
    port: int = 0,
) -> UiServer:
    state = UiState(
        schedule_path=Path(schedule_path).expanduser().resolve(),
        output_dir=Path(output_dir).expanduser().resolve(),
        log_file=Path(log_file or "./recordings/runner.log").expanduser().resolve(),
        obs_port=obs_port,
        token=secrets.token_urlsafe(24),
    )
    return UiServer((host, port), state)


def serve(
    *,
    schedule_path: str | Path,
    output_dir: str | Path,
    log_file: str | Path | None = None,
    obs_port: int = 4455,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    server = make_server(
        schedule_path=schedule_path,
        output_dir=output_dir,
        log_file=log_file,
        obs_port=obs_port,
        host=host,
        port=port,
    )
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"zoom recorder UI: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nstopping UI (the runner keeps going)")
    finally:
        server.server_close()
