"""The card: a painting, framed, with its details underneath, as one PNG for sharing.

The painting hangs in a frame on the cutting mat (the app's own backdrop), with a
paper label under it like a gallery's: the title Painty gave it, the model, the
prompt, the shapes and cost, then under another line the tokens in and out. A
painting made from a reference image shows it small over the frame's corner. The card is as wide as the framed
painting, so square, landscape and portrait paintings each get a card of their
own shape, and the label's text wraps to fit. Long prompts are cut short with
an ellipsis, so the card never grows past a few lines of prompt.

Drawn with Pillow from the saved painting.png, so it needs no browser, and an
imported painting gets a card too. The font is Pillow's own (no font files to ship).
"""

from __future__ import annotations

import io
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .gallery import GALLERY_ZOOM

MAT = (45, 94, 74)          # --mat
GRID_FINE = (52, 101, 81)   # the mat's 16 px lines, already blended
GRID_MAJOR = (68, 115, 94)  # the mat's 64 px lines
PAPER = (247, 247, 242)     # --paper
PAPER_EDGE = (220, 221, 212)
GRAPHITE = (35, 40, 42)
GRAPHITE_SOFT = (93, 102, 104)
CADMIUM = (244, 180, 0)
FRAME_LIGHT = (70, 78, 80)  # the frame's bevel

PAD = 72       # mat round the frame
FRAME = 22     # the frame's moulding
MOUNT = 36     # the paper mount inside the frame
LABEL_PAD = 40
RADIUS = 10
PROMPT_LINES = 4


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def _usd(n: float) -> str:
    """$1.24, $0.042, $0.0037: like the app."""
    if n >= 1:
        return f"${n:.2f}"
    if n == 0:
        return "$0.00"
    if n < 0.0001:
        return "under $0.0001"
    return f"${n:.2f}" if n >= 0.1 else f"${n:#.2g}"  # # keeps a trailing 0: $0.040


def cost_text(cost: dict | None) -> str:
    """What OpenRouter billed, or the estimate (with a ~), or a dash when there's no price."""
    cost = cost or {}
    if isinstance(cost.get("billed"), (int, float)):
        return _usd(cost["billed"])
    if isinstance(cost.get("usd"), (int, float)):
        return f"~{_usd(cost['usd'])}"
    return "–"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int, max_lines: int) -> list[str]:
    """Words in lines no wider than width, at most max_lines, the last one ending in …
    if the text didn't fit. A word too long for a line is broken anywhere."""
    lines: list[str] = []
    line = ""
    words = text.split()
    i = 0
    while i < len(words):
        word = words[i]
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width:
            line = trial
            i += 1
            continue
        if not line:  # one word wider than the line: break it
            cut = max(1, next((k for k in range(len(word), 0, -1)
                               if draw.textlength(word[:k], font=font) <= width), 1))
            line, words[i] = word[:cut], word[cut:]
        lines.append(line)
        line = ""
        if len(lines) == max_lines:
            break
    if line and len(lines) < max_lines:
        lines.append(line)
    if i < len(words) and lines:  # cut short: end the last line with an ellipsis
        last = lines[-1]
        while last and draw.textlength(f"{last}…", font=font) > width:
            last = last[:-1]
        lines[-1] = f"{last.rstrip(' ,.;:')}…"
    return lines


def _mat(width: int, height: int) -> Image.Image:
    """The cutting mat: fine lines every 16 px, stronger ones every 64."""
    im = Image.new("RGB", (width, height), MAT)
    d = ImageDraw.Draw(im)
    for step, colour in ((16, GRID_FINE), (64, GRID_MAJOR)):
        for x in range(0, width, step):
            d.line([(x, 0), (x, height)], fill=colour)
        for y in range(0, height, step):
            d.line([(0, y), (width, y)], fill=colour)
    return im


def _brand(d: ImageDraw.ImageDraw, x: int, y: int) -> None:
    """Painty's three shapes and its name, from (x, y) at the top left."""
    s = 26
    d.ellipse([x, y, x + s, y + s], fill=(232, 72, 59))
    d.polygon([(x + s + 10 + s / 2, y), (x + 2 * s + 10, y + s), (x + s + 10, y + s)], fill=CADMIUM)
    d.rectangle([x + 2 * s + 20, y, x + 3 * s + 20, y + s], fill=(47, 127, 216))
    d.text((x + 3 * s + 36, y + s / 2), "Painty", font=_font(34), fill=CADMIUM, anchor="lm",
           stroke_width=1, stroke_fill=CADMIUM)


