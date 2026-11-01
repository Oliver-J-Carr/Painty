"""The gallery: finished paintings saved as plain files, so runs can be compared.

Each painting gets its own folder under DATA_DIR/gallery:

    20261031-114502-sunny-face/
        painting.png      clean painting at 2x (1024 px), for sharing
        painting.svg      the same painting as SVG
        shapes.json       every shape in paint order
        looks/look-1.png  exactly what the agent saw on each look (grid included)
        meta.json         prompt, title, note, settings, budgets used, tokens

Plain files, no database: easy to browse, copy, zip or delete by hand.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .canvas import Canvas
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
    looks: list[bytes] = field(default_factory=list)
    top_ups: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
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

        svg = req.canvas.to_svg()
        (folder / "painting.svg").write_text(svg, encoding="utf-8")
        (folder / "painting.png").write_bytes(render_png(svg, zoom=GALLERY_ZOOM))
        (folder / "shapes.json").write_text(json.dumps(req.canvas.shape_log(), indent=1), encoding="utf-8")
        for i, png in enumerate(req.looks, start=1):
            (folder / "looks" / f"look-{i}.png").write_bytes(png)

        meta = {
            "id": folder_id,
            "title": req.title or "Untitled",
            "note": req.note,
            "prompt": req.prompt,
            "ending": req.ending,
            "created_at": now,
            "duration_s": round(now - req.started_at, 1) if req.started_at else None,
            "settings": req.settings,
            "shapes_used": len(req.canvas),
            "shapes_budget": req.shapes_budget,
            "looks_used": len(req.looks),
            "looks_budget": req.looks_budget,
            "top_ups": req.top_ups,
            "input_tokens": req.input_tokens,
            "output_tokens": req.output_tokens,
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


def settings_dict(settings) -> dict:
    """A drawing's settings snapshot as plain data, for meta.json and the UI."""
    return asdict(settings)
