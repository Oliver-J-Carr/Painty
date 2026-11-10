"""Whole drawings with a scripted (fake) model: no API key, no cost.

ScriptedModel is the Agents SDK's own test double. We script exactly which
tool calls the "painter" makes, then check the canvas, the gallery and the
events the browser would receive.
"""

import asyncio
import base64
import dataclasses
import io
import json
from types import SimpleNamespace

import pytest
from agents import Usage, set_tracing_disabled
from PIL import Image
from agents.testing import ScriptedModel, assistant_message, function_call
from openai import BadRequestError
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from app.gallery import Gallery
from app.settings_store import SettingsStore
import app.studio as studio_module
from app.studio import Studio

set_tracing_disabled(True)


def rect(x0, y0, x1, y1, colour="#336699"):
    return f"rectangle {x0} {y0} {x1} {y1} {colour}"


def paint(shapes, cid, note="test"):
    return function_call("paint", {"shapes": shapes, "note": note}, call_id=cid)


def look(cid, focus="check"):
    return function_call("look", {"focus": focus}, call_id=cid)


def finish(cid, title="Test", note="A test painting."):
    return function_call("finish", {"title": title, "note": note}, call_id=cid)


def plan(cid, text="1. Composition: a red square on blue."):
    return function_call("plan", {"plan": text}, call_id=cid)


def make_studio(tmp_path, steps, usage=None, **settings):
    """usage: the Usage every scripted model call reports (None = all zeros)."""
    events = []

    async def send(event):
        events.append(event)

    store = SettingsStore(tmp_path / "settings.json", ("fake-model",))
    store.update(settings)
    model = ScriptedModel(steps, default_usage=usage)
    studio = Studio(send, store, Gallery(tmp_path / "gallery"), model=model)
    return studio, model, events


def run(coro_fn):
    return asyncio.run(coro_fn())


async def _wait(studio):
    while studio.task and not studio.task.done():
        await asyncio.sleep(0.01)


def types(events, kind):
    return [e for e in events if e["type"] == kind]


def test_paint_look_finish_saves_to_gallery(tmp_path):
    steps = [
        [paint([rect(0, 0, 512, 512, "#87ceeb"), rect(100, 100, 200, 200, "#ff0000")], "c1")],
        [look("c2", "is the red square there?")],
        [finish("c3", "Red square", "A red square on a blue sky.")],
    ]
    studio, model, events = make_studio(tmp_path, steps)

    async def go():
        await studio.start("a red square")
        await _wait(studio)
    run(go)

    assert studio.state == "finished"
    assert len(studio.ctx.canvas) == 2
    shape_events = types(events, "shapes")
    assert [s["n"] for s in shape_events[0]["shapes"]] == [1, 2]
    looks = types(events, "look")
    assert len(looks) == 1 and looks[0]["image"].startswith("data:image/png;base64,")
    assert looks[0]["key"] == "c2"

    # The model received the image as part of the look tool's output.
    outputs = [i for i in model.calls[2].input if isinstance(i, dict) and i.get("type") == "function_call_output"]
    look_output = next(o for o in outputs if o["call_id"] == "c2")["output"]
    assert any(part.get("type") == "input_image" for part in look_output)

    final = types(events, "status")[-1]
    assert final["state"] == "finished" and final["title"] == "Red square"
    folder = tmp_path / "gallery" / final["saved_id"]
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["shapes_used"] == 2 and meta["looks_used"] == 1 and meta["ending"] == "finished"
    assert (folder / "painting.png").exists() and (folder / "looks" / "look-1.png").exists()
    assert (folder / "shapes.txt").read_text() == (
        f"{rect(0, 0, 512, 512, '#87ceeb')}\n{rect(100, 100, 200, 200, '#ff0000')}\n")
    assert meta["shape_format"] == "notation"
    log = json.loads((folder / "shapes.json").read_text())
    assert [s["text"] for s in log] == [rect(0, 0, 512, 512, "#87ceeb"), rect(100, 100, 200, 200, "#ff0000")]
    assert log[1]["kind"] == "rectangle" and log[1]["fill"]["colors"] == ["#ff0000"]


def _px(path, x, y):
    return Image.open(io.BytesIO(path.read_bytes())).convert("RGB").getpixel((x, y))


def test_every_paint_call_is_saved_as_a_stage_with_its_looks(tmp_path):
    steps = [
        [paint([rect(0, 0, 512, 512, "#0000ff")], "c1", note="sky")],
        [look("c2", "is the sky there?")],
        [paint([rect(100, 100, 200, 200, "#ff0000")], "c3", note="red square")],
        [finish("c4")],
    ]
    studio, _, events = make_studio(tmp_path, steps)

    async def go():
        await studio.start("a red square on blue")
        await _wait(studio)
    run(go)

    folder = tmp_path / "gallery" / studio.saved_id
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["stages"] == [
        {"n": 1, "first": 1, "last": 1, "note": "sky", "looks": [1]},
        {"n": 2, "first": 2, "last": 2, "note": "red square", "looks": []},
    ]
    assert meta["looks"] == [{"n": 1, "focus": "is the sky there?", "shapes": 1}]
    # Each stage is the canvas as it was after that paint call, with no grid.
    assert _px(folder / "stages" / "stage-1.png", 150, 150) == (0, 0, 255)
    assert _px(folder / "stages" / "stage-2.png", 150, 150) == (255, 0, 0)
    assert _px(folder / "stages" / "stage-1.png", 64, 64) == (0, 0, 255)  # no grid line


def test_stages_follow_what_was_actually_painted(tmp_path):
    bad = rect(10, 10, 50, 50, "red")  # not hex
    steps = [
        [look("c1", "blank?")],
        [paint([rect(0, 0, 100, 100), bad, rect(60, 60, 90, 90)], "c2", note="partly")],
        [paint([bad], "c3", note="nothing")],
        [paint([rect(10, 10, 50, 50, "#ff0000"), rect(60, 60, 90, 90)], "c4", note="the rest")],
        [finish("c5")],
    ]
    studio, _, events = make_studio(tmp_path, steps)

    async def go():
        await studio.start("anything")
        await _wait(studio)
    run(go)

    folder = tmp_path / "gallery" / studio.saved_id
    meta = json.loads((folder / "meta.json").read_text())
    assert [(st["n"], st["first"], st["last"], st["note"], st["looks"]) for st in meta["stages"]] == [
        (0, 0, 0, "Blank canvas", [1]),   # the look before any paint
        (1, 1, 1, "partly", []),          # only the shape before the bad one
        (2, 2, 3, "the rest", []),        # the batch that painted nothing made no stage
    ]
    assert sorted(p.name for p in (folder / "stages").iterdir()) == [
        "stage-0.png", "stage-1.png", "stage-2.png"]
    assert _px(folder / "stages" / "stage-0.png", 50, 50) == (255, 255, 255)


def test_cached_and_reasoning_tokens_are_counted_and_saved(tmp_path):
    per_call = Usage(requests=1, input_tokens=1000, output_tokens=200, total_tokens=1200,
                     input_tokens_details=InputTokensDetails(cached_tokens=600, cache_write_tokens=100),
                     output_tokens_details=OutputTokensDetails(reasoning_tokens=150))
    steps = [
        [paint([rect(0, 0, 512, 512)], "c1")],
        [finish("c2")],
    ]
    studio, _, events = make_studio(tmp_path, steps, usage=per_call)

    async def go():
        await studio.start("a blue square")
        await _wait(studio)
    run(go)

    final = types(events, "status")[-1]
    assert final["tokens"] == {"input": 2000, "cached": 1200, "cache_write": 200,
                               "output": 400, "reasoning": 300}
    meta = json.loads((tmp_path / "gallery" / final["saved_id"] / "meta.json").read_text())
    assert (meta["input_tokens"], meta["cached_tokens"], meta["cache_write_tokens"]) == (2000, 1200, 200)
    assert (meta["output_tokens"], meta["reasoning_tokens"]) == (400, 300)


