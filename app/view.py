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

GRID_STEP = 64          # px between grid lines (8 x 8 cells on a 512 canvas)
FRAME = 30              # px of ruler margin around the painting
FRAME_COLOUR = (236, 233, 226)
TICK_COLOUR = (90, 90, 90)
GRID_COLOUR = (128, 128, 128, 115)  # mid grey, faint: visible on light and dark paint


def render_png(svg: str, zoom: float = 1.0) -> bytes:
    """Rasterise an SVG string to PNG bytes."""
    return bytes(resvg_py.svg_to_bytes(svg_string=svg, zoom=zoom))


def agent_view_png(svg: str, size: int) -> bytes:
    """The painting as the agent sees it: ruler frame plus faint grid."""
    painting = Image.open(io.BytesIO(render_png(svg))).convert("RGBA")
    full = size + 2 * FRAME
    view = Image.new("RGBA", (full, full), FRAME_COLOUR + (255,))
    view.paste(painting, (FRAME, FRAME))

    grid = Image.new("RGBA", view.size, (0, 0, 0, 0))
    g = ImageDraw.Draw(grid)
    for v in range(GRID_STEP, size, GRID_STEP):
        g.line([(FRAME + v, FRAME), (FRAME + v, FRAME + size - 1)], fill=GRID_COLOUR, width=1)
        g.line([(FRAME, FRAME + v), (FRAME + size - 1, FRAME + v)], fill=GRID_COLOUR, width=1)
    view = Image.alpha_composite(view, grid)

    d = ImageDraw.Draw(view)
    font = ImageFont.load_default(size=11)
    for v in range(0, size + 1, GRID_STEP):
        p = FRAME + v
        label = str(v)
        # ticks on all four sides
        d.line([(p, FRAME - 5), (p, FRAME - 1)], fill=TICK_COLOUR)
        d.line([(p, FRAME + size), (p, FRAME + size + 4)], fill=TICK_COLOUR)
        d.line([(FRAME - 5, p), (FRAME - 1, p)], fill=TICK_COLOUR)
        d.line([(FRAME + size, p), (FRAME + size + 4, p)], fill=TICK_COLOUR)
        # x labels above and below, y labels left and right
        d.text((p, FRAME - 7), label, fill=TICK_COLOUR, font=font, anchor="mb")
        d.text((p, FRAME + size + 6), label, fill=TICK_COLOUR, font=font, anchor="mt")
        d.text((FRAME - 7, p), label, fill=TICK_COLOUR, font=font, anchor="rm")
        d.text((FRAME + size + 6, p), label, fill=TICK_COLOUR, font=font, anchor="lm")

    out = io.BytesIO()
    view.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