STAT_H = 74      # a stat: its name, then its value
TOKENS_H = 108   # the tokens: a stat and its detail line
REFERENCE_SIDE = 0.28  # the reference's longer side, as a share of the painting's shorter side


def token_groups(meta: dict) -> list[tuple[str, str, str]]:
    """Tokens in and out, each with its detail like the gallery's: the share of input
    served from the cache, and how much of the output was reasoning. Older paintings
    have no cached or reasoning counts, so they get no detail."""
    tin, tout = meta.get("input_tokens") or 0, meta.get("output_tokens") or 0
    cached, reasoning = meta.get("cached_tokens"), meta.get("reasoning_tokens")
    cache = f"{round(100 * cached / tin)}% cached" if isinstance(cached, int) and tin else ""
    thinking = f"{reasoning:,} reasoning" if reasoning else ""
    return [("Tokens in", f"{tin:,}", cache), ("Tokens out", f"{tout:,}", thinking)]


def _stat(d: ImageDraw.ImageDraw, x: float, y: float, name: str, value: str) -> None:
    d.text((x, y), name.upper(), font=_font(20), fill=GRAPHITE_SOFT)
    d.text((x, y + 28), value, font=_font(36), fill=GRAPHITE, stroke_width=1, stroke_fill=GRAPHITE)


def _reference(card: Image.Image, folder: Path, meta: dict, right: int, bottom: int, short: int) -> None:
    """The reference image, small, over the frame's bottom right corner (as on a gallery
    tile), with a white border and a shadow. Nothing if there wasn't one."""
    path = folder / "reference.png"
    if not meta.get("reference") or not path.is_file():
        return
    try:
        with Image.open(path) as im:
            ref = im.convert("RGB")
    except OSError:
        return
    scale = REFERENCE_SIDE * short / max(ref.size)
    ref = ref.resize((max(1, round(ref.width * scale)), max(1, round(ref.height * scale))), Image.LANCZOS)
    border = 8
    x1, y1 = right + 20, bottom + 20  # its outer corner, just past the frame's
    x0, y0 = x1 - ref.width - 2 * border, y1 - ref.height - 2 * border
    shadow = Image.new("L", card.size, 0)
    ImageDraw.Draw(shadow).rectangle([x0 + 2, y0 + 8, x1 + 2, y1 + 8], fill=140)
    card.paste((16, 36, 28), (0, 0), shadow.filter(ImageFilter.GaussianBlur(10)))
    ImageDraw.Draw(card).rectangle([x0, y0, x1 - 1, y1 - 1], fill=(255, 255, 255))
    card.paste(ref, (x0 + border, y0 + border))


