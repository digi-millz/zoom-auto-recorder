import json
import shutil
import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner

import cli
import postprocess


def payload(streams, duration="120.5"):
    return json.dumps(
        {
            "format": {"duration": duration},
            "streams": streams,
        }
    )


VIDEO = {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}
SECOND_VIDEO = {"codec_type": "video", "codec_name": "h264", "width": 320, "height": 240}
AUDIO = {"codec_type": "audio", "codec_name": "aac"}


class AssessTests(unittest.TestCase):
    def test_a_normal_recording_passes(self):
        verdict = postprocess.assess(payload([VIDEO, AUDIO]))
        self.assertTrue(verdict.ok)
        self.assertEqual((verdict.video_streams, verdict.audio_streams), (1, 1))
        self.assertEqual((verdict.width, verdict.height), (1920, 1080))

    def test_a_second_video_stream_fails(self):
        verdict = postprocess.assess(payload([VIDEO, SECOND_VIDEO, AUDIO]))
        self.assertFalse(verdict.ok)
        self.assertIn("2 video streams", verdict.problems[0])

    def test_missing_audio_fails(self):
        verdict = postprocess.assess(payload([VIDEO]))
        self.assertFalse(verdict.ok)
        self.assertIn("no audio stream", verdict.problems)

    def test_a_zero_duration_fails(self):
        verdict = postprocess.assess(payload([VIDEO, AUDIO], duration="0"))
        self.assertFalse(verdict.ok)
        self.assertIn("duration is zero", verdict.problems)

    def test_the_duration_is_compared_with_the_wall_clock(self):
        short = postprocess.assess(payload([VIDEO, AUDIO], duration="60"), expected_seconds=1800)
        self.assertFalse(short.ok)
        self.assertIn("away from the", short.problems[0])
        close = postprocess.assess(payload([VIDEO, AUDIO], duration="1790"), expected_seconds=1800)
        self.assertTrue(close.ok)


class VerifyCommandTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            self.skipTest("ffmpeg/ffprobe not installed")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def make_clip(self, *mapping, seconds="2"):
        import subprocess

        target = self.dir / f"clip-{len(list(self.dir.iterdir()))}.mp4"
        args = [
            "ffmpeg", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", seconds,
            *mapping,
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            str(target),
        ]
        subprocess.run(args, check=True, capture_output=True)
        return target

    def run_verify(self, *args):
        return CliRunner().invoke(cli.cli, ["verify", *args])

    def test_one_window_with_audio_passes(self):
        clip = self.make_clip()
        result = self.run_verify(str(clip))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("PASS", result.output)
        self.assertIn("1 stream(s)", result.output)

    def test_two_video_streams_fail_with_a_reason(self):
        import subprocess

        two = self.dir / "two.mp4"
        subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
                "-f", "lavfi", "-i", "color=red:size=160x120:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=440",
                "-t", "2",
                "-map", "0:v", "-map", "1:v", "-map", "2:a",
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                str(two),
            ],
            check=True,
            capture_output=True,
        )
        result = self.run_verify(str(two))
        self.assertEqual(result.exit_code, cli.EXIT_NO_FILE, result.output)
        self.assertIn("2 video streams", result.output)


if __name__ == "__main__":
    unittest.main()
