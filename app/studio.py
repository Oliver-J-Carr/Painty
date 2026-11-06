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
from .pricing import cost
from .settings_store import SettingsStore
from .tools import budget_left
from .exemplar import exemplar_text, exemplar_views
from .view import agent_view, data_url, reference_png

log = logging.getLogger("painty")

MAX_PROMPT = 500
GRID_WORDS = {"frame": "in the ruler frame", "overlay": "with the grid drawn on it", "none": "with no grid"}
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
    async def start(self, prompt: str, shape: str = DEFAULT_SHAPE, reference: str | None = None) -> None:
        """Start a brand new drawing on a fresh canvas of the given shape
        (square, landscape or portrait; anything else means square).

        reference: an optional image (a data URL) to work from. With no prompt,
        Painty recreates it; with one, it uses it as the prompt says.
        """
        prompt = (prompt or "").strip()[:MAX_PROMPT]
        if not prompt and not reference:
            return
        if self.state == "painting":
            await self._error("Still painting. Stop it first.")
            return

        canvas = Canvas(shape)  # unknown shapes fall back to square
        ref = view = None
        if reference:
            try:
                ref = await asyncio.to_thread(reference_png, reference, canvas.width, canvas.height)
            except ValueError as exc:
                await self._error(str(exc))
                return
        s = self.store.current
        if ref:
            view = await asyncio.to_thread(agent_view, ref, canvas.width, canvas.height, s.grid)
        exemplar = await asyncio.to_thread(exemplar_views, s.grid) if s.exemplar else ()
        self.ctx = PaintyContext(send=self.send, settings=s, prompt=prompt, canvas=canvas,
                                 reference=ref, reference_view=view, exemplar=exemplar,
                                 budget=Budget(s.shape_budget, s.look_budget if s.vision else 0))
        self.drawing_id = f"painty_{uuid.uuid4().hex[:12]}"
        self.agent = build_painter_agent(s, self.model, cache_key=self.drawing_id)
        self.history = None
        self.saved_id = None

        await self._emit({"type": "canvas_reset", "prompt": prompt,
                          "reference": data_url(ref) if ref else None,
                          "settings": settings_dict(s), **canvas.info()})
        await self.ctx.send_budget()
        what = prompt or "recreate the reference image"
        await self.ctx.activity("agent", f"🎨 New drawing: {what}",
                                f"{s.model}, vision {'on' if s.vision else 'off'}, "
                                f"{canvas.shape} {canvas.width} × {canvas.height}"
                                + (", with a reference image" if prompt and ref else ""))
        if exemplar:
            await self.ctx.activity("agent", "📐 The exemplar sheets Painty was shown",
                                    f"{len(exemplar)} sheets of numbered examples, before your prompt",
                                    image=data_url(exemplar[0]))
        if view:
            await self.ctx.activity("agent", "🖼️ The reference image Painty was given",
                                    f"Cropped and scaled to the canvas ({canvas.width} × {canvas.height}), "
                                    f"as Painty sees it ({GRID_WORDS.get(s.grid, s.grid)})",
                                    image=data_url(view))
        self._launch(_first_message(prompt, view, exemplar), label="start")

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
                                 "reference": str(ctx.reference is not None).lower(),
                                 "vision": str(ctx.settings.vision).lower(),
                                 "grid": ctx.settings.grid,
                                 "exemplar": str(ctx.settings.exemplar).lower()}):
                result = await Runner.run(self.agent, run_input, context=ctx,
                                          hooks=self.hooks, max_turns=_turn_limit(ctx.budget))
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
            canvas=c.canvas, prompt=c.prompt, reference=c.reference,
            reference_view=c.reference_view, exemplar=c.exemplar, title=title, note=note, ending=ending,
            settings=settings_dict(c.settings), shapes_budget=c.budget.shapes_total,
            looks_budget=c.budget.looks_total, looks=list(c.looks), stages=list(c.stages),
            top_ups=c.budget.top_ups,
            input_tokens=c.tokens.input, cached_tokens=c.tokens.cached,
            cache_write_tokens=c.tokens.cache_write,
            output_tokens=c.tokens.output, reasoning_tokens=c.tokens.reasoning,
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
                              tokens=c.tokens.to_dict(),
                              cost=cost(c.settings.model, c.tokens.input, c.tokens.cached,
                                        c.tokens.output, env.prices, c.tokens.cache_write))

    @staticmethod
    async def _quietly(coro) -> None:
        """Send an event, ignoring a closed connection (e.g. the tab was closed)."""
        try:
            await coro
        except Exception:
            pass


def _first_message(prompt: str, reference_view: bytes | None, exemplar: tuple[bytes, ...] = ()):
    """The person's opening message. The exemplar sheets and a reference image go
    in it once, in the drawing's grid mode: they stay in the history, so every
    later call (and keep going) still has them, served from the prompt cache
    after the first. The exemplar comes first, so the request is read after it."""
    if reference_view is None and not exemplar:
        return f"Paint this: {prompt}"
    content: list[dict] = []
    if exemplar:
        content.append({"type": "input_text", "text": exemplar_text()})
        content += [{"type": "input_image", "image_url": data_url(png), "detail": "high"}
                    for png in exemplar]
    if reference_view is None:
        content.append({"type": "input_text", "text": f"Paint this: {prompt}"})
    else:
        text = (f"Paint this: {prompt}\n\nHere is the reference image." if prompt
                else "Recreate this reference image as a painting.")
        content += [{"type": "input_text", "text": text},
                    {"type": "input_image", "image_url": data_url(reference_view), "detail": "high"}]
    return [{"role": "user", "content": content}]


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
