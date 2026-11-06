"""The gallery: finished paintings saved as plain files, so runs can be compared.

Each painting gets its own folder under DATA_DIR/gallery:

    20261031-114502-sunny-face/
        painting.png      clean painting at 2x (1024 px wide for a square), for sharing
        painting.svg      the same painting as SVG
        shapes.txt        every shape in paint order, as the model wrote it
        shapes.json       every shape in paint order, as parsed data
        stages/stage-1.png  the canvas after each paint call (512 px, no grid)
        looks/look-1.png  exactly what the agent saw on each look (grid included)
        reference.png     the reference image, if one was given (canvas size)
        reference-view.png  the reference as the agent saw it (grid included)
        exemplar-N.png    the exemplar sheets as the agent saw them, if shown
        meta.json         prompt, title, note, canvas, settings, budgets used, tokens,
                          and favourite (set from the gallery; the only thing that changes later)

Plain files, no database: easy to browse, copy, zip or delete by hand.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .canvas import Canvas
from .context import Look, Stage
from .view import render_png

GALLERY_ZOOM = 2
_ID = re.compile(r"[A-Za-z0-9._-]+")  # a painting's folder name


@dataclass
class SaveRequest:
    canvas: Canvas
    prompt: str
    title: str
    note: str
    ending: str              # "finished" (agent called finish), "ended" (agent stopped
                             # without finish) or "saved" (you saved it by hand)
    settings: dict
    shapes_budget: int       # total, including any top-ups
    looks_budget: int
    reference: bytes | None = None       # the reference image, clean PNG at canvas size
    reference_view: bytes | None = None  # the same in the drawing's grid mode, as the agent saw it
    exemplar: tuple[bytes, ...] = ()     # the exemplar sheets as the agent saw them, if shown
    looks: list[Look] = field(default_factory=list)
    stages: list[Stage] = field(default_factory=list)
    top_ups: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0      # part of input_tokens, served from the prompt cache
    cache_write_tokens: int = 0  # part of input_tokens, written to the prompt cache
    output_tokens: int = 0
    reasoning_tokens: int = 0   # part of output_tokens
    started_at: float = 0.0


def _slug(text: str) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return words[:40].strip("-") or "painting"


class Gallery:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, req: SaveRequest, version: str) -> dict:
        """Write one painting's folder and return its meta. Raises OSError on failure."""
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        folder_id = f"{stamp}-{_slug(req.title or req.prompt)}"
        folder = self.root / folder_id
        n = 2
        while folder.exists():  # two saves in the same second
            folder = self.root / f"{folder_id}-{n}"
            n += 1
        folder_id = folder.name
        (folder / "looks").mkdir(parents=True)
        (folder / "stages").mkdir()

        svg = req.canvas.to_svg()
        (folder / "painting.svg").write_text(svg, encoding="utf-8")
        (folder / "painting.png").write_bytes(render_png(svg, zoom=GALLERY_ZOOM))
        (folder / "shapes.txt").write_text(req.canvas.shape_text(), encoding="utf-8")
        (folder / "shapes.json").write_text(json.dumps(req.canvas.shape_log(), indent=1), encoding="utf-8")
        if req.reference:
            (folder / "reference.png").write_bytes(req.reference)
        if req.reference_view:
            (folder / "reference-view.png").write_bytes(req.reference_view)
        exemplar_files = [f"exemplar-{i}.png" for i in range(1, len(req.exemplar) + 1)]
        for name, png in zip(exemplar_files, req.exemplar):
            (folder / name).write_bytes(png)
        for i, look in enumerate(req.looks, start=1):
            (folder / "looks" / f"look-{i}.png").write_bytes(look.png)
        stages = _stage_log(req.stages, req.looks)
        for st in stages:
            png = render_png(req.canvas.to_svg(upto=st["last"]))
            (folder / "stages" / f"stage-{st['n']}.png").write_bytes(png)

        meta = {
            "id": folder_id,
            "title": req.title or "Untitled",
            "note": req.note,
            "prompt": req.prompt,
            "ending": req.ending,
            # The canvas shape and size. Older paintings have none: 512 x 512 square.
            "canvas": req.canvas.info(),
            # True when the person gave a reference image (reference.png, and
            # reference-view.png as the agent saw it). Older paintings have none.
            "reference": req.reference is not None,
            # The exemplar sheets the agent was shown, as it saw them (they change
            # between versions). Empty when it wasn't shown; older paintings have none.
            "exemplar_sheets": exemplar_files,
            "created_at": now,
            "duration_s": round(now - req.started_at, 1) if req.started_at else None,
            "settings": req.settings,
            "shapes_used": len(req.canvas),
            "shapes_budget": req.shapes_budget,
            "looks_used": len(req.looks),
            "looks_budget": req.looks_budget,
            # Each paint call that painted, with the looks taken at that point.
            # Older paintings have no stages, looks list or stages/ folder.
            "stages": stages,
            "looks": [{"n": i, "focus": look.focus, "shapes": look.shapes}
                      for i, look in enumerate(req.looks, start=1)],
            "top_ups": req.top_ups,
            # How the model wrote its shapes. Older paintings (JSON shapes) have no
            # shape_format and no shapes.txt.
            "shape_format": "notation",
            "input_tokens": req.input_tokens,
            "cached_tokens": req.cached_tokens,
            # Older paintings have none: their cost counts writes as plain input.
            "cache_write_tokens": req.cache_write_tokens,
            "output_tokens": req.output_tokens,
            "reasoning_tokens": req.reasoning_tokens,
            "version": version,
        }
        (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta

    def set_favourite(self, painting_id: str, favourite: bool) -> dict | None:
        """Mark a painting as a favourite (or not) in its meta.json, and return the meta.

        None if there's no such painting. The id must be one folder name straight
        under the gallery, so nothing outside it can be touched. Raises OSError if
        the file can't be written.
        """
        if not _ID.fullmatch(painting_id or "") or painting_id in (".", ".."):
            return None
        path = self.root / painting_id / "meta.json"
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        meta["favourite"] = bool(favourite)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic, so a crash never leaves a half-written meta.json
        return meta

    def list(self) -> list[dict]:
        """Every saved painting's meta, newest first. Unreadable folders are skipped."""
        items = []
        if not self.root.is_dir():
            return items
        for meta_path in self.root.glob("*/meta.json"):
            try:
                items.append(json.loads(meta_path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(items, key=lambda m: m.get("created_at", 0), reverse=True)


def _stage_log(stages: list[Stage], looks: list[Look]) -> list[dict]:
    """Stages as plain data, each with the numbers of the looks that saw it.

    A look sees the stage whose last shape was the last on the canvas at the
    time. A look at an empty canvas gets a stage 0 (the blank canvas)."""
    log = [{"n": i, "first": st.first, "last": st.last, "note": st.note, "looks": []}
           for i, st in enumerate(stages, start=1)]
    by_last = {st["last"]: st for st in log}
    for i, look in enumerate(looks, start=1):
        st = by_last.get(look.shapes)
        if st is None and look.shapes == 0:
            st = {"n": 0, "first": 0, "last": 0, "note": "Blank canvas", "looks": []}
            log.insert(0, st)
            by_last[0] = st
        if st is not None:
            st["looks"].append(i)
    return log


def settings_dict(settings) -> dict:
    """A drawing's settings snapshot as plain data, for meta.json and the UI."""
    return asdict(settings)
