"""OBS scene bootstrap tests — a fake obs-websocket server, no OBS required."""

from __future__ import annotations

import unittest

import scene
from window_manager import WindowInfo


class FakeResponse:
    def __init__(self, datain: dict | None = None, status: bool = True) -> None:
        self.datain = datain or {}
        self.status = status


class FakeOBS:
    """Just enough of obs-websocket v5 to exercise scene.prepare()."""

    def __init__(self, audio_inputs: tuple[str, ...] = ("Desktop Audio", "Mic/Aux")) -> None:
        self.scenes: list[str] = []
        self.inputs: dict[str, str] = {}
        self.items: dict[str, list[str]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.audio_inputs = set(audio_inputs)
        self.muted: dict[str, bool] = {}
        self.enabled: dict[tuple[str, int], bool] = {}

    def __call__(self, request):
        name = type(request).__name__
        payload = request.data()
        self.calls.append((name, payload))
        if name == "GetSceneList":
            return FakeResponse({"scenes": [{"sceneName": s} for s in self.scenes]})
        if name == "CreateScene":
            self.scenes.append(payload["sceneName"])
            self.items.setdefault(payload["sceneName"], [])
            return FakeResponse()
        if name == "GetInputList":
            return FakeResponse(
                {"inputs": [{"inputName": n, "inputKind": k} for n, k in self.inputs.items()]}
            )
        if name == "CreateInput":
            self.inputs[payload["inputName"]] = payload["inputKind"]
            self.items.setdefault(payload["sceneName"], []).append(payload["inputName"])
            return FakeResponse()
        if name == "SetInputSettings":
            return FakeResponse()
        if name == "GetSceneItemList":
            return FakeResponse(
                {
                    "sceneItems": [
                        {"sourceName": source, "sceneItemId": index}
                        for index, source in enumerate(
                            self.items.get(payload["sceneName"], []), start=1
                        )
                    ]
                }
            )
        if name == "CreateSceneItem":
            self.items.setdefault(payload["sceneName"], []).append(payload["sourceName"])
            return FakeResponse()
        if name == "SetSceneItemEnabled":
            self.enabled[(payload["sceneName"], payload["sceneItemId"])] = bool(
                payload["sceneItemEnabled"]
            )
            return FakeResponse()
        if name == "SetInputMute":
            target = payload["inputName"]
            if target not in self.audio_inputs:
                return FakeResponse({"error": "no such input"}, status=False)
            self.muted[target] = bool(payload["inputMuted"])
            return FakeResponse()
        raise AssertionError(f"unexpected OBS request {name}")

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


WINDOW = WindowInfo(hwnd=0x10D34, pid=1234, title="Zoom Webinar",
                    cls="ConfMultiTabContentWndClass", visible=True, width=1550, height=830)


class CompositeValueTests(unittest.TestCase):
    def test_composite_is_title_class_exe(self) -> None:
        self.assertEqual(
            scene.composite(WINDOW), "Zoom Webinar:ConfMultiTabContentWndClass:Zoom.exe"
        )

    def test_window_settings_pin_dxgi_and_full_match(self) -> None:
        settings = scene.window_settings(WINDOW)
        self.assertEqual(settings["method"], 2, "WGC renders black for Zoom; DXGI is pinned")
        self.assertEqual(settings["priority"], 0, "the full title:class:exe value is required")
        self.assertEqual(settings["capture_mode"], "window")
        self.assertFalse(settings["cursor"])

    def test_audio_settings_target_the_zoom_window(self) -> None:
        self.assertEqual(
            scene.audio_settings(WINDOW),
            {"window": "Zoom Webinar:ConfMultiTabContentWndClass:Zoom.exe"},
        )


class PrepareTests(unittest.TestCase):
    def test_creates_scene_sources_and_items_on_empty_obs(self) -> None:
        obs = FakeOBS()
        result = scene.prepare(obs, WINDOW)
        self.assertEqual(result["scene"], scene.SCENE_NAME)
        self.assertIn(scene.SCENE_NAME, obs.scenes)
        self.assertEqual(obs.inputs[scene.WINDOW_SOURCE], scene.WINDOW_KIND)
        self.assertEqual(obs.inputs[scene.AUDIO_SOURCE], scene.AUDIO_KIND)
        self.assertIn(scene.WINDOW_SOURCE, obs.items[scene.SCENE_NAME])
        self.assertIn(scene.AUDIO_SOURCE, obs.items[scene.SCENE_NAME])

    def test_global_audio_is_muted_so_zoom_is_not_recorded_twice(self) -> None:
        # OBS mixes "Desktop Audio" into every scene; it already contains Zoom, so leaving it on doubles the
        # remote voices (heard as echo / double voice).
        obs = FakeOBS()
        result = scene.prepare(obs, WINDOW)
        self.assertEqual(obs.muted, {"Desktop Audio": True, "Mic/Aux": True})
        self.assertCountEqual(result["muted"], ["Desktop Audio", "Mic/Aux"])

    def test_deleted_global_device_is_not_an_error(self) -> None:
        obs = FakeOBS(audio_inputs=())
        result = scene.prepare(obs, WINDOW)
        self.assertEqual(result["muted"], [])

    def test_repointing_is_idempotent(self) -> None:
        obs = FakeOBS()
        scene.prepare(obs, WINDOW)
        obs.calls.clear()
        scene.prepare(obs, WINDOW)
        names = obs.names()
        self.assertNotIn("CreateScene", names)
        self.assertNotIn("CreateInput", names)
        self.assertNotIn("CreateSceneItem", names)
        self.assertEqual(names.count("SetInputSettings"), 2)

    def test_title_change_updates_the_capture_target(self) -> None:
        obs = FakeOBS()
        scene.prepare(obs, WINDOW)
        obs.calls.clear()
        renamed = WindowInfo(hwnd=WINDOW.hwnd, pid=WINDOW.pid, title="Weekly sync",
                             cls=WINDOW.cls, visible=True, width=1550, height=830)
        scene.prepare(obs, renamed)
        window_updates = [
            payload["inputSettings"]
            for name, payload in obs.calls
            if name == "SetInputSettings" and payload["inputName"] == scene.WINDOW_SOURCE
        ]
        self.assertEqual(window_updates[-1]["window"], "Weekly sync:ConfMultiTabContentWndClass:Zoom.exe")

    def test_wrong_input_kind_is_a_loud_error(self) -> None:
        obs = FakeOBS()
        obs.inputs[scene.WINDOW_SOURCE] = "monitor_capture"
        with self.assertRaises(scene.SceneError):
            scene.prepare(obs, WINDOW)

    def test_rejected_request_raises(self) -> None:
        def call(request):
            return FakeResponse({"error": "no"}, status=False)

        with self.assertRaises(scene.SceneError):
            scene.prepare(call, WINDOW)


class MicrophoneTests(unittest.TestCase):
    def test_a_mic_entry_creates_the_microphone_and_turns_it_on(self) -> None:
        obs = FakeOBS()
        result = scene.prepare(obs, WINDOW, mic=True)
        self.assertEqual(obs.inputs[scene.MIC_SOURCE], scene.MIC_KIND)
        self.assertIn(scene.MIC_SOURCE, obs.items[scene.SCENE_NAME])
        self.assertEqual(result["mic"], "on")

    def test_remote_only_entries_leave_the_mic_alone(self) -> None:
        obs = FakeOBS()
        result = scene.prepare(obs, WINDOW)
        self.assertNotIn(scene.MIC_SOURCE, obs.inputs)
        self.assertEqual(result["mic"], "off")

    def test_turning_the_mic_off_disables_the_item_without_deleting_it(self) -> None:
        obs = FakeOBS()
        scene.prepare(obs, WINDOW, mic=True)
        result = scene.prepare(obs, WINDOW, mic=False)
        mic_id = obs.items[scene.SCENE_NAME].index(scene.MIC_SOURCE) + 1
        self.assertIn(scene.MIC_SOURCE, obs.inputs, "the OBS input must survive")
        self.assertIs(obs.enabled[(scene.SCENE_NAME, mic_id)], False)
        self.assertEqual(result["mic"], "off")

    def test_a_mic_input_with_the_wrong_kind_is_a_loud_error(self) -> None:
        obs = FakeOBS()
        obs.inputs[scene.MIC_SOURCE] = "wasapi_output_capture"
        with self.assertRaises(scene.SceneError):
            scene.prepare(obs, WINDOW, mic=True)


class CaptureProbeTests(unittest.TestCase):
    @staticmethod
    def _response(image) -> FakeResponse:
        import base64
        import io

        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        data = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
        return FakeResponse({"imageData": data})

    def test_black_frame_is_reported_as_black(self) -> None:
        from PIL import Image

        def call(_request):
            return self._response(Image.new("RGB", (32, 32), (0, 0, 0)))

        self.assertEqual(scene.source_luma(call), 0)
        self.assertTrue(scene.looks_black(0))

    def test_picture_is_not_black(self) -> None:
        from PIL import Image

        def call(_request):
            return self._response(Image.new("RGB", (32, 32), (200, 200, 200)))

        luma = scene.source_luma(call)
        self.assertIsNotNone(luma)
        assert luma is not None
        self.assertGreater(luma, scene.BLACK_LUMA)
        self.assertFalse(scene.looks_black(luma))

    def test_missing_image_data_is_unknown_not_black(self) -> None:
        self.assertIsNone(scene.source_luma(lambda _request: FakeResponse({})))
        self.assertFalse(scene.looks_black(None), "unknown must never be treated as a failure")

    def test_a_failing_request_never_raises(self) -> None:
        def call(_request):
            raise RuntimeError("socket gone")

        self.assertIsNone(scene.source_luma(call))


if __name__ == "__main__":
    unittest.main()
