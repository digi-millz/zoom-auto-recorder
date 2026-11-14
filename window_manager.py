"""Launch the Zoom client, locate its meeting window and maximise it (Windows only).

The Windows API is bound lazily and with explicit prototypes: passing an ``HWND``
as a plain Python int truncates it on 64-bit builds, so every function used here
has ``argtypes``/``restype`` set.

Window selection is a pure function over :class:`WindowInfo` values so it can be
tested without a live Zoom instance.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterable, Sequence
from urllib.parse import parse_qs, quote, urlencode, urlparse

log = logging.getLogger(__name__)

SW_RESTORE = 9
SW_MAXIMIZE = 3

# Zoom window classes as observed on the desktop client.
PRIMARY_MEETING_CLASSES = frozenset(
    {
        "ZPContentViewWndClass",        # meeting content/frame (older builds)
        "ZPMeetingMainWndClass",        # meeting frame
        "ConfMultiTabContentWndClass",  # in-meeting window (Zoom Workplace 7.x)
        "zWaitingRoomWndClass",         # waiting room
        "zChangeNameWndClass",          # pre-join name prompt
    }
)
PROMPT_CLASSES = frozenset(
    {
        "zRichMsgBoxClass",   # "Disclosure" / recording notice, join confirmations
        "ConfPopupTopClass",  # misc meeting popups
    }
)
PREJOIN_CLASSES = frozenset({"VideoPreviewWndClass"})  # camera/mic preview with Join
SECONDARY_CLASSES = frozenset({"zVideoShowWndClass"})  # fullscreen/pop-out video
EXCLUDED_CLASSES = frozenset({"ZPPTMainFrmWndClass"})  # Zoom home/Workplace window
HOME_TITLES = frozenset({"zoom", "zoom workplace", "zoom cloud meetings", "zoom meetings"})
MEETING_TITLE_MARKERS = ("zoom meeting", "zoom webinar")
ZOOM_PROCESS = "zoom.exe"


class WindowNotFound(RuntimeError):
    """Raised when no Zoom meeting window appears before the deadline."""


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    cls: str
    visible: bool = True
    width: int = 0
    height: int = 0

    @property
    def area(self) -> int:
        return self.width * self.height


# --------------------------------------------------------------------------- #
# URL handling (pure)
# --------------------------------------------------------------------------- #

def to_zoommtg(url: str, passcode: str | None = None) -> str | None:
    """Convert a Zoom join link into a ``zoommtg://`` protocol URL.

    Returns ``None`` when *url* is not a recognisable Zoom join link, in which
    case the caller should fall back to opening it in the browser. Already
    ``zoommtg://`` URLs pass through untouched.
    """
    url = url.strip()
    if url.lower().startswith("zoommtg://"):
        return url

    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    if "zoom.us" not in host:
        return None

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0] not in {"j", "s", "w"} or not parts[1].isdigit():
        return None

    params = {"action": "join", "confno": parts[1]}
    pwd = passcode or parse_qs(parsed.query).get("pwd", [None])[0]
    if pwd:
        params["pwd"] = pwd
    return "zoommtg://zoom.us/join?" + urlencode(params, quote_via=quote)


def select_meeting_window(
    windows: Iterable[WindowInfo],
    *,
    extra_classes: Iterable[str] = (),
    title_pattern=None,
) -> WindowInfo | None:
    """Pick the most likely meeting window from *windows*.

    The meeting frame class outweighs everything so a pop-out video window
    (which is also titled "Zoom Meeting") can never win; ties break on area.
    """
    best: WindowInfo | None = None
    best_score = 0
    for window in windows:
        if not window.visible or not window.title.strip():
            continue
        if window.cls in EXCLUDED_CLASSES:
            continue

        title = window.title.strip().lower()
        score = 0
        if window.cls in PRIMARY_MEETING_CLASSES or window.cls in extra_classes:
            score += 10
        elif window.cls in SECONDARY_CLASSES:
            score += 3
        if title_pattern is not None and title_pattern.search(window.title):
            score += 10
        if any(marker in title for marker in MEETING_TITLE_MARKERS):
            score += 1
        if title in HOME_TITLES:
            score -= 5
        if score <= 0:
            continue

        if best is None or (score, window.area) > (best_score, best.area):
            best, best_score = window, score
    return best


# --------------------------------------------------------------------------- #
# Win32 bindings
# --------------------------------------------------------------------------- #

if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsWindow.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.BringWindowToTop.restype = wintypes.BOOL
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.AttachThreadInput.restype = wintypes.BOOL
    _user32.keybd_event.argtypes = [
        ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, ctypes.c_size_t
    ]
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.PostMessageW.restype = wintypes.BOOL
    _user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    _user32.SetCursorPos.restype = wintypes.BOOL
    _user32.mouse_event.argtypes = [
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t
    ]
else:  # pragma: no cover - exercised only off Windows
    _user32 = None


def is_windows() -> bool:
    return _user32 is not None


def _require_windows() -> None:
    if _user32 is None:
        raise RuntimeError("Zoom window automation requires Windows")


def _window_text(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def _process_names(pids: Sequence[int]) -> dict[int, str]:
    """Map pid -> lowercased image name; unknown pids are omitted."""
    import psutil

    names: dict[int, str] = {}
    for pid in dict.fromkeys(pids):
        if not pid:
            continue
        try:
            names[pid] = (psutil.Process(pid).name() or "").lower()
        except Exception:  # AccessDenied / NoSuchProcess / zombie
            continue
    return names


def iter_windows(process_name: str = ZOOM_PROCESS) -> list[WindowInfo]:
    """Enumerate top-level windows owned by *process_name*."""
    _require_windows()
    handles: list[int] = []

    @_EnumWindowsProc
    def _collect(hwnd, _lparam):
        handles.append(hwnd)
        return True

    _user32.EnumWindows(_collect, 0)

    pids: dict[int, int] = {}
    for hwnd in handles:
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            pids[hwnd] = pid.value
    names = _process_names(list(pids.values()))

    windows: list[WindowInfo] = []
    for hwnd, pid in pids.items():
        if names.get(pid) != process_name:
            continue
        rect = wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        windows.append(
            WindowInfo(
                hwnd=int(hwnd),
                pid=pid,
                title=_window_text(hwnd),
                cls=_class_name(hwnd),
                visible=bool(_user32.IsWindowVisible(hwnd)),
                width=max(rect.right - rect.left, 0),
                height=max(rect.bottom - rect.top, 0),
            )
        )
    return windows


def find_process(process_name: str = ZOOM_PROCESS) -> int | None:
    """Return the pid of the first process with *process_name*, else ``None``."""
    import psutil

    target = process_name.lower()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() == target:
                return int(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def current_meeting_window(
    process_name: str = ZOOM_PROCESS,
    *,
    extra_classes: Iterable[str] = (),
    title_pattern=None,
) -> WindowInfo | None:
    """Snapshot of the Zoom meeting window right now, or ``None``.

    Used by the recording monitor: re-enumerating avoids treating a window
    recreation (screen share, fullscreen toggle) as the meeting ending.
    """
    _require_windows()
    return select_meeting_window(
        iter_windows(process_name),
        extra_classes=extra_classes,
        title_pattern=title_pattern,
    )


def find_meeting_window(
    *,
    process_name: str = ZOOM_PROCESS,
    timeout: float = 300.0,
    poll: float = 1.0,
    extra_classes: Iterable[str] = (),
    title_pattern=None,
) -> WindowInfo:
    """Poll for a Zoom meeting window until *timeout* seconds elapse."""
    _require_windows()
    deadline = time.monotonic() + timeout
    last_dump = -1.0

    while True:
        windows = iter_windows(process_name)
        match = select_meeting_window(
            windows, extra_classes=extra_classes, title_pattern=title_pattern
        )
        if match is not None:
            log.info(
                "found Zoom meeting window hwnd=%#x pid=%s class=%r title=%r",
                match.hwnd,
                match.pid,
                match.cls,
                match.title,
            )
            return match

        now = time.monotonic()
        if windows and now - last_dump >= 5.0:
            last_dump = now
            log.debug(
                "candidate Zoom windows: %s",
                [(w.cls, w.title, w.visible) for w in windows],
            )
        if now >= deadline:
            seen = [(w.cls, w.title, w.visible) for w in windows]
            raise WindowNotFound(
                f"no Zoom meeting window after {timeout:.0f}s (zoom windows seen: {seen})"
            )
        time.sleep(poll)


def maximize(hwnd: int) -> None:
    """Restore, maximise and raise *hwnd* to the foreground (best effort)."""
    _require_windows()
    _user32.ShowWindow(hwnd, SW_RESTORE)
    if not _user32.ShowWindow(hwnd, SW_MAXIMIZE):
        raise OSError(ctypes.get_last_error(), "ShowWindow(SW_MAXIMIZE) failed")
    _force_foreground(hwnd)


def _force_foreground(hwnd: int) -> None:
    """Windows may refuse SetForegroundWindow; attach to the FG thread and retry."""
    if _user32.SetForegroundWindow(hwnd):
        return
    foreground = _user32.GetForegroundWindow()
    foreground_tid = _user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    target_tid = _user32.GetWindowThreadProcessId(hwnd, None)
    if foreground_tid and foreground_tid != target_tid:
        _user32.AttachThreadInput(foreground_tid, target_tid, True)
        try:
            _user32.SetForegroundWindow(hwnd)
            _user32.BringWindowToTop(hwnd)
        finally:
            _user32.AttachThreadInput(foreground_tid, target_tid, False)
    else:  # pragma: no cover - depends on desktop state
        _user32.BringWindowToTop(hwnd)


def select_prompt_window(
    windows: Iterable[WindowInfo], meeting_hwnd: int | None = None
) -> WindowInfo | None:
    """Pick a Zoom dialog that is blocking the join (disclosure/confirmation)."""
    for window in windows:
        if not window.visible or not window.title.strip():
            continue
        if meeting_hwnd is not None and window.hwnd == meeting_hwnd:
            continue
        if window.cls in PROMPT_CLASSES:
            return window
    for window in windows:
        if (
            window.visible
            and window.cls in PREJOIN_CLASSES
            and (meeting_hwnd is None or window.hwnd != meeting_hwnd)
        ):
            return window
    return None


def _click_relative(hwnd: int, fx: float, fy: float) -> None:
    rect = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    x = rect.left + int((rect.right - rect.left) * fx)
    y = rect.top + int((rect.bottom - rect.top) * fy)
    _user32.SetCursorPos(x, y)
    time.sleep(0.15)
    _user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.08)
    _user32.mouse_event(0x0004, 0, 0, 0, 0)


def accept_prompt(meeting_hwnd: int | None = None) -> WindowInfo | None:
    """Dismiss the first blocking Zoom prompt or press Join on the preview."""
    _require_windows()
    prompt = select_prompt_window(iter_windows(), meeting_hwnd)
    if prompt is None:
        return None
    _user32.SetForegroundWindow(prompt.hwnd)
    time.sleep(0.2)
    if prompt.cls in PREJOIN_CLASSES:
        _click_relative(prompt.hwnd, 0.88, 0.92)
    else:
        _user32.keybd_event(0x0D, 0, 0, 0)
        time.sleep(0.05)
        _user32.keybd_event(0x0D, 0, 2, 0)
    return prompt


def close_window(hwnd: int) -> None:
    """Ask a window to close (WM_CLOSE), used by `probe --leave`."""
    _require_windows()
    _user32.PostMessageW(hwnd, 0x0010, 0, 0)


def is_alive(hwnd: int) -> bool:
    """True while the window handle still refers to a live window."""
    _require_windows()
    return bool(_user32.IsWindow(hwnd))


def launch_meeting(
    url: str, mode: str = "zoommtg", passcode: str | None = None
) -> str:
    """Open *url* with the Zoom client (default) or the default browser.

    Returns the URL that was actually handed to the OS.
    """
    if mode == "zoommtg":
        target = to_zoommtg(url, passcode=passcode)
        if target and hasattr(os, "startfile"):
            try:
                os.startfile(target)  # noqa: S606 - shell protocol handler by design
                log.info("launched Zoom via the zoommtg protocol handler")
                return target
            except OSError as exc:
                log.warning("zoommtg launch failed (%s); falling back to the browser", exc)
        elif target is None:
            log.warning("could not convert %r to a zoommtg URL; using the browser", url)

    import webbrowser

    if not webbrowser.open(url):
        raise RuntimeError(f"could not open {url}")
    log.info("opened the meeting link in the default browser")
    return url
