"""Turning the canvas SVG into PNG images, and preparing reference images.

Two pictures come from the same SVG:
- the clean painting (for the gallery), and
- the agent's view: the painting inside a ruler frame, with a faint grid
  over it. Vision models judge *where* things are far better than they read
  exact coordinates; the grid and numbered ruler bridge the two. Only the
  agent's view has them. They are never part of the painting.

A reference image (one the person adds to a new painting) is cropped and
scaled to the canvas size, and the agent sees it in the same ruler frame, so
a position in the reference is the same position on the canvas.
"""

from __future__ import annotations

import base64
import binascii
import io

import resvg_py
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

GRID_STEP = 64          # px between grid lines (8 cells across 512 px)
FRAME = 30              # px of ruler margin around the painting
FRAME_COLOUR = (236, 233, 226)
TICK_COLOUR = (90, 90, 90)
GRID_COLOUR = (128, 128, 128, 115)  # mid grey, faint: visible on light and dark paint

# Reference images. The browser sends one already cropped to the canvas, as a
# small JPEG, so these limits only catch something that didn't come from the app.
REFERENCE_FORMATS = ("PNG", "JPEG", "WEBP")
MAX_REFERENCE_BYTES = 8 * 1024 * 1024
MAX_REFERENCE_PIXELS = 40_000_000


def render_png(svg: str, zoom: float = 1.0) -> bytes:
    """Rasterise an SVG string to PNG bytes."""
    return bytes(resvg_py.svg_to_bytes(svg_string=svg, zoom=zoom))


def agent_view_png(svg: str, width: int, height: int) -> bytes:
    """The painting as the agent sees it: ruler frame plus faint grid."""
    return framed_png(render_png(svg), width, height)


def framed_png(png: bytes, width: int, height: int) -> bytes:
    """A width x height picture inside the numbered ruler frame, with a faint grid over it."""
    picture = Image.open(io.BytesIO(png)).convert("RGBA")
    view = Image.new("RGBA", (width + 2 * FRAME, height + 2 * FRAME), FRAME_COLOUR + (255,))
    view.paste(picture, (FRAME, FRAME))

    grid = Image.new("RGBA", view.size, (0, 0, 0, 0))
    g = ImageDraw.Draw(grid)
    for x in range(GRID_STEP, width, GRID_STEP):
        g.line([(FRAME + x, FRAME), (FRAME + x, FRAME + height - 1)], fill=GRID_COLOUR, width=1)
    for y in range(GRID_STEP, height, GRID_STEP):
        g.line([(FRAME, FRAME + y), (FRAME + width - 1, FRAME + y)], fill=GRID_COLOUR, width=1)
    view = Image.alpha_composite(view, grid)

    d = ImageDraw.Draw(view)
    font = ImageFont.load_default(size=11)
    # x: ticks and labels above and below
    for x in range(0, width + 1, GRID_STEP):
        p = FRAME + x
        d.line([(p, FRAME - 5), (p, FRAME - 1)], fill=TICK_COLOUR)
        d.line([(p, FRAME + height), (p, FRAME + height + 4)], fill=TICK_COLOUR)
        d.text((p, FRAME - 7), str(x), fill=TICK_COLOUR, font=font, anchor="mb")
        d.text((p, FRAME + height + 6), str(x), fill=TICK_COLOUR, font=font, anchor="mt")
    # y: ticks and labels left and right
    for y in range(0, height + 1, GRID_STEP):
        p = FRAME + y
        d.line([(FRAME - 5, p), (FRAME - 1, p)], fill=TICK_COLOUR)
        d.line([(FRAME + width, p), (FRAME + width + 4, p)], fill=TICK_COLOUR)
        d.text((FRAME - 7, p), str(y), fill=TICK_COLOUR, font=font, anchor="rm")
        d.text((FRAME + width + 6, p), str(y), fill=TICK_COLOUR, font=font, anchor="lm")

    out = io.BytesIO()
    view.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def reference_png(url: str, width: int, height: int) -> bytes:
    """A reference image from the browser (a data URL) as a clean width x height PNG.

    Anything not already that size is centre-cropped and scaled to fit, and
    transparency is laid on white like the canvas. Re-encoding also drops any
    metadata the file carried. Raises ValueError with a message for the person.
    """
    head, comma, body = (url or "").partition(",")
    if not comma or not head.startswith("data:image/") or ";base64" not in head:
        raise ValueError("The reference isn't an image.")
    if len(body) > MAX_REFERENCE_BYTES * 4 // 3 + 4:
        raise ValueError("The reference image is too big. Try a smaller one.")
    try:
        raw = base64.b64decode(body, validate=True)
        img = Image.open(io.BytesIO(raw))
        if img.format not in REFERENCE_FORMATS:
            raise ValueError("The reference must be a PNG, JPEG or WebP image.")
        if img.width * img.height > MAX_REFERENCE_PIXELS:
            raise ValueError("The reference image is too big. Try a smaller one.")
        img = ImageOps.exif_transpose(img)
        img.load()
    except (binascii.Error, UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("The reference image couldn't be read.") from None

    img = img.convert("RGBA")
    flat = Image.new("RGBA", img.size, (255, 255, 255, 255))
    flat.alpha_composite(img)
    if flat.size != (width, height):
        flat = ImageOps.fit(flat, (width, height), method=Image.Resampling.LANCZOS)
    out = io.BytesIO()
    flat.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
