import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from obs_controller import (
    ObsConfig,
    ObsController,
    ObsError,
    find_recent_recording,
    obs_launch_args,
)


class Request:
    name = "Request"

    def __init__(self, **kwargs):
        self.dataout = kwargs


class FakeRequests:
    pass


for _name in (
    "GetVersion",
    "StartRecord",
    "StopRecord",
    "GetRecordStatus",
    "SetRecordDirectory",
    "GetRecordDirectory",
    "GetProfileParameter",
    "SetProfileParameter",
    "GetVideoSettings",
    "SetVideoSettings",
):
    setattr(FakeRequests, _name, type(_name, (Request,), {"name": _name}))


class Response:
    def __init__(self, datain=None, status=True):
        self.datain = datain or {}
        self.status = status

    def __getattr__(self, item):
        if item.startswith("get") and len(item) > 3:
            key = item[3].lower() + item[4:]
            if key in self.__dict__["datain"]:
                return lambda: self.__dict__["datain"][key]
        raise AttributeError(item)


class FakeClient:
    """Minimal stand-in for ``obswebsocket.obsws``."""

    def __init__(self, *, set_directory_ok=True, start_works=True, reject_status=False):
        self.calls = []
        self.handlers = []
        self.recording = False
        self.set_directory_ok = set_directory_ok
        self.start_works = start_works
        self.reject_status = reject_status
        self.directories = []
        self.profile_writes = []
        self.disconnected = False

    def connect(self):
        pass

    def register(self, func, event=None):
        self.handlers.append(func)

    def disconnect(self):
        self.disconnected = True


    def emit(self, state, path=None):
        data = {"outputState": state}
        if path:
            data["outputPath"] = path
        for handler in self.handlers:
            handler(SimpleNamespace(datain=data))

    def call(self, request):
        name = request.name
        self.calls.append(name)

        if name == "GetVersion":
            return Response({"obsVersion": "30.1.2"})
        if name == "GetRecordStatus":
            if self.reject_status:
                return Response({}, status=False)
            return Response({"outputActive": self.recording})
        if name == "StartRecord":
            if self.start_works:
                self.recording = True
            return Response()
        if name == "StopRecord":
            self.recording = False
            return Response()
        if name == "GetRecordDirectory":
            return Response({"recordDirectory": "C:/old"})
        if name == "SetRecordDirectory":
            if not self.set_directory_ok:
                return Response({}, status=False)
            self.directories.append(request.dataout["recordDirectory"])
            return Response()

        if name == "GetProfileParameter":
            return Response({"parameterValue": "C:/old"})
        if name == "SetProfileParameter":
            self.profile_writes.append(request.dataout)
            return Response()
        raise AssertionError(f"unexpected request {name}")


class ObsControllerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name) / "recordings"
        self.client = FakeClient()
        self.controller = ObsController(
            ObsConfig(port=4455),
            client_factory=lambda: self.client,
            requests_module=FakeRequests,
        )

    def connect(self):
        with mock.patch("obs_controller.socket.create_connection"):
            return self.controller.connect()

    def test_connect_reports_version_and_subscribes(self):
        version = self.connect()
        self.assertEqual(version, "30.1.2")
        self.assertEqual(len(self.client.handlers), 1)
        self.assertEqual(self.controller.version, "30.1.2")

    def test_start_record_waits_for_obs_to_confirm(self):
        self.connect()
        self.controller.start_record(timeout=1.0)
        self.assertTrue(self.controller.is_recording())
        self.assertIn("StartRecord", self.client.calls)

    def test_start_record_fails_when_obs_never_confirms(self):
        self.client.start_works = False
        self.connect()
        with self.assertRaises(ObsError):
            self.controller.start_record(timeout=0.2)

    def test_rejected_request_raises(self):
        self.client.reject_status = True
        self.connect()
        with self.assertRaises(ObsError):
            self.controller.is_recording()

    def test_ensure_record_dir_uses_request_when_supported(self):
        self.connect()
        self.assertTrue(self.controller.ensure_record_dir(self.output))
        self.assertEqual(self.client.directories, [str(self.output.resolve())])
        self.assertNotIn("SetProfileParameter", self.client.calls)

        self.controller.restore_record_dir()
        self.assertEqual(self.client.directories, [str(self.output.resolve()), "C:/old"])

    def test_ensure_record_dir_falls_back_to_profile_parameter(self):
        self.client.set_directory_ok = False
        self.connect()
        self.assertTrue(self.controller.ensure_record_dir(self.output))
        self.assertEqual(
            self.client.profile_writes[0]["parameterValue"], str(self.output.resolve())
        )

        self.controller.restore_record_dir()
        self.assertEqual(self.client.profile_writes[1]["parameterValue"], "C:/old")
        self.assertEqual(self.client.profile_writes[1]["parameterName"], "FilePath")

    def test_stop_record_returns_path_from_event(self):
        self.connect()
        self.controller.start_record(timeout=1.0)
        self.client.emit("OBS_WEBSOCKET_OUTPUT_STOPPED", "C:/rec/meeting.mkv")

        path = self.controller.stop_record(timeout=1.0)
        self.assertEqual(path, "C:/rec/meeting.mkv")
        self.assertFalse(self.controller.is_recording())

    def test_close_restores_directory_and_disconnects(self):
        self.connect()
        self.controller.ensure_record_dir(self.output)
        self.controller.close()
        self.assertTrue(self.client.disconnected)
        self.assertEqual(self.client.directories[-1], "C:/old")
        self.assertFalse(self.controller.connected)


