"""The canvas: shape definitions, paint rules and SVG rendering. No AI here.

The canvas is an ordered list of painted shapes. Order is everything: each
shape is laid on top of the ones before it, like real paint, so where shapes
overlap the newest one wins. Nothing is ever removed.

Every shape becomes one self-contained SVG fragment (a <g> with its own
gradient <defs>). The browser shows those fragments, and the backend joins
the same fragments into one SVG and rasterises it for the agent's eyes and
the gallery. One renderer, so what you see is exactly what the agent sees.

Field descriptions in the models below are sent to the model as part of the
paint tool's schema. Treat vague wording as a bug.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

CANVAS_SIZE = 512
BACKGROUND = "#ffffff"
# Shapes may start or end a little off the canvas (anything outside is clipped).
COORD_MIN = -CANVAS_SIZE // 2
COORD_MAX = CANVAS_SIZE + CANVAS_SIZE // 2
MIN_AREA_SIDE = 2      # px: smallest width/height for rectangles, ellipses, triangles
MIN_LINE_LENGTH = 1    # px
MAX_LINE_THICKNESS = 100
MAX_OUTLINE_THICKNESS = 40
MAX_COLOURS = 3

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


# --------------------------------------------------------------------- models
class Point(BaseModel):
    x: float = Field(description="Pixels from the left edge. 0 is the left edge, 512 the right edge.")
    y: float = Field(description="Pixels down from the top edge. 0 is the top edge, 512 the bottom edge.")


class Fill(BaseModel):
    style: Literal["solid", "linear", "radial"] = Field(
        description=(
            "solid: one flat colour. "
            "linear: colours blend in a straight direction (see angle). "
            "radial: colours blend outwards from the centre of the shape to its edge."
        )
    )
    colors: list[str] = Field(
        description=(
            "Hex colours such as #1f6feb. solid uses one colour. linear and radial use "
            "2 or 3 colours, listed from where the blend starts to where it ends "
            "(for radial: centre first, edge last)."
        )
    )
    angle: float = Field(
        description=(
            "linear only: the compass direction the blend travels, in degrees. "
            "0 = from bottom to top, 90 = from left to right, 180 = from top to bottom, "
            "270 = from right to left. On a rotated shape the blend turns with the shape. "
            "Use 0 for solid and radial."
        )
    )


class Shape(BaseModel):
    kind: Literal["line", "rectangle", "ellipse", "triangle"] = Field(
        description=(
            "line: a straight stroke from start to end. "
            "rectangle, ellipse and triangle fill the box that has start and end at "
            "opposite corners, like dragging a shape tool from one corner to the other."
        )
    )
    start: Point = Field(description="Where the drag starts: one corner of the box, or the first end of a line.")
    end: Point = Field(description="Where the drag ends: the opposite corner of the box, or the other end of a line.")
    fill: Fill = Field(description="The paint. For a line this is the colour of the line itself.")
    opacity: float = Field(
        description=(
            "How solid the paint is, from 0.05 to 1. 1 covers everything beneath it. "
            "Lower values let the paint beneath show through, good for shading and highlights."
        )
    )
    rotation: float = Field(
        description=(
            "Turns the shape clockwise around the centre of its box, in degrees. 0 = no turn. "
            "A triangle with rotation 0 points up (its tip is the middle of the box's top edge); "
            "90 points right, 180 down, 270 left, and any angle in between works. Ignored for lines."
        )
    )
    thickness: float = Field(
        description=(
            "Lines: how thick the line is, 1 to 100 px. Other shapes: outline thickness, "
            "0 to 40 px, where 0 means no outline."
        )
    )
    outline_color: str | None = Field(
        description="Outline colour as hex such as #000000, or null for no outline. Ignored for lines."
    )


@dataclass(frozen=True)
class PaintedShape:
    n: int        # 1-based position in paint order
    shape: Shape
    svg: str      # self-contained SVG fragment


# ----------------------------------------------------------------- validation
def _fmt(v: float) -> str:
    """Short number text for SVG and messages: 12.0 -> '12', 12.345 -> '12.35'."""
    text = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def normalise_hex(colour: str) -> str | None:
    """'#ABC' -> '#aabbcc'. None if it isn't a hex colour."""
    c = (colour or "").strip()
    if not _HEX.match(c):
        return None
    c = c.lower()
    if len(c) == 4:
        c = "#" + "".join(ch * 2 for ch in c[1:])
    return c


