"""Score a painting made from a benchmark card against the card.

The card's own pixels (the painting's reference.png) are compared with the
painting (painting.svg rendered at canvas size), item by item, in each item's
cell. A pixel has paint when any channel differs from white by more than
PAINT_THRESHOLD. Edges get EDGE_TOLERANCE px of grace: a pixel only counts as
missing or extra if the other image has no paint that close, and its colour only
counts as wrong if it doesn't fit the colours that close in the other image (an
anti-aliased edge is a blend of them). Without this a thin stroke, nearly all
edge, drops to about 66% from a 1 px shift that a large shape barely notices.
For each item:

    score    pixels where the paint is in the right place and the colours match
             (within COLOUR_TOLERANCE), out of the pixels where either has paint.
             The headline number: 1 is a perfect copy.
    shape    the same without the colour check.
    colour   how close the colours are where both have paint (1 = the same).
    offset   the shift (dx, dy) that best lines the copy up with the card,
             within MAX_SHIFT px: positive dx means the copy is to the right.
    shifted  the shape score at that offset. A high shifted score with a low
             shape score means the right shape in the wrong place.

The whole card gets the mean item score, a global offset (the shift that
best lines up the whole painting) and a whole-canvas score that includes any
stray paint outside the cells.

A painting made from a card is scored when it's saved: the result goes in its
meta.json (benchmark_score, a record()) and the picture of the difference in
benchmark-diff.png, for the finished card and the gallery. --rescore writes them
again, for paintings saved before scoring or after the scorer changes (bump
SCORER_VERSION when it does, so a stale score can be told apart).

Pillow only. Run it from the repo root (or in the container):

    python -m app.benchmark_score                 every benchmark painting, one line each
    python -m app.benchmark_score <id> [--diff out.png] [--json]
    python -m app.benchmark_score [<id>] --rescore  save the score with the painting(s) again
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageStat

from .view import render_png

PAINT_THRESHOLD = 24     # 0-255: a pixel further than this from white has paint
COLOUR_TOLERANCE = 40    # 0-255: the most any channel may differ for colours to match
EDGE_TOLERANCE = 1       # px: how far off an edge may be before it counts as wrong
MAX_SHIFT = 32           # px: the widest offset searched
COARSE = 4               # px: the first pass of the offset search
SCORER_VERSION = 2       # saved with each score: bump it when the scoring changes

Box = tuple[int, int, int, int]


@dataclass
class ItemScore:
    name: str
    level: str
    score: float
    shape: float
    colour: float | None     # None when the copy has no paint in the cell
    offset: tuple[int, int] | None
    shifted: float


@dataclass
class CardScore:
    card: str
    score: float             # the mean of the item scores
    canvas: float            # the whole canvas, stray paint included
    offset: tuple[int, int] | None
    items: list[ItemScore]


# ------------------------------------------------------------------ masks
def _largest_channel(img: Image.Image) -> Image.Image:
    r, g, bl = img.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), bl)


def _distance(a: Image.Image, b: Image.Image) -> Image.Image:
    """Per pixel, the largest channel difference between two RGB images (mode L)."""
    return _largest_channel(ImageChops.difference(a, b))


def _outside(a: Image.Image, b: Image.Image) -> Image.Image:
    """Per pixel, how far a's colour is outside the range of b's colours within
    EDGE_TOLERANCE px (mode L). Inside the range is 0."""
    size = 2 * EDGE_TOLERANCE + 1
    low, high = b.filter(ImageFilter.MinFilter(size)), b.filter(ImageFilter.MaxFilter(size))
    return _largest_channel(ImageChops.lighter(ImageChops.subtract(low, a), ImageChops.subtract(a, high)))


def _colour_gap(a: Image.Image, b: Image.Image) -> Image.Image:
    """Per pixel, how far apart two RGB images' colours are, allowing for edges that
    are up to EDGE_TOLERANCE px off. 0 for the same picture."""
    return ImageChops.darker(_outside(a, b), _outside(b, a))


def _over(img: Image.Image, threshold: int) -> Image.Image:
    return img.point(lambda v: 255 if v > threshold else 0)


def _count(mask: Image.Image) -> int:
    return mask.histogram()[255]


def _paint_mask(img: Image.Image) -> Image.Image:
    white = Image.new("RGB", img.size, (255, 255, 255))
    return _over(_distance(img, white), PAINT_THRESHOLD)


def _iou(a: Image.Image, b: Image.Image) -> float:
    union = _count(ImageChops.lighter(a, b))
    return _count(ImageChops.darker(a, b)) / union if union else 1.0


def _grown(mask: Image.Image) -> Image.Image:
    return mask.filter(ImageFilter.MaxFilter(2 * EDGE_TOLERANCE + 1))


def _near(a: Image.Image, b: Image.Image) -> Image.Image:
    """The paint in either mask that has paint in the other within EDGE_TOLERANCE px."""
    return ImageChops.lighter(ImageChops.darker(a, _grown(b)), ImageChops.darker(b, _grown(a)))


def _shape_score(a: Image.Image, b: Image.Image) -> float:
    union = _count(ImageChops.lighter(a, b))
    return _count(_near(a, b)) / union if union else 1.0


def _crop(mask: Image.Image, box: Box, dx: int = 0, dy: int = 0) -> Image.Image:
    """mask cropped to box moved by (dx, dy). Outside the image is empty (black)."""
    x0, y0, x1, y1 = box
    return mask.crop((x0 + dx, y0 + dy, x1 + dx, y1 + dy))


def best_offset(target: Image.Image, copy: Image.Image, box: Box,
                reach: int = MAX_SHIFT) -> tuple[tuple[int, int] | None, float]:
    """The shift of the copy that best overlaps the target's paint in box, and the
    shape score there. A coarse grid first, then every pixel round the best. Ties go
    to the smaller shift. The search uses exact overlap, with no edge tolerance, so
    the offset stays precise. None when the copy has no paint near the box."""
    want = _crop(target, box)

    def score(d: tuple[int, int]) -> tuple[float, int]:
        return _iou(want, _crop(copy, box, *d)), -(d[0] * d[0] + d[1] * d[1])

    coarse = [(dx, dy) for dx in range(-reach, reach + 1, COARSE)
              for dy in range(-reach, reach + 1, COARSE)]
    best = max(coarse, key=score)
    fine = [(best[0] + dx, best[1] + dy) for dx in range(-COARSE + 1, COARSE)
            for dy in range(-COARSE + 1, COARSE)
            if abs(best[0] + dx) <= reach and abs(best[1] + dy) <= reach]
    best = max(fine, key=score)
    if score(best)[0] == 0:
        return None, 0.0
    return best, _shape_score(want, _crop(copy, box, *best))


# ------------------------------------------------------------------ scoring
def _region_scores(card: Image.Image, copy: Image.Image, box: Box, blur: int = 0):
    """score, shape and colour for one region of two same-size RGB images."""
    a, b = card.crop(box), copy.crop(box)
    if blur:
        a, b = a.filter(ImageFilter.BoxBlur(blur)), b.filter(ImageFilter.BoxBlur(blur))
    pa, pb = _paint_mask(a), _paint_mask(b)
    both = ImageChops.darker(pa, pb)
    near = _near(pa, pb)
    union = _count(ImageChops.lighter(pa, pb))
    gap = _colour_gap(a, b)
    agree = ImageChops.darker(near, gap.point(lambda v: 255 if v <= COLOUR_TOLERANCE else 0))
    score = _count(agree) / union if union else 1.0
    shape = _count(near) / union if union else 1.0
    colour = None
    if _count(both):
        colour = 1 - ImageStat.Stat(gap, both).mean[0] / 255
    return score, shape, colour


def score_images(card_img: Image.Image, copy_img: Image.Image, card: dict) -> CardScore:
    """Score a copy against a card (both RGB, canvas size). card: benchmark.json data."""
    card_img, copy_img = card_img.convert("RGB"), copy_img.convert("RGB")
    if copy_img.size != card_img.size:
        copy_img = copy_img.resize(card_img.size, Image.LANCZOS)
    mask_card, mask_copy = _paint_mask(card_img), _paint_mask(copy_img)
    items = []
    for item in card["items"]:
        box = tuple(item["box"])
        score, shape, colour = _region_scores(card_img, copy_img, box, item.get("blur", 0))
        offset, shifted = best_offset(mask_card, mask_copy, box)
        items.append(ItemScore(item["name"], item["level"], round(score, 3), round(shape, 3),
                               None if colour is None else round(colour, 3), offset,
                               round(shifted, 3)))
    whole = (0, 0, *card_img.size)
    canvas, _, _ = _region_scores(card_img, copy_img, whole)
    offset, _ = best_offset(mask_card, mask_copy, whole)
    mean = sum(i.score for i in items) / len(items) if items else 0.0
    return CardScore(card.get("name", "?"), round(mean, 3), round(canvas, 3), offset, items)


def score_painting(card_png: bytes, svg: str, card: dict) -> tuple[CardScore, bytes]:
    """Score a painting (its SVG) against a card (its PNG and benchmark.json data).
    Returns the score and the difference picture (diff_png)."""
    card_img = Image.open(io.BytesIO(card_png)).convert("RGB")
    copy_img = Image.open(io.BytesIO(render_png(svg))).convert("RGB")
    result = score_images(card_img, copy_img, card)
    return result, diff_png(card_img, copy_img, card, result)


def record(result: CardScore) -> dict:
    """The score as saved in meta.json (benchmark_score)."""
    return {**asdict(result), "scorer": SCORER_VERSION}


def _folder_inputs(folder: Path) -> tuple[bytes, str, dict]:
    card_file = folder / "benchmark.json"
    if not card_file.is_file():
        raise ValueError(f"{folder.name} wasn't painted from a benchmark card.")
    return ((folder / "reference.png").read_bytes(),
            (folder / "painting.svg").read_text(encoding="utf-8"),
            json.loads(card_file.read_text(encoding="utf-8")))


def score_folder(folder: Path) -> CardScore:
    """Score a saved painting. Raises ValueError if it wasn't made from a benchmark card."""
    return score_folder_with_diff(folder)[0]


