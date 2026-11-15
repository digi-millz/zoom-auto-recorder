import tempfile
import unittest
from pathlib import Path

import cli


class SchtasksCommandTests(unittest.TestCase):
    def test_create_command_targets_the_runner_at_logon(self):
        with tempfile.TemporaryDirectory() as temp:
            args = cli.build_schtasks_args(
                name="ZoomRecorder",
                schedule_path=Path(temp) / "schedule.json",
                output_dir=Path(temp) / "rec",
                log_file=Path(temp) / "runner.log",
                python_exe="pythonw.exe",
            )

        self.assertEqual(args[:3], ["schtasks", "/Create", "/TN"])
        self.assertIn("ONLOGON", args)
        self.assertEqual(args[args.index("/TN") + 1], "ZoomRecorder")

        command = args[args.index("/TR") + 1]
        self.assertIn(" run ", f" {command} ")
        self.assertIn("--schedule", command)
        self.assertIn("--log-file", command)
        self.assertIn("main.py", command)
        self.assertTrue(command.startswith("pythonw.exe "))

    def test_paths_are_absolute(self):
        args = cli.build_schtasks_args(
            name="ZoomRecorder",
            schedule_path="schedule.json",
            output_dir="recordings",
            python_exe="pythonw.exe",
        )
        command = args[args.index("/TR") + 1]
        self.assertNotIn('"schedule.json"', command)
        self.assertNotIn('"recordings"', command)


class DiskPreflightTests(unittest.TestCase):
    def test_refuses_to_start_below_the_free_space_threshold(self):
        with tempfile.TemporaryDirectory() as temp:
            result = cli._record_session(
                url="https://zoom.us/j/1",
                output_path=Path(temp),
                config=cli.ObsConfig(),
                obs_exe=None,
                launch_mode="zoommtg",
                join_timeout=1.0,
                join_delay=0.0,
                max_duration=0.0,
                close_grace=0.0,
                keep_recording_on_interrupt=False,
                min_free_gb=1_000_000.0,
            )
        self.assertEqual(result.exit_code, cli.EXIT_DISK)


class RecordingNameTests(unittest.TestCase):
    def test_generic_window_title_falls_back_to_the_meeting_id(self) -> None:
        # The waiting room and webinar windows are both titled "Zoom Workplace" / "Zoom Webinar".
        self.assertEqual(
            cli._recording_title("Zoom Workplace", "https://us06web.zoom.us/j/85330823611"),
            "zoom-85330823611",
        )
        self.assertEqual(
            cli._recording_title("Zoom Webinar", "https://zoom.us/j/81234567890?pwd=abc"),
            "zoom-81234567890",
        )

    def test_real_topic_is_kept(self) -> None:
        self.assertEqual(
            cli._recording_title("Live Chart Watching", "https://us06web.zoom.us/j/85330823611"),
            "Live Chart Watching",
        )

    def test_unparseable_link_still_names_the_file(self) -> None:
        self.assertEqual(cli._recording_title("", "https://example.com/whatever"), "meeting")


class WaitingRoomTimeoutTests(unittest.TestCase):
    def test_zero_timeout_waits_forever(self) -> None:
        self.assertFalse(cli.waiting_room_expired(0.0, 99999.0, 0))

    def test_still_within_the_window(self) -> None:
        self.assertFalse(cli.waiting_room_expired(100.0, 150.0, 60.0))

    def test_expires_at_the_limit(self) -> None:
        self.assertTrue(cli.waiting_room_expired(100.0, 160.0, 60.0))

    def test_not_in_the_waiting_room_is_never_expired(self) -> None:
        self.assertFalse(cli.waiting_room_expired(None, 99999.0, 60.0))


class DiskGuardTests(unittest.TestCase):
    def test_unlimited_threshold_is_never_low(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(cli.disk_is_low(Path(tmp), 0))

    def test_impossible_threshold_is_low(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(cli.disk_is_low(Path(tmp), 1_000_000))

    def test_unreadable_path_is_not_treated_as_low(self) -> None:
        # A transient stat failure must not end a recording.
        self.assertFalse(cli.disk_is_low(Path("Z:/no-such-volume"), 1))

    def test_free_gb_reads_a_real_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            available = cli.free_gb(Path(tmp))
        self.assertIsNotNone(available)
        assert available is not None
        self.assertGreater(available, 0)

if __name__ == "__main__":
    unittest.main()
