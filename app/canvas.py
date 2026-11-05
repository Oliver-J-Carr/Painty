"""The canvas: shape definitions, paint rules and SVG rendering. No AI here.

The canvas is an ordered list of painted shapes. Order is everything: each
shape is laid on top of the ones before it, like real paint, so where shapes
overlap the newest one wins. Nothing is ever removed.

Every shape becomes one self-contained SVG fragment (a <g> with its own
gradient <defs>). The browser shows those fragments, and the backend joins
the same fragments into one SVG and rasterises it for the agent's eyes and
the gallery. One renderer, so what you see is exactly what the agent sees.

The model never sees the Shape model below. It writes each shape as one short
line of text (the shape notation, read by parse_shape), which costs far fewer
tokens than JSON. The prompt and the paint tool's docstring teach the notation,
and the messages here are read by the model, so treat vague wording as a bug.

    kind x1 y1 x2 y2 fill [name=value extras]
    ellipse 196 180 316 300 #f2c9a0 outline=#5a3a1a thickness=3
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

# The canvas shapes a drawing can use: (width, height) in px. The short side is
# always 512, so details are the same size in pixels on every canvas, and both
# sides are multiples of the agent's 64 px grid.
CANVAS_SHAPES: dict[str, tuple[int, int]] = {
    "square": (512, 512),
    "landscape": (768, 512),
    "portrait": (512, 768),
}
DEFAULT_SHAPE = "square"
CANVAS_SIZE = CANVAS_SHAPES[DEFAULT_SHAPE][0]
BACKGROUND = "#ffffff"


def coord_range(side: int) -> tuple[int, int]:
    """Shapes may start or end a little off the canvas (anything outside is
    clipped): half the side's length past either edge."""
    return -side // 2, side + side // 2

MIN_AREA_SIDE = 2      # px: smallest width/height for rectangles, ellipses, triangles
MIN_LINE_LENGTH = 1    # px
MAX_LINE_THICKNESS = 100
MAX_OUTLINE_THICKNESS = 40
MAX_COLOURS = 3
DEFAULT_LINE_THICKNESS = 3
DEFAULT_OUTLINE_THICKNESS = 2

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
KINDS = ("line", "rectangle", "ellipse", "triangle")
_KIND_ALIASES = {"rect": "rectangle"}
EXTRAS = ("opacity", "rotation", "thickness", "outline")


# --------------------------------------------------------------------- models
class Point(BaseModel):
    x: float = Field(description="Pixels from the left edge. 0 is the left edge.")
    y: float = Field(description="Pixels down from the top edge. 0 is the top edge.")


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
    text: str = ""  # the shape notation exactly as the model wrote it


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


def check_shape(shape: Shape, width: int = CANVAS_SIZE,
                height: int = CANVAS_SIZE) -> tuple[list[str], list[str]]:
    """Problems that stop a shape being painted on a width x height canvas,
    and harmless notes."""
    errors: list[str] = []
    notes: list[str] = []

    for name, v, side in (("x1", shape.start.x, width), ("y1", shape.start.y, height),
                          ("x2", shape.end.x, width), ("y2", shape.end.y, height)):
        lo, hi = coord_range(side)
        if not math.isfinite(v):
            errors.append(f"{name} is not a number")
        elif not lo <= v <= hi:
            errors.append(f"{name} = {_fmt(v)} is too far off the canvas "
                          f"(allowed {lo} to {hi})")

    colours = shape.fill.colors
    if not colours:
        errors.append("the fill has no colours; give at least one hex colour such as #ff8800")
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
        errors.append("the linear blend angle is not a number")

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
            errors.append(f"outline '{shape.outline_color}' is not a hex colour like #000000")
        if t > 0 and shape.outline_color is None:
            notes.append("thickness without an outline colour, so no outline was painted")
        if t == 0 and shape.outline_color is not None:
            notes.append("outline thickness is 0, so no outline was painted")
    return errors, notes


# ------------------------------------------------------------ shape notation
def split_batch(texts: list[str]) -> list[str]:
    """One string per shape. Several shapes crammed into one string (split by
    new lines or semicolons) are split apart, and blank ones are dropped."""
    return [part.strip() for text in texts for part in re.split(r"[\n;]", text or "")
            if part.strip()]


def kind_of(text: str) -> str:
    """The kind a shape's text starts with, for the activity feed."""
    word = (text.split() or ["?"])[0].lower()
    return _KIND_ALIASES.get(word, word)


def _number(token: str) -> float | None:
    try:
        return float(token)
    except ValueError:
        return None


