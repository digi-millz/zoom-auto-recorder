"""Grab one window's pixels on Windows (no screen-recording involved).

``PrintWindow(PW_RENDERFULLCONTENT)`` renders the window into an off-screen DC, so the frame is correct even when
the window is covered by another app (but **not** when it is minimised or the session is locked — those give a
black frame, which :func:`is_black` catches).

The returned :class:`Capture` carries the window's screen origin: OCR boxes are relative to the bitmap, so
``origin + box.center`` is where to click.

DPI awareness is raised to PerMonitorV2 before the first grab; on a scaled display the coordinates are physical
pixels only if that happens before any window is created.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass

from PIL import Image

log = logging.getLogger(__name__)

PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
BLACK_MAX_LUMA = 4

_dpi_done = False


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _user32() -> ctypes.WinDLL:
    return ctypes.WinDLL("user32", use_last_error=True)


def _gdi32() -> ctypes.WinDLL:
    return ctypes.WinDLL("gdi32", use_last_error=True)


def _prototype(user32, gdi32) -> None:
    """Set restype/argtypes once — a truncated HWND/HDC silently captures the wrong window."""
    HDC = wintypes.HDC
    HBITMAP = wintypes.HBITMAP
    HWND = wintypes.HWND

    user32.GetWindowDC.argtypes = [HWND]
    user32.GetWindowDC.restype = HDC
    user32.ReleaseDC.argtypes = [HWND, HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.PrintWindow.argtypes = [HWND, HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [HWND]
    user32.IsWindow.restype = wintypes.BOOL

    gdi32.CreateCompatibleDC.argtypes = [HDC]
    gdi32.CreateCompatibleDC.restype = HDC
    gdi32.CreateCompatibleBitmap.argtypes = [HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = HBITMAP
    gdi32.SelectObject.argtypes = [HDC, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        HDC,
        HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int


def ensure_dpi_aware() -> None:
    """Raise to PerMonitorV2 so capture and click coordinates are physical pixels."""
    global _dpi_done
    if _dpi_done:
        return
    _dpi_done = True
    try:  # Windows 10 1703+
        if _user32().SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:  # Windows 8.1 fallback: PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """``(left, top, right, bottom)`` in screen pixels."""
    ensure_dpi_aware()
    rect = wintypes.RECT()
    if not _user32().GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError(ctypes.get_last_error(), "GetWindowRect failed")
    return rect.left, rect.top, rect.right, rect.bottom


@dataclass(frozen=True)
class Capture:
    """A frame plus where it came from, in screen pixels."""

    image: Image.Image
    origin: tuple[int, int]
    hwnd: int
    method: str

    def to_screen(self, x: int, y: int) -> tuple[int, int]:
        """Convert a bitmap coordinate (e.g. an OCR box centre) to screen pixels."""
        return self.origin[0] + x, self.origin[1] + y


def is_black(image: Image.Image, threshold: int = BLACK_MAX_LUMA) -> bool:
    """True when the frame is (near) uniformly black — minimised window, locked session, failed render."""
    grey = image.convert("L")
    _low, high = grey.getextrema()
    return high <= threshold


def _print_window(hwnd: int, width: int, height: int) -> Image.Image | None:
    user32, gdi32 = _user32(), _gdi32()
    _prototype(user32, gdi32)

    window_dc = user32.GetWindowDC(hwnd)
    if not window_dc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height) if mem_dc else None
    if not mem_dc or not bitmap:
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, window_dc)
        return None

    old = gdi32.SelectObject(mem_dc, bitmap)
    try:
        if not user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT):
            log.debug("PrintWindow(PW_RENDERFULLCONTENT) failed for hwnd=%#x", hwnd)
            return None

        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # top-down
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB

        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(info), DIB_RGB_COLORS):
            log.debug("GetDIBits failed for hwnd=%#x", hwnd)
            return None
        return Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1).copy()
    finally:
        if old:
            gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, window_dc)


def _grab_screen(rect: tuple[int, int, int, int]) -> Image.Image | None:
    from PIL import ImageGrab

    try:
        return ImageGrab.grab(bbox=rect, all_screens=True)
    except OSError as exc:  # pragma: no cover - desktop dependent
        log.debug("ImageGrab fallback failed: %s", exc)
        return None


def grab(hwnd: int, *, allow_screen_fallback: bool = True) -> Capture | None:
    """Capture *hwnd* (window rect, including the title bar).

    Returns ``None`` when the handle is dead or the window has no area. A black ``PrintWindow`` frame triggers the
    screen-grab fallback only when ``allow_screen_fallback`` is set — that path needs the window on top, so the
    caller must raise it first if correctness matters.
    """
    ensure_dpi_aware()
    left, top, right, bottom = window_rect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        log.debug("hwnd=%#x has no area (%sx%s)", hwnd, width, height)
        return None

    image = _print_window(hwnd, width, height)
    method = "printwindow"
    if image is None or is_black(image):
        if not allow_screen_fallback:
            if image is None:
                return None
            return Capture(image=image, origin=(left, top), hwnd=hwnd, method="printwindow-black")
        fallback = _grab_screen((left, top, right, bottom))
        if fallback is not None:
            image = fallback
            method = "imagegrab"
    if image is None:
        return None
    if image.size != (width, height):
        image = image.crop((0, 0, width, height))
    return Capture(image=image, origin=(left, top), hwnd=hwnd, method=method)
