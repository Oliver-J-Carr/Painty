"""Whole drawings with a scripted (fake) model: no API key, no cost.

ScriptedModel is the Agents SDK's own test double. We script exactly which
tool calls the "painter" makes, then check the canvas, the gallery and the
events the browser would receive.
"""

import asyncio
import base64
import io
import json

from agents import Usage, set_tracing_disabled
from PIL import Image
from agents.testing import ScriptedModel, assistant_message, function_call
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from app.gallery import Gallery
from app.settings_store import SettingsStore
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
                     input_tokens_details=InputTokensDetails(cached_tokens=600, cache_write_tokens=0),
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
    assert final["tokens"] == {"input": 2000, "cached": 1200, "output": 400, "reasoning": 300}
    meta = json.loads((tmp_path / "gallery" / final["saved_id"] / "meta.json").read_text())
    assert (meta["input_tokens"], meta["cached_tokens"]) == (2000, 1200)
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
    studio, model, events = make_studio(tmp_path, steps, shape_budget=2, top_up_shapes=5,
                                        top_up_looks=1)

    async def go():
        await studio.start("squares")
        await _wait(studio)
        assert studio.state == "paused"
        paused = types(events, "status")[-1]
        assert paused["state"] == "paused" and paused["top_up"] == {"shapes": 5, "looks": 1}
        await studio.keep_going()
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
    studio, model, events = make_studio(tmp_path, steps)

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
    studio, model, events = make_studio(tmp_path, [[paint([rect(0, 0, 10, 10)], "c1")], [finish("c2")]])

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
        await studio.keep_going()
        await _wait(studio)
    run(go)

    assert studio.state == "finished" and len(studio.ctx.canvas) == 1


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
