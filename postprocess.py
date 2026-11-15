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
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

REMOUNT_SUFFIXES = {".mkv", ".flv", ".ts"}
TARGET_SUFFIX = ".mp4"
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


def remux_args(source: Path, target: Path, ffmpeg: str = "ffmpeg") -> list[str]:
    """ffmpeg argv that rewraps without re-encoding."""
    return [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(target),
    ]


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


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=600, check=False)


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
                result = _run(remux_args(source, final, binary))
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
            elif result is not None:
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
