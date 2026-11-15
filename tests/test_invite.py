"""Invitation parsing tests: links, invite blocks, bare IDs, passcodes."""

from __future__ import annotations

import unittest

import invite


FULL_INVITE = """Join Zoom Meeting
https://us06web.zoom.us/j/81234567890?pwd=abcDEF123

Meeting ID: 812 3456 7890
Passcode: abc123
"""

NO_PWD_INVITE = (
    "Join Zoom Meeting https://us02web.zoom.us/j/89538600487 "
    "Meeting ID: 895 3860 0487 Passcode: 1a2b3c"
)


class ParseTests(unittest.TestCase):
    def test_full_invite_uses_the_link_and_its_pwd(self) -> None:
        parsed = invite.parse(FULL_INVITE)
        self.assertEqual(parsed.meeting_id, "81234567890")
        self.assertEqual(parsed.passcode, "abcDEF123", "the link's ?pwd= wins over the prose")
        self.assertEqual(parsed.host, "us06web.zoom.us")
        self.assertEqual(parsed.url, "https://us06web.zoom.us/j/81234567890?pwd=abcDEF123")

    def test_invite_without_pwd_uses_the_passcode_line(self) -> None:
        parsed = invite.parse(NO_PWD_INVITE)
        self.assertEqual(parsed.meeting_id, "89538600487")
        self.assertEqual(parsed.passcode, "1a2b3c")
        self.assertEqual(parsed.url, "https://us02web.zoom.us/j/89538600487?pwd=1a2b3c")

    def test_spaced_meeting_id_is_reassembled(self) -> None:
        self.assertEqual(invite.parse("Meeting ID: 812 3456 7890").meeting_id, "81234567890")

    def test_bare_meeting_id_is_accepted(self) -> None:
        self.assertEqual(invite.parse("85330823611").meeting_id, "85330823611")

    def test_plain_link_without_passcode(self) -> None:
        parsed = invite.parse("https://zoom.us/j/89712840393")
        self.assertEqual(parsed.meeting_id, "89712840393")
        self.assertIsNone(parsed.passcode)
        self.assertEqual(parsed.zoommtg(), "zoommtg://zoom.us/join?action=join&confno=89712840393")

    def test_nothing_recognisable_yields_nothing(self) -> None:
        parsed = invite.parse("lunch at noon")
        self.assertIsNone(parsed.meeting_id)
        self.assertIsNone(parsed.url)
        self.assertIsNone(parsed.zoommtg())

    def test_empty_input_is_safe(self) -> None:
        self.assertIsNone(invite.parse("").meeting_id)
        self.assertIsNone(invite.parse(None or "").meeting_id)

    def test_non_zoom_link_is_not_claimed(self) -> None:
        self.assertIsNone(invite.parse("https://example.com/meeting/81234567890").meeting_id)

    def test_zoommtg_carries_the_passcode(self) -> None:
        parsed = invite.parse(NO_PWD_INVITE)
        self.assertEqual(
            parsed.zoommtg(),
            "zoommtg://zoom.us/join?action=join&confno=89538600487&pwd=1a2b3c",
        )


class WithPasscodeTests(unittest.TestCase):
    def test_adds_a_passcode(self) -> None:
        self.assertEqual(
            invite.with_passcode("https://us06web.zoom.us/j/8123456789", "zzz"),
            "https://us06web.zoom.us/j/8123456789?pwd=zzz",
        )

    def test_replaces_an_existing_passcode(self) -> None:
        self.assertEqual(
            invite.with_passcode("https://zoom.us/j/8123456789?pwd=old", "new"),
            "https://zoom.us/j/8123456789?pwd=new",
        )

    def test_no_passcode_is_a_no_op(self) -> None:
        self.assertEqual(invite.with_passcode("https://zoom.us/j/1", None), "https://zoom.us/j/1")


if __name__ == "__main__":
    unittest.main()