def check_shape(shape: Shape) -> tuple[list[str], list[str]]:
    """Problems that stop a shape being painted, and harmless notes."""
    errors: list[str] = []
    notes: list[str] = []

    for label, p in (("start", shape.start), ("end", shape.end)):
        for axis, v in (("x", p.x), ("y", p.y)):
            if not math.isfinite(v):
                errors.append(f"{label}.{axis} is not a number")
            elif not COORD_MIN <= v <= COORD_MAX:
                errors.append(f"{label}.{axis} = {_fmt(v)} is too far off the canvas "
                              f"(allowed {COORD_MIN} to {COORD_MAX})")

    colours = shape.fill.colors
    if not colours:
        errors.append("fill.colors is empty; give at least one hex colour")
    for c in colours:
        if normalise_hex(c) is None:
            errors.append(f"'{c}' is not a hex colour like #ff8800")
    if shape.fill.style == "solid" and len(colours) > 1:
        notes.append("solid fill uses only the first colour")
    if shape.fill.style != "solid" and len(colours) == 1:
        notes.append(f"{shape.fill.style} fill has one colour, so it painted as solid")
    if len(colours) > MAX_COLOURS:
        notes.append(f"only the first {MAX_COLOURS} colours were used")
    if not math.isfinite(shape.fill.angle):
        errors.append("fill.angle is not a number")

    if not (math.isfinite(shape.opacity) and 0.05 <= shape.opacity <= 1):
        errors.append(f"opacity {_fmt(shape.opacity)} must be between 0.05 and 1")
    if not math.isfinite(shape.rotation):
        errors.append("rotation is not a number")

    if errors:  # geometry checks below need finite numbers
        return errors, notes

    t = shape.thickness
    if shape.kind == "line":
        length = math.dist((shape.start.x, shape.start.y), (shape.end.x, shape.end.y))
        if length < MIN_LINE_LENGTH:
            errors.append("the line has no length; use a small ellipse to paint a dot")
        if not (math.isfinite(t) and 1 <= t <= MAX_LINE_THICKNESS):
            errors.append(f"line thickness {_fmt(t)} must be between 1 and {MAX_LINE_THICKNESS}")
        if shape.rotation % 360:
            notes.append("rotation is ignored for lines")
    else:
        w = abs(shape.end.x - shape.start.x)
        h = abs(shape.end.y - shape.start.y)
        if w < MIN_AREA_SIDE or h < MIN_AREA_SIDE:
            errors.append(f"the box is {_fmt(w)} x {_fmt(h)} px; it must be at least "
                          f"{MIN_AREA_SIDE} px wide and tall")
        if not (math.isfinite(t) and 0 <= t <= MAX_OUTLINE_THICKNESS):
            errors.append(f"outline thickness {_fmt(t)} must be between 0 and {MAX_OUTLINE_THICKNESS}")
        if shape.outline_color is not None and normalise_hex(shape.outline_color) is None:
            errors.append(f"outline_color '{shape.outline_color}' is not a hex colour like #000000")
        if t > 0 and shape.outline_color is None:
            notes.append("no outline_color, so no outline was painted")
        if t == 0 and shape.outline_color is not None:
            notes.append("outline thickness is 0, so no outline was painted")
    return errors, notes


# ------------------------------------------------------------------ rendering
def _box(shape: Shape) -> tuple[float, float, float, float]:
    """The drag box with corners sorted, so any drag direction works."""
    x0, x1 = sorted((shape.start.x, shape.end.x))
    y0, y1 = sorted((shape.start.y, shape.end.y))
    return x0, y0, x1, y1


def _colours(fill: Fill) -> list[str]:
    cs = [normalise_hex(c) for c in fill.colors[:MAX_COLOURS]]
    return [c for c in cs if c]


def _stops(colours: list[str]) -> str:
    last = len(colours) - 1
    return "".join(
        f'<stop offset="{_fmt(i / last)}" stop-color="{c}"/>' for i, c in enumerate(colours)
    )


def _paint(n: int, shape: Shape, x0: float, y0: float, x1: float, y1: float) -> tuple[str, str]:
    """(defs, paint) for this shape. paint is a colour or url(#id)."""
    colours = _colours(shape.fill)
    if shape.fill.style == "solid" or len(colours) < 2:
        return "", colours[0]

    gid = f"p{n}-g"
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w, h = x1 - x0, y1 - y0
    if shape.fill.style == "linear":
        # Compass angle -> screen direction (y grows downwards): 0 = up, 90 = right.
        a = math.radians(shape.fill.angle)
        dx, dy = math.sin(a), -math.cos(a)
        # Span the whole box along that direction, so the first colour sits on one
        # side of the shape and the last colour on the other.
        half = max((abs(w * dx) + abs(h * dy)) / 2, 0.5)
        coords = (f'x1="{_fmt(cx - dx * half)}" y1="{_fmt(cy - dy * half)}" '
                  f'x2="{_fmt(cx + dx * half)}" y2="{_fmt(cy + dy * half)}"')
        defs = (f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" {coords}>'
                f"{_stops(colours)}</linearGradient>")
    elif shape.kind == "line":
        # A line's box can be zero pixels tall, so size the circle in pixels.
        r = max(math.dist((shape.start.x, shape.start.y), (shape.end.x, shape.end.y)) / 2, 0.5)
        defs = (f'<radialGradient id="{gid}" gradientUnits="userSpaceOnUse" '
                f'cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(r)}">{_stops(colours)}</radialGradient>')
    else:
        # Fits the shape's own box, so an ellipse gets an elliptical blend.
        defs = (f'<radialGradient id="{gid}" cx="0.5" cy="0.5" r="0.5">'
                f"{_stops(colours)}</radialGradient>")
    return f"<defs>{defs}</defs>", f"url(#{gid})"