def score_folder_with_diff(folder: Path) -> tuple[CardScore, bytes]:
    return score_painting(*_folder_inputs(folder))


# ------------------------------------------------------------------ the diff picture
def diff_png(card_img: Image.Image, copy_img: Image.Image, card: dict, result: CardScore) -> bytes:
    """Card | painting | difference, side by side, each cell outlined and labelled
    with its item's score. In the difference, every pixel that isn't grey counts
    against the score: red is paint the copy is missing, blue is paint the copy has
    that the card doesn't, and amber is paint in the right place in the wrong colour."""
    a = card_img.convert("RGB")
    b = copy_img.convert("RGB").resize(a.size)
    pa, pb = _paint_mask(a), _paint_mask(b)
    w, h = a.size
    diff = Image.new("RGB", a.size, (255, 255, 255))
    near = _near(pa, pb)
    diff.paste((200, 200, 200), mask=near)
    diff.paste((235, 160, 30), mask=ImageChops.darker(near, _over(_colour_gap(a, b), COLOUR_TOLERANCE)))
    diff.paste((220, 50, 50), mask=ImageChops.subtract(pa, _grown(pb)))
    diff.paste((50, 100, 220), mask=ImageChops.subtract(pb, _grown(pa)))
    gap = 12
    out = Image.new("RGB", (w * 3 + gap * 2, h + 24), (255, 255, 255))
    font = ImageFont.load_default(size=12)
    d = ImageDraw.Draw(out)
    for i, (img, label) in enumerate(((a, "card"), (b, "painting"), (diff, "difference"))):
        x = i * (w + gap)
        out.paste(img, (x, 24))
        d.text((x + 4, 5), label, fill=(40, 40, 40), font=font)
        for item, s in zip(card["items"], result.items):
            x0, y0, x1, y1 = item["box"]
            d.rectangle((x + x0, 24 + y0, x + x1 - 1, 24 + y1 - 1), outline=(214, 40, 110))
            if i == 2:
                d.text((x + x0 + 4, 24 + y0 + 3), f"{s.name}: {round(100 * s.score)}%",
                       fill=(214, 40, 110), font=font)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ------------------------------------------------------------------ the command line
