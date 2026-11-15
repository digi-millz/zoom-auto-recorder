import re
import unittest
from window_manager import (
    WindowInfo,
    desktop_locked,
    display_awake,
    keep_display_awake,
    select_meeting_window,
    to_zoommtg,
)

HOME = WindowInfo(1, 10, "Zoom Workplace", "ZPPTMainFrmWndClass", True, 1200, 800)
FRAME = WindowInfo(2, 10, "Weekly sync", "ZPContentViewWndClass", True, 1600, 900)
VIDEO = WindowInfo(3, 10, "Zoom Meeting", "zVideoShowWndClass", True, 640, 360)
SECOND_FRAME = WindowInfo(4, 10, "Weekly sync", "ZPMeetingMainWndClass", True, 1920, 1080)


class ToZoommtgTests(unittest.TestCase):
    def test_https_join_link_keeps_passcode(self):
        self.assertEqual(
            to_zoommtg("https://us02web.zoom.us/j/81234567890?pwd=abcDEF.123"),
            "zoommtg://zoom.us/join?action=join&confno=81234567890&pwd=abcDEF.123",
        )

    def test_schemeless_short_link(self):
        self.assertEqual(
            to_zoommtg("zoom.us/j/987654321"),
            "zoommtg://zoom.us/join?action=join&confno=987654321",
        )

    def test_webinar_link(self):
        self.assertEqual(
            to_zoommtg("https://acme.zoom.us/w/87654321"),
            "zoommtg://zoom.us/join?action=join&confno=87654321",
        )

    def test_protocol_url_passes_through(self):
        url = "zoommtg://zoom.us/join?action=join&confno=1&pwd=x"
        self.assertEqual(to_zoommtg(url), url)

    def test_non_zoom_host_is_rejected(self):
        self.assertIsNone(to_zoommtg("https://example.com/j/81234567890"))

    def test_non_numeric_meeting_id_is_rejected(self):
        self.assertIsNone(to_zoommtg("https://zoom.us/j/not-a-meeting"))

    def test_explicit_passcode_is_forwarded(self):
        self.assertEqual(
            to_zoommtg("https://zoom.us/j/81234567890", passcode="s3cret"),
            "zoommtg://zoom.us/join?action=join&confno=81234567890&pwd=s3cret",
        )

    def test_url_passcode_wins_by_default(self):
        self.assertIn(
            "pwd=fromurl", to_zoommtg("https://zoom.us/j/81234567890?pwd=fromurl")
        )


class SelectMeetingWindowTests(unittest.TestCase):
    def test_meeting_frame_beats_video_popout_and_home(self):
        match = select_meeting_window([HOME, VIDEO, FRAME])
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.hwnd, FRAME.hwnd)

    def test_invisible_and_untitled_windows_are_ignored(self):
        invisible = WindowInfo(5, 10, "Weekly sync", "ZPContentViewWndClass", False, 1600, 900)
        untitled = WindowInfo(6, 10, "   ", "ZPContentViewWndClass", True, 1600, 900)
        self.assertIsNone(select_meeting_window([invisible, untitled]))

    def test_home_window_only_never_matches(self):
        self.assertIsNone(select_meeting_window([HOME]))

    def test_larger_frame_wins_on_equal_score(self):
        match = select_meeting_window([FRAME, SECOND_FRAME])
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.hwnd, SECOND_FRAME.hwnd)


class WindowOverrideTests(unittest.TestCase):
    def test_extra_class_is_treated_as_meeting_window(self):
        custom = WindowInfo(9, 10, "Room 42", "CustomZoomClass", True, 800, 600)
        match = select_meeting_window([custom], extra_classes={"CustomZoomClass"})
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.hwnd, 9)

    def test_title_pattern_matches_unknown_class(self):
        custom = WindowInfo(10, 10, "Room 42", "SomeOtherClass", True, 800, 600)
        match = select_meeting_window([custom], title_pattern=re.compile("room 42", re.I))
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.hwnd, 10)


class SessionStateTests(unittest.TestCase):
    def test_lock_detection_answers_boolean(self):
        # The real answer depends on the machine; the contract is that it never raises.
        self.assertIsInstance(desktop_locked(), bool)

    def test_display_requests_are_granted_and_released(self):
        with display_awake() as granted:
            self.assertTrue(granted)
        self.assertTrue(keep_display_awake(False))

if __name__ == "__main__":
    unittest.main()
