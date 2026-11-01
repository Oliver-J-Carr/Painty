"""Whole drawings with a scripted (fake) model: no API key, no cost.

ScriptedModel is the Agents SDK's own test double. We script exactly which
tool calls the "painter" makes, then check the canvas, the gallery and the
events the browser would receive.
"""

import asyncio
import json

from agents import set_tracing_disabled
from agents.testing import ScriptedModel, assistant_message, function_call

from app.gallery import Gallery
from app.settings_store import SettingsStore
from app.studio import Studio

set_tracing_disabled(True)


def rect(x0, y0, x1, y1, colour="#336699", **extra):
    shape = {"kind": "rectangle", "start": {"x": x0, "y": y0}, "end": {"x": x1, "y": y1},
             "fill": {"style": "solid", "colors": [colour], "angle": 0},
             "opacity": 1, "rotation": 0, "thickness": 0, "outline_color": None}
    shape.update(extra)
    return shape


def paint(shapes, cid, note="test"):
    return function_call("paint", {"shapes": shapes, "note": note}, call_id=cid)


def look(cid, focus="check"):
    return function_call("look", {"focus": focus}, call_id=cid)


def finish(cid, title="Test", note="A test painting."):
    return function_call("finish", {"title": title, "note": note}, call_id=cid)


def make_studio(tmp_path, steps, **settings):
    events = []

    async def send(event):
        events.append(event)

    store = SettingsStore(tmp_path / "settings.json", ("fake-model",))
    store.update(settings)
    model = ScriptedModel(steps)
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
    assert len(json.loads((folder / "shapes.json").read_text())) == 2


def test_invalid_batch_paints_nothing_and_costs_nothing(tmp_path):
    bad = rect(10, 10, 50, 50, "red")  # not hex
    steps = [
        [paint([rect(0, 0, 100, 100), bad], "c1")],
        [paint([rect(0, 0, 100, 100), rect(10, 10, 50, 50, "#ff0000")], "c2")],
        [finish("c3")],
    ]
    studio, model, events = make_studio(tmp_path, steps)

    async def go():
        await studio.start("anything")
        await _wait(studio)
    run(go)

    results = {e["key"]: e for e in types(events, "activity") if e.get("status") in ("done", "error") and e.get("key")}
    assert results["c1"]["status"] == "error" and "'red' is not a hex colour" in results["c1"]["result"]
    assert results["c2"]["status"] == "done"
    assert len(studio.ctx.canvas) == 2 and studio.ctx.budget.shapes_used == 2


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
    meta = json.loads((tmp_path / "gallery" / studio.saved_id / "meta.json").read_text())
    assert meta["top_ups"] == 1 and meta["shapes_budget"] == 7


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
