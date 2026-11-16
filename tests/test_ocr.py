"""OCR coordinate handling: boxes must always come back in the caller's frame."""

from __future__ import annotations

import unittest

import ocr


class ScaleWordsTests(unittest.TestCase):
    def test_boxes_are_mapped_back_to_the_original_frame(self) -> None:
        found = [ocr.Word(text="Join", x=1094, y=948, width=54, height=20, line=5)]
        mapped = ocr._scale_words(found, 2)
        self.assertEqual(
            mapped, [ocr.Word(text="Join", x=547, y=474, width=27, height=10, line=5)]
        )

    def test_no_scaling_is_a_no_op(self) -> None:
        found = [ocr.Word(text="Join", x=547, y=474, width=27, height=10, line=5)]
        self.assertEqual(ocr._scale_words(found, 1), found)

    def test_a_tiny_box_never_collapses_to_zero(self) -> None:
        mapped = ocr._scale_words([ocr.Word(text="Join", x=3, y=3, width=1, height=1)], 2)
        self.assertEqual((mapped[0].width, mapped[0].height), (1, 1))


class UpscaleThresholdTests(unittest.TestCase):
    def test_dialog_sized_frames_are_upscaled(self) -> None:
        # The prejoin window measured 646x524; its "Join" label only appears at 2x.
        self.assertLess(max(646, 524), ocr.UPSCALE_BELOW)
        self.assertEqual(ocr.UPSCALE, 2)

    def test_a_full_screen_frame_is_not_upscaled(self) -> None:
        self.assertGreaterEqual(max(1920, 1080), ocr.UPSCALE_BELOW)


if __name__ == "__main__":
    unittest.main()
