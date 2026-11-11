"""One studio = one browser connection.

Holds the current drawing (canvas, budget, conversation history) and runs
the painter agent as a background task, so the WebSocket keeps listening for
"stop" while it paints.

A drawing moves through these states:

    idle --paint--> painting --finish--> finished (saved to the gallery)
                       |   \\--stop----> stopped  (you can save it)
                       |    \\-error---> error    (you can save it)
                       v
                     paused  (out of shapes, a look with none left, or the turn limit)
                       |--keep going--> painting (same conversation, the budget you add)
                       \\--finish here-> finished (saved)

Drawing state is per connection and in memory: refreshing the page starts
fresh. Only the gallery and the settings outlive a connection.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import uuid

from agents import ItemHelpers, RunConfig, Runner, ToolExecutionConfig, trace
from agents.exceptions import MaxTurnsExceeded
from openai import BadRequestError

from . import __version__
from .canvas import CANVAS_SHAPES, DEFAULT_SHAPE, Canvas
from .config import settings as env
from .context import Budget, PaintyContext, SendEvent
from .gallery import Gallery, SaveRequest, settings_dict
from .hooks import ActivityHooks
from .painter import PAUSED, PAUSED_LOOKS, build_painter_agent, planning_agent
from .model_library import ModelLibrary
from .pricing import Prices, cost
from .providers import is_openrouter
from .settings_store import SettingsStore
from .tools import budget_left
from .benchmark import BENCHMARK_PROMPT, CARDS, card_json, card_list, card_png
from .benchmark_score import record, score_painting
from .exemplar import exemplar_text, exemplar_views
from .view import agent_view, data_url, reference_png

log = logging.getLogger("painty")

MAX_PROMPT = 500
GRID_WORDS = {"frame": "in the ruler frame", "overlay": "with the grid drawn on it", "none": "with no grid"}
# Spare model calls on top of the budget, for invalid batches and thinking.
SPARE_TURNS = 10
# The planning turn's own limit: it is forced to call plan and stops straight after.
PLANNING_TURNS = 2
# Keep going: what you may add each time (min, max), and what the paused card suggests
# for whatever has run out. You type the amounts on the card, so nothing is fixed.
TOP_UP_LIMITS = {"shapes": (0, 500), "looks": (0, 50)}
TOP_UP_SUGGESTED = {"shapes": 20, "looks": 2}
# One tool at a time, in the order the model asked. parallel_tool_calls=False should mean
# one call per turn, but not every OpenRouter host supports it, and paint order is the picture.
RUN_CONFIG = RunConfig(tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1))


def _turn_limit(budget: Budget) -> int:
    """Hidden runaway guard. Each model call makes at most one tool call, so a
    well-behaved run never needs more than one call per shape and look."""
    return budget.shapes_left + budget.looks_left + SPARE_TURNS


def _top_up_amount(value, kind: str) -> int:
    """A typed top-up as a whole number within TOP_UP_LIMITS (anything else is 0)."""
    lo, hi = TOP_UP_LIMITS[kind]
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


class Studio:
    def __init__(self, send: SendEvent, store: SettingsStore, gallery: Gallery, model=None,
                 library: ModelLibrary | None = None) -> None:
        self.send = send
        self.store = store
        self.gallery = gallery
        self.model = model  # tests pass a scripted fake model; None = use settings
        self.library = library  # None = only the prices set in .env
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
                "settings": settings_dict(self.store.current),
                "options": {**self.store.options(),
                            "openrouter_key": bool(env.openrouter_api_key),
                            "model_info": self.library.model_info() if self.library else {},
                            "top_up_limits": {k: list(v) for k, v in TOP_UP_LIMITS.items()}},
                "benchmarks": card_list(), "benchmark_prompt": BENCHMARK_PROMPT}

    # ------------------------------------------------------------ commands
    async def start(self, prompt: str, shape: str = DEFAULT_SHAPE, reference: str | None = None,
                    benchmark: str | None = None) -> None:
        """Start a brand new drawing on a fresh canvas of the given shape
        (square, landscape or portrait; anything else means square).

        reference: an optional image (a data URL) to work from. With no prompt,
        Painty recreates it; with one, it uses it as the prompt says.
        benchmark: a benchmark card's name. The card is the reference, painted here
        (exact pixels, not the browser's copy), and it sets the canvas shape and the
        shape and look budgets (fixed, with no top-ups, so runs compare fairly).
        """
        prompt = (prompt or "").strip()[:MAX_PROMPT]
        if not prompt and not reference and not benchmark:
            return
        if self.state == "painting":
            await self._error("Still painting. Stop it first.")
            return
        if benchmark and benchmark not in CARDS:
            await self._error(f"There's no benchmark card called {benchmark}.")
            return
        chosen = self.store.current.model
        if self.model is None and is_openrouter(chosen) and not env.openrouter_api_key:
            await self._error(f"{chosen} runs through OpenRouter. Set OPENROUTER_API_KEY in .env first.")
            return
        caps = self.library.capabilities(chosen) if self.library else None
        if caps and not caps["vision"] and (reference or benchmark):
            await self._error(f"{chosen} can't see images, so it can't work from a reference image "
                              "or a benchmark card. Remove it, or choose a model that can see.")
            return

        canvas = Canvas(CARDS[benchmark].shape if benchmark else shape)  # unknown shapes: square
        ref = view = None
        if benchmark:
            ref = await asyncio.to_thread(card_png, benchmark)
        elif reference:
            try:
                ref = await asyncio.to_thread(reference_png, reference, canvas.width, canvas.height)
            except ValueError as exc:
                await self._error(str(exc))
                return
        s, adjusted = _fit_model(self.store.current, caps)
        if benchmark:  # this drawing only: the saved settings keep your own budgets
            card = CARDS[benchmark]
            s = dataclasses.replace(s, shape_budget=card.shape_budget, look_budget=card.look_budget)
        if ref:
            view = await asyncio.to_thread(agent_view, ref, canvas.width, canvas.height, s.grid)
        exemplar = await asyncio.to_thread(exemplar_views, s.grid) if s.exemplar else ()
        self.ctx = PaintyContext(send=self.send, settings=s, prompt=prompt, canvas=canvas,
                                 reference=ref, reference_view=view, exemplar=exemplar,
                                 benchmark=benchmark,
                                 budget=Budget(s.shape_budget, s.look_budget if s.vision else 0))
        self.drawing_id = f"painty_{uuid.uuid4().hex[:12]}"
        self.agent = build_painter_agent(s, self.model, cache_key=self.drawing_id)
        self.history = None
        self.saved_id = None

        await self._emit({"type": "canvas_reset", "prompt": prompt,
                          "reference": data_url(ref) if ref else None, "benchmark": benchmark,
                          "settings": settings_dict(s), **canvas.info()})
        await self.ctx.send_budget()
        what = prompt or "recreate the reference image"
        await self.ctx.activity("agent", f"🎨 New drawing: {what}",
                                f"{s.model}, vision {'on' if s.vision else 'off'}, "
                                f"{canvas.shape} {canvas.width} × {canvas.height}"
                                + (", with a reference image" if prompt and ref else ""))
        if adjusted:
            await self.ctx.activity("agent", f"⚙️ Fitted to {s.model}", " ".join(adjusted))
        if exemplar:
            await self.ctx.activity("agent", "📐 The exemplar sheets Painty was shown",
                                    f"{len(exemplar)} sheets of numbered examples, before your prompt",
                                    images=[data_url(png) for png in exemplar])
        if benchmark:
            card = CARDS[benchmark]
            looks = f" and {card.look_budget} looks" if s.vision else ", painting blind"
            await self.ctx.activity("agent", f"🎯 Benchmark: {card.title}",
                                    f"{card.about} The card has {card.shape_count} shapes. Fixed budget: "
                                    f"{card.shape_budget} shapes{looks}, no top-ups. Score it with "
                                    "python -m app.benchmark_score once it's saved.")
        if view:
            await self.ctx.activity("agent", "🖼️ The reference image Painty was given",
                                    f"Cropped and scaled to the canvas ({canvas.width} × {canvas.height}), "
                                    f"as Painty sees it ({GRID_WORDS.get(s.grid, s.grid)})",
                                    image=data_url(view))
        self._launch(_first_message(prompt, view, exemplar), label="start")

    async def keep_going(self, shapes=0, looks=0) -> None:
        """Add the shapes and looks typed on the paused card (either may be 0) and
        carry on the same conversation."""
        if self.state != "paused" or not self.ctx or self.history is None:
            await self._error("There's no paused drawing to continue.")
            return
        if self.ctx.benchmark:
            await self._error("A benchmark has a fixed budget, so it can't keep going. "
                              "Finish here to save it for scoring.")
            return
        shapes = _top_up_amount(shapes, "shapes")
        looks = _top_up_amount(looks, "looks") if self.ctx.settings.vision else 0
        b = self.ctx.budget
        if b.shapes_left + shapes <= 0:
            await self._error("Painty has no shapes left, so add at least one to keep going.")
            return
        self.ctx.wants_look = False
        if shapes or looks:
            b.top_up(shapes, looks)
        await self.ctx.send_budget()
        added = " and ".join([f"{n} more {what}" for n, what in ((shapes, "shapes"), (looks, "looks")) if n])
        await self.ctx.activity("agent", f"⏩ Keep going: {added or 'nothing added'}")
        given = f"You have been given {added}." if added else "No more shapes or looks were added."
        blind = (" You have no looks left, so carry on from what you already know."
                 if self.ctx.settings.vision and b.looks_left == 0 else "")
        message = (f"{given} {budget_left(self.ctx)}{blind} Carry on from where you stopped, "
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
                                 "exemplar": str(ctx.settings.exemplar).lower(),
                                 "planning": str(ctx.settings.planning).lower()}):
                if label == "start" and ctx.settings.planning:
                    run_input = await self._plan_first(run_input)
                result = await Runner.run(self.agent, run_input, context=ctx, hooks=self.hooks,
                                          max_turns=_turn_limit(ctx.budget), run_config=RUN_CONFIG)
            self.history = result.to_input_list()
            if ctx.finished:
                await self._save_and_finish("finished", ctx.finish_title, ctx.finish_note)
            elif result.final_output == PAUSED:
                await self._pause("shapes")
            elif result.final_output == PAUSED_LOOKS:
                await self._pause("looks")
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
            await self._quietly(self.ctx.activity("error", "⚠️ The drawing failed",
                                                  _provider_message(exc) or repr(exc)))
            await self._quietly(self._set_state("error", reason=_short_error(exc)))

    async def _plan_first(self, run_input):
        """The planning turn: the painter, forced to call plan at the planning effort.
        Returns the conversation for the painter to carry on from. A planning turn
        that writes no plan doesn't stop the drawing: it paints without one.
        Some models refuse a forced tool call, so they are only asked to plan."""
        ctx = self.ctx
        ctx.plan_forced = True

        async def planning_turn():
            return await Runner.run(planning_agent(self.agent, ctx.settings, forced=ctx.plan_forced),
                                    run_input, context=ctx, hooks=self.hooks, max_turns=PLANNING_TURNS,
                                    run_config=RUN_CONFIG)
        try:
            try:
                result = await planning_turn()
            except BadRequestError as exc:
                if "tool_choice" not in f"{exc.body} {exc}":
                    raise
                ctx.plan_forced = False
                await ctx.activity("agent", "📝 This model can't be made to call a tool",
                                   "So nothing forces the planning turn: only the prompt asks for a plan.")
                result = await planning_turn()
            history, said = result.to_input_list(), str(result.final_output or "")
        except MaxTurnsExceeded as exc:
            history, said = _history_from(exc), str(exc)
        if ctx.plans:
            return history
        await ctx.activity("error", "📝 Painty didn't write a plan on its planning turn", said)
        if history is None:
            return run_input
        last = history[-1] if history else {}
        if isinstance(last, dict) and last.get("type") == "function_call_output":
            return history  # a refused plan: the error says what to fix, and it can plan again
        return history + [{"role": "user", "content": "Now paint it."}]

    async def _pause(self, reason: str) -> None:
        """Pause and ask how much to add. top_up holds the amounts the paused card
        suggests (only for what has run out; looks is None when painting blind),
        or is None for a benchmark, which can only finish here."""
        why = {"shapes": "The shape budget is used up.",
               "looks": "Painty wanted to look, but it has no looks left.",
               }.get(reason, "The hidden turn limit was reached.")
        if self.ctx.benchmark:  # a fixed budget: no top-ups, only finish here
            await self._set_state("paused", reason=why, top_up=None)
            return
        b, vision = self.ctx.budget, self.ctx.settings.vision
        await self._set_state("paused", reason=why, top_up={
            "shapes": TOP_UP_SUGGESTED["shapes"] if b.shapes_left == 0 else 0,
            "looks": (TOP_UP_SUGGESTED["looks"] if b.looks_left == 0 else 0) if vision else None,
        })

    def _prices(self) -> Prices:
        return self.library.prices() if self.library else env.prices

    async def _score(self) -> tuple[dict | None, bytes | None]:
        """A benchmark drawing's score and difference picture, worked out off the event
        loop. (None, None) for other drawings, or if scoring fails (it never stops a save)."""
        c = self.ctx
        if not c.benchmark or not len(c.canvas):
            return None, None
        try:
            result, diff = await asyncio.to_thread(score_painting, c.reference, c.canvas.to_svg(),
                                                   card_json(c.benchmark))
        except Exception:
            log.exception("could not score the benchmark painting")
            return None, None
        return record(result), diff

    async def _save_and_finish(self, ending: str, title: str, note: str) -> None:
        c = self.ctx
        score, diff = await self._score()
        req = SaveRequest(
            canvas=c.canvas, prompt=c.prompt, reference=c.reference,
            reference_view=c.reference_view, exemplar=c.exemplar, title=title, note=note, ending=ending,
            benchmark=card_json(c.benchmark) if c.benchmark else None,
            benchmark_score=score, benchmark_diff=diff,
            settings=settings_dict(c.settings), shapes_budget=c.budget.shapes_total,
            looks_budget=c.budget.looks_total, looks=list(c.looks), stages=list(c.stages),
            plans=list(c.plans), plan_forced=c.plan_forced, billed_usd=c.tokens.billed,
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
                              saved_id=self.saved_id, meta=meta, benchmark_score=score,
                              tokens=c.tokens.to_dict(),
                              cost=cost(c.settings.model, c.tokens.input, c.tokens.cached,
                                        c.tokens.output, self._prices(), c.tokens.cache_write,
                                        c.tokens.billed))

    @staticmethod
    async def _quietly(coro) -> None:
        """Send an event, ignoring a closed connection (e.g. the tab was closed)."""
        try:
            await coro
        except Exception:
            pass


def _fit_model(s, caps: dict | None):
    """A drawing's settings fitted to what its model can do, and what changed. This drawing
    only: the saved settings keep your own values for the next model. A text-only model
    paints blind with no exemplar sheets, and an effort the model doesn't take becomes its
    default. Unknown capabilities (OpenRouter's catalogue not fetched) change nothing."""
    if not caps:
        return s, []
    adjusted = []
    if not caps["vision"] and (s.vision or s.exemplar):
        s = dataclasses.replace(s, vision=False, exemplar=False)
        adjusted.append("It can't see images, so it paints blind, without the exemplar sheets.")
    reasoning = caps.get("reasoning") or {"options": [], "default": ""}
    allowed = {value for value, _ in reasoning["options"]} or {""}
    for name in ("reasoning_effort", "planning_effort"):
        if getattr(s, name) not in allowed:
            s = dataclasses.replace(s, **{name: reasoning["default"]})
            what = "reasoning effort" if name == "reasoning_effort" else "planning effort"
            adjusted.append(f"Its {what} is its default, as it doesn't take the one chosen."
                            if reasoning["options"] else f"It can't reason, so no {what} is sent.")
    return s, adjusted


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


def _provider_message(exc: Exception) -> str | None:
    """What the model's host really said. OpenRouter's own message is only "Provider
    returned error": the reason is in the error body's metadata.raw, often as JSON."""
    body = getattr(exc, "body", None)
    meta = body.get("metadata") if isinstance(body, dict) else None
    raw = meta.get("raw") if isinstance(meta, dict) else None
    if not raw:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return str(raw)
    error = parsed.get("error") if isinstance(parsed, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if not message and isinstance(parsed, dict):
        message = parsed.get("message")
    return str(message or raw)


def _short_error(exc: Exception) -> str:
    text = _provider_message(exc) or str(exc) or exc.__class__.__name__
    return text if len(text) <= 300 else text[:300] + "…"