class LaunchArgsTests(unittest.TestCase):
    def test_start_recording_default(self):
        args = obs_launch_args(r"C:\Program Files\obs-studio\bin\64bit\obs64.exe")
        self.assertIn("--startrecording", args)
        self.assertIn("--minimize-to-tray", args)

    def test_without_start_recording(self):
        args = obs_launch_args("obs64.exe", start_recording=False)
        self.assertNotIn("--startrecording", args)



class FindRecentRecordingTests(unittest.TestCase):
    def test_picks_newest_non_empty_recording_after_cutoff(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            old = directory / "old.mkv"
            old.write_bytes(b"old")
            stale = datetime.now().timestamp() - 3600
            os.utime(old, (stale, stale))

            (directory / "empty.mkv").write_bytes(b"")
            (directory / "notes.txt").write_text("not a recording", encoding="utf-8")
            newest = directory / "meeting.mkv"
            newest.write_bytes(b"data")

            found = find_recent_recording(
                [directory], since=datetime.now() - timedelta(seconds=60)
            )
            self.assertEqual(found, newest)

    def test_returns_none_without_matching_files(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "old.mkv").write_bytes(b"old")
            stale = datetime.now().timestamp() - 7200
            os.utime(directory / "old.mkv", (stale, stale))
            self.assertIsNone(
                find_recent_recording([directory], since=datetime.now() - timedelta(seconds=60))
            )


class TransportFailureTests(unittest.TestCase):
    def test_a_dead_socket_is_reported_as_obs_error(self) -> None:
        # A force-killed OBS gives ConnectionResetError (WinError 10054); every caller guards ObsError only.
        class DeadClient:
            def call(self, _request):
                raise ConnectionResetError(10054, "An existing connection was forcibly closed")

        controller = ObsController(ObsConfig(), client_factory=lambda: DeadClient())
        controller._client = DeadClient()  # noqa: SLF001 - no socket needed for this contract
        with self.assertRaises(ObsError) as caught:
            controller.call(FakeRequests.GetVersion())
        self.assertIn("lost the OBS connection", str(caught.exception))


class RecordingFormatTests(unittest.TestCase):
    class _Client:
        """Answers the profile-parameter requests the format switch uses."""

        def __init__(self, accepts: bool = True, value: str = "hybrid_mp4") -> None:
            self.accepts = accepts
            self.value = value

        def call(self, request):
            if request.name == "GetProfileParameter":
                return Response({"parameterValue": self.value})
            if request.name == "SetProfileParameter":
                if self.accepts:
                    self.value = request.dataout["parameterValue"]
                return Response()
            raise AssertionError(request.name)

    def _controller(self, client) -> ObsController:
        return ObsController(
            ObsConfig(), client_factory=lambda: client, requests_module=FakeRequests
        )

    def test_switches_an_mp4_profile_to_mkv(self) -> None:
        client = self._Client(value="hybrid_mp4")
        controller = self._controller(client)
        controller._client = client  # noqa: SLF001 - the contract under test needs no socket
        self.assertEqual(controller.ensure_recording_format("mkv"), "mkv")
        self.assertEqual(client.value, "mkv", "the profile is changed, not just reported")

    def test_reports_what_obs_actually_uses_when_it_refuses(self) -> None:
        client = self._Client(accepts=False, value="hybrid_mp4")
        controller = self._controller(client)
        controller._client = client  # noqa: SLF001
        self.assertEqual(controller.ensure_recording_format("mkv"), "hybrid_mp4")

    def test_already_mkv_is_a_no_op(self) -> None:
        client = self._Client(value="mkv")
        controller = self._controller(client)
        controller._client = client  # noqa: SLF001
        self.assertEqual(controller.ensure_recording_format("mkv"), "mkv")


class VideoSizeTests(unittest.TestCase):
    class _Client:
        def __init__(self, width=1280, height=720, accepts=True) -> None:
            self.width, self.height, self.accepts = width, height, accepts

        def call(self, request):
            if request.name == "GetVideoSettings":
                return Response({"baseWidth": self.width, "baseHeight": self.height})
            if request.name == "SetVideoSettings":
                if self.accepts:
                    self.width = request.dataout["baseWidth"]
                    self.height = request.dataout["baseHeight"]
                return Response()
            raise AssertionError(request.name)

    def _controller(self, client) -> ObsController:
        controller = ObsController(
            ObsConfig(), client_factory=lambda: client, requests_module=FakeRequests
        )
        controller._client = client  # noqa: SLF001 - no socket needed for this contract
        return controller

    def test_sets_canvas_and_output_resolution(self) -> None:
        client = self._Client()
        controller = self._controller(client)
        self.assertEqual(controller.set_video_size(1920, 1080), (1920, 1080))
        self.assertEqual((client.width, client.height), (1920, 1080))

    def test_reports_what_obs_kept_when_it_refuses(self) -> None:
        client = self._Client(accepts=False)
        controller = self._controller(client)
        self.assertEqual(controller.set_video_size(1920, 1080), (1280, 720))

    def test_unreadable_settings_are_not_fatal(self) -> None:
        class Broken:
            def call(self, _request):
                raise ConnectionResetError(10054, "gone")

        self.assertIsNone(self._controller(Broken()).video_size())

if __name__ == "__main__":
    unittest.main()
