"""Tools the painter agent can call.

paint  lays down an ordered batch of shapes (the only way to put paint down)
look   shows the agent a picture of the canvas (only when vision is on)
finish ends the drawing with a title and a short artist's note

Errors are returned as "Error: ..." text, not raised, so the model can read
them and try again. A batch is painted only up to its first invalid shape, so
the layering the agent planned is never left with a gap in it: the agent sends
the rest again, fixed, and it lands in the planned order.

Docstrings matter: the SDK sends them to the model as tool descriptions.
"""

from __future__ import annotations

from agents import RunContextWrapper, ToolOutputImage, ToolOutputText, function_tool

from .canvas import check_batch, split_batch
from .context import Look, PaintyContext, Stage
from .view import GRID_STEP, agent_view_png, data_url

Ctx = RunContextWrapper[PaintyContext]

MAX_TITLE = 80
MAX_NOTE = 500


def budget_left(c: PaintyContext) -> str:
    b = c.budget
    text = f"Shapes left: {b.shapes_left} of {b.shapes_total}."
    if c.settings.vision:
        text += f" Looks left: {b.looks_left} of {b.looks_total}."
    return text


@function_tool
async def paint(ctx: Ctx, shapes: list[str], note: str) -> str:
    """Paint a batch of shapes onto the canvas, in the order given.

    Each shape is one string: kind x1 y1 x2 y2 fill, then any extras, such as
    "ellipse 196 180 316 300 #f2c9a0 outline=#5a3a1a thickness=3". Your
    instructions explain the fills and extras.

    Paint is permanent. Each shape is laid on top of everything painted before it,
    so where shapes overlap the later one covers the earlier one (partly, if its
    opacity is below 1). Nothing can be erased: fix a mistake by painting over it.
    Plan each batch back to front: the first shape in the list goes underneath.

    If a shape has a problem, the shapes before it are painted, but it and every
    shape after it are not, so your layers stay in the order you planned. You get
    a list of every problem. Fix them and send the unpainted shapes again, in the
    same order. Unpainted shapes cost no budget.

    Args:
        shapes: The shapes to paint, one string each, bottom layer first.
        note: A few words on what this batch adds, e.g. "sky and sea" or "shading under the chin".
    """
    c = ctx.context
    b = c.budget
    limit = c.settings.max_shapes_per_call
    texts = split_batch(shapes)
    if not texts:
        return "Error: the batch is empty. Send at least one shape."
    if len(texts) > limit:
        return (f"Error: nothing was painted. A batch can hold at most {limit} shapes and this one "
                f"has {len(texts)}. Split it into smaller batches, in the same order.")
    if len(texts) > b.shapes_left:
        return (f"Error: nothing was painted. Only {b.shapes_left} shapes are left in your budget "
                f"and this batch has {len(texts)}. Choose the most important ones.")

    good, errors, notes = check_batch(texts, c.canvas.width, c.canvas.height)
    listed = "\n".join(f"- {e}" for e in errors)
    if not good:
        return ("Error: nothing was painted, because of these problems "
                "(shape numbers count from 1 within this batch):\n"
                f"{listed}\nFix them and send the batch again.")

    async with c.paint_lock:
        first = len(c.canvas) + 1
        painted = c.canvas.add([s for _, s in good], [t for t, _ in good])
        b.shapes_used += len(painted)
        c.stages.append(Stage(first, first + len(painted) - 1, (note or "").strip()[:MAX_NOTE]))
        await c.emit({"type": "shapes", "key": ctx.tool_call_id,
                      "shapes": [{"n": p.n, "svg": p.svg} for p in painted]})
        await c.send_budget()

    last = first + len(painted) - 1
    span = f"shape {first}" if first == last else f"shapes {first} to {last}"
    if errors:
        stop = len(painted) + 1
        result = (f"Painted only the first {len(painted)} of this batch ({span} on the canvas). "
                  f"Shape {stop} onwards was not painted, because of these problems "
                  f"(shape numbers count from 1 within this batch):\n{listed}\n"
                  f"Send shape {stop} onwards again, fixed, in the same order. {budget_left(c)}")
    else:
        result = f"Painted {len(painted)} ({span} on the canvas). {budget_left(c)}"
    if notes:
        result += "\nNotes: " + "; ".join(notes) + "."
    if b.shapes_left == 0:
        result += "\nYour shape budget is used up, so the drawing pauses here."
    return result


@function_tool
async def look(ctx: Ctx, focus: str) -> list[ToolOutputText | ToolOutputImage]:
    """Look at the canvas as it is right now.

    Your view has a numbered ruler around the edge and faint grey grid lines every
    64 px, to help you match what you see to coordinates. The ruler and grid are
    only in your view: they are not paint and are not part of the picture.

    Each look uses one look from your budget, so look after a meaningful stage
    rather than after every small change.

    Args:
        focus: What you want to check, e.g. "are the eyes level and evenly spaced?"
    """
    c = ctx.context
    b = c.budget
    if b.looks_left <= 0:
        return [ToolOutputText(text="Error: no looks left. Carry on from what you already know, "
                                    "or call finish if the painting is done.")]

    png = agent_view_png(c.canvas.to_svg(), c.canvas.width, c.canvas.height)
    b.looks_used += 1
    c.looks.append(Look(png, (focus or "").strip()[:MAX_NOTE], len(c.canvas)))
    url = data_url(png)
    await c.emit({"type": "look", "key": ctx.tool_call_id, "n": b.looks_used,
                  "focus": focus, "image": url})
    await c.send_budget()
    text = (f"Look {b.looks_used}: the canvas after {len(c.canvas)} shapes. "
            f"Ruler and grid lines (every {GRID_STEP} px) are in your view only. {budget_left(c)}")
    return [ToolOutputText(text=text), ToolOutputImage(image_url=url, detail="high")]


@function_tool
async def finish(ctx: Ctx, title: str, note: str) -> str:
    """Finish the painting. Call this once, when the picture is complete or as
    good as you can make it. Painting stops after this call. Call it straight
    after your last paint call: there is no need to look at the canvas first.

    Args:
        title: A short title for the painting, e.g. "Ginger cat in the sun".
        note: One or two sentences for the viewer: what you painted and how.
    """
    c = ctx.context
    c.finish_title = (title or "").strip()[:MAX_TITLE] or "Untitled"
    c.finish_note = (note or "").strip()[:MAX_NOTE]
    return "Finished."


def tools_for(vision: bool) -> list:
    """With vision off, the look tool doesn't exist at all for the agent."""
    return [paint, look, finish] if vision else [paint, finish]