def _results(events):
    return {e["key"]: e for e in types(events, "activity")
            if e.get("status") in ("done", "error") and e.get("key")}


def test_bad_shape_stops_the_batch_there_and_costs_nothing(tmp_path):
    bad = rect(10, 10, 50, 50, "red")  # not hex
    steps = [
        [paint([rect(0, 0, 100, 100), bad, rect(60, 60, 90, 90)], "c1")],
        [paint([rect(10, 10, 50, 50, "#ff0000"), rect(60, 60, 90, 90)], "c2")],  # the rest, fixed
        [finish("c3")],
    ]
    studio, model, events = make_studio(tmp_path, steps)

    async def go():
        await studio.start("anything")
        await _wait(studio)
    run(go)

    results = _results(events)
    first = results["c1"]["result"]
    assert results["c1"]["status"] == "done"
    assert "Painted only the first 1" in first and "Send shape 2 onwards again" in first
    assert "'red' is not a hex colour" in first
    shapes_events = types(events, "shapes")
    assert [s["n"] for s in shapes_events[0]["shapes"]] == [1]   # nothing after the bad one
    assert results["c2"]["status"] == "done"
    assert len(studio.ctx.canvas) == 3 and studio.ctx.budget.shapes_used == 3
    assert [ps.text for ps in studio.ctx.canvas.shapes][1] == "rectangle 10 10 50 50 #ff0000"


def test_bad_first_shape_paints_nothing_and_lists_every_problem(tmp_path):
    steps = [
        [paint(["ellips 0 0 100 100 #fff", rect(0, 0, 10, 10), "line 0 0 50 50 #000 wobble=2"], "c1")],
        [finish("c2")],
    ]
    studio, _, events = make_studio(tmp_path, steps)

    async def go():
        await studio.start("anything")
        await _wait(studio)
    run(go)

    result = _results(events)["c1"]
    assert result["status"] == "error" and result["result"].startswith("Error: nothing was painted")
    assert "shape 1 \"ellips 0 0 100 100 #fff\": unknown kind 'ellips'" in result["result"]
    assert "shape 3" in result["result"] and "unknown extra 'wobble'" in result["result"]
    assert len(studio.ctx.canvas) == 0 and studio.ctx.budget.shapes_used == 0


def test_shapes_crammed_into_one_string_are_split(tmp_path):
    steps = [
        [paint([f"{rect(0, 0, 100, 100)}\n{rect(10, 10, 50, 50)}; {rect(20, 20, 30, 30)}"], "c1")],
        [finish("c2")],
    ]
    studio, _, events = make_studio(tmp_path, steps, max_shapes_per_call=3)

    async def go():
        await studio.start("anything")
        await _wait(studio)
    run(go)

    assert len(studio.ctx.canvas) == 3
    running = next(e for e in types(events, "activity") if e.get("key") == "c1" and e["status"] == "running")
    assert running["text"].startswith("🖌️ Painting 3 shapes") and running["detail"] == "3 rectangles"


def test_batch_over_per_call_limit_is_rejected(tmp_path):
    steps = [
        [paint([rect(0, 0, 10 + i, 10 + i) for i in range(4)], "c1")],
        [finish("c2")],
    ]
    studio, _, events = make_studio(tmp_path, steps, max_shapes_per_call=3)

    async def go():
        await studio.start("anything")
        await _wait(studio)
    run(go)

    assert len(studio.ctx.canvas) == 0
    err = next(e for e in types(events, "activity") if e.get("key") == "c1" and e.get("status") == "error")
    assert "at most 3 shapes" in err["result"]


def test_budget_pause_then_keep_going_resumes_same_conversation(tmp_path):
    steps = [
        [paint([rect(0, 0, 100, 100), rect(0, 0, 50, 50)], "c1")],   # uses the whole budget
        [paint([rect(200, 200, 300, 300)], "c2")],                     # after keep going
        [finish("c3", "Done")],
    ]
    studio, model, events = make_studio(tmp_path, steps, shape_budget=2)

    async def go():
        await studio.start("squares")
        await _wait(studio)
        assert studio.state == "paused"
        paused = types(events, "status")[-1]
        # Suggests shapes (they ran out) and no looks (some are left); you type the amounts.
        assert paused["state"] == "paused" and paused["top_up"] == {"shapes": 20, "looks": 0}
        await studio.keep_going(5, 0)
        await _wait(studio)
    run(go)

    assert studio.state == "finished"
    assert len(studio.ctx.canvas) == 3
    assert studio.ctx.budget.shapes_total == 7 and studio.ctx.budget.top_ups == 1
    # The resumed call saw the earlier paint call and the keep-going message.
    resumed_input = model.calls[1].input
    assert any(isinstance(i, dict) and i.get("call_id") == "c1" for i in resumed_input)
    assert "5 more shapes" in str(resumed_input[-1])
    assert "Shapes left: 5 of 7." in str(resumed_input[-1])
    # The new totals reach the instructions, and the drawing keeps its cache key.
    assert "at most 2 shapes" in model.calls[0].system_instructions
    assert "at most 7 shapes" in model.calls[1].system_instructions
    keys = {c.model_settings.extra_args["prompt_cache_key"] for c in model.calls}
    assert keys == {studio.drawing_id}
    meta = json.loads((tmp_path / "gallery" / studio.saved_id / "meta.json").read_text())
    assert meta["top_ups"] == 1 and meta["shapes_budget"] == 7


def test_keep_going_needs_a_shape_when_none_are_left(tmp_path):
    steps = [[paint([rect(0, 0, 100, 100)], "c1")], [finish("c2")]]
    studio, _, events = make_studio(tmp_path, steps, shape_budget=1)

    async def go():
        await studio.start("a square")
        await _wait(studio)
        await studio.keep_going(0, 3)
        assert studio.state == "paused" and studio.ctx.budget.top_ups == 0
        assert "add at least one" in types(events, "error")[-1]["text"]
        await studio.keep_going("lots", 3)  # not a number: counts as 0
        assert studio.state == "paused"
        await studio.keep_going(9999, 9999)  # clamped to the most you may add
        await _wait(studio)
    run(go)

    assert studio.ctx.budget.shapes_total == 1 + 500 and studio.ctx.budget.looks_total == 5 + 50
    assert studio.state == "finished"


def test_a_refused_look_pauses_to_ask_for_more_looks(tmp_path):
    steps = [
        [paint([rect(0, 0, 100, 100)], "c1")],
        [look("c2")],                          # no looks left: refused, and the run pauses
        [look("c3")],                          # after keep going with 2 more looks
        [finish("c4")],
    ]
    studio, model, events = make_studio(tmp_path, steps, shape_budget=10, look_budget=0)

    async def go():
        await studio.start("a square")
        await _wait(studio)
        assert studio.state == "paused"
        paused = types(events, "status")[-1]
        assert paused["reason"] == "Painty wanted to look, but it has no looks left."
        assert paused["top_up"] == {"shapes": 0, "looks": 2}  # shapes are left, so none suggested
        await studio.keep_going(0, 2)
        await _wait(studio)
    run(go)

    assert studio.state == "finished"
    b = studio.ctx.budget
    assert (b.shapes_total, b.looks_total, b.looks_used, b.top_ups) == (10, 2, 1, 1)
    refused, message = model.calls[2].input[-2:]
    assert refused["call_id"] == "c2" and "pauses here" in str(refused["output"])
    assert message["content"].startswith("You have been given 2 more looks. Shapes left: 9 of 10. Looks left: 2 of 2.")


