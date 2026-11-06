"""Turning the canvas SVG into PNG images, and preparing reference images.

Two pictures come from the same SVG:
- the clean painting (for the gallery), and
- the agent's view, in one of three grid modes (a per-drawing setting, for
  comparison):
  - frame: the painting inside a numbered ruler frame, with a faint grid over
    it. The image is 60 px bigger than the canvas each way.
  - overlay: the same grid, with small numbers drawn inside the picture along
    its top and left edges, so the image is exactly the canvas size.
  - none: the painting exactly as it is.
  Vision models judge *where* things are far better than they read exact
  coordinates; the grid and numbers bridge the two. Only the agent's view has
  them. They are never part of the painting.

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
HALO_COLOUR = (255, 255, 255, 190)  # behind overlay labels, so they read on any paint

GRID_MODES = ("frame", "overlay", "none")
DEFAULT_GRID = "frame"

# How each mode's view is described to the agent. Filled into the prompts, so
# it must read as the end of a sentence about an image of the canvas.
GRID_NOTES = {
    "frame": ("shown inside a numbered ruler frame, with faint grey grid lines every 64 px. "
              "The ruler is numbered in canvas pixels, so read positions off the ruler, not off "
              "the image's own pixels: the frame adds a margin round the picture. "
              "The ruler and grid are there to help you measure: they are not paint"),
    "overlay": ("shown with faint grey grid lines every 64 px and small numbers along the top "
                "and left edges giving each line's position in canvas pixels. The image is "
                "exactly the size of the canvas, so one pixel of the image is one pixel of the "
                "canvas. The grid lines and numbers are there to help you measure: they are not paint"),
    "none": ("shown exactly as painted, with no grid or ruler. The image is exactly the size of "
             "the canvas, so one pixel of the image is one pixel of the canvas, with (0, 0) at "
             "its top-left corner"),
}

# Reference images. The browser sends one already cropped to the canvas, as a
# small JPEG, so these limits only catch something that didn't come from the app.
REFERENCE_FORMATS = ("PNG", "JPEG", "WEBP")
MAX_REFERENCE_BYTES = 8 * 1024 * 1024
MAX_REFERENCE_PIXELS = 40_000_000


def render_png(svg: str, zoom: float = 1.0) -> bytes:
    """Rasterise an SVG string to PNG bytes."""
    return bytes(resvg_py.svg_to_bytes(svg_string=svg, zoom=zoom))


def agent_view_png(svg: str, width: int, height: int, grid: str = DEFAULT_GRID) -> bytes:
    """The painting as the agent sees it, in the drawing's grid mode."""
    return agent_view(render_png(svg), width, height, grid)


def agent_view(png: bytes, width: int, height: int, grid: str = DEFAULT_GRID) -> bytes:
    """A width x height picture as the agent sees it, in a grid mode (see GRID_MODES)."""
    if grid == "overlay":
        return overlay_png(png, width, height)
    if grid == "none":
        return png
    return framed_png(png, width, height)


def _grid_lines(width: int, height: int, offset: int, size: tuple[int, int]) -> Image.Image:
    """Faint grid lines every GRID_STEP px, on a transparent layer."""
    grid = Image.new("RGBA", size, (0, 0, 0, 0))
    g = ImageDraw.Draw(grid)
    for x in range(GRID_STEP, width, GRID_STEP):
        g.line([(offset + x, offset), (offset + x, offset + height - 1)], fill=GRID_COLOUR, width=1)
    for y in range(GRID_STEP, height, GRID_STEP):
        g.line([(offset, offset + y), (offset + width - 1, offset + y)], fill=GRID_COLOUR, width=1)
    return grid


def overlay_png(png: bytes, width: int, height: int) -> bytes:
    """A width x height picture with the grid and small numbers drawn on it.

    The same size as the canvas, so image pixels and canvas pixels agree. Each
    number sits just right of (or below) its line, along the top and left edges,
    on a light halo so it reads on any paint.
    """
    picture = Image.open(io.BytesIO(png)).convert("RGBA")
    view = Image.alpha_composite(picture, _grid_lines(width, height, 0, picture.size))

    labels = Image.new("RGBA", view.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(labels)
    font = ImageFont.load_default(size=10)
    spots = [(x + 2, 1, str(x)) for x in range(GRID_STEP, width, GRID_STEP)]
    spots += [(2, y + 1, str(y)) for y in range(GRID_STEP, height, GRID_STEP)]
    for x, y, text in spots:
        left, top, right, bottom = d.textbbox((x, y), text, font=font, anchor="lt")
        d.rectangle((left - 1, top - 1, right + 1, bottom + 1), fill=HALO_COLOUR)
        d.text((x, y), text, fill=TICK_COLOUR + (255,), font=font, anchor="lt")
    view = Image.alpha_composite(view, labels)

    out = io.BytesIO()
    view.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def framed_png(png: bytes, width: int, height: int) -> bytes:
    """A width x height picture inside the numbered ruler frame, with a faint grid over it."""
    picture = Image.open(io.BytesIO(png)).convert("RGBA")
    view = Image.new("RGBA", (width + 2 * FRAME, height + 2 * FRAME), FRAME_COLOUR + (255,))
    view.paste(picture, (FRAME, FRAME))

    view = Image.alpha_composite(view, _grid_lines(width, height, FRAME, view.size))

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
