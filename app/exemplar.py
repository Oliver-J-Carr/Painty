"""The exemplar: practice sheets that show the agent how the notation paints.

Each sheet is a 768 x 512 page of numbered examples, painted by the real
renderer (Canvas and shape_svg), so it can never disagree with what the paint
tool does. Labels go on top, in the agent's view only: each example's number,
its drag box as a dashed outline, and its corners (or a line's ends, a bend's
middle, a trail's corners) as dots with their coordinates. Then the drawing's
grid mode is applied, the same as a look.

The examples are written in the notation here, and the text sent with the
sheets is built from the same lines, so the text and the picture always agree.
Built from code rather than kept as a saved PNG, so a change to the renderer
changes the exemplar with it. It takes milliseconds and is cached per grid mode.

"Exemplar" rather than "reference": a reference is the person's own image,
given with a prompt. The exemplar is the same for every drawing.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .canvas import Canvas, Shape, _bend_control, _box, check_batch, walk_points
from .view import DEFAULT_GRID, GRID_MODES, agent_view, render_png

SHEET = (768, 512)       # px: each sheet's width and height
COLUMNS, ROWS = 4, 3     # cells per sheet
MARK_COLOUR = (214, 40, 110)         # labels: a strong pink no example paints with
LABEL_COLOUR = (40, 40, 40)
HALO_COLOUR = (255, 255, 255, 215)
DASH = 5                 # px: dash and gap length of a drag box outline


@dataclass(frozen=True)
class Example:
    caption: str                  # what it shows, sent with its notation
    shapes: tuple[str, ...] = ()  # painted in order, written in the notation
    marks: bool = True            # dashed boxes and labelled corners (off for busy groups)
    compass: bool = False         # a compass rose instead of shapes


# Each sheet: (what it's about, its examples in reading order, one per cell).
SHEETS: tuple[tuple[str, tuple[Example, ...]], ...] = (
    ("box shapes, rotation and skew", (
        Example("A rectangle fills its drag box.",
                ("rectangle 32 40 160 130 #4a90d9",)),
        Example("An ellipse fills the same box.",
                ("ellipse 224 40 352 130 #e8a33d",)),
        Example("A triangle at rotation 0: its tip is the middle of the box's top edge, its base the bottom edge.",
                ("triangle 416 40 544 130 #6aa84f",)),
        Example("A diamond: its corners are the middle of each edge of the box.",
                ("diamond 608 40 736 130 #b04ac0",)),
        Example("rotation=90 turns the triangle clockwise around the centre of its box: the tip points right.",
                ("triangle 40 210 150 300 #6aa84f rotation=90",)),
        Example("rotation=180: the tip points down.",
                ("triangle 232 210 342 300 #6aa84f rotation=180",)),
        Example("rotation=45: the tip points up and right, and the corners poke outside the box.",
                ("triangle 424 210 534 300 #6aa84f rotation=45",)),
        Example("rotation=30 on a rectangle: it turns around the centre of its box.",
                ("rectangle 616 215 726 295 #4a90d9 rotation=30",)),
        Example("skew=30: the top and bottom edges tilt, the right side drops, the sides stay upright.",
                ("rectangle 40 390 150 460 #4a90d9 skew=30",)),
        Example("skew=-30: the right side rises.",
                ("rectangle 232 390 342 460 #4a90d9 skew=-30",)),
        Example("A box seen from above at an angle: two skewed rectangles for the sides, "
                "then a diamond for the top.",
                ("rectangle 420 412 480 472 #3b6ea5 skew=30",
                 "rectangle 480 412 540 472 #24507f skew=-30",
                 "diamond 420 360 540 430 #7fb0e0"), marks=False),
        Example("An outline round an ellipse.",
                ("ellipse 616 380 726 470 #f4d06f outline=#7a4a10 thickness=4",)),
    )),
    ("lines, trails and paint", (
        Example("A line runs from (x1, y1) to (x2, y2).",
                ("line 32 140 160 40 #333333 thickness=4",)),
        Example("bend=40@0 pulls the middle of a line 40 px towards heading 0 (up). The ends stay put.",
                ("line 224 130 352 130 #333333 thickness=4 bend=40@0",)),
        Example("taper=end thins a line from full width at (x1, y1) to a point at (x2, y2).",
                ("line 416 60 544 140 #333333 thickness=16 taper=end",)),
        Example("Compass headings, used by bend, trail legs and linear blends.", compass=True),
        Example("A trail walks from its start point, leg by leg: 60@90 is 60 px right, 50@0 is 50 px up.",
                ("trail 40 300 60@90 50@0 60@90 #333333 thickness=4",)),
        Example("The same trail with smooth=1: a curve through every corner.",
                ("trail 232 300 60@90 50@0 60@90 #333333 thickness=4 smooth=1",)),
        Example("Legs at 135 (down and right), 90 (right) and 45 (up and right), tapered.",
                ("trail 424 220 50@135 50@90 40@45 #333333 thickness=8 taper=end",)),
        Example("Layering and opacity: the second shape covers the first, and opacity=0.5 lets it show through.",
                ("rectangle 616 200 700 290 #4a90d9",
                 "ellipse 650 230 736 310 #e8a33d opacity=0.5"), marks=False),
        Example("linear ... 0: the blend travels from the bottom (first colour) to the top (last colour).",
                ("rectangle 32 370 160 470 linear #1d4e89 #cfe8ff 0",)),
        Example("linear ... 90: the blend travels from left to right.",
                ("rectangle 224 370 352 470 linear #1d4e89 #cfe8ff 90",)),
        Example("radial: from the centre (first colour) out to the edge (last colour).",
                ("ellipse 416 370 544 470 radial #ffffff #e8a33d",)),
        Example("spray=0.4 lands 40% of the paint as speckle; soft=8 feathers the edge.",
                ("rectangle 608 370 668 470 #333333 spray=0.4",
                 "ellipse 680 380 740 460 #e8a33d soft=8"), marks=False),
    )),
)


def _examples() -> list[tuple[int, int, Example]]:
    """(sheet index, number, example) for every numbered example, numbered across sheets."""
    out, n = [], 0
    for s, (_, examples) in enumerate(SHEETS):
        for ex in examples:
            if ex.shapes:
                n += 1
            out.append((s, n if ex.shapes else 0, ex))
    return out


def exemplar_text() -> str:
    """What is sent with the sheets: every example's number, caption and notation."""
    w, h = SHEET
    lines = [f"Exemplar: {len(SHEETS)} practice sheets, each {w} x {h} px. They are not your canvas "
             "and not the subject: they show how shapes written in the notation land. "
             "Each numbered example below was painted by exactly the shapes listed under it."]
    current = -1
    for s, n, ex in _examples():
        if s != current:
            current = s
            lines.append(f"\nSheet {s + 1}: {SHEETS[s][0]}.")
        if ex.shapes:
            lines.append(f"{n}. {ex.caption}")
            lines += [f"   {text}" for text in ex.shapes]
        else:
            lines.append(f"(Key) {ex.caption}")
    return "\n".join(lines)