def test_keep_going_without_more_looks_carries_on_blind(tmp_path):
    steps = [[look("c1")], [paint([rect(0, 0, 100, 100)], "c2")], [finish("c3")]]
    studio, model, events = make_studio(tmp_path, steps, shape_budget=10, look_budget=0)

    async def go():
        await studio.start("a square")
        await _wait(studio)
        assert studio.state == "paused"
        await studio.keep_going(0, 0)
        await _wait(studio)
    run(go)

    assert studio.state == "finished" and studio.ctx.budget.top_ups == 0
    message = str(model.calls[1].input[-1])
    assert "No more shapes or looks were added" in message and "no looks left" in message


def test_painting_blind_ignores_added_looks(tmp_path):
    steps = [[paint([rect(0, 0, 100, 100)], "c1")], [finish("c2")]]
    studio, _, events = make_studio(tmp_path, steps, shape_budget=1, vision=False)

    async def go():
        await studio.start("a square")
        await _wait(studio)
        assert types(events, "status")[-1]["top_up"] == {"shapes": 20, "looks": None}
        await studio.keep_going(3, 4)
        await _wait(studio)
    run(go)

    assert studio.ctx.budget.shapes_total == 4 and studio.ctx.budget.looks_total == 0


def test_instructions_stay_the_same_within_a_run(tmp_path):
    # The prompt cache only reuses an exact prefix, and the instructions come first,
    # so live counts in them would make every call miss the cache.
    steps = [
        [paint([rect(0, 0, 512, 512)], "c1")],
        [look("c2")],
        [paint([rect(100, 100, 200, 200)], "c3")],
        [finish("c4")],
    ]
    studio, model, _ = make_studio(tmp_path, steps)

    async def go():
        await studio.start("squares")
        await _wait(studio)
    run(go)

    assert studio.state == "finished" and len(model.calls) == 4
    assert len({c.system_instructions for c in model.calls}) == 1
    assert all(c.model_settings.extra_args == {"prompt_cache_key": studio.drawing_id}
               for c in model.calls)


def test_a_portrait_drawing_uses_a_tall_canvas_everywhere(tmp_path):
    steps = [
        [paint([rect(0, 0, 512, 768, "#87ceeb"), rect(0, 700, 512, 768, "#00aa00")], "c1")],
        [look("c2")],
        [finish("c3")],
    ]
    studio, model, events = make_studio(tmp_path, steps)

    async def go():
        await studio.start("a tall tree", "portrait")
        await _wait(studio)
    run(go)

    assert studio.state == "finished" and len(studio.ctx.canvas) == 2
    reset = types(events, "canvas_reset")[0]
    assert (reset["shape"], reset["width"], reset["height"]) == ("portrait", 512, 768)

    # The prompt describes the real canvas, with no placeholder left unfilled.
    instructions = model.calls[0].system_instructions
    assert "portrait (taller than wide) canvas, 512 pixels wide and 768 pixels tall" in instructions
    assert "(512, 768) the bottom-right" in instructions and "The centre is (256, 384)" in instructions
    assert "{{" not in instructions

    folder = tmp_path / "gallery" / types(events, "status")[-1]["saved_id"]
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["canvas"] == {"shape": "portrait", "width": 512, "height": 768}
    assert Image.open(folder / "painting.png").size == (1024, 1536)
    assert _px(folder / "stages" / "stage-1.png", 10, 750) == (0, 170, 0)


def test_unknown_canvas_shape_falls_back_to_square(tmp_path):
    studio, _, events = make_studio(tmp_path, [[finish("c1")]])

    async def go():
        await studio.start("anything", "hexagon")
        await _wait(studio)
    run(go)

    assert studio.ctx.canvas.info() == {"shape": "square", "width": 512, "height": 512}
    assert studio.hello()["canvases"] == {"square": [512, 512], "landscape": [768, 512],
                                          "portrait": [512, 768]}


def _reference_url(size=(900, 600), colour=(200, 120, 40)):
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _images_in(items):
    """Every input_image in a model call's input items."""
    found = []
    for item in items:
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, list):
            found += [part for part in content if isinstance(part, dict) and part.get("type") == "input_image"]
    return found


def test_reference_alone_means_recreate_it(tmp_path):
    steps = [
        [paint([rect(0, 0, 768, 512, "#c87828")], "c1")],
        [finish("c2", "Orange", "A copy of the reference.")],
    ]
    studio, model, events = make_studio(tmp_path, steps, exemplar=False)

    async def go():
        await studio.start("", "landscape", _reference_url())
        await _wait(studio)
    run(go)

    assert studio.state == "finished"
    # The reference is in the first user message, with the ask to recreate it...
    first = model.calls[0].input[0]
    assert first["role"] == "user"
    assert any("Recreate this reference image" in part.get("text", "") for part in first["content"])
    assert len(_images_in(model.calls[0].input)) == 1
    # ...and stays in the history, so later calls still have it (once, not again each time).
    assert len(_images_in(model.calls[1].input)) == 1

    instructions = model.calls[0].system_instructions
    assert "# The reference image" in instructions and "768 x 512" in instructions
    assert "When you look, compare your canvas with the reference" in instructions
    assert "{{" not in instructions

    reset = types(events, "canvas_reset")[0]
    assert reset["prompt"] == "" and reset["reference"].startswith("data:image/png;base64,")
    assert any("recreate the reference image" in e["text"] for e in types(events, "activity"))
    # The feed shows exactly the image the model got: canvas-sized, inside the ruler frame.
    shown = next(e["image"] for e in types(events, "activity") if e.get("image"))
    assert shown == _images_in(model.calls[0].input)[0]["image_url"]
    png = base64.b64decode(shown.split(",", 1)[1])
    assert Image.open(io.BytesIO(png)).size == (768 + 60, 512 + 60)

    folder = tmp_path / "gallery" / types(events, "status")[-1]["saved_id"]
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["reference"] is True and meta["prompt"] == ""
    assert Image.open(folder / "reference.png").size == (768, 512)
    assert Image.open(folder / "reference-view.png").size == (768 + 60, 512 + 60)


def test_reference_with_a_prompt_paints_the_prompt_and_works_blind(tmp_path):
    studio, model, _ = make_studio(tmp_path, [[finish("c1")]], vision=False)

    async def go():
        await studio.start("my dog as a cartoon", "square", _reference_url())
        await _wait(studio)
    run(go)

    texts = [p.get("text", "") for p in model.calls[0].input[0]["content"]]
    assert any(t.startswith("Paint this: my dog as a cartoon") for t in texts)
    assert "You can see the reference but not your canvas" in model.calls[0].system_instructions


def test_unreadable_reference_starts_nothing(tmp_path):
    studio, model, events = make_studio(tmp_path, [[finish("c1")]])

    async def go():
        await studio.start("", "square", "data:image/png;base64,AAAA")
        await _wait(studio)
    run(go)

    assert studio.state == "idle" and studio.ctx is None and not model.calls
    assert not types(events, "canvas_reset")
    assert "couldn't be read" in types(events, "error")[0]["text"]


def test_no_reference_section_without_a_reference(tmp_path):
    studio, model, events = make_studio(tmp_path, [[paint([rect(0, 0, 10, 10)], "c1")], [finish("c2")]],
                                        exemplar=False)

    async def go():
        await studio.start("a dot")
        await _wait(studio)
    run(go)

    instructions = model.calls[0].system_instructions
    assert "reference" not in instructions.lower() and "\n\n\n" not in instructions
    assert model.calls[0].input[0]["content"] == "Paint this: a dot"
    folder = tmp_path / "gallery" / types(events, "status")[-1]["saved_id"]
    assert json.loads((folder / "meta.json").read_text())["reference"] is False
    assert not (folder / "reference.png").exists()


