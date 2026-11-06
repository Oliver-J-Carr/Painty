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

A trail is the exception to the drag box: a start point, then legs written
distance@heading on the same compass as rotation. There are never point lists
or path commands, so the model can't paste memorised SVG. There is no filled
walk: a closed, filled one was tried and models used it to trace polygons.

    trail x y distance@heading... fill [name=value extras]
    trail 150 300 60@100 50@80 #5a3a1a thickness=4 smooth=1
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from statistics import NormalDist
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
MAX_SKEW = 70          # degrees either way
MAX_LEGS = 12          # per trail, so one shape can't wander forever
MIN_SPRAY = 0.05
MAX_SOFT = 30          # px of feathered edge
MAX_BEND = 256         # px the middle of a line can be pulled aside (half the short side)
TAPERS = ("none", "end", "both")
BEND_NOTE = 8          # px: bends at least this deep get a note saying where the curve runs

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
KINDS = ("line", "rectangle", "ellipse", "triangle", "diamond", "trail")
_KIND_ALIASES = {"rect": "rectangle"}
EXTRAS = ("opacity", "rotation", "skew", "thickness", "outline", "bend", "taper", "smooth",
          "spray", "soft")
_YES = {"1": True, "yes": True, "true": True, "on": True,
        "0": False, "no": False, "false": False, "off": False}

# Spray: fine fractal noise, cut so only the asked-for share of the paint lands.
# The noise values fall on a bell curve (measured through resvg), so the cut
# comes from that curve. Seeded by shape number, so every render matches.
SPRAY_GRAIN = 0.8      # feTurbulence base frequency: pixel-sized speckle
_NOISE = NormalDist(0.515, 0.12)


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


class Leg(BaseModel):
    distance: float = Field(description="How far this leg goes, in pixels.")
    heading: float = Field(description="Compass direction: 0 = up, 90 = right, 180 = down, 270 = left.")


class Shape(BaseModel):
    kind: Literal["line", "rectangle", "ellipse", "triangle", "diamond", "trail"] = Field(
        description=(
            "line: a straight stroke from start to end. "
            "rectangle, ellipse, triangle and diamond fill the box that has start and end at "
            "opposite corners, like dragging a shape tool from one corner to the other. "
            "trail: a stroke walked leg by leg from start."
        )
    )
    start: Point = Field(description=("Where the drag starts: one corner of the box, the first end "
                                      "of a line, or where a walk starts."))
    end: Point = Field(description=("Where the drag ends: the opposite corner of the box, the other "
                                    "end of a line, or where a walk's last leg ends."))
    legs: list[Leg] = Field(default_factory=list, description="trail only: the walk, in order.")
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
    skew: float = Field(
        default=0,
        description=("Box shapes: tilts the top and bottom edges by this many degrees, left and right "
                     "edges staying upright. Positive drops the right side. Applied before rotation."),
    )
    bend: Leg | None = Field(
        default=None,
        description=("line only: pulls the middle of the line this far towards this heading, "
                     "so it curves through both ends, like Paint's curve tool."),
    )
    taper: Literal["none", "end", "both"] = Field(
        default="none",
        description="line and trail: thin to a point at the end, or at both ends, like a brush stroke.",
    )
    smooth: bool = Field(default=False, description="trail only: curve through the corners.")
    spray: float = Field(default=1, description="Share of the paint that lands, 0.05 to 1, as speckle.")
    soft: float = Field(default=0, description="Feathered edge width in px, 0 to 30.")


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


def _compass(degrees: float) -> tuple[float, float]:
    """Compass heading -> screen direction (y grows downwards): 0 = up, 90 = right."""
    a = math.radians(degrees)
    return math.sin(a), -math.cos(a)


def _walk(x: float, y: float, legs: list[Leg]) -> list[tuple[float, float]]:
    points = [(x, y)]
    for leg in legs:
        dx, dy = _compass(leg.heading)
        x, y = x + dx * leg.distance, y + dy * leg.distance
        points.append((x, y))
    return points


def walk_points(shape: Shape) -> list[tuple[float, float]]:
    """Every corner of a trail, from its start, leg by leg."""
    return _walk(shape.start.x, shape.start.y, shape.legs)


