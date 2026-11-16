"""OCR a window frame into words with pixel boxes (Windows only).

``Windows.Media.Ocr`` ships with Windows 10/11 — no Tesseract, no model download. The engine needs an
image file or stream, so a frame is written to a temporary PNG and decoded through ``BitmapDecoder``.
One engine instance is cached per language-tag; recognition is ~30-60 ms for a 1080p frame.

Word boxes are **pixels inside the supplied image**. Callers add the window's screen origin before clicking.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass

from PIL import Image

log = logging.getLogger(__name__)

# OCR ignores anything smaller than 40x40 and refuses images above 10000 px per side.
MAX_SIDE = 10_000

_engine = None
_engine_error: str | None = None


@dataclass(frozen=True)
class Word:
    """A recognised word and its box inside the captured frame."""

    text: str
    x: int
    y: int
    width: int
    height: int
    line: int = 0

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


def _get_engine():
    """Cached ``OcrEngine`` for the user's profile language, or ``None`` if unavailable."""
    global _engine, _engine_error
    if _engine is not None or _engine_error is not None:
        return _engine
    try:
        from winrt.windows.media.ocr import OcrEngine

        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            _engine_error = "no OCR language pack installed for this user profile"
            log.warning("OCR unavailable: %s", _engine_error)
            return None
        _engine = engine
        log.debug("OCR engine language=%s", engine.recognizer_language.language_tag)
    except Exception as exc:  # pragma: no cover - depends on Windows/runtime presence
        _engine_error = f"{type(exc).__name__}: {exc}"
        log.warning("OCR unavailable: %s", _engine_error)
    return _engine


def available() -> bool:
    """True when an OCR engine can be created (used by ``inspect``/``status``)."""
    return _get_engine() is not None


def unavailable_reason() -> str | None:
    """Why OCR is unavailable, or ``None`` when it works."""
    _get_engine()
    return _engine_error


async def _recognize_async(path: str) -> list[Word]:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage import FileAccessMode, StorageFile

    engine = _get_engine()
    if engine is None:
        return []

    file = await StorageFile.get_file_from_path_async(path)
    stream = await file.open_async(FileAccessMode.READ)
    try:
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        result = await engine.recognize_async(bitmap)
    finally:
        stream.close()

    words: list[Word] = []
    for line_index, line in enumerate(result.lines):
        for word in line.words:
            rect = word.bounding_rect
            words.append(
                Word(
                    text=word.text,
                    x=int(rect.x),
                    y=int(rect.y),
                    width=int(rect.width),
                    height=int(rect.height),
                    line=line_index,
                )
            )
    return words


# Dialogs are small and Windows OCR skips short labels at 1:1: the prejoin "Join" button (white on blue, ~11 px
# tall) is invisible in a 650x529 capture but recognised at 2x — measured 2026-11-15 on the 21:00 prejoin window,
# where the missing word cost the whole meeting. Boxes are divided back so callers keep working in capture pixels.
UPSCALE_BELOW = 1000
UPSCALE = 2


def _scale_words(found: list[Word], scale: int) -> list[Word]:
    """Map boxes recognised on an upscaled frame back onto the original frame."""
    if scale == 1:
        return found
    return [
        Word(
            text=word.text,
            x=word.x // scale,
            y=word.y // scale,
            width=max(1, word.width // scale),
            height=max(1, word.height // scale),
            line=word.line,
        )
        for word in found
    ]


def words(image: Image.Image) -> list[Word]:
    """Recognise ``image`` and return its words, in the coordinates of ``image``. Never raises: failure is ``[]``."""
    if image.width > MAX_SIDE or image.height > MAX_SIDE:
        log.warning("frame too large for OCR (%sx%s)", image.width, image.height)
        return []
    if image.width < 40 or image.height < 40:
        return []

    scale = UPSCALE if max(image.size) < UPSCALE_BELOW else 1
    frame = image.convert("RGB")
    if scale > 1:
        frame = frame.resize((image.width * scale, image.height * scale), Image.LANCZOS)

    fd, path = tempfile.mkstemp(suffix=".png", prefix="zar-ocr-")
    os.close(fd)
    try:
        frame.save(path, "PNG")
        # pywinrt async objects are awaitable; a private loop is cheap and keeps callers synchronous.
        return _scale_words(asyncio.run(_recognize_async(path)), scale)
    except Exception as exc:  # pragma: no cover - runtime/OS dependent
        log.warning("OCR failed: %s: %s", type(exc).__name__, exc)
        return []
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def text(image: Image.Image) -> str:
    """All recognised text, one line per OCR line (debug/`inspect` helper)."""
    words_ = words(image)
    lines: dict[int, list[Word]] = {}
    for word in words_:
        lines.setdefault(word.line, []).append(word)
    return "\n".join(
        " ".join(w.text for w in sorted(line, key=lambda w: w.x)) for line in lines.values()
    )