def test_settings_sent_just_before_paint_apply_to_that_drawing(tmp_path):
    # The New painting dialog sends its settings, then paint, on the same socket.
    studio, model, events = make_studio(tmp_path, [[finish("c1")]])

    async def go():
        await studio.update_settings({"shape_budget": 7, "look_budget": 2, "vision": False,
                                      "max_shapes_per_call": 3, "reasoning_effort": "high"})
        await studio.start("a dot")
        await _wait(studio)
    run(go)

    c = studio.ctx
    assert (c.budget.shapes_total, c.budget.looks_total) == (7, 0)  # vision off: no looks
    assert c.settings.max_shapes_per_call == 3 and c.settings.reasoning_effort == "high"
    assert "look" not in {t.name for t in model.calls[0].tools}
    assert types(events, "canvas_reset")[0]["settings"]["shape_budget"] == 7


def test_paintings_can_be_starred_and_unstarred(tmp_path):
    studio, _, events = make_studio(tmp_path, [[paint([rect(0, 0, 10, 10)], "c1")], [finish("c2")]])

    async def go():
        await studio.start("a dot")
        await _wait(studio)
    run(go)

    gallery = studio.gallery
    pid = types(events, "status")[-1]["saved_id"]
    assert "favourite" not in gallery.list()[0]  # new paintings aren't starred
    assert gallery.set_favourite(pid, True)["favourite"] is True
    assert gallery.list()[0]["favourite"] is True
    assert gallery.list()[0]["title"] == "Test"  # the rest of meta.json is kept
    gallery.set_favourite(pid, False)
    assert gallery.list()[0]["favourite"] is False
    assert not list((tmp_path / "gallery" / pid).glob("*.tmp"))


def test_starring_refuses_unknown_or_outside_ids(tmp_path):
    root = tmp_path / "gallery"
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "meta.json").write_text("{}")
    gallery = Gallery(root)
    root.mkdir()
    for bad in ("nope", "..", "../secret", "a/b", ""):
        assert gallery.set_favourite(bad, True) is None
    assert json.loads((tmp_path / "secret" / "meta.json").read_text()) == {}


def test_vision_off_has_no_look_tool(tmp_path):
    steps = [[paint([rect(0, 0, 100, 100)], "c1")], [finish("c2")]]
    studio, model, _ = make_studio(tmp_path, steps, vision=False)

    async def go():
        await studio.start("blind")
        await _wait(studio)
    run(go)

    tool_names = [t.name for t in model.calls[0].tools]
    assert tool_names == ["paint", "finish"]
    assert "cannot see the canvas" in model.calls[0].system_instructions
    assert studio.ctx.budget.looks_total == 0


def test_looks_run_out_gracefully(tmp_path):
    steps = [[look("c1")], [look("c2")], [finish("c3")]]
    studio, _, events = make_studio(tmp_path, steps, look_budget=1)

    async def go():
        await studio.start("look twice")
        await _wait(studio)
    run(go)

    assert len(types(events, "look")) == 1
    err = next(e for e in types(events, "activity") if e.get("key") == "c2" and e.get("status") == "error")
    assert "no looks left" in err["result"]


def test_agent_stopping_without_finish_still_saves(tmp_path):
    steps = [[paint([rect(0, 0, 100, 100)], "c1")], [assistant_message("All done, a blue square.")]]
    studio, _, events = make_studio(tmp_path, steps)

    async def go():
        await studio.start("square")
        await _wait(studio)
    run(go)

    final = types(events, "status")[-1]
    assert final["state"] == "finished" and final["ending"] == "ended"
    assert final["note"] == "All done, a blue square."


def test_turn_limit_pauses_and_can_resume(tmp_path):
    # Budget 1 shape + 0 looks + 10 spare = 11 model calls allowed. Waste them all.
    steps = [[paint([], f"w{i}")] for i in range(11)] + [
        [paint([rect(0, 0, 100, 100)], "c1")],
        [finish("c2")],
    ]
    studio, _, events = make_studio(tmp_path, steps, shape_budget=1, look_budget=0)

    async def go():
        await studio.start("waste turns")
        await _wait(studio)
        assert studio.state == "paused"
        assert types(events, "status")[-1]["reason"] == "The hidden turn limit was reached."
        assert studio.history is not None
        await studio.keep_going(5, 0)
        await _wait(studio)
    run(go)

    assert studio.state == "finished" and len(studio.ctx.canvas) == 1


def test_finished_carries_an_estimated_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(studio_module, "env", dataclasses.replace(
        studio_module.env, prices={"PAINTY_PRICE_FAKE_MODEL": (2.0, 0.5, 10.0, 2.5)}))
    per_call = Usage(requests=1, input_tokens=1000, output_tokens=200, total_tokens=1200,
                     input_tokens_details=InputTokensDetails(cached_tokens=600, cache_write_tokens=100),
                     output_tokens_details=OutputTokensDetails(reasoning_tokens=0))
    steps = [[paint([rect(0, 0, 512, 512)], "c1")], [finish("c2")]]
    studio, _, events = make_studio(tmp_path, steps, usage=per_call)

    async def go():
        await studio.start("a blue square")
        await _wait(studio)
    run(go)

    c = types(events, "status")[-1]["cost"]
    # two calls: 600 uncached at $2, 1200 cached at $0.50, 200 written at $2.50, 400 out at $10
    assert c["usd"] == pytest.approx((600 * 2 + 1200 * 0.5 + 200 * 2.5 + 400 * 10) / 1e6)


def test_stopped_drawing_keeps_the_tokens_it_used(tmp_path):
    # Regression: tokens were read from the run's result, which a stopped run never has.
    per_call = Usage(requests=1, input_tokens=1000, output_tokens=10, total_tokens=1010)
    steps = [[paint([rect(0, 0, 100, 100)], "c1")]] + [[look(f"l{i}")] for i in range(50)]
    studio, _, events = make_studio(tmp_path, steps, usage=per_call, look_budget=50, shape_budget=2)

    async def go():
        await studio.start("stop me")
        while len(types(events, "look")) < 2:
            await asyncio.sleep(0.01)
        await studio.stop()
        await _wait(studio)
        await studio.save()
    run(go)

    meta = json.loads((tmp_path / "gallery" / studio.saved_id / "meta.json").read_text())
    assert meta["input_tokens"] >= 3000 and meta["input_tokens"] % 1000 == 0  # paint and two looks
    assert meta["output_tokens"] == meta["input_tokens"] // 100


def test_stop_cancels_and_save_keeps_the_painting(tmp_path):
    steps = [[paint([rect(0, 0, 100, 100)], "c1")]] + [[look(f"l{i}")] for i in range(50)]
    studio, _, events = make_studio(tmp_path, steps, look_budget=50, shape_budget=2)

    async def go():
        await studio.start("stop me")
        while len(types(events, "look")) < 2:
            await asyncio.sleep(0.01)
        await studio.stop()
        await _wait(studio)
        assert studio.state == "stopped"
        await studio.save()
    run(go)

    assert studio.state == "finished" and studio.saved_id
    meta = json.loads((tmp_path / "gallery" / studio.saved_id / "meta.json").read_text())
    assert meta["ending"] == "saved"


def test_settings_locked_while_painting(tmp_path):
    steps = [[look(f"l{i}")] for i in range(50)]
    studio, _, events = make_studio(tmp_path, steps, look_budget=50)

    async def go():
        await studio.start("busy")
        while not types(events, "look"):
            await asyncio.sleep(0.01)
        await studio.update_settings({"shape_budget": 7})
        await studio.stop()
        await _wait(studio)
    run(go)

    assert studio.store.current.shape_budget != 7
    assert any(e["type"] == "error" and "locked" in e["text"] for e in events)


