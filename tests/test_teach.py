import json
import tempfile
import unittest
from pathlib import Path

import teach


class ClassNameTests(unittest.TestCase):
    def test_a_watcher_screenshot_names_its_window_class(self):
        self.assertEqual(
            teach.class_name("194041_00360d4e_before_zRichMsgBoxClass.png"), "zRichMsgBoxClass"
        )
        self.assertIsNone(teach.class_name("194042_00360d4e_after.png"))
        self.assertIsNone(teach.class_name("notes.txt"))


class CollectShotsTests(unittest.TestCase):
    def test_the_newest_session_wins_and_repeats_collapse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for session, names in {
                "20261114-194039": ["120000_aa_before_zRichMsgBoxClass.png",
                                    "120001_bb_before_zRichMsgBoxClass.png"],
                "20261114-200132": ["130000_cc_before_PrejoinWndClass.png"],
                "not-a-session": ["130000_dd_before_Bogus.png"],
                "20261114-200133": ["readme.txt"],
            }.items():
                (root / session).mkdir()
                for name in names:
                    (root / session / name).write_bytes(b"x")
            shots = teach.collect_shots(root)
        self.assertEqual(
            [(shot.session, shot.cls) for shot in shots],
            [("20261114-200132", "PrejoinWndClass"), ("20261114-194039", "zRichMsgBoxClass")],
        )
        self.assertEqual(shots[0].url, "/api/shots/20261114-200132/130000_cc_before_PrejoinWndClass.png")

    def test_one_class_across_sessions_is_taught_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for session, name in [
                ("20261114-194039", "120000_aa_before_zRichMsgBoxClass.png"),
                ("20261114-200132", "130000_cc_before_zRichMsgBoxClass.png"),
            ]:
                (root / session).mkdir()
                (root / session / name).write_bytes(b"x")
            shots = teach.collect_shots(root)
        self.assertEqual([(shot.session, shot.cls) for shot in shots],
                         [("20261114-200132", "zRichMsgBoxClass")])

    def test_a_missing_folder_is_an_empty_list(self):
        self.assertEqual(teach.collect_shots("/no/such/folder"), [])


class DraftRuleTests(unittest.TestCase):
    deny = ["Cancel", "Leave Meeting"]

    def test_a_taught_rule_clicks_the_label_in_its_scope(self):
        rule = teach.draft_rule(self.deny, "zRichMsgBoxClass", "Acknowledge", "dialog")
        self.assertEqual(rule["match"], {"text": ["Acknowledge"]})
        self.assertEqual(rule["scope"], "dialog")
        self.assertEqual(rule["max_fires"], 1)
        self.assertEqual(rule["name"], "taught-zrichmsgboxclass-acknowledge")

    def test_a_denylisted_label_is_refused_case_insensitively(self):
        with self.assertRaises(ValueError):
            teach.draft_rule(self.deny, "zRichMsgBoxClass", "cancel", "dialog")

    def test_empty_label_and_bad_scope_are_refused(self):
        with self.assertRaises(ValueError):
            teach.draft_rule(self.deny, "zRichMsgBoxClass", "  ", "dialog")
        with self.assertRaises(ValueError):
            teach.draft_rule(self.deny, "zRichMsgBoxClass", "OK", "everywhere")


class AddRuleTests(unittest.TestCase):
    def test_the_rule_is_appended_and_the_file_stays_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps({"version": 1, "deny": ["Cancel"], "rules": []}), encoding="utf-8")
            rule = teach.add_rule(path, cls="zRichMsgBoxClass", label="Acknowledge", scope="dialog")
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["rules"], [rule])
        self.assertFalse((Path(tmp) / "rules.json.tmp").exists())

    def test_an_already_handled_label_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            teach.add_rule(path, cls="A", label="Continue", scope="dialog")
            with self.assertRaises(ValueError):
                teach.add_rule(path, cls="B", label="continue", scope="any")

    def test_a_refused_rule_leaves_the_file_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text('{"version": 1, "deny": ["Leave"], "rules": []}\n', encoding="utf-8")
            before = path.read_text(encoding="utf-8")
            with self.assertRaises(ValueError):
                teach.add_rule(path, cls="A", label="Leave", scope="dialog")
            self.assertEqual(path.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
