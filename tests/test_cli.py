import tempfile
import unittest
from pathlib import Path
from datetime import datetime

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


if __name__ == "__main__":
    unittest.main()
