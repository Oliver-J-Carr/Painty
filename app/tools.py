"""Tools the painter agent can call.

plan            writes down the agent's plan for the painting (only when planning is on)
paint           lays down an ordered batch of shapes (the only way to put paint down)
look            shows the agent a picture of the canvas (only when vision is on)
request_shapes  asks the person for more shapes near the end (not in a benchmark)
finish          ends the drawing with a title and a short artist's note

Errors are returned as "Error: ..." text, not raised, so the model can read
them and try again. A batch is painted only up to its first invalid shape, so
the layering the agent planned is never left with a gap in it: the agent sends
the rest again, fixed, and it lands in the planned order.

Docstrings matter: the SDK sends them to the model as tool descriptions.
"""

from __future__ import annotations

from agents import RunContextWrapper, ToolOutputImage, ToolOutputText, function_tool

from .canvas import check_batch, split_batch
from .context import Look, PaintyContext, Plan, ShapeRequest, Stage
from .view import DEFAULT_GRID, GRID_STEP, agent_view_png, data_url

Ctx = RunContextWrapper[PaintyContext]

MAX_TITLE = 80
MAX_NOTE = 500
MAX_PLAN = 8000  # characters of a plan kept for the gallery (the model's own copy stays whole)
# What a look's result says about the view, for each grid mode.
GRID_RESULT = {
    "frame": f"Ruler and grid lines (every {GRID_STEP} px) are in your view only.",
    "overlay": f"Grid lines (every {GRID_STEP} px) and their numbers are in your view only.",
    "none": "No grid: one pixel of the image is one pixel of the canvas.",
}


LEDGER_NOTE = 120  # characters of each batch's note in the record of what is on the canvas


def canvas_record(c: PaintyContext) -> str:
    """What is on the canvas, batch by batch in paint order, from the agent's own
    notes. Every paint result ends with it, so the latest record of what is done
    is always the most recent thing in the conversation."""
    lines = []
    for st in c.stages:
        span = f"shape {st.first}" if st.first == st.last else f"shapes {st.first} to {st.last}"
        note = st.note if len(st.note) <= LEDGER_NOTE else st.note[:LEDGER_NOTE].rstrip() + "…"
        cut = " (cut short by a problem)" if st.partial else ""
        lines.append(f"- {span}: {note or '(no note)'}{cut}")
    return "On the canvas so far, in paint order (from your notes):\n" + "\n".join(lines)


# The agent may ask for more shapes once this many or fewer are left: near the end, not up front.
REQUEST_AT = 10
REQUEST_MAX = 500  # the most it may ask for at once (the paused card's own limit)


def budget_left(c: PaintyContext) -> str:
    b = c.budget
    if b.shapes_over:
        text = (f"Shapes: {b.shapes_used} painted, {b.shapes_over} over your budget of "
                f"{b.shapes_total}, so none are left.")
    else:
        text = f"Shapes left: {b.shapes_left} of {b.shapes_total}."
    if c.settings.vision:
        if b.looks_total is None:
            text += f" Looks so far: {b.looks_used}."
        else:
            text += f" Looks left: {b.looks_left} of {b.looks_total}."
    return text


def out_of_shapes(c: PaintyContext) -> str:
    """What to do once the shape budget is used up."""
    if c.benchmark:
        return "This benchmark's budget is fixed, so call finish now with a title and a note."
    if c.refused:
        return "Your request for more shapes was turned down, so call finish now with a title and a note."
    return ("If the painting is done, or close enough, call finish. If you need a few more shapes "
            "to finish what you are working on, call request_shapes.")


@function_tool
async def plan(ctx: Ctx, plan: str) -> str:
    """Write down your plan for the whole painting, before you paint anything.
    It costs no paint. The plan stays in front of you for the rest of the drawing,
    so make it specific enough to paint from. Use these five parts:

    1. Composition: what the picture shows, where the main subject sits on the
       canvas and how big it is, and what the background is.
    2. Depth: for each prominent area of the picture, its parts listed from the
       furthest away to the nearest, as the real object sits in three dimensions.
       Name every opening, hollow or frame, and what is inside it or seen through
       it: that is further back, so it is painted first, and the edge, rim or
       frame around it is painted after, overlapping it.
    3. Parts, in paint order, back to front, following the depth: every part of
       the picture, each with the shapes you will paint it with, its rough box in
       pixels (x1 y1 x2 y2) and its colours.
    4. Overlap check: for every pair of parts that touch or overlap, say which is
       nearer the viewer and confirm it comes later in the paint order. Fix the
       order where it doesn't.
    5. Stages: how the parts group into paint calls, and where you will look at
       the canvas to check (if you can look).

    Call it again only if the plan itself changes (a part moves, changes size or
    needs a different paint order), with the whole corrected plan.

    Args:
        plan: The plan, as plain text in the five parts above.
    """
    c = ctx.context
    text = (plan or "").strip()
    if not text:
        return "Error: the plan is empty. Write the plan in the five parts the tool describes."
    c.plans.append(Plan(text[:MAX_PLAN], len(c.canvas)))
    n = len(c.plans)
    again = "Your plan is updated." if n > 1 else "Now paint it, following your plan."
    return (f"Plan {n} recorded. {again} Call plan again only if the plan itself changes. "
            f"{budget_left(c)}")


