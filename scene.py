"""Bootstrap the OBS scene so a recording is the Zoom window and Zoom audio — nothing else.

Measured on OBS 33.0.0 / obs-websocket 5.7.6 against the live Zoom client (see ``docs/spikes.md``):

* ``window_capture`` only finds a window when it is given the full ``"<title>:<class>:<exe>"`` value;
  title-only, class-only and exe-only matching all render black.
* **Windows Graphics Capture (method 3) renders black for Zoom's CEF windows.** DXGI (2) and BitLift (1) both
  render, and stay correct when another window covers Zoom. Method 0 (auto) also works; 2 is pinned so a future
  OBS default cannot silently switch to the method that fails.

Because the value embeds the meeting title (which changes per meeting), :func:`prepare` is called after the meeting
window exists and again whenever the title changes.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from obswebsocket import requests
from window_manager import WindowInfo

log = logging.getLogger(__name__)

SCENE_NAME = "Zoom auto-recorder"
WINDOW_SOURCE = "Zoom meeting"
AUDIO_SOURCE = "Zoom audio"
WINDOW_KIND = "window_capture"
AUDIO_KIND = "wasapi_process_output_capture"
MIC_SOURCE = "Microphone"
MIC_KIND = "wasapi_input_capture"

CAPTURE_METHOD_DXGI = 2

# OBS feeds these global devices into *every* scene's mix, unmuted by default. The desktop device is the whole
# system output — which already contains Zoom — so leaving it on records Zoom twice and the mix sounds like an
# echo / double voice. The per-process "Zoom audio" source is the one we want.
GLOBAL_AUDIO_SOURCES = ("Desktop Audio", "Mic/Aux")
CAPTURE_PRIORITY_TITLE = 0

# Max luminance at or below which a frame is treated as black (same idea as `win_capture.BLACK_MAX_LUMA`).
BLACK_LUMA = 4

Call = Callable[[Any], Any]


class SceneError(RuntimeError):
    """OBS refused a scene/input request."""


def composite(window: WindowInfo, exe: str = "Zoom.exe") -> str:
    """The window value OBS matches on: ``"title:class:exe"``."""
    return f"{window.title}:{window.cls}:{exe}"


def window_settings(window: WindowInfo) -> dict[str, Any]:
    """Capture settings for the Zoom meeting window."""
    return {
        "window": composite(window),
        "priority": CAPTURE_PRIORITY_TITLE,
        "method": CAPTURE_METHOD_DXGI,
        "capture_mode": "window",
        "cursor": False,
    }


def audio_settings(window: WindowInfo) -> dict[str, Any]:
    """Per-process audio: only Zoom's own output, not the whole desktop."""
    return {"window": composite(window)}


def _data(response: Any) -> Mapping[str, Any]:
    """Payload of a request result, raising on a rejected request."""
    status = getattr(response, "status", True)
    data = getattr(response, "datain", None)
    if status is False:
        raise SceneError(f"OBS rejected the request: {data}")
    return data or {}


def _names(items: list[Mapping[str, Any]], key: str) -> set[str]:
    return {str(item.get(key, "")) for item in items}


def prepare(
    call: Call,
    window: WindowInfo,
    *,
    scene: str = SCENE_NAME,
    window_source: str = WINDOW_SOURCE,
    audio_source: str = AUDIO_SOURCE,
    mic: bool = False,
) -> dict[str, str]:
    """Create or re-point the scene, window capture and app-audio capture.

    Idempotent: run it before every recording, and again when the meeting title changes. Returns the source names
    that were ensured.
    """

    scenes = _data(call(requests.GetSceneList())).get("scenes", [])
    if scene not in _names(scenes, "sceneName"):
        log.info("creating OBS scene %r", scene)
        _data(call(requests.CreateScene(sceneName=scene)))

    inputs = _data(call(requests.GetInputList())).get("inputs", [])
    existing = {name: kind for name, kind in ((str(i.get("inputName")), str(i.get("inputKind"))) for i in inputs)}

    wanted = {
        window_source: (WINDOW_KIND, window_settings(window)),
        audio_source: (AUDIO_KIND, audio_settings(window)),
    }
    for name, (kind, settings) in wanted.items():
        if name not in existing:
            log.info("creating OBS input %r (%s) for %r", name, kind, window.title)
            _data(
                call(
                    requests.CreateInput(
                        sceneName=scene,
                        inputName=name,
                        inputKind=kind,
                        inputSettings=settings,
                        sceneItemEnabled=True,
                    )
                )
            )
            continue
        if existing[name] != kind:
            raise SceneError(f"input {name!r} exists as {existing[name]!r}, expected {kind!r}")
        log.debug("re-pointing OBS input %r at %r", name, window.title)
        _data(call(requests.SetInputSettings(inputName=name, inputSettings=settings)))

    items = _data(call(requests.GetSceneItemList(sceneName=scene))).get("sceneItems", [])
    present = _names(items, "sourceName")
    for name in wanted:
        if name in present:
            continue
        log.info("adding %r to scene %r", name, scene)
        _data(call(requests.CreateSceneItem(sceneName=scene, sourceName=name, sceneItemEnabled=True)))

    mic_on = ensure_microphone(call, scene=scene, enabled=mic)
    muted = mute_global_audio(call)
    log.info(
        "OBS scene ready: %s (window=%s, method=%d, muted=%s, mic=%s)",
        scene,
        composite(window),
        CAPTURE_METHOD_DXGI,
        ",".join(muted) or "none",
        "on" if mic_on else "off",
    )
    # The scene existing is not the same as OBS outputting it: whatever scene was live (the user's own, with its
    # own capture) is what gets recorded. Measured on this machine — the same source read luma 0 with another
    # scene live and 255 after the switch, so every recording before this line was of somebody else's scene.
    current = _data(call(requests.GetSceneList())).get("currentProgramSceneName")
    if current != scene:
        _data(call(requests.SetCurrentProgramScene(sceneName=scene)))
        log.info("switched the OBS program scene to %r (was %r)", scene, current)
    return {
        "scene": scene,
        "window_source": window_source,
        "audio_source": audio_source,
        "mic": "on" if mic_on else "off",
        "muted": muted,
    }