# ------------------------------------------------- new shapes, exemplar, grid
def test_new_kinds_and_extras_paint_and_save(tmp_path):
    shapes = ["diamond 60 60 233 160 #eeeeee", "rectangle 60 135 146 275 #c03030 skew=30",
              "trail 280 80 60@90 40@135 #1d3557 thickness=6 smooth=1",
              "ellipse 300 260 400 330 #6b8e23 spray=0.5",
              "line 420 260 480 300 #c0392b soft=4 bend=10@0 taper=end"]
    steps = [[paint(shapes, "c1")], [finish("c2")]]
    studio, model, events = make_studio(tmp_path, steps)

    async def go():
        await studio.start("an isometric box")
        await _wait(studio)
    run(go)

    assert len(studio.ctx.canvas) == 5
    running = next(e for e in types(events, "activity") if e.get("key") == "c1" and e["status"] == "running")
    assert running["detail"] == "1 diamond, 1 rectangle, 1 trail, 1 ellipse, 1 line"
    # The painter hears where each walk ended.
    outputs = [i for i in model.calls[1].input if isinstance(i, dict) and i.get("type") == "function_call_output"]
    result = next(o for o in outputs if o["call_id"] == "c1")["output"]
    assert "shape 3 (trail): turns at (340, 80), then ends at (368, 108)" in result
    folder = tmp_path / "gallery" / types(events, "status")[-1]["saved_id"]
    log = json.loads((folder / "shapes.json").read_text())
    assert log[1]["skew"] == 30 and log[2]["smooth"] is True and log[3]["spray"] == 0.5
    assert [leg["heading"] for leg in log[2]["legs"]] == [90, 135]
    assert log[4]["bend"] == {"distance": 10, "heading": 0} and log[4]["taper"] == "end"


def test_exemplar_comes_first_in_the_opening_message_and_is_saved(tmp_path):
    steps = [[paint([rect(0, 0, 10, 10)], "c1")], [finish("c2")]]
    studio, model, events = make_studio(tmp_path, steps)  # the exemplar is on by default

    async def go():
        await studio.start("a dot")
        await _wait(studio)
    run(go)

    content = model.calls[0].input[0]["content"]
    assert content[0]["type"] == "input_text" and content[0]["text"].startswith("Exemplar:")
    assert [p["type"] for p in content[1:]] == ["input_image", "input_image", "input_text"]
    assert content[-1]["text"] == "Paint this: a dot"
    # Sent once: still two images on the next call, not four.
    assert len(_images_in(model.calls[1].input)) == 2
    assert "# The exemplar sheets" in model.calls[0].system_instructions
    assert any(e["text"].startswith("📐") for e in types(events, "activity"))
    folder = tmp_path / "gallery" / studio.saved_id
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["exemplar_sheets"] == ["exemplar-1.png", "exemplar-2.png"]
    assert meta["settings"]["exemplar"] is True and meta["settings"]["grid"] == "frame"
    assert (folder / "exemplar-2.png").exists()


def test_exemplar_off_sends_none(tmp_path):
    steps = [[paint([rect(0, 0, 10, 10)], "c1")], [finish("c2")]]
    studio, model, events = make_studio(tmp_path, steps, exemplar=False)

    async def go():
        await studio.start("a dot")
        await _wait(studio)
    run(go)

    assert model.calls[0].input[0]["content"] == "Paint this: a dot"
    assert "exemplar" not in model.calls[0].system_instructions.lower()
    meta = json.loads((tmp_path / "gallery" / studio.saved_id / "meta.json").read_text())
    assert meta["exemplar_sheets"] == []


def _look_size(model, call):
    """The size of the image the look in an earlier step returned."""
    for item in model.calls[call].input:
        if isinstance(item, dict) and item.get("type") == "function_call_output" \
                and isinstance(item.get("output"), list):
            for part in item["output"]:
                if part.get("type") == "input_image":
                    raw = base64.b64decode(part["image_url"].split(",", 1)[1])
                    return Image.open(io.BytesIO(raw)).size
    return None


def test_grid_mode_sets_what_every_view_looks_like(tmp_path):
    for grid, size, note in (("frame", (828, 572), "ruler frame"),
                             ("overlay", (768, 512), "small numbers along the top and left"),
                             ("none", (768, 512), "with no grid or ruler")):
        steps = [[look("c1")], [finish("c2")]]
        studio, model, _ = make_studio(tmp_path / grid, steps, grid=grid)

        async def go():
            await studio.start("", "landscape", _reference_url())
            await _wait(studio)
        run(go)

        assert _look_size(model, 1) == size, grid
        images = _images_in(model.calls[0].input)  # two exemplar sheets, then the reference
        sizes = [Image.open(io.BytesIO(base64.b64decode(i["image_url"].split(",", 1)[1]))).size
                 for i in images]
        assert sizes == [size] * 3, grid
        instructions = model.calls[0].system_instructions
        assert note in instructions, grid
        assert instructions.count(note) == 3, grid  # the reference, the looks and the exemplar


def test_unknown_grid_mode_keeps_the_current_one(tmp_path):
    studio, _, _ = make_studio(tmp_path, [], grid="overlay")
    studio.store.update({"grid": "hexagons", "exemplar": 0})
    assert studio.store.current.grid == "overlay" and studio.store.current.exemplar is False
    assert studio.hello()["options"]["grids"] == ["frame", "overlay", "none"]


def test_a_benchmark_card_is_the_reference_and_is_saved_for_scoring(tmp_path):
    from app.benchmark import BENCHMARK_PROMPT, CARDS, card_json, card_png
    from app.view import agent_view, data_url

    steps = [[paint([rect(0, 0, 10, 10)], "c1")], [finish("c2")]]
    studio, model, events = make_studio(tmp_path, steps, exemplar=False, grid="none")

    async def go():
        # The card sets the canvas shape, and replaces any image the browser sent.
        await studio.start(BENCHMARK_PROMPT, "portrait", _reference_url(), benchmark="intermediate")
        await _wait(studio)
    run(go)

    assert studio.state == "finished"
    reset = types(events, "canvas_reset")[0]
    assert reset["benchmark"] == "intermediate" and (reset["width"], reset["height"]) == (768, 512)
    # Painty gets the card's exact pixels, in the drawing's grid mode.
    sent = _images_in(model.calls[0].input)
    assert len(sent) == 1
    assert sent[0]["image_url"] == data_url(agent_view(card_png("intermediate"), 768, 512, "none"))
    assert any(e["text"] == "🎯 Benchmark: Rotation, skew and blends" for e in types(events, "activity"))

    folder = tmp_path / "gallery" / studio.saved_id
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["benchmark"] == "intermediate" and meta["reference"] is True
    assert (folder / "reference.png").read_bytes() == card_png("intermediate")
    assert json.loads((folder / "benchmark.json").read_text()) == card_json("intermediate")
    # Scored when saved: in meta.json and the finished status, with the difference picture.
    score = meta["benchmark_score"]
    assert score["card"] == "intermediate" and score["scorer"] == 2
    assert 0 <= score["score"] < 0.2 and len(score["items"]) == len(CARDS["intermediate"].items)
    assert types(events, "status")[-1]["benchmark_score"] == score
    assert Image.open(folder / "benchmark-diff.png").size == (768 * 3 + 24, 512 + 24)
    hello = studio.hello()
    assert [c["name"] for c in hello["benchmarks"]] == list(CARDS)
    assert hello["benchmark_prompt"] == BENCHMARK_PROMPT


def test_a_perfect_copy_of_a_card_scores_full_marks(tmp_path):
    from app.benchmark import CARDS

    shapes = [t for item in CARDS["solids"].items for t in item.shapes]
    steps = [[paint(shapes, "c1")], [finish("c2")]]
    studio, _, events = make_studio(tmp_path, steps, exemplar=False)

    async def go():
        await studio.start("", "", None, benchmark="solids")
        await _wait(studio)
    run(go)

    score = types(events, "status")[-1]["benchmark_score"]
    assert score["score"] == 1 and list(score["offset"]) == [0, 0]
    assert all(i["score"] == 1 for i in score["items"])


