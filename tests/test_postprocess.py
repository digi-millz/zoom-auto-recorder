"""Post-processing tests: naming, ffmpeg argv, ffprobe parsing, and the no-ffmpeg fallback."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import postprocess


class NamingTests(unittest.TestCase):
    def test_slug_is_filesystem_safe(self) -> None:
        self.assertEqual(postprocess.slug("Weekly Sync (Zoom) / Team"), "weekly-sync-zoom-team")

    def test_slug_never_empty(self) -> None:
        self.assertEqual(postprocess.slug("///"), "meeting")
        self.assertEqual(postprocess.slug(""), "meeting")

    def test_slug_is_bounded(self) -> None:
        self.assertLessEqual(len(postprocess.slug("x" * 500)), 60)

    def test_stem_uses_local_date_and_time(self) -> None:
        when = datetime(2026, 11, 9, 10, 5, 30)
        self.assertEqual(postprocess.stem_for(when, "Weekly Sync"), "2026-11-09_10-05_weekly-sync")


class ArgumentTests(unittest.TestCase):
    def test_remux_copies_streams_and_sets_faststart(self) -> None:
        args = postprocess.remux_args(Path("in.mkv"), Path("out.mp4"), "ffmpeg")
        self.assertIn("copy", args)
        self.assertIn("+faststart", args)
        self.assertNotIn("-c:v", args, "remux must not re-encode")

    def test_probe_asks_for_json(self) -> None:
        args = postprocess.probe_args(Path("a.mp4"), "ffprobe")
        self.assertIn("-print_format", args)
        self.assertIn("json", args)

    def test_a_multi_gigabyte_recording_gets_patience_and_no_faststart(self) -> None:
        # 11.6 GB was killed by a fixed 600 s timeout mid-rewrite; `+faststart` doubles the write volume.
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "16-00.mkv"
            with big.open("wb") as handle:
                handle.truncate(5 * 1024**3)
            args = postprocess.remux_args(big, Path(tmp) / "out.mp4", "ffmpeg")
            timeout = postprocess.remux_timeout(big)
        self.assertNotIn("+faststart", args)
        self.assertGreater(timeout, postprocess.REMOUNT_MIN_SECONDS)

    def test_a_normal_recording_keeps_faststart_and_the_default_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            small = Path(tmp) / "short.mkv"
            small.write_bytes(b"x" * 1024)
            args = postprocess.remux_args(small, Path(tmp) / "out.mp4", "ffmpeg")
            timeout = postprocess.remux_timeout(small)
        self.assertIn("+faststart", args)
        self.assertEqual(timeout, postprocess.REMOUNT_MIN_SECONDS)

    def test_a_failed_remux_leaves_no_partial_mp4(self) -> None:
        # A killed ffmpeg writes 11 GB of header-less mp4 that looks finished and plays as "invalid data".
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "rec.mkv"
            source.write_bytes(b"data")

            def fake_run(args, timeout=600):
                if "-print_format" in args:
                    return subprocess.CompletedProcess(args, 1, "", "no probe")
                Path(args[-1]).write_bytes(b"partial")
                return subprocess.CompletedProcess(args, 1, "", "boom")

            with patch.object(postprocess, "_run", side_effect=fake_run), patch.object(
                postprocess.shutil, "which", return_value="ffmpeg"
            ):
                recording = postprocess.finalize(
                    source, root / "out", datetime(2026, 11, 15, 16, 0), "STB"
                )
            leftovers = list((root / "out").glob("*.mp4"))
            self.assertEqual(leftovers, [])
            self.assertEqual(recording.path.suffix, ".mkv")


class ProbeParsingTests(unittest.TestCase):
    def test_reads_duration_and_stream_kinds(self) -> None:
        payload = (
            '{"streams":[{"codec_type":"video"},{"codec_type":"audio"}],'
            '"format":{"duration":"123.4"}}'
        )
        duration, video, audio = postprocess.parse_probe(payload)
        self.assertAlmostEqual(duration, 123.4)
        self.assertTrue(video)
        self.assertTrue(audio)

    def test_falls_back_to_stream_duration(self) -> None:
        payload = '{"streams":[{"codec_type":"video","duration":"42.0"}],"format":{}}'
        duration, video, _audio = postprocess.parse_probe(payload)
        self.assertAlmostEqual(duration, 42.0)
        self.assertTrue(video)

    def test_garbage_is_not_fatal(self) -> None:
        self.assertEqual(postprocess.parse_probe(""), (0.0, False, False))


class FinalizeTests(unittest.TestCase):
    def test_missing_file_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = postprocess.finalize(Path(tmp) / "nope.mkv", Path(tmp), datetime(2026, 1, 1), "x")
            self.assertFalse(result.ok)
            self.assertIn("missing", result.warning or "")

    def test_unusable_remux_keeps_the_original_with_its_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "rec.mkv"
            source.write_bytes(b"not really a video")
            result = postprocess.finalize(
                source, root / "out", datetime(2026, 11, 9, 10, 0), "Weekly Sync",
                ffmpeg="this-binary-does-not-exist",
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.path.suffix, ".mkv", "a failed remux must not lose the file")
            self.assertFalse(result.remuxed)
            self.assertFalse(source.exists(), "the file is moved, not copied")
            self.assertIsNotNone(result.warning)

    def test_mp4_source_is_renamed_without_remux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "already.mp4"
            source.write_bytes(b"x" * 64)
            result = postprocess.finalize(source, root / "out", datetime(2026, 11, 9, 10, 0), "Sync")
            self.assertFalse(result.remuxed)
            self.assertEqual(result.path.suffix, ".mp4")
            self.assertTrue(result.path.exists())


class PruneTests(unittest.TestCase):
    def _age(self, path, days):
        stamp = (datetime.now() - timedelta(days=days)).timestamp()
        os.utime(path, (stamp, stamp))

    def test_removes_only_old_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, fresh, log = root / "old.mp4", root / "fresh.mp4", root / "runner.log"
            for path in (old, fresh, log):
                path.write_bytes(b"x")
            self._age(old, 40)
            self._age(log, 40)

            removed = postprocess.prune(root, 30)

            self.assertEqual([p.name for p in removed], ["old.mp4"])
            self.assertTrue(fresh.exists())
            self.assertTrue(log.exists(), "logs live next to recordings and must survive")

    def test_zero_days_keeps_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "old.mp4"
            old.write_bytes(b"x")
            self._age(old, 4000)
            self.assertEqual(postprocess.prune(root, 0), [])
            self.assertTrue(old.exists())

    def test_missing_directory_is_not_an_error(self) -> None:
        self.assertEqual(postprocess.prune(Path("Z:/definitely-not-here"), 30), [])


if __name__ == "__main__":
    unittest.main()