def check_shape(shape: Shape, width: int = CANVAS_SIZE,
                height: int = CANVAS_SIZE) -> tuple[list[str], list[str]]:
    """Problems that stop a shape being painted on a width x height canvas,
    and harmless notes."""
    errors: list[str] = []
    notes: list[str] = []
    walk = shape.kind == "trail"
    xlo, xhi = coord_range(width)
    ylo, yhi = coord_range(height)

    corners = (("x", shape.start.x, width), ("y", shape.start.y, height)) if walk else (
        ("x1", shape.start.x, width), ("y1", shape.start.y, height),
        ("x2", shape.end.x, width), ("y2", shape.end.y, height))
    for name, v, side in corners:
        lo, hi = coord_range(side)
        if not math.isfinite(v):
            errors.append(f"{name} is not a number")
        elif not lo <= v <= hi:
            errors.append(f"{name} = {_fmt(v)} is too far off the canvas "
                          f"(allowed {lo} to {hi})")
    if walk:
        if not shape.legs:
            errors.append("a trail needs at least 1 leg")
        if len(shape.legs) > MAX_LEGS:
            errors.append(f"a trail can have at most {MAX_LEGS} legs; this one has "
                          f"{len(shape.legs)}. Split it into more than one trail")
        for i, leg in enumerate(shape.legs, start=1):
            if not (math.isfinite(leg.distance) and math.isfinite(leg.heading)):
                errors.append(f"leg {i} is not a number")
            elif leg.distance < MIN_LINE_LENGTH:
                errors.append(f"leg {i} is {_fmt(leg.distance)} px long; each leg must be at least "
                              f"{MIN_LINE_LENGTH} px")
        if not errors:
            for i, (x, y) in enumerate(walk_points(shape)[1:], start=1):
                if not (xlo <= x <= xhi and ylo <= y <= yhi):
                    errors.append(f"leg {i} ends at ({_fmt(x)}, {_fmt(y)}), too far off the canvas "
                                  f"(allowed x {xlo} to {xhi}, y {ylo} to {yhi})")

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
    if not (math.isfinite(shape.skew) and -MAX_SKEW <= shape.skew <= MAX_SKEW):
        errors.append(f"skew {_fmt(shape.skew)} must be between -{MAX_SKEW} and {MAX_SKEW}")
    if not (math.isfinite(shape.spray) and MIN_SPRAY <= shape.spray <= 1):
        errors.append(f"spray {_fmt(shape.spray)} must be between {MIN_SPRAY} and 1")
    if not (math.isfinite(shape.soft) and 0 <= shape.soft <= MAX_SOFT):
        errors.append(f"soft {_fmt(shape.soft)} must be between 0 and {MAX_SOFT}")
    if shape.bend is not None:
        if not (math.isfinite(shape.bend.distance) and math.isfinite(shape.bend.heading)):
            errors.append("bend is not a number")
        elif not 0 <= shape.bend.distance <= MAX_BEND:
            errors.append(f"bend {_fmt(shape.bend.distance)} must be between 0 and {MAX_BEND} px; "
                          "to bend the other way, turn the heading round by 180")

    if errors:  # geometry checks below need finite numbers
        return errors, notes

    t = shape.thickness
    if shape.kind in ("line", "trail"):
        if shape.kind == "line":
            length = math.dist((shape.start.x, shape.start.y), (shape.end.x, shape.end.y))
            if length < MIN_LINE_LENGTH:
                errors.append("the line has no length; use a small ellipse to paint a dot")
        if not (math.isfinite(t) and 1 <= t <= MAX_LINE_THICKNESS):
            errors.append(f"line thickness {_fmt(t)} must be between 1 and {MAX_LINE_THICKNESS}")
    else:
        x0, y0, x1, y1 = _box(shape)
        w, h = x1 - x0, y1 - y0
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
    if errors:
        return errors, notes

    if shape.kind == "line" and shape.rotation % 360:
        notes.append("rotation is ignored for lines")
    if shape.kind == "line" and shape.skew:
        notes.append("skew is ignored for lines")
    if walk and (shape.rotation % 360 or shape.skew):
        notes.append("rotation and skew are ignored for a trail: its leg headings set its direction")
    if shape.smooth and not walk:
        notes.append("smooth only bends trails, so it was ignored")
    if shape.bend is not None and shape.kind != "line":
        notes.append("bend only curves lines, so it was ignored")
    if shape.taper != "none" and shape.kind not in ("line", "trail"):
        notes.append("taper only thins lines and trails, so it was ignored")
    if shape.kind == "line" and shape.bend and shape.bend.distance >= BEND_NOTE:
        # Only the ends and middle are easy to work out; say where the rest of the curve runs.
        (ax, ay), (cx, cy), (bx, by) = ((shape.start.x, shape.start.y), _bend_control(shape),
                                        (shape.end.x, shape.end.y))
        passes = ", ".join(
            f"({round((1 - t) ** 2 * ax + 2 * (1 - t) * t * cx + t * t * bx)}, "
            f"{round((1 - t) ** 2 * ay + 2 * (1 - t) * t * cy + t * t * by)})"
            for t in (0.25, 0.5, 0.75))
        notes.append(f"curves through {passes} between its ends")
    if walk:
        # Where every bend and the end really landed. Models add up headings
        # badly in their heads, so they need these to place things along a trail.
        *_, end = points = [f"({round(x)}, {round(y)})" for x, y in walk_points(shape)]
        bends = ", ".join(points[1:-1])
        notes.append((f"turns at {bends}, then " if bends else "") + f"ends at {end}")
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


