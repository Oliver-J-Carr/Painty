"""Turning the canvas SVG into PNG images.

Two pictures come from the same SVG:
- the clean painting (for the gallery), and
- the agent's view: the painting inside a ruler frame, with a faint grid
  over it. Vision models judge *where* things are far better than they read
  exact coordinates; the grid and numbered ruler bridge the two. Only the
  agent's view has them. They are never part of the painting.
"""

from __future__ import annotations

import base64
import io

import resvg_py
from PIL import Image, ImageDraw, ImageFont

GRID_STEP = 64          # px between grid lines (8 cells across 512 px)
FRAME = 30              # px of ruler margin around the painting
FRAME_COLOUR = (236, 233, 226)
TICK_COLOUR = (90, 90, 90)
GRID_COLOUR = (128, 128, 128, 115)  # mid grey, faint: visible on light and dark paint


def render_png(svg: str, zoom: float = 1.0) -> bytes:
    """Rasterise an SVG string to PNG bytes."""
    return bytes(resvg_py.svg_to_bytes(svg_string=svg, zoom=zoom))


def agent_view_png(svg: str, width: int, height: int) -> bytes:
    """The painting as the agent sees it: ruler frame plus faint grid."""
    painting = Image.open(io.BytesIO(render_png(svg))).convert("RGBA")
    view = Image.new("RGBA", (width + 2 * FRAME, height + 2 * FRAME), FRAME_COLOUR + (255,))
    view.paste(painting, (FRAME, FRAME))

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


def data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