def parse_shape(text: str) -> tuple[Shape | None, list[str], list[str]]:
    """Read one shape written in the notation: (shape or None, errors, notes).

    Forgiving about letter case, commas, spaces around '=' and 'rect' for
    rectangle, so small slips don't cost a repaint. Colours and ranges are
    checked afterwards by check_shape.
    """
    notes: list[str] = []
    tokens = re.sub(r"\s*=\s*", "=", (text or "").replace(",", " ")).lower().split()
    if not tokens:
        return None, ["the shape is empty"], notes

    kind = _KIND_ALIASES.get(tokens[0], tokens[0])
    if kind not in KINDS:
        return None, [f"unknown kind '{tokens[0]}'; use line, rectangle, ellipse or triangle"], notes
    coords = [_number(t) for t in tokens[1:5]]
    if len(coords) < 4 or None in coords:
        return None, [f"'{tokens[0]}' must be followed by four numbers: x1 y1 x2 y2"], notes
    rest = tokens[5:]
    if not rest or "=" in rest[0]:
        return None, ["x1 y1 x2 y2 must be followed by the fill: a colour such as #ff8800, "
                      "linear #a #b angle, or radial #a #b"], notes

    # The fill: one colour, or linear/radial with 2 or 3 colours (and an angle for linear).
    errors: list[str] = []
    style, colours, angle = "solid", [], 0.0
    if rest[0] in ("linear", "radial"):
        style = rest.pop(0)
        while rest and "=" not in rest[0] and _number(rest[0]) is None:
            colours.append(rest.pop(0))
    else:
        colours.append(rest.pop(0))
        while rest and rest[0].startswith("#"):
            colours.append(rest.pop(0))
    if style == "linear":
        if rest and _number(rest[0]) is not None:
            angle = _number(rest.pop(0))
        else:
            errors.append("a linear fill needs its angle after the colours, "
                          "as in linear #4a90d9 #cfe8ff 180")
    elif style == "radial" and rest and _number(rest[0]) is not None:
        notes.append(f"radial fills have no angle, so {rest.pop(0)} was ignored")

    # Extras: name=value, in any order.
    values: dict[str, float | str] = {}
    for token in rest:
        name, eq, value = token.partition("=")
        if not eq:
            errors.append(f"unexpected '{token}'; extras are written name=value, such as opacity=0.5")
        elif name not in EXTRAS:
            errors.append(f"unknown extra '{name}'; use opacity, rotation, thickness or outline")
        elif name == "outline":
            values[name] = value
        elif _number(value) is None:
            errors.append(f"{name}={value} is not a number")
        else:
            values[name] = _number(value)
    if errors:
        return None, errors, notes

    outline = values.get("outline")
    if kind == "line":
        thickness = DEFAULT_LINE_THICKNESS
        if outline is not None:
            notes.append("outline is ignored for lines")
            outline = None
    else:
        thickness = DEFAULT_OUTLINE_THICKNESS if outline is not None else 0
    shape = Shape(
        kind=kind, start=Point(x=coords[0], y=coords[1]), end=Point(x=coords[2], y=coords[3]),
        fill=Fill(style=style, colors=colours, angle=angle),
        opacity=values.get("opacity", 1), rotation=values.get("rotation", 0),
        thickness=values.get("thickness", thickness), outline_color=outline,
    )
    return shape, [], notes


def check_batch(texts: list[str], width: int = CANVAS_SIZE,
                height: int = CANVAS_SIZE) -> tuple[list[tuple[str, Shape]], list[str], list[str]]:
    """Read and check a batch of shapes for a width x height canvas, in paint order.

    Returns the shapes that can be painted (every shape before the first bad
    one, with its text), every problem in the whole batch, and notes on the
    shapes that can be painted. Messages name each shape by its place in the batch.
    """
    good: list[tuple[str, Shape]] = []
    errors: list[str] = []
    notes: list[str] = []
    for i, text in enumerate(texts, start=1):
        shape, problems, remarks = parse_shape(text)
        if shape is not None:
            problems, more = check_shape(shape, width, height)
            remarks += more
        if problems:
            quoted = text if len(text) <= 80 else text[:80] + "…"
            errors.append(f'shape {i} "{quoted}": ' + "; ".join(problems))
        elif not errors:
            good.append((text, shape))
            notes += [f"shape {i} ({shape.kind}): {r}" for r in remarks]
    return good, errors, notes


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

    def __init__(self, shape: str = DEFAULT_SHAPE, background: str = BACKGROUND) -> None:
        if shape not in CANVAS_SHAPES:
            shape = DEFAULT_SHAPE
        self.shape = shape
        self.width, self.height = CANVAS_SHAPES[shape]
        self.background = background
        self.shapes: list[PaintedShape] = []

    def __len__(self) -> int:
        return len(self.shapes)

    def add(self, shapes: list[Shape], texts: list[str] | None = None) -> list[PaintedShape]:
        """Paint shapes that have already passed check_batch(), in order.
        texts: each shape's notation as the model wrote it, kept for the shape log."""
        painted = []
        for i, shape in enumerate(shapes):
            n = len(self.shapes) + 1
            text = texts[i] if texts else ""
            ps = PaintedShape(n=n, shape=shape, svg=shape_svg(n, shape), text=text)
            self.shapes.append(ps)
            painted.append(ps)
        return painted

    def to_svg(self, upto: int | None = None) -> str:
        """The painting as one SVG. upto: only the first `upto` shapes (a stage
        snapshot); None means all of them."""
        w, h = self.width, self.height
        shapes = self.shapes if upto is None else self.shapes[:upto]
        body = "".join(ps.svg for ps in shapes)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
                f'width="{w}" height="{h}">'
                f'<rect width="{w}" height="{h}" fill="{self.background}"/>{body}</svg>')

    def info(self) -> dict:
        """The canvas shape and size, for the browser and meta.json."""
        return {"shape": self.shape, "width": self.width, "height": self.height}

    def shape_log(self) -> list[dict]:
        """Every shape in paint order, as plain data (saved with the painting)."""
        return [{"n": ps.n, "text": ps.text, **ps.shape.model_dump()} for ps in self.shapes]

    def shape_text(self) -> str:
        """Every shape in paint order, one line each, exactly as the model wrote it."""
        return "".join(f"{ps.text}\n" for ps in self.shapes)