def _offset(o: tuple[int, int] | None) -> str:
    return "-" if o is None else f"{o[0]:+d},{o[1]:+d}"


def _table(result: CardScore, meta: dict) -> str:
    s = meta.get("settings") or {}
    lines = [f"{meta.get('id', '')}  card {result.card}  model {s.get('model', '?')}"
             f"  effort {s.get('reasoning_effort') or 'default'}  vision {'on' if s.get('vision') else 'off'}"
             f"  grid {s.get('grid', 'frame')}  exemplar {'on' if s.get('exemplar') else 'off'}",
             f"shapes {meta.get('shapes_used', '?')} of {meta.get('shapes_budget', '?')}  "
             f"looks {meta.get('looks_used', '?')} of {meta.get('looks_budget', '?')}", "",
             f"{'item':<30} {'level':<13} {'score':>6} {'shape':>6} {'colour':>7} {'offset':>8} {'shifted':>8}"]
    for i in result.items:
        colour = "-" if i.colour is None else f"{i.colour:.2f}"
        lines.append(f"{i.name:<30} {i.level:<13} {i.score:6.2f} {i.shape:6.2f} {colour:>7} "
                     f"{_offset(i.offset):>8} {i.shifted:8.2f}")
    lines += ["", f"score {result.score:.3f}  whole canvas {result.canvas:.3f}  "
                  f"offset {_offset(result.offset)}"]
    return "\n".join(lines)


