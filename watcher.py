"""Session-wide Zoom dialog watcher: capture → read → decide → click → verify.

Replaces the old "press Enter and hope" path. Runs from before the meeting window appears until the recording
stops, because consent/acknowledge dialogs can show up late.

Every click is: located from text (`dialogs.pick`), audited (before/after PNG when a shots dir is set), and
verified (the window must close or its text must change). A dialog nobody has a rule for produces an alert, not a
click.

Windows I/O is injectable (``grab``/``read_words``/``click``/``alive``/``windows``) so the loop can be tested
without a desktop.
"""

from __future__ import annotations

import logging
import threading
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import dialogs
import ocr
import window_manager
import win_capture

log = logging.getLogger(__name__)

ALERT_AFTER = 6.0
VERIFY_DELAY = 0.45
POLL_INTERVAL = 0.7
# Windows worth OCR'd: anything smaller is a tooltip or a zero-size proxy window.
MIN_AREA_SIDE = 40


def _squash(text: str) -> str:
    """Drop whitespace: OCR splits an address into ``myermillen gmail com`` and would fail a substring test."""
    return "".join(text.split())


@dataclass
class Stats:
    scans: int = 0
    clicks: int = 0
    verified: int = 0
    unverified: int = 0
    alerts: int = 0
    typed: int = 0
    skipped: int = 0


@dataclass
class WatcherAlert:
    hwnd: int
    cls: str
    title: str
    text: str
    since: float
    screenshot: str | None = None


