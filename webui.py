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
import time
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import recurrence
import teach
import zoom_account
from defaults import DEFAULT_TZ
from obs_controller import is_websocket_reachable
from scheduler import ScheduleError, parse_start, runner_needed
from store import RunnerLock, ScheduleStore, StoreError, request_stop

RULES_FILE = Path(__file__).with_name("rules.json")

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


def resolve_output_dir(raw: str) -> Path:
    """Validate a user-supplied recordings folder, creating it if needed."""
    text = raw.strip().strip('"').strip("'")
    if not text:
        raise ValueError("a folder is required")
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise ValueError(f"{path} is a file, not a folder")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"cannot create {path}: {exc}") from exc
    return path


def output_dir_for(state: UiState, store: ScheduleStore) -> Path:
    """Recordings folder from the schedule file, falling back to the CLI argument."""
    configured = store.settings.get("output_dir")
    if not configured:
        return state.output_dir
    try:
        return Path(configured).expanduser().resolve()
    except OSError:
        return state.output_dir



def default_email(state: UiState, store: ScheduleStore) -> str:
    """Saved setting, then anything explicit in Zoom's config — empty when nothing is configured.

    ``state`` is unused but keeps the call signature symmetrical with :func:`output_dir_for`.
    """
    return store.setting("email") or zoom_account.discover() or ""


def status_payload(state: UiState) -> dict:
    store = ScheduleStore(state.schedule_path)
    pending = store.pending()
    pid = RunnerLock(store.path).owner_pid()
    return {
        "schedule": str(store.path),
        "output_dir": str(output_dir_for(state, store)),
        "default_output_dir": str(state.output_dir),
        "default_tz": DEFAULT_TZ,
        "default_email": default_email(state, store),
        "notify_url": store.setting("notify_url") or "",
        "calendar_url": store.setting("calendar_url") or "",
        "runner_pid": pid,
        # Same predicate the CLI uses, so the chip and `main.py status` cannot disagree.
        "runner_needed": bool(pending) and runner_needed(pending[0].start_at, datetime.now(), pid),
        "obs_reachable": _obs_reachable(state),
        "obs_port": state.obs_port,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "next": pending[0].to_dict() if pending else None,
        "entries": [entry.to_dict() for entry in store.sorted_entries()],
    }

def occurrences_payload(state: UiState, month: str) -> dict:
    """Every start each entry contributes to ``month`` (``"YYYY-MM"``), so the grid can badge repeat dates.

    The store keeps a series as one entry with an RRULE, so ``entry.start`` is the anchor, not the calendar: without
    this the page showed a weekly meeting on its first date only.
    """
    try:
        first = datetime.strptime(month, "%Y-%m").date()
    except ValueError as exc:
        raise ScheduleError(f"month must be YYYY-MM, got {month!r}") from exc
    last = date(first.year + (first.month == 12), first.month % 12 + 1, 1) - timedelta(days=1)

    store = ScheduleStore(state.schedule_path)
    found: list[dict] = []
    for entry in store.sorted_entries():
        spec = (entry.rrule or "").strip() or recurrence.legacy_repeat_to_rrule(entry.repeat)
        if not spec:
            day = parse_start(entry.start).date()
            if first <= day <= last:
                found.append({"id": entry.id, "start": entry.start})
            continue
        for moment in recurrence.occurrences_between(
            spec, entry.start, entry.tz, first_day=first, last_day=last
        ):
            found.append({"id": entry.id, "start": moment.strftime("%Y-%m-%d %H:%M")})
    found.sort(key=lambda item: item["start"])
    return {"month": month, "occurrences": found}


def preview_payload(payload: dict) -> dict:
    """Next occurrences for the form's repeat builder (server-side, so the maths is the real maths)."""
    start = str(payload.get("start") or "").strip()
    tz = str(payload.get("tz") or "").strip() or None
    spec = str(payload.get("rrule") or "").strip()
    try:
        count = max(1, min(10, int(payload.get("count") or 5)))
    except (TypeError, ValueError):
        count = 5
    if not start:
        raise ScheduleError("a start date and time are required")
    if not spec:
        return {"occurrences": [], "summary": "", "count": 0}
    start_dt = parse_start(start)
    # Preview from "now" when the series already began, otherwise from its first occurrence.
    anchor = min(datetime.now(), start_dt) - timedelta(seconds=1)
    moments = recurrence.occurrences(spec, start, tz, count=count, after=anchor)
    return {
        "occurrences": [moment.strftime("%Y-%m-%d %H:%M") for moment in moments],
        "summary": recurrence.describe(spec, tz),
        "count": len(moments),
        "rrule": spec,
        "tz": tz,
    }


