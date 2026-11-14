"""Per-drawing run context, shared by tools and hooks.

The Agents SDK passes this object to every tool (via RunContextWrapper) and
every hook, but never sends it to the model. It is the "backstage" area.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

from .canvas import Canvas
from .settings_store import StudioSettings

# A function that pushes one JSON event down the WebSocket to the browser.
SendEvent = Callable[[dict[str, Any]], Awaitable[None]]

_event_ids = itertools.count(1)


@dataclass
class Budget:
    """The shape budget, and the looks taken. shapes_used may pass shapes_total: a
    paint call that crosses the budget is painted in full (its shapes are already
    written and paid for), and then no more is painted until more shapes are given.
    looks_total is None when looks are unlimited (only a benchmark has a fixed
    number), and 0 when painting blind."""
    shapes_total: int
    looks_total: int | None = None
    shapes_used: int = 0
    looks_used: int = 0
    top_ups: int = 0

    @property
    def shapes_left(self) -> int:
        return max(0, self.shapes_total - self.shapes_used)

    @property
    def shapes_over(self) -> int:
        return max(0, self.shapes_used - self.shapes_total)

    @property
    def looks_left(self) -> int | None:
        return None if self.looks_total is None else max(0, self.looks_total - self.looks_used)

    def top_up(self, shapes: int) -> None:
        """More shapes, on top of everything used so far: an overshoot is counted
        against them, so the agent gets what it was given beyond what it has painted."""
        self.shapes_total = max(self.shapes_total, self.shapes_used) + shapes
        self.top_ups += 1

    def to_dict(self) -> dict:
        return {"shapes_used": self.shapes_used, "shapes_total": self.shapes_total,
                "shapes_left": self.shapes_left, "shapes_over": self.shapes_over,
                "looks_used": self.looks_used, "looks_total": self.looks_total,
                "looks_left": self.looks_left, "top_ups": self.top_ups}


@dataclass
class ShapeRequest:
    """The agent asking for more shapes near the end of a painting."""
    shapes: int          # how many it asked for, rounded to the nearest 10
    reason: str          # what it needs them for
    shapes_used: int     # shapes on the canvas when it asked
    granted: int | None = None  # how many it was given (0 = turned down); None while undecided


@dataclass
class Tokens:
    """Tokens used by every model call in the drawing, added up as each call ends.

    Counted per call (in the on_llm_end hook) rather than from the run's result, so
    a drawing that is stopped or fails mid-run still counts the calls it made."""
    input: int = 0
    cached: int = 0       # the part of input read from the prompt cache
    cache_write: int = 0  # the part of input written to the prompt cache (billed from GPT-5.6 on)
    output: int = 0
    reasoning: int = 0    # the part of output spent on reasoning
    billed: float | None = None  # US dollars charged, when the service says (OpenRouter does)

    def add_billed(self, raw_usage) -> None:
        """Add one call's charge from its raw usage (a dict, or None if not kept)."""
        charge = raw_usage.get("cost") if isinstance(raw_usage, dict) else None
        if isinstance(charge, (int, float)) and not isinstance(charge, bool):
            self.billed = (self.billed or 0.0) + charge

    def add(self, usage) -> None:
        self.input += getattr(usage, "input_tokens", 0) or 0
        self.output += getattr(usage, "output_tokens", 0) or 0
        details = getattr(usage, "input_tokens_details", None)
        self.cached += getattr(details, "cached_tokens", 0) or 0
        self.cache_write += getattr(details, "cache_write_tokens", 0) or 0
        details = getattr(usage, "output_tokens_details", None)
        self.reasoning += getattr(details, "reasoning_tokens", 0) or 0

    def to_dict(self) -> dict:
        """The token counts. billed isn't one: it travels with the cost."""
        counts = asdict(self)
        del counts["billed"]
        return counts


@dataclass
class Stage:
    """One paint call that put shapes down: canvas shapes first to last."""
    first: int
    last: int
    note: str = ""
    partial: bool = False  # a problem in the batch stopped it before its end


@dataclass
class Plan:
    """One plan the agent wrote, and how many shapes were on the canvas when it did."""
    text: str
    shapes: int


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
    benchmark: str | None = None         # the benchmark card's name, when the reference is one
    canvas: Canvas = field(default_factory=Canvas)
    budget: Budget = field(default_factory=lambda: Budget(0, 0))
    looks: list[Look] = field(default_factory=list)    # what the agent saw, in order
    stages: list[Stage] = field(default_factory=list)  # each paint call that painted, in order
    plans: list[Plan] = field(default_factory=list)    # each plan it wrote (planning on), in order
    plan_forced: bool | None = None    # planning on: was the planning turn forced? (some models refuse)
    tokens: Tokens = field(default_factory=Tokens)      # every model call so far, all segments
    finish_title: str | None = None    # set by the finish tool
    finish_note: str = ""
    requests: list[ShapeRequest] = field(default_factory=list)  # each request for more shapes
    asking: bool = False               # it just asked for more shapes: pause for the answer
    refused: bool = False              # a request was turned down: it may only finish now
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
                       image: str | None = None, images: list[str] | None = None) -> None:
        """Add an entry to the browser's activity feed. image: an optional picture
        (a data URL) shown as a thumbnail that enlarges on click; images: several, side by side."""
        event = {"type": "activity", "kind": kind, "status": status, "text": text, "detail": detail}
        if image:
            event["image"] = image
        if images:
            event["images"] = images
        await self.emit(event)

    async def send_budget(self) -> None:
        await self.emit({"type": "budget", **self.budget.to_dict()})