def test_a_failed_score_never_stops_the_save(tmp_path, monkeypatch):
    def broken(*args):
        raise RuntimeError("scorer broke")
    monkeypatch.setattr(studio_module, "score_painting", broken)
    steps = [[paint([rect(0, 0, 10, 10)], "c1")], [finish("c2")]]
    studio, _, events = make_studio(tmp_path, steps, exemplar=False)

    async def go():
        await studio.start("", "", None, benchmark="basic")
        await _wait(studio)
    run(go)

    meta = json.loads((tmp_path / "gallery" / studio.saved_id / "meta.json").read_text())
    assert meta["benchmark"] == "basic" and "benchmark_score" not in meta
    assert types(events, "status")[-1]["benchmark_score"] is None


def test_a_benchmark_card_alone_means_recreate_it(tmp_path):
    studio, model, _ = make_studio(tmp_path, [[finish("c1")]], exemplar=False)

    async def go():
        await studio.start("", "", None, benchmark="basic")
        await _wait(studio)
    run(go)

    texts = [p.get("text", "") for p in model.calls[0].input[0]["content"]]
    assert "Recreate this reference image as a painting." in texts


def test_unknown_benchmark_card_starts_nothing(tmp_path):
    studio, model, events = make_studio(tmp_path, [[finish("c1")]])

    async def go():
        await studio.start("copy it", "square", None, benchmark="mona-lisa")
        await _wait(studio)
    run(go)

    assert studio.state == "idle" and studio.ctx is None and not model.calls
    assert "no benchmark card called mona-lisa" in types(events, "error")[0]["text"]


def test_a_painting_without_a_card_has_no_benchmark(tmp_path):
    steps = [[paint([rect(0, 0, 10, 10)], "c1")], [finish("c2")]]
    studio, _, _ = make_studio(tmp_path, steps, exemplar=False)

    async def go():
        await studio.start("a dot")
        await _wait(studio)
    run(go)

    folder = tmp_path / "gallery" / studio.saved_id
    meta = json.loads((folder / "meta.json").read_text())
    assert "benchmark" not in meta and "benchmark_score" not in meta
    assert not (folder / "benchmark.json").exists() and not (folder / "benchmark-diff.png").exists()


def test_a_benchmark_has_a_fixed_budget_and_no_top_ups(tmp_path):
    from app.benchmark import CARDS

    card = CARDS["basic"]  # 8 shapes: a budget of 35 shapes and 3 looks
    assert card.shape_budget == 35
    batch = [rect(0, 0, 10 + i, 10) for i in range(15)]
    steps = [[paint(batch, "c1")], [look("c2")], [paint(batch, "c3")], [paint(batch[:5], "c4")]]
    studio, model, events = make_studio(tmp_path, steps, exemplar=False, shape_budget=200, look_budget=9)

    async def go():
        await studio.start("copy it", "", None, benchmark="basic")
        await _wait(studio)
    run(go)

    # The card's budget, not the saved settings, which keep your own.
    assert (studio.ctx.budget.shapes_total, studio.ctx.budget.looks_total) == (card.shape_budget, card.look_budget)
    assert studio.ctx.settings.shape_budget == card.shape_budget
    assert studio.store.current.shape_budget == 200 and studio.store.current.look_budget == 9
    assert f"{card.shape_budget} shapes" in model.calls[0].system_instructions

    # Out of shapes: paused with no top-up offered, and keep going is refused.
    paused = types(events, "status")[-1]
    assert paused["state"] == "paused" and paused["top_up"] is None

    async def more():
        await studio.keep_going()
    run(more)
    assert studio.state == "paused" and studio.ctx.budget.top_ups == 0
    assert "fixed budget" in types(events, "error")[-1]["text"]

    async def save():
        await studio.save()
    run(save)
    meta = json.loads((tmp_path / "gallery" / studio.saved_id / "meta.json").read_text())
    assert meta["shapes_budget"] == card.shape_budget and meta["looks_budget"] == card.look_budget
    assert meta["settings"]["shape_budget"] == card.shape_budget


def test_a_blind_benchmark_has_no_looks(tmp_path):
    studio, _, _ = make_studio(tmp_path, [[finish("c1")]], exemplar=False, vision=False)

    async def go():
        await studio.start("", "", None, benchmark="letters")
        await _wait(studio)
    run(go)

    assert studio.ctx.budget.looks_total == 0


def test_a_benchmark_carries_on_after_a_refused_look(tmp_path):
    # A benchmark can't keep going, so running out of looks mustn't pause it.
    steps = [[look(f"c{i}")] for i in range(1, 5)] + [[finish("c5")]]  # the card allows 3 looks
    studio, model, events = make_studio(tmp_path, steps, exemplar=False)

    async def go():
        await studio.start("copy it", "", None, benchmark="basic")
        await _wait(studio)
    run(go)

    assert studio.state == "finished" and studio.ctx.budget.looks_used == 3
    refused = model.calls[4].input[-1]
    assert refused["call_id"] == "c4" and "Carry on" in str(refused["output"])


# ------------------------------------------------------------------ planning

def test_planning_off_has_no_plan_tool_or_section(tmp_path):
    steps = [[paint([rect(0, 0, 100, 100)], "c1")], [finish("c2")]]
    studio, model, _ = make_studio(tmp_path, steps)

    async def go():
        await studio.start("no plan")
        await _wait(studio)
    run(go)

    assert [t.name for t in model.calls[0].tools] == ["paint", "look", "finish"]
    assert "# Planning" not in model.calls[0].system_instructions
    assert model.calls[0].model_settings.tool_choice is None
    meta = json.loads((tmp_path / "gallery" / studio.saved_id / "meta.json").read_text())
    assert meta["plans"] == [] and not (tmp_path / "gallery" / studio.saved_id / "plan.txt").exists()


def test_planning_turn_is_forced_at_its_own_effort_then_paints(tmp_path):
    steps = [
        [plan("c1", "Parts: sky, then square.")],
        [paint([rect(0, 0, 512, 512, "#0000ff"), rect(100, 100, 200, 200, "#ff0000")], "c2")],
        [finish("c3")],
    ]
    studio, model, events = make_studio(tmp_path, steps, planning=True, planning_effort="low",
                                        reasoning_effort="medium")

    async def go():
        await studio.start("a red square on blue")
        await _wait(studio)
    run(go)

    assert studio.state == "finished" and len(model.calls) == 3
    first, second = model.calls[0], model.calls[1]
    assert first.model_settings.tool_choice == "plan" and first.model_settings.reasoning.effort == "low"
    assert second.model_settings.tool_choice is None and second.model_settings.reasoning.effort == "medium"
    # Everything before the conversation is the same, so the painter reuses the cache.
    assert len({c.system_instructions for c in model.calls}) == 1
    assert len({tuple(t.name for t in c.tools) for c in model.calls}) == 1
    assert [t.name for t in first.tools] == ["plan", "paint", "look", "finish"]
    assert "# Planning" in first.system_instructions
    assert all(c.model_settings.extra_args == {"prompt_cache_key": studio.drawing_id}
               for c in model.calls)
    # The painter carries on from the planning turn's history, plan included.
    outputs = [i for i in second.input if isinstance(i, dict) and i.get("type") == "function_call_output"]
    assert any(o["call_id"] == "c1" and "Plan 1 recorded" in o["output"] for o in outputs)

    feed = types(events, "activity")
    assert any("is planning" in e["text"] for e in feed)
    plan_entry = next(e for e in feed if e.get("key") == "c1" and e.get("status") == "running")
    assert plan_entry["tool"] == "plan" and plan_entry["detail"] == "Parts: sky, then square."

    folder = tmp_path / "gallery" / studio.saved_id
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["plans"] == [{"n": 1, "shapes": 0}]
    assert meta["settings"]["planning"] is True and meta["settings"]["planning_effort"] == "low"
    assert "Plan 1, written before painting" in (folder / "plan.txt").read_text()
    assert "Parts: sky, then square." in (folder / "plan.txt").read_text()
    assert studio.ctx.budget.shapes_used == 2  # planning costs no paint


