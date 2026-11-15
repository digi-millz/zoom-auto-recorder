import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

import cli
from window_manager import WindowInfo


MEETING = WindowInfo(7, 1, "Weekly sync", "ZPContentViewWndClass", True, 1280, 720)
WAITING = WindowInfo(8, 1, "Zoom Meeting", cli.WAITING_ROOM_CLASS, True, 1280, 720)


class FakeController:
    """Counts polls so a test can stop the loop without touching OBS."""

    def __init__(self, recording=True):
        self.recording = recording
        self.calls = 0

    def is_recording(self):
        self.calls += 1
        return self.recording


class MonitorLoopTests(unittest.TestCase):
    def monitor(self, windows, **kwargs):
        """Drive `_monitor` with a scripted window sequence; sleeps are removed."""
        options = dict(
            controller=FakeController(),
            max_duration=0.0,
            close_grace=0.0,
            should_stop=lambda: False,
            output_path=None,
            min_free_gb=0.0,
            extra_classes=(),
            title_pattern=None,
        )
        options.update(kwargs)
        with patch.object(cli, "current_meeting_window", side_effect=windows), patch.object(
            cli, "find_process", return_value=1234
        ), patch.object(cli.time, "sleep", return_value=None):
            return cli._monitor(**options)

    def test_it_stops_after_the_window_is_gone_for_the_grace_period(self):
        code = self.monitor([MEETING, MEETING, None, None])
        self.assertEqual(code, cli.EXIT_OK)

    def test_a_stop_request_ends_the_loop_before_touching_windows(self):
        with patch.object(cli, "current_meeting_window") as lookup:
            code = cli._monitor(
                None, 0.0, 0.0, should_stop=lambda: True, extra_classes=()
            )
        self.assertEqual(code, cli.EXIT_OK)
        lookup.assert_not_called()

    def test_low_disk_stops_the_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cli, "disk_is_low", return_value=True):
                code = self.monitor(
                    [MEETING] * 5,
                    output_path=Path(tmp),
                    min_free_gb=1.0,
                    should_stop=lambda: False,
                )
        self.assertEqual(code, cli.EXIT_DISK)

    def test_a_black_capture_repoints_once_then_alerts(self):
        repoints = []
        alerts = []
        controller = FakeController()
        with patch.object(cli, "BLACK_FIRST_CHECK", 0.0), patch.object(
            cli, "BLACK_RECHECK", 0.0
        ):
            code = self.monitor(
                [MEETING] * 8,
                controller=controller,
                should_stop=lambda: controller.calls >= 4,
                capture_luma=lambda: 0,
                repoint_capture=lambda: repoints.append(1),
                on_black=alerts.append,
            )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(len(repoints), 1, "re-point exactly once, then alarm")
        self.assertGreaterEqual(len(alerts), 2)

    def test_an_expired_waiting_room_stops_the_recording(self):
        with patch.object(cli, "waiting_room_expired", return_value=True):
            code = self.monitor(
                [WAITING] * 6, waiting_room_timeout=60.0, should_stop=lambda: False
            )
        self.assertEqual(code, cli.EXIT_OK)


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


NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class TaskXmlTests(unittest.TestCase):
    def build(self, **overrides):
        options = dict(
            schedule_path="schedule.json",
            output_dir="recordings",
            python_exe="pythonw.exe",
        )
        options.update(overrides)
        return ET.fromstring(cli.build_task_xml(**options))

    def test_it_starts_at_logon_and_restarts_a_crashed_runner(self):
        root = self.build()
        self.assertIsNotNone(root.find(".//t:LogonTrigger", NS))
        restart = root.find(".//t:RestartOnFailure", NS)
        assert restart is not None
        self.assertEqual(restart.find("t:Interval", NS).text, "PT1M")
        self.assertEqual(restart.find("t:Count", NS).text, "3")

    def test_the_task_is_not_killed_after_a_few_days(self):
        root = self.build()
        self.assertEqual(root.find(".//t:ExecutionTimeLimit", NS).text, "PT0S")
        self.assertEqual(
            root.find(".//t:MultipleInstancesPolicy", NS).text, "IgnoreNew"
        )

    def test_the_action_runs_the_resident_runner(self):
        root = self.build()
        action = root.find(".//t:Exec", NS)
        assert action is not None
        self.assertEqual(action.find("t:Command", NS).text, "pythonw.exe")
        arguments = action.find("t:Arguments", NS).text
        self.assertIn("main.py run", arguments)
        self.assertIn("--schedule", arguments)
        self.assertTrue(Path(action.find("t:WorkingDirectory", NS).text).is_dir())

    def test_arguments_with_xml_metacharacters_are_escaped(self):
        root = self.build(python_exe="C:/odd & dir/pythonw.exe")
        self.assertEqual(
            root.find(".//t:Command", NS).text, "C:/odd & dir/pythonw.exe"
        )


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


