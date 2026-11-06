"""Per-drawing run context, shared by tools and hooks.

The Agents SDK passes this object to every tool (via RunContextWrapper) and
every hook, but never sends it to the model. It is the "backstage" area.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .canvas import Canvas
from .settings_store import StudioSettings

# A function that pushes one JSON event down the WebSocket to the browser.
SendEvent = Callable[[dict[str, Any]], Awaitable[None]]

_event_ids = itertools.count(1)


@dataclass
class Budget:
    shapes_total: int
    looks_total: int
    shapes_used: int = 0
    looks_used: int = 0
    top_ups: int = 0

    @property
    def shapes_left(self) -> int:
        return max(0, self.shapes_total - self.shapes_used)

    @property
    def looks_left(self) -> int:
        return max(0, self.looks_total - self.looks_used)

    def top_up(self, shapes: int, looks: int) -> None:
        self.shapes_total += shapes
        self.looks_total += looks
        self.top_ups += 1

    def to_dict(self) -> dict:
        return {"shapes_used": self.shapes_used, "shapes_total": self.shapes_total,
                "shapes_left": self.shapes_left, "looks_used": self.looks_used,
                "looks_total": self.looks_total, "looks_left": self.looks_left,
                "top_ups": self.top_ups}


@dataclass
class Stage:
    """One paint call that put shapes down: canvas shapes first to last."""
    first: int
    last: int
    note: str = ""


@dataclass
class Look:
    """One look: exactly what the agent saw, and how many shapes were on the canvas."""
    png: bytes
    focus: str
    shapes: int


@dataclass
class PaintyContext:
    send: SendEvent
    settings: StudioSettings           # frozen copy taken when the drawing started
    prompt: str = ""
    reference: bytes | None = None       # the reference image as a clean PNG, canvas-sized
    reference_view: bytes | None = None  # the same in the drawing's grid mode: what the agent saw
    exemplar: tuple[bytes, ...] = ()     # the exemplar sheets as the agent saw them, if shown
    canvas: Canvas = field(default_factory=Canvas)
    budget: Budget = field(default_factory=lambda: Budget(0, 0))
    looks: list[Look] = field(default_factory=list)    # what the agent saw, in order
    stages: list[Stage] = field(default_factory=list)  # each paint call that painted, in order
    finish_title: str | None = None    # set by the finish tool
    finish_note: str = ""
    started_at: float = field(default_factory=time.time)
    # Serialises painting, so batches always land in the order they were made.
    paint_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def finished(self) -> bool:
        return self.finish_title is not None

    # ---- event helpers ---------------------------------------------------
    async def emit(self, event: dict[str, Any]) -> None:
        event.setdefault("id", next(_event_ids))
        event.setdefault("ts", time.time())
        await self.send(event)

    async def activity(self, kind: str, text: str, detail: str = "", status: str = "done",
                       image: str | None = None) -> None:
        """Add an entry to the browser's activity feed. image: an optional picture
        (a data URL) shown as a thumbnail that enlarges on click."""
        event = {"type": "activity", "kind": kind, "status": status, "text": text, "detail": detail}
        if image:
            event["image"] = image
        await self.emit(event)

    async def send_budget(self) -> None:
        await self.emit({"type": "budget", **self.budget.to_dict()})
