"""Name the recording and prove it is playable.

The OBS file is written as mkv (crash-safe). After the stop is confirmed this module remuxes it to mp4 without
re-encoding, renames it to ``<date>_<time>_<meeting title>``, and reads the result back with ``ffprobe`` so a
zero-length or audio-less file is reported instead of assumed fine.

Every step is best-effort: a failed remux keeps the mkv, and the caller is told which path survived.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

REMOUNT_SUFFIXES = {".mkv", ".flv", ".ts"}
TARGET_SUFFIX = ".mp4"
SHOTS_DIRNAME = "_shots"
_SHORT_LIMIT = 60


@dataclass(frozen=True)
class Recording:
    """What we ended up with on disk."""

    path: Path
    duration_sec: float
    size_bytes: int
    has_video: bool
    has_audio: bool
    remuxed: bool
    warning: str | None = None

    @property
    def ok(self) -> bool:
        return self.size_bytes > 0 and self.duration_sec > 0 and self.has_video


def slug(text: str, limit: int = _SHORT_LIMIT) -> str:
    """Filesystem-safe fragment of a meeting title."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (cleaned[:limit].rstrip("-")) or "meeting"


def stem_for(when: datetime, title: str) -> str:
    """``2026-11-09_10-00_weekly-sync``."""
    return f"{when.strftime('%Y-%m-%d_%H-%M')}_{slug(title)}"

# A remux with `+faststart` rewrites the whole file, so a fixed 600 s timeout killed the 11.6 GB 16:00 recording
# mid-rewrite and left 11 GB of header-less mp4 beside the good mkv. Allow 8 MB/s of source, never under 10 min.
REMOUNT_MIN_SECONDS = 600
REMOUNT_BYTES_PER_SECOND = 8 * 1024 * 1024
# `+faststart` matters for streaming, not local playback, and it doubles the write volume on huge files.
FASTSTART_LIMIT_BYTES = 4 * 1024**3


def remux_timeout(source: Path) -> int:
    """Seconds to allow ffmpeg for *source*, scaled by its size."""
    try:
        size = Path(source).stat().st_size
    except OSError:
        return REMOUNT_MIN_SECONDS
    return max(REMOUNT_MIN_SECONDS, int(size / REMOUNT_BYTES_PER_SECOND) + 60)

def remux_args(source: Path, target: Path, ffmpeg: str = "ffmpeg") -> list[str]:
    """ffmpeg argv that rewraps without re-encoding."""
    args = [ffmpeg, "-y", "-loglevel", "error", "-i", str(source), "-c", "copy"]
    try:
        big = Path(source).stat().st_size > FASTSTART_LIMIT_BYTES
    except OSError:
        big = False
    if not big:
        args += ["-movflags", "+faststart"]
    return args + [str(target)]


def probe_args(path: Path, ffprobe: str = "ffprobe") -> list[str]:
    return [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]


