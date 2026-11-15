import tempfile
import unittest
from pathlib import Path

import report


class RenderTests(unittest.TestCase):
    def record(self, **overrides):
        data = {
            "ts": "2026-11-14T10:05:12",
            "entry": "a1b2",
            "meeting": "Weekly sync",
            "scheduled": "2026-11-14 10:00",
            "status": "done",
            "exit_code": 0,
            "seconds": 1830,
            "path": "E:/rec/2026-11-14_1000_Weekly sync.mp4",
            "detail": "",
        }
        data.update(overrides)
        return data

    def test_fields_and_the_recording_path_are_in_the_page(self):
        page = report.render(self.record())
        self.assertIn("Weekly sync", page)
        self.assertIn("2026-11-14T10:05:12", page)
        self.assertIn("done", page)
        self.assertIn("1830s", page)
        self.assertIn("E:/rec/2026-11-14_1000_Weekly sync.mp4", page)

    def test_a_missing_recording_is_stated_not_hidden(self):
        self.assertIn("No recording file", report.render(self.record(path="")))

    def test_values_are_html_escaped(self):
        page = report.render(self.record(meeting="<script>alert(1)</script>"))
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_screenshots_are_embedded_as_data_uris(self):
        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "dialog.png"
            shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            page = report.render(self.record(), [shot])
        self.assertIn("data:image/png;base64,", page)
        self.assertIn("dialog.png", page)

    def test_missing_or_oversized_images_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "gone.png"
            page = report.render(self.record(), [missing])
        self.assertIn("No screenshots were taken", page)
        self.assertIsNone(report.data_uri(missing))

    def test_an_oversized_image_is_not_embedded(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.png"
            big.write_bytes(b"x" * (report.MAX_IMAGE_BYTES + 1))
            self.assertIsNone(report.data_uri(big))


if __name__ == "__main__":
    unittest.main()