def shape_svg(n: int, shape: Shape) -> str:
    """One self-contained SVG fragment for a valid shape."""
    x0, y0, x1, y1 = _box(shape)
    defs, paint = _paint(n, shape, x0, y0, x1, y1)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

    if shape.kind == "line":
        body = (f'<line x1="{_fmt(shape.start.x)}" y1="{_fmt(shape.start.y)}" '
                f'x2="{_fmt(shape.end.x)}" y2="{_fmt(shape.end.y)}" stroke="{paint}" '
                f'stroke-width="{_fmt(shape.thickness)}" stroke-linecap="round"/>')
    else:
        rotation = shape.rotation % 360
        turn = f' transform="rotate({_fmt(rotation)} {_fmt(cx)} {_fmt(cy)})"' if rotation else ""
        outline = ""
        if shape.thickness > 0 and shape.outline_color is not None:
            outline = (f' stroke="{normalise_hex(shape.outline_color)}" '
                       f'stroke-width="{_fmt(shape.thickness)}" stroke-linejoin="round"')
        if shape.kind == "rectangle":
            body = (f'<rect x="{_fmt(x0)}" y="{_fmt(y0)}" width="{_fmt(x1 - x0)}" '
                    f'height="{_fmt(y1 - y0)}" fill="{paint}"{outline}{turn}/>')
        elif shape.kind == "ellipse":
            body = (f'<ellipse cx="{_fmt(cx)}" cy="{_fmt(cy)}" rx="{_fmt((x1 - x0) / 2)}" '
                    f'ry="{_fmt((y1 - y0) / 2)}" fill="{paint}"{outline}{turn}/>')
        else:  # triangle: tip at the middle of the top edge, base along the bottom
            points = f"{_fmt(cx)},{_fmt(y0)} {_fmt(x1)},{_fmt(y1)} {_fmt(x0)},{_fmt(y1)}"
            body = f'<polygon points="{points}" fill="{paint}"{outline}{turn}/>'

    opacity = f' opacity="{_fmt(shape.opacity)}"' if shape.opacity < 1 else ""
    return f'<g data-n="{n}"{opacity}>{defs}{body}</g>'


# --------------------------------------------------------------------- canvas
class Canvas:
    """The painting so far. Shapes are only ever added, never removed."""

    def __init__(self, size: int = CANVAS_SIZE, background: str = BACKGROUND) -> None:
        self.size = size
        self.background = background
        self.shapes: list[PaintedShape] = []

    def __len__(self) -> int:
        return len(self.shapes)

    @staticmethod
    def check(shapes: list[Shape]) -> tuple[list[str], list[str]]:
        """Check a whole batch. Messages name each shape by its place in the batch."""
        errors: list[str] = []
        notes: list[str] = []
        for i, shape in enumerate(shapes, start=1):
            e, n = check_shape(shape)
            errors += [f"shape {i} ({shape.kind}): {msg}" for msg in e]
            notes += [f"shape {i} ({shape.kind}): {msg}" for msg in n]
        return errors, notes

    def add(self, shapes: list[Shape]) -> list[PaintedShape]:
        """Paint a batch that has already passed check(), in order."""
        painted = []
        for shape in shapes:
            n = len(self.shapes) + 1
            ps = PaintedShape(n=n, shape=shape, svg=shape_svg(n, shape))
            self.shapes.append(ps)
            painted.append(ps)
        return painted

    def to_svg(self) -> str:
        s = self.size
        body = "".join(ps.svg for ps in self.shapes)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {s} {s}" '
                f'width="{s}" height="{s}">'
                f'<rect width="{s}" height="{s}" fill="{self.background}"/>{body}</svg>')

    def shape_log(self) -> list[dict]:
        """Every shape in paint order, as plain data (saved with the painting)."""
        return [{"n": ps.n, **ps.shape.model_dump()} for ps in self.shapes]