def parse_probe(payload: str) -> tuple[float, bool, bool]:
    """``(duration_sec, has_video, has_audio)`` from ffprobe JSON."""
    data = json.loads(payload or "{}")
    duration = 0.0
    try:
        duration = float(data.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    streams = data.get("streams", []) or []
    kinds = {str(stream.get("codec_type", "")) for stream in streams}
    if duration <= 0.0:
        for stream in streams:
            try:
                duration = max(duration, float(stream.get("duration") or 0.0))
            except (TypeError, ValueError):
                continue
    return duration, "video" in kinds, "audio" in kinds


@dataclass(frozen=True)
class Verdict:
    """The P4 acceptance questions, answered from one ffprobe payload."""

    duration: float
    video_streams: int
    audio_streams: int
    width: int | None
    height: int | None
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


def assess(payload: str, expected_seconds: float | None = None) -> Verdict:
    """Judge a recording: one video stream (the Zoom window), audio present, duration sane.

    Two video streams mean the scene captured something besides the meeting or was recorded twice; no audio means
    the mix was wrong; a duration far from the wall clock means the recording started late, stopped early, or is
    the wrong file. Pure: the caller supplies ffprobe's JSON.
    """
    data = json.loads(payload or "{}")
    streams = data.get("streams", []) or []
    video = [stream for stream in streams if str(stream.get("codec_type")) == "video"]
    audio = [stream for stream in streams if str(stream.get("codec_type")) == "audio"]
    duration, _has_video, _has_audio = parse_probe(payload)
    width = video[0].get("width") if video else None
    height = video[0].get("height") if video else None

    problems: list[str] = []
    if not video:
        problems.append("no video stream")
    elif len(video) > 1:
        problems.append(f"{len(video)} video streams (expected one: the Zoom window)")
    if not audio:
        problems.append("no audio stream")
    if duration <= 0:
        problems.append("duration is zero")
    if expected_seconds and duration > 0:
        allowed = max(5.0, expected_seconds * 0.1)
        if abs(duration - expected_seconds) > allowed:
            problems.append(
                f"duration {duration:.0f}s is {abs(duration - expected_seconds):.0f}s away from the "
                f"{expected_seconds:.0f}s expected"
            )
    return Verdict(duration, len(video), len(audio), width, height, tuple(problems))


def probe_payload(path: Path) -> str | None:
    """ffprobe's JSON for *path*, or ``None`` when ffprobe is missing or rejects the file."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        log.warning("ffprobe is not on PATH; cannot verify %s", path)
        return None
    try:
        result = _run(probe_args(path, ffprobe))
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ffprobe failed on %s: %s", path, exc)
        return None
    if result.returncode != 0:
        log.warning("ffprobe rejected %s: %s", path, (result.stderr or "").strip()[:200])
        return None
    return result.stdout


def _run(args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def inspect(path: Path) -> tuple[float, bool, bool]:
    """Probe a file; on any failure report zeros rather than raising."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return 0.0, False, False
    try:
        result = _run(probe_args(path, ffprobe))
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ffprobe failed on %s: %s", path, exc)
        return 0.0, False, False
    if result.returncode != 0:
        log.warning("ffprobe rejected %s: %s", path, (result.stderr or "").strip()[:200])
        return 0.0, False, False
    return parse_probe(result.stdout)


# A frame whose brightest pixel is at or below this is black: the recorder's own threshold is the same idea.
BLACK_FRAME_LUMA = 8


def sample_at(duration_sec: float) -> float:
    """Where to sample a finished recording: 10 % in, clamped so short takes still get a real frame."""
    if duration_sec <= 0:
        return 1.0
    return max(1.0, min(30.0, duration_sec * 0.1))


def sample_luma(path: Path, at: float | None = None) -> int | None:
    """Brightest pixel in one frame of *path*, or ``None`` when ffmpeg/Pillow cannot produce one.

    The recorder checks the live OBS source during a session; this checks the **file**, which is what the user will
    actually open. A black file with valid streams is exactly the failure that went unnoticed on 2026-11-15.
    """
    binary = shutil.which("ffmpeg")
    if binary is None:
        return None
    path = Path(path)
    duration = inspect(path)[0]
    moment = sample_at(duration if at is None else at)
    if duration > 0:
        # Seeking to (or past) the end yields no frame: a 1-second test clip sampled at 1.0 s came back empty.
        moment = min(moment, duration / 2)
    args = [binary, "-v", "error", "-ss", f"{moment:.1f}", "-i", str(path),
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"]
    try:
        result = subprocess.run(args, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("could not sample a frame from %s: %s", path, exc)
        return None
    if result.returncode != 0 or not result.stdout:
        log.warning("ffmpeg produced no frame from %s: %s", path, (result.stderr or b"").decode()[:160])
        return None
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(result.stdout)) as image:
            _low, high = image.convert("L").getextrema()
        return int(high)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never fail a recording
        log.debug("could not read the sampled frame: %s", exc)
        return None

def finalize(
    source: Path,
    target_dir: Path,
    when: datetime,
    title: str,
    *,
    keep_original: bool = False,
    ffmpeg: str | None = None,
) -> Recording:
    """Remux (if needed), rename into ``target_dir`` and probe the result."""
    source = Path(source)
    target_dir = Path(target_dir)
    name = stem_for(when, title)

    if not source.exists():
        return Recording(source, 0.0, 0, False, False, False, warning="recording file is missing")

    target_dir.mkdir(parents=True, exist_ok=True)
    final = target_dir / f"{name}{TARGET_SUFFIX}"
    remuxed = False

    if source.suffix.lower() in REMOUNT_SUFFIXES:
        binary = ffmpeg or shutil.which("ffmpeg")
        if binary is None:
            log.warning("ffmpeg not found; keeping %s as-is", source.name)
        else:
            result = None
            try:
                result = _run(remux_args(source, final, binary), timeout=remux_timeout(source))
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("ffmpeg could not run (%s); keeping %s", exc, source.name)
            if result is not None and result.returncode == 0 and final.exists():
                remuxed = True
                log.info("remuxed %s -> %s", source.name, final.name)
                if not keep_original:
                    try:
                        source.unlink()
                    except OSError as exc:
                        log.debug("could not delete %s: %s", source, exc)
            else:
                # A killed or failed ffmpeg leaves a truncated file with no moov atom — 11 GB that looks like a
                # finished mp4 but plays as "invalid data". Remove it and keep the mkv.
                if final.exists():
                    try:
                        final.unlink()
                        log.info("removed the partial %s", final.name)
                    except OSError as exc:
                        log.debug("could not delete %s: %s", final, exc)
                if result is not None:
                    log.warning(
                        "remux failed (%s); keeping %s",
                        (result.stderr or "").strip()[:200] or f"exit {result.returncode}",
                        source.name,
                    )
    if not remuxed:
        if source.suffix.lower() != TARGET_SUFFIX:
            final = target_dir / f"{name}{source.suffix.lower()}"
        if source.resolve() != final.resolve():
            if final.exists():
                stamp = datetime.now().strftime("%H%M%S")
                final = target_dir / f"{name}_{stamp}{source.suffix.lower()}"
            shutil.move(str(source), str(final))

    duration, has_video, has_audio = inspect(final)
    size = final.stat().st_size if final.exists() else 0
    warning = None
    if size == 0:
        warning = "recording file is empty"
    elif not has_video:
        warning = "no video stream"
    elif not has_audio:
        warning = "no audio stream (check the OBS app-audio source)"
    elif duration <= 0:
        warning = "duration could not be read"
    if warning:
        log.warning("%s: %s", final.name, warning)
    return Recording(final, duration, size, has_video, has_audio, remuxed, warning)


def prune(
    directory: Path,
    days: int,
    *,
    now: datetime | None = None,
    suffixes: set[str] | None = None,
) -> list[Path]:
    """Delete recordings older than *days* in *directory*; returns what was removed.

    Only media files are considered — the run log and the dialog screenshots live next to the recordings and must
    survive. A missing directory, an unreadable file or a failed delete is not an error: retention is housekeeping.
    """
    if days <= 0:
        return []
    keep = suffixes if suffixes is not None else {".mp4", ".mkv", ".mov", ".ts", ".flv", ".m4v"}
    cutoff = ((now or datetime.now()) - timedelta(days=days)).timestamp()
    removed: list[Path] = []
    try:
        entries = list(Path(directory).iterdir())
    except OSError:
        return removed
    for entry in entries:
        if entry.suffix.lower() not in keep:
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
        except OSError as exc:
            log.debug("could not prune %s: %s", entry, exc)
            continue
        removed.append(entry)
    if removed:
        log.info("retention: removed %d recording(s) older than %d day(s)", len(removed), days)
    return removed


def prune_shots(directory: Path, days: int, now: datetime | None = None) -> list[Path]:
    """Delete ``_shots/<session>`` folders older than *days*; returns what was removed.

    The dialog watcher writes one PNG per attempt plus one per unrecognised window. With the alert de-duplication the
    count is small (37 MB over three sessions), but it is unbounded, so it follows the same ``--retention-days`` as
    the recordings. ``days <= 0`` keeps everything.
    """
    if days <= 0:
        return []
    root = Path(directory) / SHOTS_DIRNAME
    if not root.is_dir():
        return []
    cutoff = ((now or datetime.now()) - timedelta(days=days)).timestamp()
    removed: list[Path] = []
    try:
        folders = [child for child in root.iterdir() if child.is_dir()]
    except OSError:
        return removed
    for folder in folders:
        try:
            if folder.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(folder)
        except OSError as exc:
            log.debug("could not prune %s: %s", folder, exc)
            continue
        removed.append(folder)
    if removed:
        log.info("retention: removed %d screenshot folder(s) older than %d day(s)", len(removed), days)
    return removed
