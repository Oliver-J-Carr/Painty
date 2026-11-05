"""One studio = one browser connection.

Holds the current drawing (canvas, budget, conversation history) and runs
the painter agent as a background task, so the WebSocket keeps listening for
"stop" while it paints.

A drawing moves through these states:

    idle --paint--> painting --finish--> finished (saved to the gallery)
                       |   \\--stop----> stopped  (you can save it)
                       |    \\-error---> error    (you can save it)
                       v
                     paused  (shape budget or turn limit reached)
                       |--keep going--> painting (same conversation, more budget)
                       \\--finish here-> finished (saved)

Drawing state is per connection and in memory: refreshing the page starts
fresh. Only the gallery and the settings outlive a connection.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from agents import ItemHelpers, Runner, trace
from agents.exceptions import MaxTurnsExceeded

from . import __version__
from .canvas import CANVAS_SHAPES, DEFAULT_SHAPE, Canvas
from .config import settings as env
from .context import Budget, PaintyContext, SendEvent
from .gallery import Gallery, SaveRequest, settings_dict
from .hooks import ActivityHooks
from .painter import PAUSED, build_painter_agent
from .settings_store import SettingsStore
from .tools import budget_left

log = logging.getLogger("painty")

MAX_PROMPT = 500
# Spare model calls on top of the budget, for invalid batches and thinking.
SPARE_TURNS = 10


def _turn_limit(budget: Budget) -> int:
    """Hidden runaway guard. Each model call makes at most one tool call, so a
    well-behaved run never needs more than one call per shape and look."""
    return budget.shapes_left + budget.looks_left + SPARE_TURNS


class Studio:
    def __init__(self, send: SendEvent, store: SettingsStore, gallery: Gallery, model=None) -> None:
        self.send = send
        self.store = store
        self.gallery = gallery
        self.model = model  # tests pass a scripted fake model; None = use settings
        self.state = "idle"
        self.ctx: PaintyContext | None = None
        self.agent = None
        self.history: list | None = None  # the conversation so far, for keep going
        self.drawing_id = ""
        self.saved_id: str | None = None
        self.input_tokens = 0
        self.cached_tokens = 0     # the part of input_tokens served from the prompt cache
        self.output_tokens = 0
        self.reasoning_tokens = 0  # the part of output_tokens spent on reasoning
        self.task: asyncio.Task | None = None
        self.hooks = ActivityHooks()

    # ------------------------------------------------------------ helpers
    async def _emit(self, event: dict) -> None:
        if self.ctx:
            await self.ctx.emit(event)
        else:
            await self.send(event)

    async def _error(self, text: str) -> None:
        await self._emit({"type": "error", "text": text})

    async def _set_state(self, state: str, **extra) -> None:
        self.state = state
        await self._emit({"type": "status", "state": state, **extra})

    def hello(self) -> dict:
        return {"type": "hello", "version": __version__,
                "canvases": {k: list(v) for k, v in CANVAS_SHAPES.items()},
                "settings": settings_dict(self.store.current), "options": self.store.options()}

    # ------------------------------------------------------------ commands
    async def start(self, prompt: str, shape: str = DEFAULT_SHAPE) -> None:
        """Start a brand new drawing on a fresh canvas of the given shape
        (square, landscape or portrait; anything else means square)."""
        prompt = (prompt or "").strip()[:MAX_PROMPT]
        if not prompt:
            return
        if self.state == "painting":
            await self._error("Still painting. Stop it first.")
            return

        s = self.store.current
        self.ctx = PaintyContext(send=self.send, settings=s, prompt=prompt,
                                 canvas=Canvas(shape),  # unknown shapes fall back to square
                                 budget=Budget(s.shape_budget, s.look_budget if s.vision else 0))
        self.drawing_id = f"painty_{uuid.uuid4().hex[:12]}"
        self.agent = build_painter_agent(s, self.model, cache_key=self.drawing_id)
        self.history = None
        self.saved_id = None
        self.input_tokens = self.cached_tokens = self.output_tokens = self.reasoning_tokens = 0

        canvas = self.ctx.canvas
        await self._emit({"type": "canvas_reset", "prompt": prompt,
                          "settings": settings_dict(s), **canvas.info()})
        await self.ctx.send_budget()
        await self.ctx.activity("agent", f"🎨 New drawing: {prompt}",
                                f"{s.model}, vision {'on' if s.vision else 'off'}, "
                                f"{canvas.shape} {canvas.width} × {canvas.height}")
        self._launch(f"Paint this: {prompt}", label="start")

    async def keep_going(self) -> None:
        """Top up the budget and carry on the same conversation."""
        if self.state != "paused" or not self.ctx or self.history is None:
            await self._error("There's no paused drawing to continue.")
            return
        s = self.store.current  # top-up sizes are read live
        looks = s.top_up_looks if self.ctx.settings.vision else 0
        self.ctx.budget.top_up(s.top_up_shapes, looks)
        await self.ctx.send_budget()
        extra = f"{s.top_up_shapes} more shapes" + (f" and {looks} more looks" if looks else "")
        await self.ctx.activity("agent", f"⏩ Keep going: {extra}")
        message = (f"You have been given {extra}. {budget_left(self.ctx)} Carry on from where you stopped, "
                   "and call finish when the painting is done.")
        items = self.history + [{"role": "user", "content": message}]
        self._launch(items, label="keep going")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()

    async def save(self) -> None:
        """Save the current painting by hand (from paused, stopped or error)."""
        if not self.ctx or len(self.ctx.canvas) == 0:
            await self._error("There's nothing on the canvas to save.")
            return
        if self.state == "painting":
            await self._error("Stop the drawing before saving it.")
            return
        if self.saved_id:
            await self._error("This painting is already in the gallery.")
            return
        await self._save_and_finish("saved", title="", note="")

    async def update_settings(self, raw: dict) -> None:
        if self.state == "painting":
            await self._error("Settings are locked while Painty is painting.")
        else:
            self.store.update(raw if isinstance(raw, dict) else {})
        await self._emit({"type": "settings", "settings": settings_dict(self.store.current)})

    # ------------------------------------------------------------ the run
    def _launch(self, run_input, label: str) -> None:
        self.state = "painting"  # set now, so a second command can't slip in first
        self.task = asyncio.create_task(self._run(run_input, label))

    async def _run(self, run_input, label: str) -> None:
        ctx = self.ctx
        await self._set_state("painting")
        try:
            with trace(env.trace_workflow_name, group_id=self.drawing_id,
                       metadata={"app": "painty", "version": __version__, "segment": label,
                                 "model": ctx.settings.model, "canvas": ctx.canvas.shape,
                                 "vision": str(ctx.settings.vision).lower()}):
                result = await Runner.run(self.agent, run_input, context=ctx,
                                          hooks=self.hooks, max_turns=_turn_limit(ctx.budget))
            self._count_tokens(result.context_wrapper.usage)
            self.history = result.to_input_list()
            if ctx.finished:
                await self._save_and_finish("finished", ctx.finish_title, ctx.finish_note)
            elif result.final_output == PAUSED:
                await self._pause("shapes")
            else:  # the agent stopped without calling finish: treat its words as the note
                await self.ctx.activity("agent", "↪️ Painty stopped without calling finish",
                                        str(result.final_output or ""))
                await self._save_and_finish("ended", "", str(result.final_output or "").strip()[:500])
        except MaxTurnsExceeded as exc:
            self.history = _history_from(exc)
            if exc.run_data:
                self._count_tokens(exc.run_data.context_wrapper.usage)
            await self.ctx.activity("error", "⏸️ Turn limit reached", str(exc))
            await self._pause("turns")
        except asyncio.CancelledError:
            self.history = None
            await self._quietly(self._set_state("stopped", reason="You stopped the drawing."))
            raise
        except Exception as exc:  # keep the app alive, but show what broke
            log.exception("drawing failed")
            self.history = None
            await self._quietly(self.ctx.activity("error", "⚠️ The drawing failed", repr(exc)))
            await self._quietly(self._set_state("error", reason=_short_error(exc)))

    async def _pause(self, reason: str) -> None:
        s = self.store.current
        why = ("The shape budget is used up." if reason == "shapes"
               else "The hidden turn limit was reached.")
        await self._set_state("paused", reason=why,
                              top_up={"shapes": s.top_up_shapes,
                                      "looks": s.top_up_looks if self.ctx.settings.vision else 0})

    async def _save_and_finish(self, ending: str, title: str, note: str) -> None:
        c = self.ctx
        req = SaveRequest(
            canvas=c.canvas, prompt=c.prompt, title=title, note=note, ending=ending,
            settings=settings_dict(c.settings), shapes_budget=c.budget.shapes_total,
            looks_budget=c.budget.looks_total, looks=list(c.looks), stages=list(c.stages),
            top_ups=c.budget.top_ups,
            input_tokens=self.input_tokens, cached_tokens=self.cached_tokens,
            output_tokens=self.output_tokens, reasoning_tokens=self.reasoning_tokens,
            started_at=c.started_at,
        )
        meta = None
        if len(c.canvas):
            try:
                meta = self.gallery.save(req, __version__)
                self.saved_id = meta["id"]
            except OSError as exc:
                log.exception("could not save to gallery")
                await c.activity("error", "⚠️ Couldn't save to the gallery", str(exc))
                await self._error(f"Couldn't save to the gallery: {exc}. "
                                  "Check that DATA_DIR is writable.")
        self.history = None
        await self._set_state("finished", title=title or "Untitled", note=note, ending=ending,
                              saved_id=self.saved_id, meta=meta,
                              tokens={"input": self.input_tokens, "cached": self.cached_tokens,
                                      "output": self.output_tokens,
                                      "reasoning": self.reasoning_tokens})

    def _count_tokens(self, usage) -> None:
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        details = getattr(usage, "input_tokens_details", None)
        self.cached_tokens += getattr(details, "cached_tokens", 0) or 0
        details = getattr(usage, "output_tokens_details", None)
        self.reasoning_tokens += getattr(details, "reasoning_tokens", 0) or 0

    @staticmethod
    async def _quietly(coro) -> None:
        """Send an event, ignoring a closed connection (e.g. the tab was closed)."""
        try:
            await coro
        except Exception:
            pass


def _history_from(exc: MaxTurnsExceeded) -> list | None:
    """Rebuild the conversation from the data the SDK attaches to the error."""
    data = exc.run_data
    if data is None:
        return None
    return ItemHelpers.input_to_new_input_list(data.input) + [
        item.to_input_item() for item in data.new_items
    ]


def _short_error(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    return text if len(text) <= 300 else text[:300] + "…"
