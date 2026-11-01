"""The painter agent: one agent, three tools, rebuilt for each drawing."""

from __future__ import annotations

from agents import Agent, FunctionToolResult, ModelSettings, RunContextWrapper, ToolsToFinalOutputResult
from openai.types.shared import Reasoning

from .context import PaintyContext
from .prompts import PAINTER_PROMPT, VISION_OFF, VISION_ON, fill
from .settings_store import StudioSettings
from .tools import tools_for

PAUSED = "paused: shape budget used up"


def _instructions(ctx: RunContextWrapper[PaintyContext], agent: Agent) -> str:
    """Dynamic instructions, rebuilt before every model call, so the numbers
    stay right after a "keep going" top-up."""
    c = ctx.context
    b = c.budget
    size = c.canvas.size
    vision = fill(VISION_ON, LOOKS=b.looks_total) if c.settings.vision else VISION_OFF
    prompt = fill(PAINTER_PROMPT, SIZE=size, HALF=size // 2, SHAPES=b.shapes_total,
                  MAX_PER_CALL=c.settings.max_shapes_per_call, VISION_SECTION=vision)
    status = (f"Shapes painted so far: {b.shapes_used}. "
              f"Shapes left: {b.shapes_left} of {b.shapes_total}.")
    if c.settings.vision:
        status += f" Looks left: {b.looks_left} of {b.looks_total}."
    return f"{prompt}\n\n# Right now\n\n{status}"


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


def build_painter_agent(settings: StudioSettings, model=None) -> Agent[PaintyContext]:
    """model overrides settings.model (tests pass a scripted fake model)."""
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
        ),
        tool_use_behavior=_stop_when_done,
    )