def _either(words: tuple[str, ...]) -> str:
    """('a', 'b', 'c') -> 'a, b or c'."""
    return ", ".join(words[:-1]) + " or " + words[-1]


def _leg(token: str) -> Leg | None:
    """'40@90' -> 40 px heading 90 degrees."""
    distance, _, heading = token.partition("@")
    d, h = _number(distance), _number(heading)
    return None if d is None or h is None else Leg(distance=d, heading=h)


def parse_shape(text: str) -> tuple[Shape | None, list[str], list[str]]:
    """Read one shape written in the notation: (shape or None, errors, notes).

    Forgiving about letter case, commas, spaces around '=' or '@' and 'rect' for
    rectangle, so small slips don't cost a repaint. Colours and ranges are
    checked afterwards by check_shape.
    """
    notes: list[str] = []
    tokens = re.sub(r"\s*([=@])\s*", r"\1", (text or "").replace(",", " ")).lower().split()
    if not tokens:
        return None, ["the shape is empty"], notes

    kind = _KIND_ALIASES.get(tokens[0], tokens[0])
    if kind not in KINDS:
        return None, [f"unknown kind '{tokens[0]}'; use {_either(KINDS)}"], notes
    legs: list[Leg] = []
    if kind == "trail":
        coords = [_number(t) for t in tokens[1:3]]
        if len(coords) < 2 or None in coords:
            return None, [f"'{tokens[0]}' must be followed by its start point x y, then its legs "
                          "written distance@heading, such as 40@90"], notes
        rest = tokens[3:]
        while rest and "@" in rest[0]:
            token = rest.pop(0)
            leg = _leg(token)
            if leg is None:
                return None, [f"'{token}' is not a leg; write each leg as distance@heading in "
                              "numbers, such as 40@90"], notes
            legs.append(leg)
        if not legs:
            return None, ["a trail needs its legs straight after the start point x y, each written "
                          "distance@heading, such as 40@90"], notes
        end = _walk(coords[0], coords[1], legs)[-1]
        coords += list(end)
        before_fill = "the legs"
    else:
        coords = [_number(t) for t in tokens[1:5]]
        if len(coords) < 4 or None in coords:
            return None, [f"'{tokens[0]}' must be followed by four numbers: x1 y1 x2 y2"], notes
        rest = tokens[5:]
        before_fill = "x1 y1 x2 y2"
    if not rest or "=" in rest[0]:
        return None, [f"{before_fill} must be followed by the fill: a colour such as #ff8800, "
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
    values: dict[str, float | str | bool | Leg] = {}
    for token in rest:
        name, eq, value = token.partition("=")
        if not eq:
            errors.append(f"unexpected '{token}'; extras are written name=value, such as opacity=0.5")
        elif name not in EXTRAS:
            errors.append(f"unknown extra '{name}'; use {_either(EXTRAS)}")
        elif name == "outline":
            values[name] = value
        elif name == "bend":
            leg = _leg(value)
            if leg is None:
                errors.append(f"bend={value} must be written distance@heading, such as bend=20@180")
            else:
                values[name] = leg
        elif name == "taper":
            if value in TAPERS:
                values[name] = value
            else:
                errors.append(f"taper={value} must be end (thin to a point at x2 y2) or both")
        elif name == "smooth":
            if value in _YES:
                values[name] = _YES[value]
            else:
                errors.append(f"smooth={value} must be 1 (curved) or 0 (straight)")
        elif _number(value) is None:
            errors.append(f"{name}={value} is not a number")
        else:
            values[name] = _number(value)
    if errors:
        return None, errors, notes

    outline = values.get("outline")
    if kind in ("line", "trail"):
        thickness = DEFAULT_LINE_THICKNESS
        if outline is not None:
            notes.append(f"outline is ignored for {kind}s")
            outline = None
    else:
        thickness = DEFAULT_OUTLINE_THICKNESS if outline is not None else 0
    shape = Shape(
        kind=kind, start=Point(x=coords[0], y=coords[1]), end=Point(x=coords[2], y=coords[3]),
        legs=legs, fill=Fill(style=style, colors=colours, angle=angle),
        opacity=values.get("opacity", 1), rotation=values.get("rotation", 0),
        thickness=values.get("thickness", thickness), outline_color=outline,
        skew=values.get("skew", 0), bend=values.get("bend"), taper=values.get("taper", "none"),
        smooth=values.get("smooth", False),
        spray=values.get("spray", 1), soft=values.get("soft", 0),
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
    """The drag box with corners sorted, so any drag direction works.
    For a trail, the box around every corner of the walk."""
    if shape.kind == "trail":
        xs, ys = zip(*walk_points(shape))
        return min(xs), min(ys), max(xs), max(ys)
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
    """(gradient, paint) for this shape. paint is a colour or url(#id)."""
    colours = _colours(shape.fill)
    if shape.fill.style == "solid" or len(colours) < 2:
        return "", colours[0]

    gid = f"p{n}-g"
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w, h = x1 - x0, y1 - y0
    if shape.fill.style == "linear":
        dx, dy = _compass(shape.fill.angle)
        # Span the whole box along that direction, so the first colour sits on one
        # side of the shape and the last colour on the other.
        half = max((abs(w * dx) + abs(h * dy)) / 2, 0.5)
        coords = (f'x1="{_fmt(cx - dx * half)}" y1="{_fmt(cy - dy * half)}" '
                  f'x2="{_fmt(cx + dx * half)}" y2="{_fmt(cy + dy * half)}"')
        defs = (f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" {coords}>'
                f"{_stops(colours)}</linearGradient>")
    elif shape.kind in ("line", "trail"):
        # A line's box can be zero pixels tall, so size the circle in pixels.
        r = max(math.hypot(w, h) / 2, 0.5)
        defs = (f'<radialGradient id="{gid}" gradientUnits="userSpaceOnUse" '
                f'cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(r)}">{_stops(colours)}</radialGradient>')
    else:
        # Fits the shape's own box, so an ellipse gets an elliptical blend.
        defs = (f'<radialGradient id="{gid}" cx="0.5" cy="0.5" r="0.5">'
                f"{_stops(colours)}</radialGradient>")
    return defs, f"url(#{gid})"


def _points(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{_fmt(x)},{_fmt(y)}" for x, y in points)


Pt = tuple[float, float]


def _smooth_segments(points: list[Pt]) -> list[tuple[Pt, Pt, Pt, Pt]]:
    """A curve through every point (Catmull-Rom), as cubic Bezier segments."""
    count = len(points)

    def at(i: int) -> Pt:
        return points[min(max(i, 0), count - 1)]

    segments = []
    for i in range(count - 1):
        (ax, ay), (bx, by), (cx, cy), (ex, ey) = at(i - 1), at(i), at(i + 1), at(i + 2)
        segments.append(((bx, by), (bx + (cx - ax) / 6, by + (cy - ay) / 6),
                         (cx - (ex - bx) / 6, cy - (ey - by) / 6), (cx, cy)))
    return segments


def _pt(p: Pt) -> str:
    return f"{_fmt(p[0])} {_fmt(p[1])}"


def _smooth_path(points: list[Pt]) -> str:
    segments = _smooth_segments(points)
    return " ".join([f"M{_pt(points[0])}"] + [f"C{_pt(c1)} {_pt(c2)} {_pt(c)}"
                                               for _, c1, c2, c in segments])


def _bend_control(shape: Shape) -> Pt:
    """The curve's control point: a quadratic Bezier's middle sits half way to it,
    so pulling the control point twice as far moves the middle by the bend."""
    dx, dy = _compass(shape.bend.heading)
    mx, my = (shape.start.x + shape.end.x) / 2, (shape.start.y + shape.end.y) / 2
    return mx + 2 * shape.bend.distance * dx, my + 2 * shape.bend.distance * dy


def _centreline(shape: Shape, steps: int = 12) -> list[Pt]:
    """Points along the middle of a line or trail, close enough to draw a taper."""
    if shape.kind == "line":
        a, b = (shape.start.x, shape.start.y), (shape.end.x, shape.end.y)
        c = _bend_control(shape) if shape.bend else ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        segments = [(a, c, b)]
        steps *= 3
    elif shape.smooth:
        segments = _smooth_segments(walk_points(shape))
    else:
        corners = walk_points(shape)
        segments = list(zip(corners, corners[1:]))
    line = [segments[0][0]]
    for seg in segments:
        for i in range(1, steps + 1):
            t, u = i / steps, 1 - i / steps
            if len(seg) == 2:      # straight
                weights = (u, t)
            elif len(seg) == 3:    # quadratic
                weights = (u * u, 2 * u * t, t * t)
            else:                  # cubic
                weights = (u ** 3, 3 * u * u * t, 3 * u * t * t, t ** 3)
            point = (sum(w * p[0] for w, p in zip(weights, seg)),
                     sum(w * p[1] for w, p in zip(weights, seg)))
            if math.dist(point, line[-1]) > 0.01:
                line.append(point)
    return line


def _tapered(shape: Shape, paint: str) -> str:
    """A line or trail drawn as a filled outline that thins to a point, like a brush stroke."""
    line = _centreline(shape)
    along = [0.0]
    for a, b in zip(line, line[1:]):
        along.append(along[-1] + math.dist(a, b))
    total = along[-1] or 1
    left, right = [], []
    for i, (x, y) in enumerate(line):
        (ax, ay), (bx, by) = line[max(i - 1, 0)], line[min(i + 1, len(line) - 1)]
        size = math.hypot(bx - ax, by - ay) or 1
        nx, ny = -(by - ay) / size, (bx - ax) / size
        t = along[i] / total
        half = shape.thickness / 2 * ((1 - t) if shape.taper == "end" else math.sin(math.pi * t))
        left.append((x + nx * half, y + ny * half))
        right.append((x - nx * half, y - ny * half))
    body = f'<polygon points="{_points(left + right[::-1])}" fill="{paint}"/>'
    if shape.taper == "end":  # a round start, like the brush landing
        body = (f'<circle cx="{_fmt(line[0][0])}" cy="{_fmt(line[0][1])}" '
                f'r="{_fmt(shape.thickness / 2)}" fill="{paint}"/>') + body
    return body


def _turn(shape: Shape, cx: float, cy: float) -> str:
    """The transform for a box shape: skew first, then rotation, both around the centre."""
    steps = []
    rotation = shape.rotation % 360
    if rotation:
        steps.append(f"rotate({_fmt(rotation)} {_fmt(cx)} {_fmt(cy)})")
    if shape.skew:
        steps.append(f"translate({_fmt(cx)} {_fmt(cy)}) skewY({_fmt(shape.skew)}) "
                     f"translate({_fmt(-cx)} {_fmt(-cy)})")
    return f' transform="{" ".join(steps)}"' if steps else ""


def _reach(shape: Shape) -> tuple[float, float, float, float]:
    """Roughly where this shape's paint can land, for the size of its filter."""
    if shape.kind == "trail":
        corners = walk_points(shape)
    elif shape.kind == "line":
        corners = [(shape.start.x, shape.start.y), (shape.end.x, shape.end.y)]
        if shape.bend:  # the curve stays inside the triangle of its ends and control point
            corners.append(_bend_control(shape))
    else:
        x0, y0, x1, y1 = _box(shape)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        slope = math.tan(math.radians(shape.skew))
        a = math.radians(shape.rotation)
        corners = []
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            x, y = x - cx, y - cy + slope * (x - cx)
            corners.append((cx + x * math.cos(a) - y * math.sin(a),
                            cy + x * math.sin(a) + y * math.cos(a)))
    pad = shape.thickness + 1.5 * shape.soft + 2  # stroke, the blur's spread, and a margin
    xs, ys = zip(*corners)
    # Not clipped to the canvas: a fragment doesn't know which canvas it's on,
    # and anything past the edge is clipped by the canvas itself.
    left, top = min(xs) - pad, min(ys) - pad
    return left, top, max(max(xs) + pad - left, 1), max(max(ys) + pad - top, 1)


def _effects(n: int, shape: Shape) -> str:
    """The filter for soft edges and spray, or "" if the shape has neither."""
    if shape.spray >= 1 and shape.soft <= 0:
        return ""
    steps, source = [], "SourceGraphic"
    if shape.soft > 0:
        steps.append(f'<feGaussianBlur in="SourceGraphic" stdDeviation="{_fmt(shape.soft / 2)}" '
                     'result="soft"/>')
        source = "soft"
    if shape.spray < 1:
        # Keep paint only where the noise is below the cut: alpha = k * (cut - noise).
        k = 50
        cut = _NOISE.inv_cdf(shape.spray)
        steps.append(f'<feTurbulence type="fractalNoise" baseFrequency="{SPRAY_GRAIN}" '
                     f'numOctaves="2" seed="{n}" result="noise"/>'
                     f'<feComponentTransfer in="noise" result="dots">'
                     f'<feFuncA type="linear" slope="{-k}" intercept="{k * cut:.3f}"/>'
                     f'</feComponentTransfer>'
                     f'<feComposite in="{source}" in2="dots" operator="in"/>')
    x, y, w, h = _reach(shape)
    # In pixels, not the shape's box: a level line's box is 0 tall, which would hide it.
    return (f'<filter id="p{n}-f" filterUnits="userSpaceOnUse" x="{_fmt(x)}" y="{_fmt(y)}" '
            f'width="{_fmt(w)}" height="{_fmt(h)}" color-interpolation-filters="sRGB">'
            f'{"".join(steps)}</filter>')


def shape_svg(n: int, shape: Shape) -> str:
    """One self-contained SVG fragment for a valid shape."""
    x0, y0, x1, y1 = _box(shape)
    gradient, paint = _paint(n, shape, x0, y0, x1, y1)
    effects = _effects(n, shape)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

    outline = ""
    if shape.kind not in ("line", "trail") and shape.thickness > 0 and shape.outline_color is not None:
        outline = (f' stroke="{normalise_hex(shape.outline_color)}" '
                   f'stroke-width="{_fmt(shape.thickness)}" stroke-linejoin="round"')
    if shape.kind in ("line", "trail") and shape.taper != "none":
        body = _tapered(shape, paint)
    elif shape.kind == "line" and shape.bend:
        body = (f'<path d="M{_pt((shape.start.x, shape.start.y))} Q{_pt(_bend_control(shape))} '
                f'{_pt((shape.end.x, shape.end.y))}" fill="none" stroke="{paint}" '
                f'stroke-width="{_fmt(shape.thickness)}" stroke-linecap="round"/>')
    elif shape.kind == "line":
        body = (f'<line x1="{_fmt(shape.start.x)}" y1="{_fmt(shape.start.y)}" '
                f'x2="{_fmt(shape.end.x)}" y2="{_fmt(shape.end.y)}" stroke="{paint}" '
                f'stroke-width="{_fmt(shape.thickness)}" stroke-linecap="round"/>')
    elif shape.kind == "trail":
        stroke = (f'fill="none" stroke="{paint}" stroke-width="{_fmt(shape.thickness)}" '
                  'stroke-linecap="round" stroke-linejoin="round"')
        points = walk_points(shape)
        body = (f'<path d="{_smooth_path(points)}" {stroke}/>' if shape.smooth
                else f'<polyline points="{_points(points)}" {stroke}/>')
    else:
        turn = _turn(shape, cx, cy)
        if shape.kind == "rectangle":
            body = (f'<rect x="{_fmt(x0)}" y="{_fmt(y0)}" width="{_fmt(x1 - x0)}" '
                    f'height="{_fmt(y1 - y0)}" fill="{paint}"{outline}{turn}/>')
        elif shape.kind == "ellipse":
            body = (f'<ellipse cx="{_fmt(cx)}" cy="{_fmt(cy)}" rx="{_fmt((x1 - x0) / 2)}" '
                    f'ry="{_fmt((y1 - y0) / 2)}" fill="{paint}"{outline}{turn}/>')
        elif shape.kind == "diamond":  # corners at the middle of each edge of the box
            points = _points([(cx, y0), (x1, cy), (cx, y1), (x0, cy)])
            body = f'<polygon points="{points}" fill="{paint}"{outline}{turn}/>'
        else:  # triangle: tip at the middle of the top edge, base along the bottom
            points = _points([(cx, y0), (x1, y1), (x0, y1)])
            body = f'<polygon points="{points}" fill="{paint}"{outline}{turn}/>'

    opacity = f' opacity="{_fmt(shape.opacity)}"' if shape.opacity < 1 else ""
    defs = f"<defs>{gradient}{effects}</defs>" if gradient or effects else ""
    filtered = f' filter="url(#p{n}-f)"' if effects else ""
    return f'<g data-n="{n}"{opacity}{filtered}>{defs}{body}</g>'


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