class PartTitleTests(unittest.TestCase):
    def test_first_part_has_no_suffix(self) -> None:
        self.assertEqual(cli._part_title("Weekly sync", 1), "Weekly sync")

    def test_resumed_parts_are_numbered(self) -> None:
        self.assertEqual(cli._part_title("Weekly sync", 2), "Weekly sync (part 2)")
        self.assertEqual(cli._part_title("zoom-85330823611", 3), "zoom-85330823611 (part 3)")


class StopCommandTests(unittest.TestCase):
    def test_without_a_runner_it_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schedule = Path(tmp) / "schedule.json"
            result = CliRunner().invoke(cli.cli, ["stop", "--schedule", str(schedule), "--wait", "0"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("no runner", result.output)

    def test_it_writes_the_stop_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schedule = Path(tmp) / "schedule.json"
            schedule.write_text('{"version": 1, "settings": {}, "entries": []}', encoding="utf-8")
            # A live pid in the lock makes this process look like the runner.
            Path(str(schedule) + ".lock").write_text(str(os.getpid()), encoding="utf-8")

            result = CliRunner().invoke(cli.cli, ["stop", "--schedule", str(schedule), "--wait", "0"])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(
                Path(str(schedule) + ".stop").exists(),
                "the runner polls a marker file, it is never killed",
            )


class SessionGuardTests(unittest.TestCase):
    def test_a_locked_session_is_refused_before_launching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cli, "desktop_locked", return_value=True), patch.object(
                cli, "launch_meeting"
            ) as launch:
                result = cli._record_session(
                    url="https://zoom.us/j/123456789",
                    output_path=Path(tmp),
                    config=cli.ObsConfig(),
                    obs_exe=None,
                    launch_mode="browser",
                    join_timeout=1.0,
                    join_delay=0.0,
                    max_duration=0.0,
                    close_grace=0.0,
                    keep_recording_on_interrupt=True,
                )
        self.assertEqual(result.exit_code, cli.EXIT_SESSION)
        launch.assert_not_called()


class RunLogCommandTests(unittest.TestCase):
    def test_an_empty_log_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = CliRunner().invoke(
                cli.cli, ["runs", "--schedule", str(Path(tmp) / "schedule.json")]
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("no runs recorded yet", result.output)

    def test_records_print_newest_first_and_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schedule = Path(tmp) / "schedule.json"
            cli.append_run(schedule, {"ts": "t1", "status": "done", "seconds": 60, "meeting": "A"})
            cli.append_run(schedule, {"ts": "t2", "status": "failed", "seconds": 5, "meeting": "B"})
            human = CliRunner().invoke(cli.cli, ["runs", "--schedule", str(schedule)])
            machine = CliRunner().invoke(cli.cli, ["runs", "--schedule", str(schedule), "--json"])
        self.assertLess(human.output.index("t2"), human.output.index("t1"))
        parsed = [json.loads(line) for line in machine.output.splitlines() if line.strip()]
        self.assertEqual([record["ts"] for record in parsed], ["t2", "t1"])

    def test_a_session_outcome_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schedule = Path(tmp) / "schedule.json"
            cli._log_session(
                schedule,
                entry_id="abc",
                meeting="Weekly sync",
                scheduled="2026-11-14 10:00",
                result=cli.SessionResult(cli.EXIT_OK, Path("E:/rec/x.mp4"), False),
                seconds=90,
            )
            record = cli.read_runs(schedule)[0]
        self.assertEqual(Path(record["path"]), Path("E:/rec/x.mp4"))
        self.assertEqual(record["entry"], "abc")
        self.assertEqual(record["meeting"], "Weekly sync")
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["seconds"], 90)

    def test_a_session_with_screenshots_gets_an_html_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schedule = Path(tmp) / "schedule.json"
            shots = Path(tmp) / "shots"
            shots.mkdir()
            (shots / "dialog.png").write_bytes(b"fake png")
            cli._log_session(
                schedule,
                entry_id="abc",
                meeting="Weekly sync",
                scheduled="2026-11-14 10:00",
                result=cli.SessionResult(cli.EXIT_OK, Path("E:/rec/x.mp4"), False, shots),
                seconds=90,
            )
            record = cli.read_runs(schedule)[0]
            report_file = Path(record["report"])
            page = report_file.read_text(encoding="utf-8")
        self.assertEqual(report_file.parent.name, "reports")
        self.assertIn("Weekly sync", page)
        self.assertIn("data:image/png;base64,", page)

if __name__ == "__main__":
    unittest.main()
