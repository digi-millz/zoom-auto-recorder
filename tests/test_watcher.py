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
        self.typed: list[str] = []
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

    def clear(self) -> None:
        self.order.append("clear")

    def type_text(self, text: str) -> None:
        self.order.append("type")
        self.typed.append(text)

    def alive(self, hwnd: int) -> bool:
        return True


def make_watcher(desktop: FakeDesktop, rules=None, **kwargs) -> DialogWatcher:
    # Every desktop effect is injected: DialogWatcher's defaults are the real Win32 ones, and a forgotten keyword
    # here sends live keystrokes and clicks to whatever window happens to have focus on the developer's screen.
    return DialogWatcher(
        rules=rules or dialogs.load_rules(),
        grab=desktop.grab,
        read_words=desktop.read,
        click=desktop.click,
        clear=desktop.clear,
        type_text=desktop.type_text,
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

    def test_the_prejoin_topic_is_remembered_for_the_file_name(self) -> None:
        prompt = [word("Your", 60, 40), word("email", 130, 40)]
        desktop = FakeDesktop([prompt, prompt])
        desktop.windows = [window(cls="VideoPreviewWndClass", title="Day 1 of Wealth Workshop")]
        watcher = make_watcher(desktop, email="me@example.com")
        watcher.poll_once()
        self.assertEqual(watcher.prejoin_title, "Day 1 of Wealth Workshop")

    def test_a_generic_title_is_not_used_as_the_topic(self) -> None:
        prompt = [word("Your", 60, 40), word("email", 130, 40)]
        desktop = FakeDesktop([prompt, prompt])
        desktop.windows = [window(cls="VideoPreviewWndClass", title="Zoom Workplace")]
        watcher = make_watcher(desktop, email="me@example.com")
        watcher.poll_once()
        self.assertIsNone(watcher.prejoin_title)


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

    def test_the_email_prompt_is_raised_before_clicking_the_field(self) -> None:
        # 2026-11-16 09:00: the click landed on whatever was on top, the field never focused, the keystrokes went
        # nowhere, Zoom kept Join disabled and the entry ended exit 4.
        prompt = [
            word("Enter", 60, 40), word("your", 120, 40), word("email", 170, 40), word("and", 230, 40),
            word("name", 270, 40), word("Your", 60, 120), word("email", 130, 120),
        ]
        desktop = FakeDesktop([prompt, prompt])
        desktop.windows = [window(cls="zWaitingMeetingIDWndClass", title="Zoom Workplace")]
        watcher = make_watcher(desktop, email="me@example.com")
        watcher.poll_once()
        self.assertEqual(desktop.order[:2], ["focus", "click"])

    def test_email_typing_without_focus_is_not_counted_as_typed(self) -> None:
        prompt = [word("Enter", 60, 40), word("your", 120, 40), word("Your", 60, 120), word("email", 130, 120)]
        desktop = FakeDesktop([prompt, prompt])
        desktop.windows = [window(cls="zWaitingMeetingIDWndClass", title="Zoom Workplace")]
        watcher = make_watcher(desktop, email="me@example.com")
        watcher.poll_once()
        # Nothing came back from the field in this fake, so it must not claim success.
        self.assertEqual(watcher.stats.typed, 0)

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


class PrejoinEmailTests(unittest.TestCase):
    """The field must be cleared, and the address must land on the line that was clicked — exactly once."""

    PROMPT = [word("Your", 60, 120, 1), word("email", 130, 120, 1)]

    def _watcher(self, after: list[Word]):
        desktop = FakeDesktop([self.PROMPT, after])
        desktop.windows = [window(cls="zWaitingMeetingIDWndClass", title="Zoom Workplace")]
        return desktop, make_watcher(desktop, email="me@example.com")

    def test_the_field_is_cleared_before_typing(self) -> None:
        desktop, watcher = self._watcher([word("me@example.com", 60, 120, 1)])
        watcher.poll_once()
        self.assertIn("clear", desktop.order, "an existing value must be erased, never appended to")
        self.assertLess(desktop.order.index("clear"), desktop.order.index("type"))
        self.assertEqual(
            desktop.typed,
            ["me@example.com"],
            "the address is typed once, through the injected keyboard; the real one hits the focused window",
        )

    def test_an_address_in_the_wrong_field_is_not_accepted(self) -> None:
        # Keystrokes went into the name field: the address exists, but not on the clicked line.
        _desktop, watcher = self._watcher([word("me@example.com", 60, 300, 3)])
        watcher.poll_once()
        self.assertEqual(watcher.stats.typed, 0)

    def test_a_doubled_address_is_not_accepted(self) -> None:
        _desktop, watcher = self._watcher([word("me@example.comme@example.com", 60, 120, 1)])
        watcher.poll_once()
        self.assertEqual(watcher.stats.typed, 0)

    def test_one_clean_address_is_accepted(self) -> None:
        _desktop, watcher = self._watcher([word("me@example.com", 60, 120, 1)])
        watcher.poll_once()
        self.assertEqual(watcher.stats.typed, 1)

if __name__ == "__main__":
    unittest.main()
