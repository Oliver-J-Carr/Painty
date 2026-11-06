"""The benchmark: cards for Painty to reproduce, so models and settings can be compared.

Each card is a picture written in Painty's own notation and painted by the
real renderer, like the exemplar. So every part of it can be reproduced
exactly with the paint tool (a perfect copy is possible, and any gap is the
model's), we have the true shapes to score against, and a card can never
disagree with the renderer.

A card is a set of items, each in its own cell. An item is one thing being
tested (a rotated triangle, an isometric cube, a letter); the scorer
(app/benchmark_score.py) compares the painting with the card cell by cell.
Cells hold their shapes with a margin, so a copy that lands a little off
is still mostly inside its cell.

The cards use none of the exemplar's examples (a test checks), so Painty
can't copy a line it was shown. The levels: basic (plain boxes and circles),
intermediate (rotation, skew, blends), solids (3D shapes from flat ones),
effects (shading, opacity, soft edges, spray, tapers, bends) and letters
(text made from lines), then a combined card with a little of each.

A painting made from a card saves the card's definition (benchmark.json) with
it, so it is scored against the card as it was, even after the cards change.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache

from .canvas import CANVAS_SHAPES, Canvas, check_batch
from .view import render_png

LEVELS = ("basic", "intermediate", "solids", "effects", "letters")

# Sent as the prompt (it can be edited in the dialog), so runs are comparable.
BENCHMARK_PROMPT = ("Reproduce this image as exactly as you can: the same shapes, in the same "
                    "positions and sizes, with the same colours and effects.")


@dataclass(frozen=True)
class Item:
    name: str                        # what it is, shown in the score table
    level: str                       # one of LEVELS
    box: tuple[int, int, int, int]   # x0, y0, x1, y1: the cell the scorer compares
    shapes: tuple[str, ...]          # painted in order, written in the notation
    blur: int = 0                    # px: the scorer blurs both images first (for spray,
                                     # whose speckle depends on the shape's number)


@dataclass(frozen=True)
class Card:
    name: str
    title: str
    about: str
    shape: str                       # a CANVAS_SHAPES key
    items: tuple[Item, ...]

    @property
    def size(self) -> tuple[int, int]:
        return CANVAS_SHAPES[self.shape]

    @property
    def shape_count(self) -> int:
        return sum(len(item.shapes) for item in self.items)


def _cells(columns: int, rows: int, width: int, height: int) -> list[tuple[int, int, int, int]]:
    """The cells of an even grid, in reading order."""
    cw, ch = width / columns, height / rows
    return [(round(c * cw), round(r * ch), round((c + 1) * cw), round((r + 1) * ch))
            for r in range(rows) for c in range(columns)]


def _items(cells: list[tuple[int, int, int, int]], *specs) -> tuple[Item, ...]:
    """Items from (name, level, shapes[, blur]) specs, one per cell in order."""
    out = []
    for cell, (name, level, shapes, *blur) in zip(cells, specs, strict=True):
        out.append(Item(name, level, cell, tuple(shapes), blur[0] if blur else 0))
    return tuple(out)


SQUARE_6 = _cells(3, 2, 512, 512)
LANDSCAPE_8 = _cells(4, 2, 768, 512)
LANDSCAPE_6 = _cells(3, 2, 768, 512)
LANDSCAPE_3 = _cells(3, 1, 768, 512)

CARDS: dict[str, Card] = {card.name: card for card in (
    Card("basic", "Basic shapes",
         "Rectangles and circles of different sizes, a thin bar, an outline and a line.",
         "square", _items(
             SQUARE_6,
             ("tall rectangle", "basic", ["rectangle 30 34 140 222 #2f6db5"]),
             ("large circle", "basic", ["ellipse 194 58 318 182 #e2543b"]),
             ("small square and a dot", "basic", ["rectangle 372 60 432 120 #f2b632",
                                                  "ellipse 450 164 474 188 #3a3a3a"]),
             ("wide thin bar", "basic", ["rectangle 18 362 152 386 #2e9e6b"]),
             ("tall ellipse", "basic", ["ellipse 226 290 286 474 #8a5cc4"]),
             ("outlined square and a line", "basic",
              ["rectangle 380 290 470 380 #ffe7c2 outline=#a0522d thickness=6",
               "line 362 456 490 420 #1b1b1b thickness=5"]),
         )),
    Card("intermediate", "Rotation, skew and blends",
         "Triangles, a rectangle, a diamond and an ellipse turned to odd angles, two skews "
         "and a diagonal blend.",
         "landscape", _items(
             LANDSCAPE_8,
             ("triangle at 135", "intermediate", ["triangle 40 72 152 184 #d9452b rotation=135"]),
             ("thin triangle at 300", "intermediate", ["triangle 268 60 308 200 #2b8fd9 rotation=300"]),
             ("rectangle at 20", "intermediate", ["rectangle 420 84 540 164 #6b4fbf rotation=20"]),
             ("diamond at 15", "intermediate", ["diamond 620 70 720 190 #e0a21b rotation=15"]),
             ("skew -20", "intermediate", ["rectangle 40 340 150 420 #1f9e89 skew=-20"]),
             ("skew 45", "intermediate", ["rectangle 238 336 338 384 #c43c7a skew=45"]),
             ("blend at 225", "intermediate", ["rectangle 424 310 536 450 linear #ffd23f #ee4266 225"]),
             ("ellipse at 60", "intermediate", ["ellipse 610 352 734 408 #3d5a80 rotation=60"]),
         )),
    Card("solids", "Solids",
         "An isometric cube, a cylinder and a cone, built from flat shapes, skews and blends.",
         "landscape", _items(
             LANDSCAPE_3,
             ("isometric cube", "solids", ["rectangle 58 211 128 321 #d98c2b skew=30",
                                           "rectangle 128 211 198 321 #a35e14 skew=-30",
                                           "diamond 58 150 198 231 #f5c26b"]),
             ("cylinder", "solids", ["ellipse 316 330 452 380 #1d6f5f",
                                     "rectangle 316 180 452 355 linear #1d6f5f #57c2a8 #1d6f5f 90",
                                     "ellipse 316 155 452 205 #8fe0cb"]),
             ("cone", "solids", ["ellipse 566 330 714 380 #4b1f66",
                                 "triangle 566 140 714 355 linear #4b1f66 #b77fd6 #4b1f66 90"]),
         )),
    Card("effects", "Effects",
         "A shaded sphere, see-through circles, a glow, a sprayed patch, a tapered stroke and "
         "a bent arc.",
         "landscape", _items(
             LANDSCAPE_6,
             ("shaded sphere", "effects", ["ellipse 56 200 200 232 #000000 opacity=0.25 soft=6",
                                           "ellipse 58 48 198 188 radial #f0707a #7a1020",
                                           "ellipse 84 72 134 112 #ffffff opacity=0.6 soft=8"]),
             ("see-through circles", "effects", ["ellipse 302 46 402 146 #ff3b30 opacity=0.5",
                                                 "ellipse 362 46 462 146 #007aff opacity=0.5",
                                                 "ellipse 332 102 432 202 #34c759 opacity=0.5"]),
             ("glow", "effects", ["ellipse 590 68 690 168 #ffd84d soft=16",
                                  "ellipse 620 98 660 138 #fff6c8"]),
             ("sprayed patch", "effects", ["rectangle 46 304 210 452 #5b3a29 spray=0.35"], 4),
             ("tapered stroke", "effects", ["line 300 456 468 304 #1a1a1a thickness=22 taper=both"]),
             ("bent arc", "effects", ["line 562 450 718 450 #c0392b thickness=8 bend=110@0"]),
         )),
    Card("letters", "Letters",
         "HAZE in straight lines, then SOUP, whose letters need curves: a smooth trail, an outlined "
         "ellipse and bent lines.",
         "landscape", _items(
             LANDSCAPE_8,
             ("H", "letters", ["line 46 64 46 196 #22313f thickness=10",
                               "line 146 64 146 196 #22313f thickness=10",
                               "line 46 130 146 130 #22313f thickness=10"]),
             ("A", "letters", ["line 236 196 288 64 #22313f thickness=10",
                               "line 288 64 340 196 #22313f thickness=10",
                               "line 256 150 320 150 #22313f thickness=10"]),
             ("Z", "letters", ["line 430 64 530 64 #22313f thickness=10",
                               "line 530 64 430 196 #22313f thickness=10",
                               "line 430 196 530 196 #22313f thickness=10"]),
             ("E", "letters", ["line 626 64 626 196 #22313f thickness=10",
                               "line 626 64 722 64 #22313f thickness=10",
                               "line 626 130 704 130 #22313f thickness=10",
                               "line 626 196 722 196 #22313f thickness=10"]),
             ("S", "letters", ["trail 138 336 40@290 47@239 52@137 51@132 46@232 46@288 #b0413e "
                               "thickness=10 smooth=1"]),
             ("O", "letters", ["ellipse 236 320 336 448 #ffffff outline=#b0413e thickness=10"]),
             ("U", "letters", ["line 434 320 434 400 #b0413e thickness=10",
                               "line 526 320 526 400 #b0413e thickness=10",
                               "line 434 400 526 400 #b0413e thickness=10 bend=46@180"]),
             ("P", "letters", ["line 630 320 630 448 #b0413e thickness=10",
                               "line 630 320 630 390 #b0413e thickness=10 bend=50@90"]),
         )),
    Card("combined", "Combined",
         "A little of every level on one card: overlapping basics, a turned triangle, a skew, "
         "a cube, a sphere, a stroke, two letters and see-through paint.",
         "landscape", _items(
             LANDSCAPE_8,
             ("square under a circle", "basic", ["rectangle 30 46 120 136 #3a7d44",
                                                 "ellipse 96 112 168 184 #f4a259"]),
             ("triangle at 210", "intermediate", ["triangle 238 62 338 172 #5e60ce rotation=210"]),
             ("skew -35", "intermediate", ["rectangle 430 92 530 158 #ef476f skew=-35"]),
             ("isometric cube", "solids", ["rectangle 622 113 672 183 #2fb8a6 skew=30",
                                           "rectangle 672 113 722 183 #1b7a6e skew=-30",
                                           "diamond 622 70 722 128 #8fe3d6"]),
             ("shaded sphere", "effects", ["ellipse 40 430 170 456 #000000 opacity=0.2 soft=5",
                                           "ellipse 50 300 160 410 radial #9ad1ff #1b4f8a",
                                           "ellipse 70 318 108 350 #ffffff opacity=0.55 soft=6"]),
             ("bent tapered stroke", "effects", ["line 220 450 356 320 #2d3142 thickness=18 "
                                                 "taper=end bend=36@45"]),
             ("X and Y", "letters", ["line 412 320 470 440 #7b2d26 thickness=9",
                                     "line 470 320 412 440 #7b2d26 thickness=9",
                                     "line 496 320 526 380 #7b2d26 thickness=9",
                                     "line 556 320 526 380 #7b2d26 thickness=9",
                                     "line 526 380 526 440 #7b2d26 thickness=9"]),
             ("glow under see-through paint", "effects", ["ellipse 600 312 696 408 #ffbe0b soft=12",
                                                          "ellipse 646 362 740 456 #8338ec opacity=0.5"]),
         )),
)}


def card_canvas(name: str) -> Canvas:
    """The card painted on a canvas of its shape. Raises KeyError for an unknown card,
    ValueError if a shape is invalid (a bug in the card: the tests catch it)."""
    card = CARDS[name]
    canvas = Canvas(card.shape)
    for item in card.items:
        good, errors, _ = check_batch(list(item.shapes), canvas.width, canvas.height)
        if errors:
            raise ValueError(f"benchmark card {name}, {item.name}: {errors}")
        canvas.add([s for _, s in good], [t for t, _ in good])
    return canvas


@lru_cache(maxsize=len(CARDS))
def card_png(name: str) -> bytes:
    """The card as a clean PNG at canvas size: what Painty is given to reproduce."""
    return render_png(card_canvas(name).to_svg())


def card_text(name: str) -> str:
    """The card's notation, item by item, for checking by eye."""
    card = CARDS[name]
    w, h = card.size
    lines = [f"{card.title} ({card.name}, {card.shape} {w} x {h}): {card.about}"]
    for item in card.items:
        lines.append(f"\n{item.name} [{item.level}], cell {item.box}")
        lines += [f"   {text}" for text in item.shapes]
    return "\n".join(lines) + "\n"


def card_json(name: str) -> dict:
    """The card as plain data, saved with each painting made from it."""
    card = CARDS[name]
    w, h = card.size
    return {"name": card.name, "title": card.title, "about": card.about, "shape": card.shape,
            "width": w, "height": h,
            "items": [{**asdict(item), "box": list(item.box), "shapes": list(item.shapes)}
                      for item in card.items]}


def card_list() -> list[dict]:
    """Every card, for the New painting dialog."""
    return [{"name": c.name, "title": c.title, "about": c.about, "shape": c.shape,
             "shapes": c.shape_count} for c in CARDS.values()]
