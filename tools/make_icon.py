"""Draw the application icon (assets/zoom-recorder.ico).

The mark: a recording dot inside a lens ring on a deep indigo plate, with capture brackets at large sizes. It is drawn
per size rather than scaled from one bitmap, because a ring that reads at 256 px turns into a grey smudge at 16 px —
small sizes get a thicker ring, a bigger dot and no brackets.

    .venv\\Scripts\\python.exe tools\\make_icon.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZES = (16, 24, 32, 48, 64, 128, 256)
TARGET = Path(__file__).resolve().parent.parent / "assets" / "zoom-recorder.ico"

TOP = (30, 27, 75)        # indigo 950
BOTTOM = (109, 40, 217)   # violet 700
RED = (255, 61, 51)       # vivid record red
RED_DEEP = (168, 22, 22)  # shaded rim of the dot
WHITE = (248, 250, 252)


def _plate(size: int) -> Image.Image:
    """Rounded square, vertical gradient, soft highlight from the top-left."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gradient = Image.new("RGBA", (size, size))
    draw = ImageDraw.Draw(gradient)
    for y in range(size):
        t = y / max(1, size - 1)
        draw.line(
            [(0, y), (size, y)],
            fill=(
                round(TOP[0] + (BOTTOM[0] - TOP[0]) * t),
                round(TOP[1] + (BOTTOM[1] - TOP[1]) * t),
                round(TOP[2] + (BOTTOM[2] - TOP[2]) * t),
                255,
            ),
        )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(0, 0), (size - 1, size - 1)], radius=int(size * 0.23), fill=255
    )
    canvas.paste(gradient, (0, 0), mask)

    highlight = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(highlight).ellipse(
        [(-size * 0.25, -size * 0.5), (size * 0.85, size * 0.55)],
        fill=(255, 255, 255, 30),
    )
    highlight = highlight.filter(ImageFilter.GaussianBlur(size * 0.1))
    canvas.alpha_composite(Image.composite(highlight, Image.new("RGBA", canvas.size, (0, 0, 0, 0)), mask))
    return canvas


def _brackets(draw: ImageDraw.ImageDraw, size: int) -> None:
    inset, arm, width = int(size * 0.115), int(size * 0.085), max(1, int(size * 0.022))
    for x, y, dx, dy in (
        (inset, inset, 1, 1),
        (size - inset, inset, -1, 1),
        (inset, size - inset, 1, -1),
        (size - inset, size - inset, -1, -1),
    ):
        draw.line([(x, y), (x + dx * arm, y)], fill=(*WHITE, 150), width=width)
        draw.line([(x, y), (x, y + dy * arm)], fill=(*WHITE, 150), width=width)


def render(size: int) -> Image.Image:
    """One icon frame, drawn at 4x *size* and downsampled so edges stay smooth."""
    work = max(256, size * 4)
    plate = _plate(work)
    overlay = Image.new("RGBA", (work, work), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if size >= 64:
        ring_frac, dot_frac = 0.062, 0.135
    elif size >= 32:
        ring_frac, dot_frac = 0.075, 0.140
    else:
        # A thick ring at 16 px closes the gap to the dot and the mark reads as one red blob.
        ring_frac, dot_frac = 0.080, 0.140

    centre = work / 2
    ring_r = work * 0.295
    ring_w = int(work * ring_frac)

    shadow = Image.new("RGBA", (work, work), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        [centre - ring_r, centre - ring_r, centre + ring_r, centre + ring_r],
        outline=(0, 0, 0, 110),
        width=ring_w + int(work * 0.035),
    )
    overlay.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(work * 0.025)))

    draw.ellipse(
        [centre - ring_r, centre - ring_r, centre + ring_r, centre + ring_r],
        outline=(*WHITE, 255),
        width=ring_w,
    )

    dot_r = work * dot_frac
    if size >= 32:
        halo = Image.new("RGBA", (work, work), (0, 0, 0, 0))
        ImageDraw.Draw(halo).ellipse(
            [centre - dot_r * 1.45, centre - dot_r * 1.45, centre + dot_r * 1.45, centre + dot_r * 1.45],
            fill=(*RED, 150),
        )
        overlay.alpha_composite(halo.filter(ImageFilter.GaussianBlur(work * 0.028)))

    draw.ellipse(
        [centre - dot_r, centre - dot_r, centre + dot_r, centre + dot_r],
        fill=(*RED_DEEP, 255),
    )
    core = dot_r * 0.72
    draw.ellipse(
        [centre - core, centre - core, centre + core, centre + core],
        fill=(*RED, 255),
    )

    if size >= 48:
        _brackets(draw, work)

    plate.alpha_composite(overlay)
    return plate.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    frames = {size: render(size) for size in SIZES}
    frames[256].save(
        TARGET,
        format="ICO",
        sizes=[(size, size) for size in SIZES],
        append_images=[frames[size] for size in SIZES if size != 256],
    )
    frames[256].save(TARGET.with_suffix(".png"))

    strip = Image.new("RGBA", (16 + 24 + 32 + 48 + 64 + 40, 64), (24, 24, 32, 255))
    x = 8
    for size in (16, 24, 32, 48, 64):
        strip.alpha_composite(frames[size], (x, (64 - size) // 2))
        x += size + 8
    strip_path = Path(tempfile.gettempdir()) / "zoom-recorder-sizes.png"
    strip.resize((strip.width * 3, strip.height * 3), Image.Resampling.NEAREST).save(strip_path)
    print(
        f"wrote {TARGET} ({TARGET.stat().st_size} bytes) + preview; size strip: {strip_path}"
    )


if __name__ == "__main__":
    main()
