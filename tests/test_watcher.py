"""Watcher loop tests — fake windows/frames/words, no desktop.

These cover the behaviour that replaced the old blind Enter: exactly one click at the location of the matched
label, verification by re-reading the window, no click on unknown dialogs, and a single alert when a dialog stays
unhandled.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import dialogs
from ocr import Word
from watcher import DialogWatcher
from window_manager import WindowInfo
from win_capture import Capture
from PIL import Image


FRAME = Image.new("RGB", (800, 600), (20, 20, 20))


def word(text: str, x: int, y: int, line: int = 0) -> Word:
    return Word(text=text, x=x, y=y, width=120, height=30, line=line)


def window(cls: str = "zRichMsgBoxClass", title: str = "Zoom") -> WindowInfo:
    return WindowInfo(hwnd=0xABCD, pid=42, title=title, cls=cls, visible=True, width=800, height=600)


class FakeDesktop:
    """Mutable stand-in for the desktop: a frame per call, a word list per call, a recorded call order."""

    def __init__(self, word_sets: list[list[Word]]) -> None:
        self.word_sets = word_sets
        self.reads = 0
        self.clicks: list[tuple[int, int]] = []
        self.windows = [window()]
        self.order: list[str] = []

    def grab(self, hwnd: int) -> Capture:
        return Capture(image=FRAME, origin=(100, 200), hwnd=hwnd, method="test")

    def read(self, image) -> list[Word]:
        index = min(self.reads, len(self.word_sets) - 1)
        self.reads += 1
        return self.word_sets[index]

    def focus(self, hwnd: int) -> None:
        self.order.append("focus")

    def click(self, x: int, y: int) -> None:
        self.order.append("click")
        self.clicks.append((x, y))

    def alive(self, hwnd: int) -> bool:
        return True


def make_watcher(desktop: FakeDesktop, rules=None, **kwargs) -> DialogWatcher:
    return DialogWatcher(
        rules=rules or dialogs.load_rules(),
        grab=desktop.grab,
        read_words=desktop.read,
        click=desktop.click,
        alive=desktop.alive,
        focus=desktop.focus,
        windows=lambda _process: desktop.windows,
        sleep=lambda _s: None,
        **kwargs,
    )


ACK = [word("This", 60, 40), word("meeting", 150, 40), word("is", 250, 40), word("recorded", 280, 40),
       word("Acknowledge", 60, 300), word("Cancel", 400, 300, 1)]
AFTER = [word("You", 60, 40), word("are", 100, 40), word("muted", 150, 40)]


class FocusOrderTests(unittest.TestCase):
    def test_dialog_is_raised_before_the_click(self) -> None:
        desktop = FakeDesktop([ACK, AFTER])
        make_watcher(desktop).poll_once()
        self.assertEqual(
            desktop.order[:2],
            ["focus", "click"],
            "an absolute click lands on whatever is on top, so the dialog must be raised first",
        )


class ClickTests(unittest.TestCase):
    def test_clicks_the_acknowledge_label_once(self) -> None:
        desktop = FakeDesktop([ACK, AFTER])
        watcher = make_watcher(desktop)
        self.assertEqual(watcher.poll_once(), 1)
        x, y = desktop.clicks[0]
        # the "Acknowledge" word: origin (100, 200) + its own centre (60+60, 300+15)
        self.assertEqual((x, y), (220, 515))
        self.assertEqual(watcher.stats.verified, 1)
        self.assertEqual(watcher.stats.unverified, 0)
        self.assertEqual([c.label for c in watcher.logbook.clicks], ["Acknowledge"])

    def test_unverified_click_is_counted_and_logged(self) -> None:
        desktop = FakeDesktop([ACK, ACK])  # same text after the click: nothing happened
        watcher = make_watcher(desktop)
        watcher.poll_once()
        self.assertEqual(watcher.stats.unverified, 1)
        self.assertEqual(watcher.stats.verified, 0)

    def test_unknown_dialog_never_clicks(self) -> None:
        desktop = FakeDesktop([[word("Your", 60, 40), word("connection", 150, 40), word("is", 300, 40)]])
        watcher = make_watcher(desktop, alert_after=0.0)
        watcher.poll_once()
        watcher.poll_once()
        self.assertEqual(desktop.clicks, [])
        self.assertEqual(watcher.stats.alerts, 1, "alert once, not on every poll")
        self.assertIn("connection", watcher.alerts[0].text)

    def test_an_unchanged_unknown_dialog_alerts_once_not_every_alert_after(self) -> None:
        # 346 screenshots / 18 MB in 20 minutes on the 2026-11-15 prejoin window: it re-alerted every ~7 s.
        desktop = FakeDesktop([[word("Your", 60, 40), word("connection", 150, 40)]] * 6)
        watcher = make_watcher(desktop, alert_after=0.0)
        for _ in range(6):
            watcher.poll_once()
        self.assertEqual(watcher.stats.alerts, 1)

    def test_a_dialog_whose_text_changes_alerts_again(self) -> None:
        desktop = FakeDesktop(
            [
                [word("Your", 60, 40), word("connection", 150, 40)],
                [word("Your", 60, 40), word("connection", 150, 40)],
                [word("Meeting", 60, 40), word("expired", 150, 40)],
                [word("Meeting", 60, 40), word("expired", 150, 40)],
            ]
        )
        watcher = make_watcher(desktop, alert_after=0.0)
        for _ in range(4):
            watcher.poll_once()
        self.assertEqual(watcher.stats.alerts, 2)

    def test_home_window_is_not_a_dialog(self) -> None:
        desktop = FakeDesktop([[word("Home", 60, 40), word("Calendar", 150, 40)]])
        desktop.windows = [window(cls="ZPPTMainFrmWndClassEx", title="Zoom Workplace")]
        watcher = make_watcher(desktop, alert_after=0.0)
        watcher.poll_once()
        watcher.poll_once()
        self.assertEqual(desktop.clicks, [])
        self.assertEqual(watcher.stats.alerts, 0)

    def test_dry_run_never_clicks(self) -> None:
        desktop = FakeDesktop([ACK])
        watcher = make_watcher(desktop, dry_run=True)
        self.assertEqual(watcher.poll_once(), 0)
        self.assertEqual(desktop.clicks, [])
        self.assertEqual(watcher.stats.skipped, 1)

    def test_max_fires_stops_a_looping_dialog(self) -> None:
        desktop = FakeDesktop([ACK])  # text never changes
        watcher = make_watcher(desktop)
        for _ in range(6):
            watcher.poll_once()
        self.assertLessEqual(len(desktop.clicks), 3, "a rule may not click forever")

    def test_invisible_and_tiny_windows_are_skipped(self) -> None:
        desktop = FakeDesktop([ACK])
        desktop.windows = [
            WindowInfo(hwnd=1, pid=1, title="Zoom", cls="zRichMsgBoxClass", visible=False, width=800, height=600),
            WindowInfo(hwnd=2, pid=1, title="Zoom", cls="zRichMsgBoxClass", visible=True, width=10, height=10),
        ]
        watcher = make_watcher(desktop)
        self.assertEqual(watcher.poll_once(), 0)
        self.assertEqual(desktop.clicks, [])

    def test_screenshots_written_when_asked(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            desktop = FakeDesktop([ACK, AFTER])
            watcher = make_watcher(desktop, shots_dir=Path(tmp))
            watcher.poll_once()
            files = sorted(p.name for p in Path(tmp).glob("*.png"))
            self.assertEqual(len(files), 2, files)
            self.assertTrue(any("before" in f for f in files))
            self.assertTrue(any("after" in f for f in files))


if __name__ == "__main__":
    unittest.main()