def test_the_plan_can_be_revised_later(tmp_path):
    steps = [
        [plan("c1", "First plan.")],
        [paint([rect(0, 0, 100, 100)], "c2")],
        [plan("c3", "Second plan.")],
        [finish("c4")],
    ]
    studio, model, events = make_studio(tmp_path, steps, planning=True)

    async def go():
        await studio.start("revise")
        await _wait(studio)
    run(go)

    assert studio.state == "finished"
    assert [(p.text, p.shapes) for p in studio.ctx.plans] == [("First plan.", 0), ("Second plan.", 1)]
    assert model.calls[2].model_settings.tool_choice is None  # only the first turn is forced
    folder = tmp_path / "gallery" / studio.saved_id
    assert json.loads((folder / "meta.json").read_text())["plans"] == [
        {"n": 1, "shapes": 0}, {"n": 2, "shapes": 1}]
    text = (folder / "plan.txt").read_text()
    assert "Plan 2, written after 1 shapes" in text and "Second plan." in text


def test_a_planning_turn_without_a_plan_still_paints(tmp_path):
    steps = [
        [assistant_message("I will paint a square.")],
        [paint([rect(0, 0, 100, 100)], "c1")],
        [finish("c2")],
    ]
    studio, model, events = make_studio(tmp_path, steps, planning=True)

    async def go():
        await studio.start("no plan written")
        await _wait(studio)
    run(go)

    assert studio.state == "finished" and len(studio.ctx.canvas) == 1
    assert studio.ctx.plans == []
    assert any("didn't write a plan" in e["text"] for e in types(events, "activity"))

    assert model.calls[1].input[-1] == {"role": "user", "content": "Now paint it."}


def test_planning_only_happens_at_the_start_not_on_keep_going(tmp_path):
    steps = [
        [plan("c1")],
        [paint([rect(0, 0, 100, 100)], "c2")],
        [paint([rect(0, 0, 50, 50)], "c3")],
        [finish("c4")],
    ]
    studio, model, _ = make_studio(tmp_path, steps, planning=True, shape_budget=1)

    async def go():
        await studio.start("pause then carry on")
        await _wait(studio)
        assert studio.state == "paused"
        await studio.keep_going(shapes=5)
        await _wait(studio)
    run(go)

    assert studio.state == "finished" and len(model.calls) == 4
    assert [c.model_settings.tool_choice for c in model.calls] == ["plan", None, None, None]
    assert len(studio.ctx.plans) == 1


def test_an_empty_plan_is_refused(tmp_path):
    steps = [[plan("c1", "  ")], [plan("c2", "A real plan.")], [finish("c3")]]
    studio, model, events = make_studio(tmp_path, steps, planning=True)

    async def go():
        await studio.start("empty plan")
        await _wait(studio)
    run(go)

    err = next(e for e in types(events, "activity") if e.get("key") == "c1" and e.get("status") == "error")
    assert "plan is empty" in err["result"]
    assert [p.text for p in studio.ctx.plans] == ["A real plan."]
    # No extra nudge after a refused plan: the error already says what to do.
    assert not any(i == {"role": "user", "content": "Now paint it."} for i in model.calls[1].input)


# ------------------------------------------------------------------ the canvas record

def test_every_paint_result_lists_what_is_on_the_canvas_from_the_notes(tmp_path):
    steps = [
        [paint([rect(0, 0, 512, 300, "#0000ff"), rect(0, 300, 512, 512, "#00aa00")], "c1", note="sky and grass")],
        [paint([rect(200, 200, 300, 300, "#ff0000")], "c2", note="red barn")],
        [paint([rect(220, 220, 240, 240, "#ffffff"), "ellips 0 0 9 9 #fff"], "c3", note="barn windows")],
        [finish("c4")],
    ]
    studio, _, events = make_studio(tmp_path, steps)

    async def go():
        await studio.start("a barn")
        await _wait(studio)
    run(go)

    results = {e["key"]: e["result"] for e in types(events, "activity")
               if e.get("kind") == "tool" and e.get("result")}
    assert results["c1"].endswith(
        "On the canvas so far, in paint order (from your notes):\n- shapes 1 to 2: sky and grass")
    assert results["c2"].endswith("- shapes 1 to 2: sky and grass\n- shape 3: red barn")
    # A batch stopped by a bad shape says so, and only what landed is listed.
    assert results["c3"].endswith("- shape 3: red barn\n- shape 4: barn windows (cut short by a problem)")
    # The record is not a note the gallery keeps: stages keep the plain note.
    meta = json.loads((tmp_path / "gallery" / studio.saved_id / "meta.json").read_text())
    assert [st["note"] for st in meta["stages"]] == ["sky and grass", "red barn", "barn windows"]


def test_the_canvas_record_shortens_long_notes(tmp_path):
    long_note = "a very long description " * 20
    steps = [[paint([rect(0, 0, 100, 100)], "c1", note=long_note)], [finish("c2")]]
    studio, _, events = make_studio(tmp_path, steps)

    async def go():
        await studio.start("long note")
        await _wait(studio)
    run(go)

    result = next(e["result"] for e in types(events, "activity") if e.get("key") == "c1" and e.get("result"))
    line = result.splitlines()[-1]
    assert line.startswith("- shape 1: a very long description") and line.endswith("…")
    assert len(line) < 140


# ------------------------------------------------------------------ models through OpenRouter
def bad_request(body):
    """A 400 from a model's host, as the OpenAI client raises it."""
    response = SimpleNamespace(request=None, status_code=400, headers={})
    return BadRequestError("Error code: 400 - Provider returned error", response=response, body=body)


def provider_error(message):
    raw = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": message}})
    return bad_request({"message": "Provider returned error", "code": 400,
                        "metadata": {"raw": raw, "provider_name": "Anthropic"}})


def test_a_model_that_refuses_forced_tool_calls_is_asked_to_plan_instead(tmp_path):
    # Regression: Claude Sonnet 5.5 and Opus 5.5 answer a forced tool_choice with a 400,
    # which failed the whole drawing before it painted anything.
    refused = provider_error('tool_choice: type "tool" and "any" are not supported for this model.')
    steps = [refused, [plan("c0")], [paint([rect(0, 0, 512, 512)], "c1")], [finish("c2")]]
    studio, model, events = make_studio(tmp_path, steps, planning=True, planning_effort="low")

    async def go():
        await studio.start("a blue square")
        await _wait(studio)
    run(go)

    assert studio.state == "finished" and len(studio.ctx.plans) == 1
    assert [c.model_settings.tool_choice for c in model.calls] == ["plan", None, None, None]
    assert model.calls[1].model_settings.reasoning.effort == "low"  # still the planning turn
    assert any("can't be made to call a tool" in e["text"] for e in types(events, "activity"))
    meta = json.loads((tmp_path / "gallery" / studio.saved_id / "meta.json").read_text())
    assert meta["plan_forced"] is False


def test_a_forced_plan_is_recorded_and_planning_off_records_nothing(tmp_path):
    steps = [[plan("c0")], [paint([rect(0, 0, 512, 512)], "c1")], [finish("c2")]]
    studio, _, _ = make_studio(tmp_path / "on", steps, planning=True)
    other, _, _ = make_studio(tmp_path / "off", [[paint([rect(0, 0, 512, 512)], "c1")], [finish("c2")]])

    async def go():
        for s in (studio, other):
            await s.start("a blue square")
            await _wait(s)
    run(go)

    assert json.loads((tmp_path / "on" / "gallery" / studio.saved_id / "meta.json").read_text())["plan_forced"] is True
    assert "plan_forced" not in json.loads((tmp_path / "off" / "gallery" / other.saved_id / "meta.json").read_text())