def log_tail(path: Path, lines: int = 200) -> str:
    """Last *lines* lines of the runner log (missing file is not an error)."""
    try:
        if not path.exists():
            return ""
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"(cannot read {path}: {exc})"
    return "\n".join(data[-max(1, lines) :])


def start_runner(state: UiState) -> int:
    owner = RunnerLock(state.schedule_path).owner_pid()
    if owner:
        raise RuntimeError(f"runner already running (pid {owner})")

    folder = output_dir_for(state, ScheduleStore(state.schedule_path))
    command = [
        sys.executable,
        str(_PROJECT_ROOT / "main.py"),
        "run",
        "--schedule",
        str(state.schedule_path),
        "--output-dir",
        str(folder),
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
        parsed = urlparse(self.path)
        path = parsed.path
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
        elif path == "/api/occurrences":
            if not self._guard():
                return
            query = parse_qs(parsed.query)
            month = (query.get("month") or [datetime.now().strftime("%Y-%m")])[0]
            try:
                self._send_json(200, occurrences_payload(self.state, month))
            except (ScheduleError, StoreError) as exc:
                self._send_json(400, {"error": str(exc)})
        elif path == "/api/log":
            if not self._guard():
                return
            query = parse_qs(parsed.query)
            try:
                wanted = int((query.get("lines") or ["200"])[0])
            except ValueError:
                wanted = 200
            self._send_json(
                200,
                {"path": str(self.state.log_file), "text": log_tail(self.state.log_file, wanted)},
            )
        elif path == "/api/shots":
            if not self._guard():
                return
            shots_root = self._shots_root()
            shots = teach.collect_shots(shots_root)
            self._send_json(
                200,
                {
                    "root": str(shots_root),
                    "shots": [
                        {"session": shot.session, "cls": shot.cls, "url": shot.url}
                        for shot in shots
                    ],
                },
            )
        elif path.startswith("/api/shots/"):
            # <img src> cannot send the X-Auth-Token header, so this one route also accepts ?t=; the page already
            # carries the token and the server is localhost-only.
            if not self._host_ok() or not self._origin_ok():
                self._send_json(403, {"error": "forbidden"})
                return
            supplied = (parse_qs(parsed.query).get("t") or [""])[0]
            if not secrets.compare_digest(supplied, self.state.token):
                self._send_json(401, {"error": "missing or invalid token"})
                return
            self._send_shot(path[len("/api/shots/") :])
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
        elif path.startswith("/api/entries/"):
            self._update_entry(path[len("/api/entries/") :], payload)
        elif path == "/api/preview":
            try:
                self._send_json(200, preview_payload(payload))
            except (ScheduleError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
        elif path == "/api/settings":
            self._set_settings(payload)
        elif path == "/api/runner/start":
            try:
                self._send_json(200, {"pid": start_runner(self.state)})
            except RuntimeError as exc:
                self._send_json(409, {"error": str(exc)})
        elif path == "/api/runner/stop":
            self._send_json(200, {"stopped": stop_runner(self.state)})
        elif path == "/api/rules":
            try:
                rule = teach.add_rule(
                    RULES_FILE,
                    cls=str(payload.get("cls") or ""),
                    label=str(payload.get("label") or ""),
                    scope=str(payload.get("scope") or "dialog"),
                )
            except (OSError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            log.info("taught rule %s", rule["name"])
            self._send_json(201, {"rule": rule})
        else:
            self._send_json(404, {"error": "not found"})
        return

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

    def _shots_root(self) -> Path:
        """Where the watcher put its screenshots: the configured recordings folder's `_shots`."""
        try:
            store = ScheduleStore(self.state.schedule_path)
            return output_dir_for(self.state, store) / "_shots"
        except StoreError:
            return self.state.output_dir / "_shots"

    def _send_shot(self, relative: str) -> None:
        """Serve one screenshot. Names must match the generator, so no path can escape the folder."""
        parts = relative.split("/")
        if len(parts) != 2:
            self._send_json(404, {"error": "not found"})
            return
        session, name = parts
        if not teach.SESSION_PATTERN.match(session) or teach.class_name(name) is None:
            self._send_json(404, {"error": "not found"})
            return
        try:
            body = (self._shots_root() / session / name).read_bytes()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _add_entry(self, payload: dict) -> None:
        url = str(payload.get("url") or "").strip()
        when = str(payload.get("when") or "").strip()
        note = str(payload.get("note") or "").strip()
        output_dir = str(payload.get("output_dir") or "").strip() or None
        email = str(payload.get("email") or "").strip() or None
        repeat = (str(payload.get("repeat") or "").strip().lower() or None)
        if repeat in ("none", "once"):
            repeat = None
        rrule = str(payload.get("rrule") or "").strip() or None
        tz = str(payload.get("tz") or "").strip() or None
        mic = bool(payload.get("mic"))
        if not url:
            self._send_json(400, {"error": "url is required"})
            return
        if not when:
            self._send_json(400, {"error": "date and time are required"})
            return
        try:
            store = ScheduleStore(self.state.schedule_path)
            entry = store.add(
                url=url,
                start_at=when,
                output_dir=output_dir,
                note=note,
                email=email,
                repeat=repeat,
                rrule=rrule,
                tz=tz,
                mic=mic,
            )
        except (ScheduleError, StoreError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(201, {"entry": entry.to_dict()})

    def _update_entry(self, entry_id: str, payload: dict) -> None:
        """Edit an existing entry. Only the supplied fields change; ``""`` clears an optional one."""
        fields: dict[str, object] = {}
        for key in ("url", "note", "email", "output_dir", "tz", "rrule", "repeat", "mic"):
            if key in payload:
                fields[key] = payload[key]
        when = str(payload.get("when") or "").strip()
        if when:
            fields["start_at"] = when
        if not fields:
            self._send_json(400, {"error": "nothing to update"})
            return
        try:
            entry = ScheduleStore(self.state.schedule_path).update(entry_id, **fields)
        except (ScheduleError, StoreError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        if entry is None:
            self._send_json(404, {"error": f"no entry {entry_id}"})
            return
        log.info("entry %s updated", entry.id)
        self._send_json(200, {"entry": entry.to_dict()})


    def _set_settings(self, payload: dict) -> None:
        """Update persisted settings: recordings folder, Zoom prejoin email, notification webhook."""
        updated: dict[str, str] = {}
        if payload.get("output_dir") is not None:
            try:
                updated["output_dir"] = str(resolve_output_dir(str(payload["output_dir"])))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
        if payload.get("email") is not None:
            address = str(payload["email"]).strip()
            if address and ("@" not in address or address.endswith("@")):
                self._send_json(400, {"error": f"{address!r} is not an email address"})
                return
            updated["email"] = address
        for key, label in (("notify_url", "notification"), ("calendar_url", "calendar")):
            if payload.get(key) is None:
                continue
            value = str(payload[key]).strip()
            if value and not value.lower().startswith(("http://", "https://")):
                self._send_json(
                    400, {"error": f"the {label} URL must start with http:// or https://"}
                )
                return
            updated[key] = value
        if not updated:
            self._send_json(400, {"error": "nothing to update"})
            return
        try:
            store = ScheduleStore(self.state.schedule_path)
            for key, value in updated.items():
                store.set_setting(key, value)
        except StoreError as exc:
            self._send_json(500, {"error": str(exc)})
            return
        log.info("settings updated: %s", ", ".join(sorted(updated)))
        self._send_json(
            200,
            {
                "output_dir": str(output_dir_for(self.state, store)),
                "email": default_email(self.state, store),
                "notify_url": store.setting("notify_url") or "",
                "calendar_url": store.setting("calendar_url") or "",
                "updated": sorted(updated),
            },
        )


class UiServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows happily binds a second listener to the same port when SO_REUSEADDR is set, and each server has its
    # own token — requests then hit a random one and every other call fails with "missing or invalid token".
    # Refusing the address makes the second launch fail loudly instead.
    allow_reuse_address = False

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
    try:
        server = make_server(
            schedule_path=schedule_path,
            output_dir=output_dir,
            log_file=log_file,
            obs_port=obs_port,
            host=host,
            port=port,
        )
    except OSError as exc:
        raise SystemExit(
            f"cannot listen on {host}:{port} ({exc}).\n"
            f"Another recorder UI is probably already running — open "
            f"http://{host}:{port}/ , or start this one with --port {port + 1}."
        ) from exc
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
