"""The painter agent: one agent, three to five tools, rebuilt for each drawing."""

from __future__ import annotations

import dataclasses
import re

from agents import (Agent, FunctionToolResult, ModelSettings, RunContextWrapper, StopAtTools,
                    ToolsToFinalOutputResult)
from openai.types.shared import Reasoning

from .context import PaintyContext
from .exemplar import SHEET, SHEETS
from .prompts import EXEMPLAR, PAINTER_PROMPT, PLANNING, REFERENCE, VISION_OFF, VISION_ON, fill
from .providers import agent_model, provider_settings
from .settings_store import StudioSettings
from .tools import REQUEST_AT, tools_for
from .view import DEFAULT_GRID, GRID_NOTES

PAUSED_REQUEST = "paused: asked for more shapes"

# How the budget works: shapes can be asked for, except in a benchmark, whose budget is fixed.
BUDGET_REQUESTS = (
    "- A paint call that goes past the budget is still painted in full, but after that nothing more "
    "can be painted. Then, if the painting is done or close enough, call `finish`.\n"
    "- If you are near the end and need a few more shapes to finish what you are working on, call "
    "`request_shapes` with how many you need (rounded to the nearest 10) and what for. You can ask "
    "once {{REQUEST_AT}} or fewer shapes are left, never earlier: plan the painting to fit the budget. "
    "The drawing pauses while the person decides. If they say no, call `finish` straight away.")
BUDGET_FIXED = ("- This budget is fixed: no more shapes can be given. A paint call that goes past it is "
                "refused, so choose the most important shapes. When it is used up, call `finish`.")
# How many looks it may take: unlimited, except in a benchmark.
LOOKS_FREE = "as often as you need"
LOOKS_FIXED = "up to {{LOOKS}} times in this drawing"

# The reference section's last line depends on whether Painty can see its canvas.
REFERENCE_CHECK_SEEN = ("When you look, compare your canvas with the reference: what is in the wrong place, "
                        "the wrong size or the wrong colour? Fix what matters most by painting over it.")
REFERENCE_CHECK_BLIND = ("You can see the reference but not your canvas, so measure carefully in the "
                         "reference and keep track of where every shape went.")
# How to read positions off the reference, for each grid mode.
READ_POSITIONS = {
    "frame": "Read positions straight off the ruler and grid.",
    "overlay": "Read positions straight off the grid lines and their numbers.",
    "none": "Measure positions in the image's own pixels, from its top-left corner.",
}

SHAPE_WORDS = {"square": "square", "landscape": "landscape (wider than tall)",
               "portrait": "portrait (taller than wide)"}


def _instructions(ctx: RunContextWrapper[PaintyContext], agent: Agent) -> str:
    """Dynamic instructions, so the totals are right after a "keep going" top-up.

    They must not change between model calls within a run: the prompt cache only
    reuses an exact prefix, and the instructions come before the whole history.
    Live counts belong in tool results, which every paint and look call ends with.
    """
    c = ctx.context
    b = c.budget
    w, h = c.canvas.width, c.canvas.height
    grid = c.settings.grid if c.settings.grid in GRID_NOTES else DEFAULT_GRID
    note = GRID_NOTES[grid]
    looks = LOOKS_FREE if b.looks_total is None else fill(LOOKS_FIXED, LOOKS=b.looks_total)
    vision = fill(VISION_ON, LOOKS=looks, GRID_NOTE=note) if c.settings.vision else VISION_OFF
    budget = BUDGET_FIXED if c.benchmark else fill(BUDGET_REQUESTS, REQUEST_AT=REQUEST_AT)
    reference = ""
    if c.reference:
        check = REFERENCE_CHECK_SEEN if c.settings.vision else REFERENCE_CHECK_BLIND
        reference = fill(REFERENCE, WIDTH=w, HEIGHT=h, SHAPES=b.shapes_total,
                         REFERENCE_CHECK=check, GRID_NOTE=note, READ_POSITIONS=READ_POSITIONS[grid])
    exemplar = ""
    if c.settings.exemplar:
        exemplar = fill(EXEMPLAR, SHEETS=len(SHEETS), SHEET_WIDTH=SHEET[0], SHEET_HEIGHT=SHEET[1],
                        GRID_NOTE=note.replace("canvas", "sheet"))
    text = fill(PAINTER_PROMPT, SHAPE_WORD=SHAPE_WORDS[c.canvas.shape], WIDTH=w, HEIGHT=h,
                CX=w // 2, CY=h // 2, SHAPES=b.shapes_total,
                BUDGET_NOTES=budget, VISION_SECTION=vision,
                REFERENCE_SECTION=reference, EXEMPLAR_SECTION=exemplar,
                PLANNING_SECTION=PLANNING if c.settings.planning else "")
    return re.sub(r"\n{3,}", "\n\n", text)  # no gap where an optional section is left out


def _stop_when_done(ctx: RunContextWrapper[PaintyContext],
                    results: list[FunctionToolResult]) -> ToolsToFinalOutputResult:
    """Called by the SDK after each round of tool calls: should the run end here?

    It ends when the agent called finish, or asked for more shapes (the person
    answers on the paused card). Running out of shapes doesn't end it: the agent
    decides whether to finish or ask. Ending here (rather than raising) keeps the
    conversation history complete, so the drawing can resume exactly where it stopped.
    """
    c = ctx.context
    if c.finished:
        return ToolsToFinalOutputResult(is_final_output=True, final_output="finished")
    if c.asking:
        return ToolsToFinalOutputResult(is_final_output=True, final_output=PAUSED_REQUEST)
    return ToolsToFinalOutputResult(is_final_output=False)


def build_painter_agent(settings: StudioSettings, model=None, cache_key: str | None = None,
                        requests: bool = True) -> Agent[PaintyContext]:
    """model overrides settings.model (tests pass a scripted fake model).
    requests: whether it may ask for more shapes (not in a benchmark).

    cache_key is sent as the prompt_cache_key, so every call in one drawing,
    across keep going segments, is routed to the same prompt cache.
    """
    effort = settings.reasoning_effort
    return Agent[PaintyContext](
        name="Painty",
        instructions=_instructions,
        tools=tools_for(settings.vision, settings.planning, requests),
        model=model or agent_model(settings.model),
        # One tool call per model turn, so paint always lands in the order planned.
        model_settings=ModelSettings(
            parallel_tool_calls=False,
            reasoning=Reasoning(effort=effort) if effort else None,
            extra_args={"prompt_cache_key": cache_key} if cache_key else None,
            **provider_settings(settings.model),
        ),
        tool_use_behavior=_stop_when_done,
    )


def planning_agent(painter: Agent[PaintyContext], settings: StudioSettings,
                   forced: bool = True) -> Agent[PaintyContext]:
    """The painter for its first turn when planning is on: forced to call plan, at
    the planning reasoning effort, and stopping straight after.

    Everything sent before the conversation (instructions, tools, the first
    message) is the same as the painter's, so the painter's next call reuses the
    prompt cache this one fills. Only the model settings differ.

    forced=False is for models that refuse a forced tool call. Then only the
    prompt's "your first call is plan" asks for it.
    """
    effort = settings.planning_effort
    ms = dataclasses.replace(painter.model_settings, tool_choice="plan" if forced else None,
                             reasoning=Reasoning(effort=effort) if effort else None)
    return painter.clone(model_settings=ms, tool_use_behavior=StopAtTools(stop_at_tool_names=["plan"]))