@function_tool
async def paint(ctx: Ctx, shapes: list[str], note: str) -> str:
    """Paint a batch of shapes onto the canvas, in the order given.

    Each shape is one string: kind x1 y1 x2 y2 fill, then any extras, such as
    "ellipse 196 180 316 300 #f2c9a0 outline=#5a3a1a thickness=3". A trail is
    walked instead: trail x y, then legs written distance@heading, then the
    fill and extras, such as "trail 150 300 60@100 50@80 #5a3a1a smooth=1".
    Your instructions explain the shapes, fills and extras.

    Stay within your shape budget. A batch that goes past it is still painted in
    full, but after that nothing more can be painted unless you are given more
    shapes.

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
        note: Which parts of the picture this batch paints, naming each one, e.g.
            "sky and sea" or "left ear, right ear, inner ears". The viewer sees it, and
            every paint result lists your notes back to you as the record of what is
            on the canvas.
    """
    c = ctx.context
    b = c.budget
    texts = split_batch(shapes)
    if not texts:
        return "Error: the batch is empty. Send at least one shape."
    if c.refused:
        return f"Error: nothing was painted. {out_of_shapes(c)}"
    if b.shapes_left <= 0:
        return f"Error: nothing was painted. Your shape budget is used up. {out_of_shapes(c)}"
    if c.benchmark and len(texts) > b.shapes_left:  # a fixed budget: no going over
        return (f"Error: nothing was painted. Only {b.shapes_left} shapes are left in this benchmark's "
                f"fixed budget and this batch has {len(texts)}. Choose the most important ones.")

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
        c.stages.append(Stage(first, first + len(painted) - 1, (note or "").strip()[:MAX_NOTE],
                              partial=bool(errors)))
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
    result += "\n" + canvas_record(c)
    if b.shapes_left == 0:
        over = f" ({b.shapes_over} over)" if b.shapes_over else ""
        result += f"\nYour shape budget is used up{over}. {out_of_shapes(c)}"
    return result


@function_tool
async def look(ctx: Ctx, focus: str) -> list[ToolOutputText | ToolOutputImage]:
    """Look at the canvas as it is right now.

    You see the whole canvas. Your instructions say whether your view has a grid
    to help you match what you see to coordinates. Any grid, ruler or numbers are
    only in your view: they are not paint and are not part of the picture.

    Each look adds a picture to the conversation, so look after a meaningful
    stage rather than after every small change.

    Args:
        focus: What you want to check, e.g. "are the eyes level and evenly spaced?"
    """
    c = ctx.context
    b = c.budget
    if b.looks_left == 0:  # only a benchmark has a fixed number of looks
        return [ToolOutputText(text="Error: no looks left. Carry on from what you already know, "
                                    "or call finish if the painting is done.")]

    grid = c.settings.grid
    png = agent_view_png(c.canvas.to_svg(), c.canvas.width, c.canvas.height, grid)
    b.looks_used += 1
    c.looks.append(Look(png, (focus or "").strip()[:MAX_NOTE], len(c.canvas)))
    url = data_url(png)
    await c.emit({"type": "look", "key": ctx.tool_call_id, "n": b.looks_used,
                  "focus": focus, "image": url})
    await c.send_budget()
    text = f"Look {b.looks_used}: the canvas after {len(c.canvas)} shapes. " + GRID_RESULT.get(
        grid, GRID_RESULT[DEFAULT_GRID]) + f" {budget_left(c)}"
    return [ToolOutputText(text=text), ToolOutputImage(image_url=url, detail="high")]


@function_tool
async def request_shapes(ctx: Ctx, shapes: int, reason: str) -> str:
    """Ask the person for more shapes, to finish what you are working on.

    Only for the end of a painting, when your budget is nearly used up: you can
    ask once 10 or fewer shapes are left (or none). Always try to fit the painting
    in your budget first. Ask for what you really need, rounded to the nearest 10.
    The drawing pauses while the person decides. If they say yes, carry on
    painting. If they say no, call finish straight away.

    Args:
        shapes: How many more shapes you need, rounded to the nearest 10, e.g. 20.
        reason: A short message saying what you need them for, e.g. "the tiger's
            stripes and whiskers".
    """
    c = ctx.context
    b = c.budget
    if c.benchmark:
        return f"Error: {out_of_shapes(c)}"
    if c.refused:
        return f"Error: you already asked and were turned down. {out_of_shapes(c)}"
    if b.shapes_left > REQUEST_AT:
        return (f"Error: you still have {b.shapes_left} shapes left. Ask only near the end, once "
                f"{REQUEST_AT} or fewer are left. Carry on painting within your budget.")
    why = (reason or "").strip()
    if not why:
        return "Error: say what you need the shapes for in reason, then ask again."
    try:
        wanted = int(shapes)
    except (TypeError, ValueError):
        return "Error: shapes must be a whole number, such as 20."
    wanted = max(10, min(REQUEST_MAX, int(wanted / 10 + 0.5) * 10))
    c.requests.append(ShapeRequest(wanted, why[:MAX_NOTE], b.shapes_used))
    c.asking = True  # the run pauses after this, for the person to answer
    return (f"Asked for {wanted} more shapes. The drawing pauses while the person decides. "
            f"{budget_left(c)}")


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


def tools_for(vision: bool, planning: bool = False, requests: bool = True) -> list:
    """With vision off, the look tool doesn't exist at all for the agent; with
    planning off, neither does the plan tool; and in a benchmark (fixed budget),
    neither does request_shapes."""
    return (([plan] if planning else []) + [paint] + ([look] if vision else [])
            + ([request_shapes] if requests else []) + [finish])
