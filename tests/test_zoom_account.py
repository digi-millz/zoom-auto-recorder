"""Zoom account detection tests: only explicit config counts, never cache scraping."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import zoom_account


def write(base: Path, parts: tuple[str, ...], text: str) -> None:
    path = base.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class DiscoverTests(unittest.TestCase):
    def test_reads_an_explicit_account_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write(base, ("Zoom", "data", "zoomus.conf"), "[Account]\nUserEmail=someone@example.com\n")
            self.assertEqual(zoom_account.discover(base), "someone@example.com")

    def test_internal_jid_is_never_an_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write(
                base,
                ("Zoom", "data", "zoomus.conf"),
                "[Account]\nUserEmail=1234567@xmpp.zoom.us\n",
            )
            self.assertIsNone(zoom_account.discover(base))

    def test_unrelated_keys_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write(
                base,
                ("Zoom", "data", "zoomus.conf"),
                "[General]\nSupportEmail=support@zoom.us\nNickname=someone@example.com\n",
            )
            self.assertIsNone(zoom_account.discover(base))

    def test_missing_files_are_fine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(zoom_account.discover(tmp))

    def test_second_candidate_file_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write(base, ("Zoom", "data", "zoom.us.conf"), "account_email = me@example.org\n")
            self.assertEqual(zoom_account.discover(base), "me@example.org")


if __name__ == "__main__":
    unittest.main()