def _summary(gallery: Path) -> str:
    rows = []
    for meta_path in sorted(gallery.glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not meta.get("benchmark"):
            continue
        try:
            result = score_folder(meta_path.parent)
        except (OSError, ValueError) as exc:
            rows.append(f"{meta_path.parent.name}: can't score ({exc})")
            continue
        s = meta.get("settings") or {}
        rows.append(f"{meta_path.parent.name[:15]:<15} {result.card:<12} {s.get('model', '?'):<16} "
                    f"{(s.get('reasoning_effort') or 'default'):<8} {'on' if s.get('vision') else 'off':<6} "
                    f"{s.get('grid', 'frame'):<7} {'on' if s.get('exemplar') else 'off':<8} "
                    f"{meta.get('shapes_used', 0):>6} {meta.get('looks_used', 0):>5} "
                    f"{result.score:6.3f} {_offset(result.offset):>8}")
    if not rows:
        return f"No benchmark paintings in {gallery.resolve()}."
    head = (f"{'painted':<15} {'card':<12} {'model':<16} {'effort':<8} {'vision':<6} {'grid':<7} "
            f"{'exemplar':<8} {'shapes':>6} {'looks':>5} {'score':>6} {'offset':>8}")
    return "\n".join([head, *rows])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.benchmark_score",
                                     description="Score paintings made from benchmark cards.")
    parser.add_argument("painting", nargs="?",
                        help="a gallery id or folder; leave out to list every benchmark painting")
    parser.add_argument("--diff", type=Path, help="write card | painting | difference to this PNG")
    parser.add_argument("--json", action="store_true", help="print the score as JSON")
    parser.add_argument("--rescore", action="store_true",
                        help="save the score and difference picture with the painting again "
                             "(every benchmark painting, if none is named)")
    args = parser.parse_args(argv)

    from .config import settings  # here, so importing this module reads no environment
    from .gallery import Gallery
    gallery = settings.data_dir / "gallery"
    if args.rescore:
        folders = ([Path(args.painting) if Path(args.painting).is_dir() else gallery / args.painting]
                   if args.painting else [p.parent for p in sorted(gallery.glob("*/benchmark.json"))])
        failed = 0
        for folder in folders:
            try:
                result, diff = score_folder_with_diff(folder)
                Gallery(folder.parent).save_score(folder.name, record(result), diff)
                print(f"{folder.name}: {round(100 * result.score)}%")
            except (OSError, ValueError) as exc:
                print(f"{folder.name}: can't score ({exc})", file=sys.stderr)
                failed += 1
        if not folders:
            print(f"No benchmark paintings in {gallery.resolve()}.")
        return 1 if failed else 0
    if not args.painting:
        print(_summary(gallery))
        return 0
    folder = Path(args.painting)
    if not folder.is_dir():
        folder = gallery / args.painting
    try:
        result, diff = score_folder_with_diff(folder)
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Can't score {args.painting}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(asdict(result), indent=1))
    else:
        print(_table(result, meta))
    if args.diff:
        args.diff.write_bytes(diff)
        if not args.json:
            print(f"diff written to {args.diff}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