def render_card(folder: Path, meta: dict, cost: dict | None = None) -> bytes:
    """A painting's card as PNG bytes. cost is the painting's cost (pricing.cost)."""
    canvas = meta.get("canvas") or {"width": 512, "height": 512}
    pw, ph = canvas["width"] * GALLERY_ZOOM, canvas["height"] * GALLERY_ZOOM
    with Image.open(folder / "painting.png") as im:
        painting = im.convert("RGB")
    if painting.size != (pw, ph):
        painting = painting.resize((pw, ph), Image.LANCZOS)

    framed_w, framed_h = pw + 2 * (FRAME + MOUNT), ph + 2 * (FRAME + MOUNT)
    width = framed_w + 2 * PAD
    label_w = width - 2 * PAD
    text_w = label_w - 2 * LABEL_PAD

    # The label's text, measured first so the card is exactly tall enough.
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    title_font, by_font, prompt_font = _font(56), _font(28), _font(30)
    settings = meta.get("settings") or {}
    title = _wrap(scratch, meta.get("title") or "Untitled", title_font, text_w, 2)
    model = str(settings.get("model") or "an unknown model")
    effort = settings.get("reasoning_effort")
    by = _wrap(scratch, f"Painted by {model}" + (f", {effort} effort" if effort else ""), by_font, text_w, 2)
    prompt = meta.get("prompt") or ("Recreate the reference image" if meta.get("reference") else "")
    prompt_lines = _wrap(scratch, f"“{prompt}”", prompt_font, text_w, PROMPT_LINES) if prompt else []

    stats = [("Shapes", f"{meta.get('shapes_used', 0):,}"), ("Cost", cost_text(cost))]
    score = meta.get("benchmark_score")
    if isinstance(score, dict) and isinstance(score.get("score"), (int, float)):
        stats.append(("Benchmark score", f"{round(100 * score['score'])}%"))
    tokens = token_groups(meta)

    title_lh, by_lh, prompt_lh = 66, 38, 42
    label_h = (LABEL_PAD + len(title) * title_lh + 6 + len(by) * by_lh
               + (18 + len(prompt_lines) * prompt_lh if prompt_lines else 0)
               + 2 * (28 + 2 + 26) + STAT_H + TOKENS_H + LABEL_PAD)
    footer_h = 90
    height = PAD + framed_h + 48 + label_h + footer_h

    card = _mat(width, height)

    # The frame's shadow on the mat, then the frame, its mount and the painting.
    shadow = Image.new("L", (width, height), 0)
    ImageDraw.Draw(shadow).rectangle([PAD + 6, PAD + 16, PAD + framed_w + 6, PAD + framed_h + 16], fill=120)
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    card.paste((16, 36, 28), (0, 0), shadow)
    d = ImageDraw.Draw(card)
    d.rectangle([PAD, PAD, PAD + framed_w - 1, PAD + framed_h - 1], fill=GRAPHITE)
    d.rectangle([PAD + 3, PAD + 3, PAD + framed_w - 4, PAD + framed_h - 4], outline=FRAME_LIGHT, width=2)
    mount = (PAD + FRAME, PAD + FRAME, PAD + framed_w - FRAME - 1, PAD + framed_h - FRAME - 1)
    d.rectangle(mount, fill=PAPER)
    px, py = PAD + FRAME + MOUNT, PAD + FRAME + MOUNT
    d.rectangle([px - 2, py - 2, px + pw + 1, py + ph + 1], fill=PAPER_EDGE)  # the mount's cut edge
    card.paste(painting, (px, py))
    _reference(card, folder, meta, PAD + framed_w, PAD + framed_h, min(pw, ph))
    d = ImageDraw.Draw(card)

    # The label.
    top = PAD + framed_h + 48
    d.rounded_rectangle([PAD, top, PAD + label_w - 1, top + label_h - 1], RADIUS, fill=PAPER)
    x, y = PAD + LABEL_PAD, top + LABEL_PAD
    for line in title:
        d.text((x, y), line, font=title_font, fill=GRAPHITE, stroke_width=1, stroke_fill=GRAPHITE)
        y += title_lh
    y += 6
    for line in by:
        d.text((x, y), line, font=by_font, fill=GRAPHITE_SOFT)
        y += by_lh
    if prompt_lines:
        y += 18
        for line in prompt_lines:
            d.text((x, y), line, font=prompt_font, fill=GRAPHITE)
            y += prompt_lh
    y += 28
    d.line([(x, y), (x + text_w, y)], fill=PAPER_EDGE, width=2)
    y += 2 + 26
    col = text_w / 3  # three columns whatever is shown, so cards line up
    for i, (name, value) in enumerate(stats):
        _stat(d, x + round(i * col), y, name, value)
    y += STAT_H + 28

    # The tokens: a section of their own under another line, in and out in the same
    # columns as the stats above, each with its detail like the gallery's.
    d.line([(x, y), (x + text_w, y)], fill=PAPER_EDGE, width=2)
    y += 2 + 26
    for i, (name, value, detail) in enumerate(tokens):
        tx = x + round(i * col)
        _stat(d, tx, y, name, value)
        if detail:
            d.text((tx, y + STAT_H + 4), detail, font=_font(24), fill=GRAPHITE_SOFT)

    # Under the label: Painty, and when it was painted.
    foot = top + label_h + footer_h / 2
    _brand(d, PAD, round(foot - 13))
    created = meta.get("created_at")
    if isinstance(created, (int, float)):
        t = time.localtime(created)
        when = f"{t.tm_mday} {time.strftime('%B %Y', t)}"
        d.text((PAD + label_w, foot), when, font=_font(26), fill=PAPER, anchor="rm")

    buf = io.BytesIO()
    card.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