def _font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


Cell = tuple[int, int, int, int]


def _label(d: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, anchor: str,
           colour=LABEL_COLOUR, size: int = 10, within: Cell | None = None) -> None:
    """Text on a light halo. within: nudged along x to stay inside that cell."""
    font = _font(size)
    x, y = xy
    left, top, right, bottom = d.textbbox((x, y), text, font=font, anchor=anchor)
    if within:
        x += max(within[0] + 3 - left, 0) + min(within[2] - 3 - right, 0)
        left, top, right, bottom = d.textbbox((x, y), text, font=font, anchor=anchor)
    d.rectangle((left - 2, top - 1, right + 2, bottom + 1), fill=HALO_COLOUR)
    d.text((x, y), text, fill=colour, font=font, anchor=anchor)


def _dashed_box(d: ImageDraw.ImageDraw, box: tuple[float, float, float, float]) -> None:
    x0, y0, x1, y1 = box
    for (ax, ay), (bx, by) in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
                               ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        length = max(abs(bx - ax), abs(by - ay))
        steps = int(length // (2 * DASH)) + 1
        for i in range(steps):
            t0, t1 = 2 * DASH * i / length, min((2 * DASH * i + DASH) / length, 1)
            d.line([(ax + (bx - ax) * t0, ay + (by - ay) * t0),
                    (ax + (bx - ax) * t1, ay + (by - ay) * t1)], fill=MARK_COLOUR, width=1)


def _point(d: ImageDraw.ImageDraw, p: tuple[float, float], centre: tuple[float, float],
           cell: Cell) -> None:
    """A dot at p, labelled with its coordinates: above or below it (away from
    centre), and running inwards along x, so labels stay inside their cell."""
    x, y = p
    right = x >= centre[0]
    below = y > centre[1]
    anchor = ("r" if right else "l") + ("t" if below else "b")
    _dot(d, p, (anchor, cell))


def _dot(d: ImageDraw.ImageDraw, p: tuple[float, float], place: tuple[str, Cell]) -> None:
    x, y = p
    anchor, cell = place
    d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=MARK_COLOUR, outline=(255, 255, 255))
    _label(d, (x, y + (6 if anchor[1] == "t" else -6)), f"({round(x)}, {round(y)})", anchor,
           colour=MARK_COLOUR, within=cell)


def _corner_anchors(corners: list[tuple[float, float]]) -> list[str]:
    """Where to label each corner of a trail: on the side away from its own legs,
    so a label never sits on the stroke or on a neighbour's label."""
    anchors = []
    for i, (x, y) in enumerate(corners):
        ox = oy = 0.0
        for j in (i - 1, i + 1):
            if 0 <= j < len(corners):
                nx, ny = corners[j][0] - x, corners[j][1] - y
                size = (nx * nx + ny * ny) ** 0.5 or 1
                ox, oy = ox - nx / size, oy - ny / size
        across = "l" if ox > 0.25 else "r" if ox < -0.25 else "m"
        anchors.append(across + ("b" if oy < 0 else "t"))
    return anchors