def test_other_errors_on_the_planning_turn_still_fail_and_say_what_the_host_said(tmp_path):
    studio, model, events = make_studio(tmp_path, [provider_error("the image is too big")], planning=True)

    async def go():
        await studio.start("a blue square")
        await _wait(studio)
    run(go)

    assert studio.state == "error" and len(model.calls) == 1
    assert types(events, "status")[-1]["reason"] == "the image is too big"
    assert any(e.get("detail") == "the image is too big" for e in types(events, "activity"))


def test_tool_calls_in_one_turn_run_one_at_a_time_in_order(tmp_path):
    # Not every OpenRouter host honours parallel_tool_calls=False, and paint order is the picture.
    steps = [[paint([rect(0, 0, 512, 512, "#ff0000")], "c1"), look("c2")], [finish("c3")]]
    studio, _, _ = make_studio(tmp_path, steps)
    events = []

    async def slow_send(event):  # gives another tool the chance to start, if it's allowed to
        await asyncio.sleep(0.001)
        events.append(event)
    studio.send = slow_send

    async def go():
        await studio.start("a red square")
        await _wait(studio)
    run(go)

    steps_seen = [(e["key"], e["status"]) for e in types(events, "activity") if e.get("kind") == "tool"]
    assert steps_seen[:4] == [("c1", "running"), ("c1", "done"), ("c2", "running"), ("c2", "done")]
    assert studio.ctx.looks[0].shapes == 1  # the look saw the paint from the same turn


def test_what_openrouter_billed_is_added_up_and_saved(tmp_path):
    steps = [{"output": [paint([rect(0, 0, 512, 512)], "c1")], "raw_usage": {"cost": 0.002}},
             {"output": [finish("c2")], "raw_usage": {"cost": 0.0005}}]
    events = []

    async def send(event):
        events.append(event)
    # A slash in the id asks for raw usage (providers.provider_settings), as a real run would.
    store = SettingsStore(tmp_path / "settings.json", ("google/gemma-4-31b-it",))
    studio = Studio(send, store, Gallery(tmp_path / "gallery"), model=ScriptedModel(steps))

    async def go():
        await studio.start("a blue square")
        await _wait(studio)
    run(go)

    final = types(events, "status")[-1]
    assert final["cost"]["billed"] == pytest.approx(0.0025)
    assert "billed" not in final["tokens"]
    meta = json.loads((tmp_path / "gallery" / final["saved_id"] / "meta.json").read_text())
    assert meta["billed_usd"] == pytest.approx(0.0025)


def test_a_native_model_has_no_billed_amount(tmp_path):
    studio, _, events = make_studio(tmp_path, [[paint([rect(0, 0, 512, 512)], "c1")], [finish("c2")]])

    async def go():
        await studio.start("a blue square")
        await _wait(studio)
    run(go)

    final = types(events, "status")[-1]
    assert "billed" not in final["cost"]
    assert "billed_usd" not in json.loads((tmp_path / "gallery" / final["saved_id"] / "meta.json").read_text())


def test_an_openrouter_model_with_no_key_is_refused_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(studio_module, "env", dataclasses.replace(studio_module.env, openrouter_api_key=""))
    events = []

    async def send(event):
        events.append(event)
    store = SettingsStore(tmp_path / "settings.json", ("google/gemma-4-31b-it",))
    studio = Studio(send, store, Gallery(tmp_path / "gallery"))
    assert studio.hello()["options"]["openrouter_key"] is False

    async def go():
        await studio.start("a cat")
    run(go)

    assert "OPENROUTER_API_KEY" in types(events, "error")[0]["text"]
    assert studio.state == "idle" and studio.task is None



# ------------------------------------------------------------------ fitted to the model
def capable_studio(tmp_path, caps, steps=(), **settings):
    """A studio whose model has the given capabilities (vision, reasoning)."""
    events = []

    async def send(event):
        events.append(event)
    store = SettingsStore(tmp_path / "settings.json", ("fake-model",))
    store.update(settings)
    library = SimpleNamespace(capabilities=lambda model: caps, model_info=lambda: {"fake-model": caps},
                              prices=lambda: {})
    studio = Studio(send, store, Gallery(tmp_path / "gallery"), model=ScriptedModel(list(steps)), library=library)
    return studio, events


TEXT_ONLY = {"vision": False, "reasoning": {"options": [["low", "Low"], ["high", "High (default)"]], "default": "high"}}


def test_a_text_only_model_paints_blind_without_the_exemplar_and_keeps_your_settings(tmp_path):
    steps = [[paint([rect(0, 0, 512, 512)], "c1")], [finish("c2")]]
    studio, events = capable_studio(tmp_path, TEXT_ONLY, steps, vision=True, exemplar=True, reasoning_effort="medium")
    assert studio.hello()["options"]["model_info"] == {"fake-model": TEXT_ONLY}

    async def go():
        await studio.start("a blue square")
        await _wait(studio)
    run(go)

    s = studio.ctx.settings
    assert (s.vision, s.exemplar, s.reasoning_effort) == (False, False, "high")
    assert studio.ctx.exemplar == () and studio.ctx.budget.looks_total == 0
    assert any(e.get("text", "").startswith("⚙️ Fitted to") for e in types(events, "activity"))
    saved = studio.store.current  # this drawing only
    assert (saved.vision, saved.exemplar, saved.reasoning_effort) == (True, True, "medium")


def test_a_text_only_model_refuses_a_reference_or_a_benchmark(tmp_path):
    studio, events = capable_studio(tmp_path, TEXT_ONLY)
    png = io.BytesIO()
    Image.new("RGB", (512, 512), "red").save(png, "PNG")
    url = "data:image/png;base64," + base64.b64encode(png.getvalue()).decode()

    async def go():
        await studio.start("", reference=url)
        await studio.start("copy it", benchmark="basic")
    run(go)

    errors = [e["text"] for e in types(events, "error")]
    assert len(errors) == 2 and all("can't see images" in t for t in errors)
    assert studio.state == "idle" and studio.task is None


def test_a_model_that_cant_reason_is_sent_no_effort(tmp_path):
    steps = [[paint([rect(0, 0, 512, 512)], "c1")], [finish("c2")]]
    studio, _ = capable_studio(tmp_path, {"vision": True, "reasoning": {"options": [], "default": ""}}, steps,
                               reasoning_effort="medium", planning_effort="high")

    async def go():
        await studio.start("a blue square")
        await _wait(studio)
    run(go)

    assert (studio.ctx.settings.reasoning_effort, studio.ctx.settings.planning_effort) == ("", "")
    assert studio.agent.model_settings.reasoning is None


def test_an_effort_the_model_takes_is_kept_and_unknown_models_are_left_alone(tmp_path):
    steps = [[paint([rect(0, 0, 512, 512)], "c1")], [finish("c2")]]
    studio, events = capable_studio(tmp_path / "a", {"vision": True, "reasoning": {
        "options": [["medium", "Medium (default)"], ["max", "Max"]], "default": "medium"}}, steps,
        reasoning_effort="max", planning_effort="medium")
    unknown, _ = capable_studio(tmp_path / "b", None, steps, reasoning_effort="max")

    async def go():
        for s in (studio, unknown):
            await s.start("a blue square")
            await _wait(s)
    run(go)

    assert studio.ctx.settings.reasoning_effort == "max" and unknown.ctx.settings.reasoning_effort == "max"
    assert studio.agent.model_settings.reasoning.effort == "max"
    assert not any(e.get("text", "").startswith("⚙️") for e in types(events, "activity"))
