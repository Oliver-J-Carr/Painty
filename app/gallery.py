"""The gallery: finished paintings saved as plain files, so runs can be compared.

Each painting gets its own folder under DATA_DIR/gallery:

    20261031-114502-sunny-face/
        painting.png      clean painting at 2x (1024 px wide for a square), for sharing
        painting.svg      the same painting as SVG
        shapes.txt        every shape in paint order, as the model wrote it
        shapes.json       every shape in paint order, as parsed data
        stages/stage-1.png  the canvas after each paint call (512 px, no grid)
        looks/look-1.png  exactly what the agent saw on each look (grid included)
        meta.json         prompt, title, note, canvas, settings, budgets used, tokens

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
    looks: list[Look] = field(default_factory=list)
    stages: list[Stage] = field(default_factory=list)
    top_ups: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0      # part of input_tokens, served from the prompt cache
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
            "output_tokens": req.output_tokens,
            "reasoning_tokens": req.reasoning_tokens,
            "version": version,
        }
        (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
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