def _mark(d: ImageDraw.ImageDraw, shape: Shape, cell: Cell) -> None:
    """The labels for one shape: what its numbers mean on the page."""
    if shape.kind == "trail":
        corners = walk_points(shape)
        for p, anchor in zip(corners, _corner_anchors(corners)):
            _dot(d, p, (anchor, cell))
    elif shape.kind == "line":
        a, b = (shape.start.x, shape.start.y), (shape.end.x, shape.end.y)
        centre = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        _point(d, a, centre, cell)
        _point(d, b, centre, cell)
        if shape.bend:  # the curve's middle lands half way to the control point
            cx, cy = _bend_control(shape)
            middle = ((centre[0] + cx) / 2, (centre[1] + cy) / 2)
            _dot(d, middle, ("m" + ("t" if cy > centre[1] else "b"), cell))
    else:
        box = _box(shape)
        _dashed_box(d, box)
        centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        _point(d, (shape.start.x, shape.start.y), centre, cell)
        _point(d, (shape.end.x, shape.end.y), centre, cell)


def _compass(d: ImageDraw.ImageDraw, cell: Cell) -> None:
    x0, y0, x1, y1 = cell
    cx, cy, r = (x0 + x1) / 2, (y0 + y1) / 2 - 4, 50
    for heading, (dx, dy) in ((0, (0, -1)), (90, (1, 0)), (180, (0, 1)), (270, (-1, 0)),
                              (45, (0.707, -0.707)), (135, (0.707, 0.707)),
                              (225, (-0.707, 0.707)), (315, (-0.707, -0.707))):
        main = heading % 90 == 0
        length = r if main else r * 0.7
        ex, ey = cx + dx * length, cy + dy * length
        d.line([(cx, cy), (ex, ey)], fill=MARK_COLOUR if main else LABEL_COLOUR, width=2 if main else 1)
        anchor = {(0, -1): "mb", (1, 0): "lm", (0, 1): "mt", (-1, 0): "rm"}.get(
            (round(dx), round(dy)), "mm")
        lx, ly = cx + dx * (length + 6), cy + dy * (length + 6)
        _label(d, (lx, ly), str(heading), anchor, colour=MARK_COLOUR if main else LABEL_COLOUR)
    _label(d, (cx, y1 - 4), "compass headings", "mb")


def _sheet_png(index: int) -> bytes:
    """One sheet, painted and labelled, with no grid yet."""
    _, examples = SHEETS[index]
    w, h = SHEET
    canvas = Canvas("landscape")
    assert (canvas.width, canvas.height) == SHEET
    shapes: list[tuple[Example, list[Shape]]] = []
    for ex in examples:
        good, errors, _ = check_batch(list(ex.shapes), w, h)
        if errors:  # a bug in the examples: tests catch it before it ships
            raise ValueError(f"exemplar example is invalid: {errors}")
        texts = [t for t, _ in good]
        canvas.add([s for _, s in good], texts)
        shapes.append((ex, [s for _, s in good]))

    img = Image.open(io.BytesIO(render_png(canvas.to_svg()))).convert("RGBA")
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cw, ch = w / COLUMNS, h / ROWS
    numbers = {id(ex): n for s, n, ex in _examples() if s == index}
    for i, (ex, painted) in enumerate(shapes):
        col, row = i % COLUMNS, i // COLUMNS
        cell = (round(col * cw), round(row * ch), round((col + 1) * cw), round((row + 1) * ch))
        if ex.compass:
            _compass(d, cell)
            continue
        if ex.marks:
            for shape in painted:
                _mark(d, shape, cell)
        # Top-right, clear of the overlay grid's numbers (cell edges are on grid lines).
        _label(d, (cell[2] - 5, cell[1] + 4), str(numbers[id(ex)]), "rt", size=13)
    img = Image.alpha_composite(img, layer)
    # Thin lines between the cells, so each example reads as its own.
    d = ImageDraw.Draw(img)
    for c in range(1, COLUMNS):
        d.line([(round(c * cw), 0), (round(c * cw), h)], fill=(210, 210, 210), width=1)
    for r in range(1, ROWS):
        d.line([(0, round(r * ch)), (w, round(r * ch))], fill=(210, 210, 210), width=1)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


@lru_cache(maxsize=len(GRID_MODES))
def exemplar_views(grid: str = DEFAULT_GRID) -> tuple[bytes, ...]:
    """Every sheet as the agent sees it, in the drawing's grid mode."""
    if grid not in GRID_MODES:
        grid = DEFAULT_GRID
    return tuple(agent_view(_sheet_png(i), *SHEET, grid) for i in range(len(SHEETS)))
