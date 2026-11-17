"""Dialog matcher tests — no Zoom, no OBS, no Windows calls.

Every case is a recorded-style word list; ``pick`` decides which word would be clicked. These are the tests that
keep the tool from ever pressing Enter blindly again.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import dialogs
from ocr import Word


def word(text: str, x: int, y: int, width: int, height: int, line: int) -> Word:
    return Word(text=text, x=x, y=y, width=width, height=height, line=line)


def disclosure() -> list[Word]:
    """Recorded shape of the Zoom recording-consent notice."""
    return [
        word("This", 60, 71, 58, 26, 0),
        word("meeting", 130, 72, 120, 33, 0),
        word("is", 263, 72, 19, 25, 0),
        word("being", 294, 71, 81, 34, 0),
        word("recorded", 388, 71, 132, 26, 0),
        word("Acknowledge", 60, 171, 202, 34, 1),
        word("Cancel", 354, 171, 96, 26, 2),
    ]


class ConsentDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = dialogs.load_rules()

    def test_picks_acknowledge_and_clicks_inside_its_box(self) -> None:
        click = dialogs.pick(disclosure(), self.rules, scope="dialog")
        self.assertIsNotNone(click)
        assert click is not None
        self.assertEqual(click.rule, "consent")
        self.assertEqual(click.label, "Acknowledge")
        self.assertGreaterEqual(click.x, click.box.x)
        self.assertLessEqual(click.x, click.box.x + click.box.width)
        self.assertAlmostEqual(click.score, 1.0)

    def test_cancel_alone_is_never_clicked(self) -> None:
        words = [word("This", 60, 71, 58, 26, 0), word("Cancel", 354, 171, 96, 26, 1)]
        self.assertIsNone(dialogs.pick(words, self.rules, scope="dialog"))

    def test_deny_does_not_hijack_a_mixed_line(self) -> None:
        # OCR merged the row into one line: "Acknowledge   Cancel".
        words = [
            word("Acknowledge", 60, 171, 202, 34, 0),
            word("Cancel", 354, 171, 96, 26, 0),
        ]
        click = dialogs.pick(words, self.rules, scope="dialog")
        self.assertIsNotNone(click)
        assert click is not None
        self.assertEqual(click.label, "Acknowledge")
        self.assertLess(click.box.x, 300)

    def test_ocr_typo_still_matches(self) -> None:
        words = [
            word("Acknow1edge", 60, 171, 202, 34, 0),
            word("Cancel", 354, 171, 96, 26, 1),
        ]
        click = dialogs.pick(words, self.rules, scope="dialog")
        self.assertIsNotNone(click)
        assert click is not None
        self.assertEqual(click.label, "Acknow1edge")
        self.assertGreater(click.score, 0.85)

    def test_unknown_dialog_yields_no_click(self) -> None:
        words = [
            word("Your", 60, 71, 50, 26, 0),
            word("connection", 120, 71, 120, 26, 0),
            word("is", 250, 71, 19, 25, 0),
            word("unstable", 280, 71, 100, 26, 0),
        ]
        self.assertIsNone(dialogs.pick(words, self.rules, scope="dialog"))

    def test_max_fires_stops_repeat_clicks(self) -> None:
        log = dialogs.ClickLog()
        first = dialogs.pick(disclosure(), self.rules, scope="dialog", log=log)
        assert first is not None
        log.record(first)
        log.record(first)
        log.record(first)  # max_fires == 3 for "consent"
        self.assertEqual(log.count("consent"), 3)
        self.assertIsNone(dialogs.pick(disclosure(), self.rules, scope="dialog", log=log))

    def test_signature_changes_when_dialog_changes(self) -> None:
        before = dialogs.signature(disclosure())
        after = dialogs.signature([word("Acknowledge", 60, 171, 202, 34, 0)])
        self.assertNotEqual(before, after)

    def test_button_wins_over_the_word_quoted_in_the_body(self) -> None:
        # Real Zoom "Disclosure" dialog: the paragraph says `By clicking "Agree" and continuing…`, the button is
        # at the bottom. Both match the label exactly, so the click must go to the button.
        words = [
            word("By", 43, 95, 20, 12, 0),
            word("clicking", 70, 95, 60, 12, 0),
            word('"Agree"', 135, 95, 56, 12, 0),
            word("and", 200, 95, 26, 12, 0),
            word("continuing", 230, 95, 80, 12, 0),
            word("with", 315, 95, 30, 12, 0),
            word("this", 350, 95, 30, 12, 0),
            word("Zoom", 385, 95, 40, 12, 0),
            word("call", 430, 95, 30, 12, 0),
            word("Agree", 494, 477, 51, 17, 1),
            word("Cancel", 595, 476, 49, 14, 1),
        ]
        click = dialogs.pick(words, self.rules, scope="dialog")
        self.assertIsNotNone(click)
        assert click is not None
        self.assertEqual(click.label, "Agree")
        self.assertGreater(click.box.y, 400, "clicked the sentence instead of the button")


class ScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = dialogs.load_rules()

    def test_consent_rule_does_not_apply_to_prejoin(self) -> None:
        self.assertIsNone(dialogs.pick(disclosure(), self.rules, scope="prejoin"))

    def test_multiword_label_clicks_inside_its_span(self) -> None:
        words = [
            word("Join", 40, 300, 60, 30, 0),
            word("with", 110, 300, 60, 30, 0),
            word("Computer", 180, 300, 120, 30, 0),
            word("Audio", 310, 300, 80, 30, 0),
        ]
        click = dialogs.pick(words, self.rules, scope="meeting")
        self.assertIsNotNone(click)
        assert click is not None
        self.assertEqual(click.rule, "join-audio")
        self.assertGreaterEqual(click.x, 40)
        self.assertLessEqual(click.x, 390)

    def test_prejoin_prefers_audio_option_when_offered(self) -> None:
        # Prejoin screen: standalone "Join" button (line 0) plus an audio dropdown (line 1).
        words = [
            word("Join", 40, 300, 60, 30, 0),
            word("Join", 40, 380, 60, 30, 1),
            word("with", 110, 380, 60, 30, 1),
            word("Computer", 180, 380, 120, 30, 1),
            word("Audio", 310, 380, 80, 30, 1),
        ]
        click = dialogs.pick(words, self.rules, scope="prejoin")
        self.assertIsNotNone(click)
        assert click is not None
        # join-audio is ordered before prejoin, so the explicit audio choice wins over the fragment "Join".
        self.assertEqual(click.rule, "join-audio")
        self.assertEqual(click.label, "Join with Computer Audio")
        self.assertGreaterEqual(click.y, 380)

    def test_prejoin_clicks_join_when_no_audio_option(self) -> None:
        click = dialogs.pick([word("Join", 40, 300, 60, 30, 0)], self.rules, scope="prejoin")
        self.assertIsNotNone(click)
        assert click is not None
        self.assertEqual(click.rule, "prejoin")
        self.assertEqual(click.label, "Join")

    def test_deny_list_loads_from_rules_file(self) -> None:
        deny = dialogs.load_rules().deny
        self.assertIn("Decline", deny)
        self.assertIn("Switch Account", deny)

    def test_real_email_prompt_clicks_join_meeting(self) -> None:
        # OCR of the live zWaitingMeetingIDWndClass prompt (2026-11-14): note "Join meeting" is rendered
        # greyed-out until the email field has content, and "Cancel" sits right next to it.
        words = [
            word("Zoom", 10, 10, 60, 20, 0),
            word("Workplace", 80, 10, 90, 20, 0),
            word("Enter", 40, 60, 60, 24, 1),
            word("your", 110, 60, 50, 24, 1),
            word("email", 170, 60, 60, 24, 1),
            word("and", 240, 60, 40, 24, 1),
            word("name", 290, 60, 60, 24, 1),
            word("Your", 55, 130, 50, 24, 2),
            word("email", 115, 130, 60, 24, 2),
            word("Myer", 55, 190, 60, 24, 3),
            word("Millien", 125, 190, 80, 24, 3),
            word("Join", 210, 280, 40, 20, 4),
            word("meeting", 258, 280, 80, 20, 4),
            word("Cancel", 340, 280, 70, 24, 5),
        ]
        click = dialogs.pick(words, self.rules, scope="prejoin")
        self.assertIsNotNone(click, "the email prompt must be actionable")
        assert click is not None
        self.assertEqual(click.label, "Join meeting")
        self.assertGreater(click.box.x, 200, "must not drift onto Cancel")

    def test_cancel_never_wins_even_beside_join(self) -> None:
        words = [word("Cancel", 340, 280, 70, 24, 0), word("Join", 210, 280, 40, 20, 0)]
        click = dialogs.pick(words, self.rules, scope="prejoin")
        assert click is not None
        self.assertEqual(click.label, "Join")

    def test_explicit_label_overrides_denylist(self) -> None:
        # "Leave Meeting" is on the denylist, yet the leave rule names it deliberately.
        rules = dialogs.Rules(
            rules=(dialogs.Rule(name="leave", labels=("Leave meeting",), scope="meeting", max_fires=1),),
            deny=("Leave", "End meeting"),
        )
        words = [word("Leave", 40, 60, 60, 24, 0), word("meeting", 110, 60, 90, 24, 0)]
        click = dialogs.pick(words, rules, scope="meeting")
        self.assertIsNotNone(click)
        assert click is not None
        self.assertEqual(click.rule, "leave")

    def test_leave_dialog_class_is_never_auto_clicked(self) -> None:
        self.assertEqual(dialogs.scope_for_class("zLeaveWndClass"), "other")
        self.assertEqual(dialogs.scope_for_class("zWaitingMeetingIDWndClass"), "prejoin")

    def test_leave_and_join_dialog_switches_meetings(self) -> None:
        # OCR of the live zSimpleMessageDialogClass: Zoom is still in another meeting.
        words = [
            word("Join", 40, 40, 40, 20, 0),
            word("new", 90, 40, 40, 20, 0),
            word("meeting?", 140, 40, 90, 20, 0),
            word("Leave", 40, 160, 60, 24, 1),
            word("&", 110, 160, 20, 24, 1),
            word("join", 140, 160, 50, 24, 1),
            word("Cancel", 300, 160, 70, 24, 2),
        ]
        click = dialogs.pick(words, self.rules, scope="prejoin")
        self.assertIsNotNone(click)
        assert click is not None
        self.assertEqual(click.rule, "leave-and-join")
        self.assertEqual(click.label, "Leave & join")
        self.assertEqual(click.score, 1.0)

    def test_leave_and_join_class_is_prejoin(self) -> None:
        self.assertEqual(dialogs.scope_for_class("zSimpleMessageDialogClass"), "prejoin")

    def test_waiting_room_is_a_meeting_state_not_an_unknown_dialog(self) -> None:
        # It used to default to "dialog" scope and raise an alert every few seconds.
        self.assertEqual(dialogs.scope_for_class("zWaitingRoomWndClass"), "meeting")

    def test_informational_windows_are_ignored(self) -> None:
        # "This meeting is being recorded." banner + the drop-shadow overlay: nothing to click, no alert.
        for cls in ("ZDisclaimerBannerWndClass", "ZoomShadowFrameClass", "zLeaveWndClass"):
            self.assertEqual(dialogs.scope_for_class(cls), "other", cls)

    def test_noise_windows_are_never_scanned(self) -> None:
        # 2026-11-16 18:00 produced 462 "Duilib_Anim_StageWindowClassName", 149 "ATL:…" and 16 note-taking alerts,
        # each with a screenshot. None of them can hold a decision.
        for cls in (
            "CVoiceActivityDetectedDlgCls",
            "Duilib_Anim_StageWindowClassName",
            "VideoPreviewFitPanelClass",
            "ATL:00007FFFB743AD70",
        ):
            self.assertEqual(dialogs.scope_for_class(cls), "other", cls)
        # The parent prejoin window still carries the Join button.
        self.assertEqual(dialogs.scope_for_class("VideoPreviewWndClass"), "prejoin")

    def test_recording_notice_is_acknowledged(self) -> None:
        # "This meeting is being recorded." with a single blue OK — the 18:00 run logged it as needs_attention.
        words = [
            word("This", 60, 71, 58, 26, 0),
            word("meeting", 130, 72, 120, 33, 0),
            word("is", 263, 72, 19, 25, 0),
            word("being", 294, 71, 81, 34, 0),
            word("recorded", 388, 71, 132, 26, 0),
            word("0K", 540, 214, 40, 26, 3),
        ]
        click = dialogs.pick(words, self.rules, scope="prejoin")
        self.assertIsNotNone(click)
        assert click is not None
        self.assertEqual(click.rule, "recording-notice")
        self.assertEqual(click.label, "0K", "OCR reads this button as zero-K; the label list carries both")

    def test_the_notice_rule_never_outranks_the_join_button(self) -> None:
        # A prejoin window has "Join", never "OK"; the notice has "OK", never "Join" — each rule stays in its lane.
        words = [word("Join", 540, 214, 60, 26, 3)]
        click = dialogs.pick(words, self.rules, scope="prejoin")
        self.assertIsNotNone(click)
        assert click is not None
        self.assertEqual(click.rule, "prejoin")


class RulesFileTests(unittest.TestCase):
    def test_repo_rules_file_parses(self) -> None:
        rules = dialogs.load_rules(Path(__file__).resolve().parent.parent / "rules.json")
        names = [r.name for r in rules.rules]
        self.assertIn("consent", names)
        self.assertIn("join-audio", names)
        for rule in rules.rules:
            self.assertTrue(rule.labels, f"{rule.name} has no labels")
            self.assertIn(rule.scope, {"any", "dialog", "prejoin", "meeting"})

    def test_missing_file_falls_back_to_defaults(self) -> None:
        rules = dialogs.load_rules("does-not-exist.json")
        self.assertTrue(rules.rules)
        self.assertEqual(
            [rule.name for rule in rules.rules],
            [rule.name for rule in dialogs.DEFAULT_RULES.rules],
        )
        self.assertEqual(rules.deny, dialogs.DEFAULT_RULES.deny)


if __name__ == "__main__":
    unittest.main()
