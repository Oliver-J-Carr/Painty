"""Run hooks: turn agent lifecycle events into activity-feed entries.

RunHooks are callbacks the SDK fires as a run progresses. Each tool call gets
one feed entry, keyed by its call id, that flips from "running" to "done"
(or "error"). The look tool also sends a "look" event with the same key, so
the browser can pin a thumbnail of what the agent saw to that entry.
"""

from __future__ import annotations

import json
from typing import Any

from agents import Agent, RunContextWrapper, RunHooks

from .canvas import kind_of, split_batch
from .context import PaintyContext

_ICONS = {"plan": "📝", "paint": "🖌️", "look": "👀", "finish": "🏁"}


def _args(context: RunContextWrapper) -> dict[str, Any]:
    raw = getattr(context, "tool_arguments", None) or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def _shapes(a: dict[str, Any]) -> list[str]:
    """The paint call's shapes, one string each, split as the paint tool splits them."""
    shapes = a.get("shapes") or []
    return split_batch([s for s in shapes if isinstance(s, str)])


def _describe(tool_name: str, a: dict[str, Any]) -> str:
    match tool_name:
        case "plan":
            return "Writing a plan"
        case "paint":
            shapes = _shapes(a)
            note = (a.get("note") or "").strip()
            count = f"{len(shapes)} shape{'s' if len(shapes) != 1 else ''}"
            return f"Painting {count}" + (f": {note}" if note else "")
        case "look":
            focus = (a.get("focus") or "").strip()
            return "Looking at the canvas" + (f": {focus}" if focus else "")
        case "finish":
            return f"Finishing “{a.get('title', '')}”"
    return f"Using {tool_name}"


def _kinds(a: dict[str, Any]) -> str:
    """'3 ellipses, 1 triangle' for the paint entry's detail line."""
    counts: dict[str, int] = {}
    for s in _shapes(a):
        kind = kind_of(s)
        counts[kind] = counts.get(kind, 0) + 1
    return ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in counts.items())


class ActivityHooks(RunHooks[PaintyContext]):
    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:
        await context.context.emit({"type": "phase", "phase": "thinking"})

    async def on_llm_end(self, context, agent, response) -> None:
        context.context.tokens.add(response.usage)

    async def on_tool_start(self, context, agent, tool) -> None:
        args = _args(context)
        detail = {"paint": lambda: _kinds(args), "finish": lambda: args.get("note", ""),
                  "plan": lambda: args.get("plan", "")}.get(tool.name, lambda: "")()
        await context.context.emit({"type": "phase", "phase": tool.name})
        await context.context.emit(
            {"type": "activity", "kind": "tool", "status": "running",
             "key": getattr(context, "tool_call_id", None), "tool": tool.name,
             "text": f"{_ICONS.get(tool.name, '🛠️')} {_describe(tool.name, args)}",
             "detail": detail}
        )

    async def on_tool_end(self, context, agent, tool, result) -> None:
        if isinstance(result, list):  # look returns text + image; show the text part
            text = " ".join(getattr(r, "text", "") for r in result if getattr(r, "text", None))
        else:
            text = str(result)
        await context.context.emit(
            {"type": "activity", "kind": "tool",
             "status": "error" if text.startswith("Error") else "done",
             "key": getattr(context, "tool_call_id", None), "tool": tool.name, "result": text}
        )

    async def on_agent_start(self, context: RunContextWrapper[PaintyContext], agent: Agent) -> None:
        c = context.context
        if agent.model_settings.tool_choice == "plan":  # the planning turn (painter.planning_agent)
            effort = c.settings.planning_effort or "model default"
            await c.activity("agent", f"📝 {agent.name} is planning the painting",
                             f"model: {c.settings.model}, planning effort: {effort}")
            return
        await c.activity("agent", f"🧑‍🎨 {agent.name} picked up the brush", f"model: {c.settings.model}")