def mute_global_audio(call: Call, names: tuple[str, ...] = GLOBAL_AUDIO_SOURCES) -> list[str]:
    """Mute OBS's global audio devices so the meeting is captured exactly once.

    Muting (not removing) keeps the devices configured: the change is visible in OBS's mixer, and it applies to
    this machine's recorder only. A missing device is not an error — OBS lets you delete them.
    """

    muted: list[str] = []
    for name in names:
        try:
            _data(call(requests.SetInputMute(inputName=name, inputMuted=True)))
        except Exception as exc:  # noqa: BLE001 - the device may have been deleted
            log.debug("global audio source %r not muted: %s", name, exc)
            continue
        muted.append(name)
    if muted:
        log.info("muted OBS global audio %s (Zoom is captured by %s)", muted, AUDIO_SOURCE)
    return muted


def ensure_microphone(
    call: Call,
    *,
    scene: str = SCENE_NAME,
    source: str = MIC_SOURCE,
    enabled: bool,
) -> bool:
    """Create the per-entry microphone input and enable/disable it in the scene.

    Recording is remote-audio-only by default, so this source is created only when an entry asks for it; the
    global ``Mic/Aux`` device stays muted, which keeps the mix from carrying the same voice twice. Disabling (not
    deleting) a leftover source is deliberate: switching an entry back to remote-only must silence the mic without
    destroying an OBS input the user may have configured.
    """

    inputs = _data(call(requests.GetInputList())).get("inputs", [])
    existing = {str(item.get("inputName")): str(item.get("inputKind")) for item in inputs}
    if source in existing and existing[source] != MIC_KIND:
        raise SceneError(f"input {source!r} exists as {existing[source]!r}, expected {MIC_KIND!r}")
    if source not in existing:
        if not enabled:
            return False
        log.info("creating OBS microphone input %r", source)
        _data(
            call(
                requests.CreateInput(
                    sceneName=scene,
                    inputName=source,
                    inputKind=MIC_KIND,
                    inputSettings={},
                    sceneItemEnabled=enabled,
                )
            )
        )

    items = _data(call(requests.GetSceneItemList(sceneName=scene))).get("sceneItems", [])
    item = next((entry for entry in items if str(entry.get("sourceName")) == source), None)
    if item is None:
        _data(
            call(
                requests.CreateSceneItem(
                    sceneName=scene, sourceName=source, sceneItemEnabled=enabled
                )
            )
        )
    else:
        _data(
            call(
                requests.SetSceneItemEnabled(
                    sceneName=scene,
                    sceneItemId=item.get("sceneItemId"),
                    sceneItemEnabled=enabled,
                )
            )
        )
    log.info("microphone %s", "on" if enabled else "off")
    return enabled


def source_luma(call: Call, source: str = WINDOW_SOURCE, width: int = 320) -> int | None:
    """Brightest pixel in a frame of *source*, or ``None`` when OBS cannot render one.

    OBS returns a screenshot of the source only — no screen capture, no foreground requirement — so this works
    while the window is minimised or the session is locked, which are exactly the cases that produce a black
    recording. The caller decides what counts as "black"; :data:`BLACK_LUMA` is the threshold the recorder uses.
    """
    import base64
    import io

    from PIL import Image

    try:
        response = call(
            requests.GetSourceScreenshot(
                sourceName=source, imageFormat="png", imageWidth=width, imageHeight=width
            )
        )
        data = _data(response).get("imageData") or ""
        if not data:
            return None
        raw = base64.b64decode(data.split(",", 1)[-1])
        with Image.open(io.BytesIO(raw)) as image:
            _low, high = image.convert("L").getextrema()
        return int(high)
    except Exception as exc:  # noqa: BLE001 - a diagnostic probe must never break the recording
        log.debug("could not read a frame from %r: %s", source, exc)
        return None


def looks_black(luma: int | None, threshold: int = BLACK_LUMA) -> bool:
    """True only when OBS *did* render a frame and it is black. Unknown (``None``) is not a failure."""
    return luma is not None and luma <= threshold