@dataclass
class DialogWatcher:
    """Poll Zoom windows, dismiss known dialogs, alert on unknown ones."""

    rules: dialogs.Rules | None = None
    poll: float = POLL_INTERVAL
    alert_after: float = ALERT_AFTER
    verify_delay: float = VERIFY_DELAY
    dry_run: bool = False
    process_name: str = window_manager.ZOOM_PROCESS
    shots_dir: Path | None = None
    on_alert: Callable[[WatcherAlert], None] | None = None
    grab: Callable[[int], win_capture.Capture | None] = win_capture.grab
    read_words: Callable[[object], list[ocr.Word]] = ocr.words
    click: Callable[[int, int], None] = window_manager.click_at
    alive: Callable[[int], bool] = window_manager.is_alive
    focus: Callable[[int], None] = window_manager.force_foreground
    email: str | None = None
    type_text: Callable[[str], None] = window_manager.type_text
    windows: Callable[[str], list[window_manager.WindowInfo]] = window_manager.iter_windows
    sleep: Callable[[float], None] = time.sleep
    stats: Stats = field(default_factory=Stats)
    logbook: dialogs.ClickLog = field(default_factory=dialogs.ClickLog)
    alerts: list[WatcherAlert] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _unmatched: dict[int, tuple[str, float]] = field(default_factory=dict, repr=False)
    # hwnd -> signature already reported. Without it an unchanged unknown dialog re-alerted (and re-screenshotted)
    # every alert_after seconds: 346 PNGs / 18 MB in 20 minutes on the 2026-11-15 prejoin window.
    _alerted: dict[int, str] = field(default_factory=dict, repr=False)
    _typed: dict[int, int] = field(default_factory=dict, repr=False)
    _email_warned: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.rules is None:
            self.rules = dialogs.load_rules()

    # -- one pass -----------------------------------------------------------
    def poll_once(self) -> int:
        """Scan every eligible window once. Returns the number of clicks performed."""
        performed = 0
        try:
            windows = self.windows(self.process_name)
        except OSError as exc:
            log.debug("window enumeration failed: %s", exc)
            return 0

        now = time.monotonic()
        for window in windows:
            if not window.visible or not window.title.strip():
                continue
            if window.width < MIN_AREA_SIDE or window.height < MIN_AREA_SIDE:
                continue
            performed += self._inspect(window, now)
        return performed

    def _inspect(self, window: window_manager.WindowInfo, now: float) -> int:
        scope = dialogs.scope_for_class(window.cls)
        if scope == "other":
            return 0
        capture = self.grab(window.hwnd)
        if capture is None:
            log.debug("hwnd=%#x %r not capturable", window.hwnd, window.cls)
            return 0
        words = self.read_words(capture.image)
        self.stats.scans += 1
        text = " ".join(word.text for word in words)
        signature = dialogs.signature(words) or f"blank {capture.image.size}"

        if scope == "prejoin" and self._fill_prejoin(window, words, capture, text):
            return 1

        click = dialogs.pick(words, self.rules, scope=scope, log=self.logbook)
        if click is None:
            if words and scope in ("dialog", "prejoin"):
                self._maybe_alert(window, signature, words, now, capture)
            else:
                self._unmatched.pop(window.hwnd, None)
            return 0

        if self.dry_run:
            log.info(
                "dry-run: would click %r in %s (%r) at %s",
                click.label,
                window.cls,
                window.title,
                capture.to_screen(click.x, click.y),
            )
            self.stats.skipped += 1
            self._unmatched.pop(window.hwnd, None)
            return 0

        x, y = capture.to_screen(click.x, click.y)
        self._shot(window, "before", capture.image)
        # A synthetic click at absolute coordinates lands on whatever is on top at that point, so the dialog has to
        # be raised first — otherwise the click silently goes to the meeting window underneath (seen live with the
        # "Leave & join" dialog, and caught by the verification step).
        self.focus(window.hwnd)
        self.sleep(0.08)
        log.info(
            "clicking %r in %s (%r) at (%d, %d) rule=%s score=%.2f",
            click.label,
            window.cls,
            window.title,
            x,
            y,
            click.rule,
            click.score,
        )
        self.click(x, y)
        self.logbook.record(click)
        self.stats.clicks += 1
        self._unmatched.pop(window.hwnd, None)

        if self._verify(window.hwnd, signature):
            self.stats.verified += 1
            log.info("click verified: %s changed", window.cls)
        else:
            self.stats.unverified += 1
            log.warning("click NOT verified: %s still shows the same text", window.cls)
        return 1

    def _verify(self, hwnd: int, before: str) -> bool:
        """A click worked when the window closed or its text changed."""
        self.sleep(self.verify_delay)
        if not self.alive(hwnd):
            return True
        capture = self.grab(hwnd)
        if capture is None:
            return True
        after = dialogs.signature(self.read_words(capture.image)) or f"blank {capture.image.size}"
        self._shot_hwnd(hwnd, "after", capture.image)
        return after != before

    # -- prejoin form -------------------------------------------------------
    def _fill_prejoin(
        self,
        window: window_manager.WindowInfo,
        words: Sequence[ocr.Word],
        capture: win_capture.Capture,
        text: str,
    ) -> bool:
        """Type the configured email into Zoom's "Enter your email and name" prompt.

        The field is CEF-rendered, so the only way in is: click the placeholder text to focus it, send Unicode
        keystrokes, then read the window back to prove something landed. At most two attempts per window, because a
        silent failure must not turn into a keystroke loop.
        """
        if not self.email:
            if re.search(r"your\s+e-?mail", text.lower()) and not self._email_warned:
                self._email_warned = True
                log.warning(
                    "Zoom is asking for an email and none is configured — set one on the event, in the "
                    "UI's Zoom email field, or with --email; the prompt will be left alone"
                )
            return False
        if self.dry_run:
            return False
        lowered = text.lower()
        if self.email.lower() in lowered:
            return False
        if not re.search(r"your\s+e-?mail", lowered):
            return False
        if self._typed.get(window.hwnd, 0) >= 2:
            return False

        # The prompt has an instruction line ("Enter your email and name") *and* a placeholder inside the input
        # ("Your email"). Clicking the instruction focuses nothing, so pick the line with the fewest words: the
        # placeholder. Ties go to the later line, which is the field (the heading is above it).
        candidates = [
            line
            for line in dialogs.lines_of(words)
            if re.search(r"your\s+e-?mail", line.text.lower())
        ]
        if not candidates:
            return False
        field_line = min(candidates, key=lambda line: (len(line.words), -line.index))
        target = next((word for word in field_line.words if "mail" in word.text.lower()), None)
        if target is None:
            return False

        x, y = capture.to_screen(*target.center)
        self._shot(window, "before-email", capture.image)
        log.info("typing the prejoin email into %s at (%d, %d)", window.cls, x, y)
        self.click(x, y)
        # CEF needs a moment to move focus into the field; typing 250 ms after the click landed in nothing.
        self.sleep(0.5)
        self.type_text(self.email)
        self._typed[window.hwnd] = self._typed.get(window.hwnd, 0) + 1
        self.sleep(0.6)

        after = self.grab(window.hwnd)
        landed = False
        if after is not None:
            after_text = " ".join(word.text for word in self.read_words(after.image)).lower()
            self._shot_hwnd(window.hwnd, "after-email", after.image)
            # The address itself must be readable in the field. "the text changed somehow" was true when the click
            # merely moved focus, which made a still-empty field look filled and the Join click a no-op.
            landed = _squash(self.email).lower() in _squash(after_text)
        if landed:
            self.stats.typed += 1
            log.info("email entry confirmed in %s; the next poll can press Join", window.cls)
        else:
            log.warning("email entry not confirmed in %s; will retry once", window.cls)
        return True


    # -- alerts -------------------------------------------------------------
    def _maybe_alert(
        self,
        window: window_manager.WindowInfo,
        signature: str,
        words: Sequence[ocr.Word],
        now: float,
        capture: win_capture.Capture,
    ) -> None:
        if self._alerted.get(window.hwnd) == signature:
            return
        previous = self._unmatched.get(window.hwnd)
        if previous is None or previous[0] != signature:
            # New text in this window: it is a different problem than the one already reported.
            self._alerted.pop(window.hwnd, None)
            self._unmatched[window.hwnd] = (signature, now)
            return
        if now - previous[1] < self.alert_after:
            return
        self._unmatched.pop(window.hwnd, None)
        self._alerted[window.hwnd] = signature
        text = " / ".join(
            line.strip()
            for line in (dialogs.signature(words).replace("|", "\n")).splitlines()
            if line.strip()
        )[:400]
        shot = self._shot(window, "unknown", capture.image)
        alert = WatcherAlert(
            hwnd=window.hwnd, cls=window.cls, title=window.title, text=text, since=previous[1],
            screenshot=shot,
        )
        self.alerts.append(alert)
        self.stats.alerts += 1
        log.warning(
            "needs_attention: no rule matches %s %r after %.0fs (text: %s)",
            window.cls,
            window.title,
            now - previous[1],
            text[:120],
        )
        if self.on_alert is not None:
            try:
                self.on_alert(alert)
            except Exception as exc:  # pragma: no cover - callback is caller's code
                log.error("alert callback failed: %s", exc)

    # -- screenshots --------------------------------------------------------
    def _shot(self, window: window_manager.WindowInfo, phase: str, image: object) -> str | None:
        return self._shot_hwnd(window.hwnd, f"{phase}_{window.cls}", image)

    def _shot_hwnd(self, hwnd: int, phase: str, image: object) -> str | None:
        if self.shots_dir is None:
            return None
        try:
            self.shots_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%H%M%S", time.localtime())
            path = self.shots_dir / f"{stamp}_{hwnd:08x}_{phase}.png"
            image.save(path)  # type: ignore[attr-defined]
            return str(path)
        except OSError as exc:
            log.debug("could not save screenshot: %s", exc)
            return None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Start the watcher thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="dialog-watcher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Ask the watcher thread to finish and wait for it."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None

    def run(self) -> None:
        """Poll until :meth:`stop` is called."""
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # pragma: no cover - never kill the recording over a scan
                log.error("dialog scan failed: %s: %s", type(exc).__name__, exc)
            self._stop.wait(self.poll)

    def summary(self) -> dict:
        """Counters for the run report/log line."""
        return {
            "scans": self.stats.scans,
            "clicks": self.stats.clicks,
            "verified": self.stats.verified,
            "unverified": self.stats.unverified,
            "alerts": self.stats.alerts,
            "skipped": self.stats.skipped,
            "clicked_labels": [c.label for c in self.logbook.clicks],
        }
