"""The painter agent: one agent, three tools, rebuilt for each drawing."""

from __future__ import annotations

import re

from agents import Agent, FunctionToolResult, ModelSettings, RunContextWrapper, ToolsToFinalOutputResult
from openai.types.shared import Reasoning

from .context import PaintyContext
from .exemplar import SHEET, SHEETS
from .prompts import EXEMPLAR, PAINTER_PROMPT, REFERENCE, VISION_OFF, VISION_ON, fill
from .settings_store import StudioSettings
from .tools import tools_for
from .view import DEFAULT_GRID, GRID_NOTES

PAUSED = "paused: shape budget used up"

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
    vision = fill(VISION_ON, LOOKS=b.looks_total, GRID_NOTE=note) if c.settings.vision else VISION_OFF
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
                MAX_PER_CALL=c.settings.max_shapes_per_call, VISION_SECTION=vision,
                REFERENCE_SECTION=reference, EXEMPLAR_SECTION=exemplar)
    return re.sub(r"\n{3,}", "\n\n", text)  # no gap where an optional section is left out


def _stop_when_done(ctx: RunContextWrapper[PaintyContext],
                    results: list[FunctionToolResult]) -> ToolsToFinalOutputResult:
    """Called by the SDK after each round of tool calls: should the run end here?

    It ends when the agent called finish, or when the shape budget is used up.
    Ending here (rather than raising) keeps the conversation history complete,
    so "keep going" can resume it exactly where it stopped.
    """
    c = ctx.context
    if c.finished:
        return ToolsToFinalOutputResult(is_final_output=True, final_output="finished")
    if c.budget.shapes_left <= 0:
        return ToolsToFinalOutputResult(is_final_output=True, final_output=PAUSED)
    return ToolsToFinalOutputResult(is_final_output=False)


def build_painter_agent(settings: StudioSettings, model=None,
                        cache_key: str | None = None) -> Agent[PaintyContext]:
    """model overrides settings.model (tests pass a scripted fake model).

    cache_key is sent as the prompt_cache_key, so every call in one drawing,
    across keep going segments, is routed to the same prompt cache.
    """
    effort = settings.reasoning_effort
    return Agent[PaintyContext](
        name="Painty",
        instructions=_instructions,
        tools=tools_for(settings.vision),
        model=model or settings.model,
        # One tool call per model turn, so paint always lands in the order planned.
        model_settings=ModelSettings(
            parallel_tool_calls=False,
            reasoning=Reasoning(effort=effort) if effort else None,
            extra_args={"prompt_cache_key": cache_key} if cache_key else None,
        ),
        tool_use_behavior=_stop_when_done,
    )
