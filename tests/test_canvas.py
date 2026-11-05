"""The canvas and renderer: plain Python, no AI."""

import io

import pytest
from PIL import Image

from app.canvas import (Canvas, Shape, check_batch, check_shape, kind_of, normalise_hex,
                        parse_shape, shape_svg, split_batch)
from app.settings_store import SettingsStore, StudioSettings, clean
from app.view import FRAME, agent_view_png, render_png


def make(kind="rectangle", start=(10, 10), end=(110, 60), colours=("#ff0000",), style="solid",
         angle=0, opacity=1, rotation=0, thickness=0, outline=None) -> Shape:
    return Shape.model_validate({
        "kind": kind, "start": {"x": start[0], "y": start[1]}, "end": {"x": end[0], "y": end[1]},
        "fill": {"style": style, "colors": list(colours), "angle": angle},
        "opacity": opacity, "rotation": rotation, "thickness": thickness, "outline_color": outline,
    })


def pixel(svg: str, x: int, y: int) -> tuple[int, int, int]:
    return Image.open(io.BytesIO(render_png(svg))).convert("RGB").getpixel((x, y))


def test_any_drag_direction_paints_the_same_box():
    a = shape_svg(1, make(start=(10, 10), end=(110, 60)))
    b = shape_svg(1, make(start=(110, 60), end=(10, 10)))
    c = shape_svg(1, make(start=(10, 60), end=(110, 10)))
    assert a == b == c


def test_to_svg_upto_renders_only_the_first_shapes():
    canvas = Canvas()
    canvas.add([make(start=(0, 0), end=(100, 100), colours=["#ff0000"]),
                make(start=(50, 50), end=(150, 150), colours=["#0000ff"])])
    assert pixel(canvas.to_svg(upto=1), 75, 75) == (255, 0, 0)    # the blue square isn't there yet
    assert pixel(canvas.to_svg(upto=1), 125, 125) == (255, 255, 255)
    assert pixel(canvas.to_svg(upto=0), 25, 25) == (255, 255, 255)  # blank canvas
    assert pixel(canvas.to_svg(), 75, 75) == (0, 0, 255)


def test_later_shapes_cover_earlier_ones():
    canvas = Canvas()
    canvas.add([make(start=(0, 0), end=(100, 100), colours=["#ff0000"]),
                make(start=(50, 50), end=(150, 150), colours=["#0000ff"])])
    svg = canvas.to_svg()
    assert pixel(svg, 25, 25) == (255, 0, 0)
    assert pixel(svg, 75, 75) == (0, 0, 255)   # the overlap takes the newer colour
    assert pixel(svg, 300, 300) == (255, 255, 255)


def test_triangle_points_up_then_rotates_clockwise():
    up = shape_svg(1, make("triangle", (100, 100), (200, 200)))
    assert 'points="150,100 200,200 100,200"' in up and "rotate" not in up
    right = shape_svg(1, make("triangle", (100, 100), (200, 200), rotation=90))
    assert 'transform="rotate(90 150 150)"' in right
    canvas = Canvas()
    canvas.add([make("triangle", (100, 100), (200, 200), rotation=90, colours=["#000000"])])
    svg = canvas.to_svg()
    assert pixel(svg, 190, 150) == (0, 0, 0)        # tip now on the right
    assert pixel(svg, 105, 105) == (0, 0, 0)        # base now down the left side
    assert pixel(svg, 190, 105) == (255, 255, 255)  # corner beside the tip is empty


def test_linear_gradient_runs_in_compass_direction():
    canvas = Canvas()
    canvas.add([make(start=(0, 0), end=(512, 512), colours=["#000000", "#ffffff"],
                     style="linear", angle=90)])  # left to right
    svg = canvas.to_svg()
    left, right = pixel(svg, 5, 256), pixel(svg, 506, 256)
    assert left[0] < 20 and right[0] > 235


def test_opacity_blends_with_paint_beneath():
    canvas = Canvas()
    canvas.add([make(start=(0, 0), end=(100, 100), colours=["#0000ff"], opacity=0.5)])
    r, g, b = pixel(canvas.to_svg(), 50, 50)
    assert 120 < r < 135 and b == 255


def test_check_rejects_bad_values_and_notes_harmless_ones():
    errors, _ = check_shape(make(colours=["red"], opacity=0, end=(10, 900)))
    text = " ".join(errors)
    assert "'red' is not a hex colour" in text and "opacity" in text and "too far off" in text

    errors, _ = check_shape(make("line", (10, 10), (10, 10), thickness=4))
    assert any("no length" in e for e in errors)

    errors, _ = check_shape(make(start=(10, 10), end=(11, 50)))
    assert any("at least" in e for e in errors)

    errors, notes = check_shape(make(colours=["#fff", "#000"], thickness=3))
    assert errors == []
    assert any("first colour" in n for n in notes) and any("without an outline colour" in n for n in notes)


# ------------------------------------------------------------ shape notation
def parse(text) -> Shape:
    shape, errors, _ = parse_shape(text)
    assert errors == [], errors
    return shape


def test_notation_reads_every_part_of_a_shape():
    s = parse("rectangle 0 10 512 340.5 linear #4a90d9 #cfe8ff #ffffff 180 opacity=0.6 rotation=-20 "
              "outline=#222 thickness=3")
    assert s.kind == "rectangle" and (s.start.x, s.start.y, s.end.x, s.end.y) == (0, 10, 512, 340.5)
    assert s.fill.style == "linear" and s.fill.colors == ["#4a90d9", "#cfe8ff", "#ffffff"]
    assert s.fill.angle == 180
    assert (s.opacity, s.rotation, s.outline_color, s.thickness) == (0.6, -20, "#222", 3)


def test_notation_defaults():
    s = parse("ellipse 10 10 50 50 #ff0000")
    assert (s.fill.style, s.opacity, s.rotation, s.thickness, s.outline_color) == ("solid", 1, 0, 0, None)
    assert parse("ellipse 10 10 50 50 #ff0000 outline=#000").thickness == 2
    assert parse("line 10 10 50 50 #000").thickness == 3
    r = parse("triangle 10 10 50 50 radial #fff #000")
    assert r.fill.style == "radial" and r.fill.colors == ["#fff", "#000"]


def test_notation_paints_the_same_as_the_shape_it_describes():
    assert shape_svg(1, parse("rectangle 10 10 110 60 #ff0000")) == shape_svg(1, make())
    canvas = Canvas()
    canvas.add([parse("ellipse 100 100 300 300 #ffffff outline=#000000 thickness=10")])
    svg = canvas.to_svg()
    assert pixel(svg, 200, 102) == (0, 0, 0)          # the outline, on the top edge
    assert pixel(svg, 200, 200) == (255, 255, 255)    # the white fill inside


def test_notation_forgives_small_slips():
    s = parse("RECT 10, 10, 110, 60 #F00 opacity = 0.5")
    assert s.kind == "rectangle" and (s.end.x, s.end.y) == (110, 60) and s.opacity == 0.5
    assert normalise_hex(s.fill.colors[0]) == "#ff0000"


@pytest.mark.parametrize("text, message", [
    ("ellips 0 0 10 10 #fff", "unknown kind 'ellips'"),
    ("ellipse 0 0 10 #fff", "four numbers"),
    ("ellipse 0 0 10 10", "followed by the fill"),
    ("ellipse 0 0 10 10 opacity=0.5", "followed by the fill"),
    ("rectangle 0 0 10 10 linear #fff #000", "needs its angle"),
    ("rectangle 0 0 10 10 #fff wobble=2", "unknown extra 'wobble'"),
    ("rectangle 0 0 10 10 #fff 0.5", "unexpected '0.5'"),
    ("rectangle 0 0 10 10 #fff opacity=half", "opacity=half is not a number"),
])
def test_notation_rejects_what_it_cannot_read(text, message):
    shape, errors, _ = parse_shape(text)
    assert shape is None and any(message in e for e in errors), errors


def test_notation_notes_harmless_oddities():
    _, _, notes = parse_shape("ellipse 0 0 10 10 radial #fff #000 90")
    assert any("radial fills have no angle" in n for n in notes)
    shape, _, notes = parse_shape("line 0 0 10 10 #000 outline=#fff")
    assert shape.outline_color is None and any("outline is ignored" in n for n in notes)


def test_batch_is_painted_up_to_the_first_bad_shape():
    texts = ["rectangle 0 0 10 10 #fff", "rectangle 0 0 10 10 red",
             "rectangle 0 0 10 10 #fff", "ellipse 0 0 1 1 #fff",
             "rectangle 0 0 10 10 #fff #000"]
    good, errors, notes = check_batch(texts)
    assert [t for t, _ in good] == texts[:1]
    assert len(errors) == 2       # every problem is listed, not just the first
    assert errors[0].startswith('shape 2 "rectangle 0 0 10 10 red": ')
    assert errors[1].startswith("shape 4 ") and "at least" in errors[1]
    assert notes == []            # notes only cover shapes that get painted

    good, errors, notes = check_batch(texts[4:])
    assert len(good) == 1 and errors == [] and "first colour" in notes[0]


def test_crammed_shapes_are_split_and_kinds_read():
    assert split_batch(["rect 0 0 9 9 #fff\nline 0 0 9 9 #000; ", "  ", "ellipse 1 1 9 9 #fff"]) == [
        "rect 0 0 9 9 #fff", "line 0 0 9 9 #000", "ellipse 1 1 9 9 #fff"]
    assert kind_of("RECT 0 0 9 9 #fff") == "rectangle" and kind_of("") == "?"


def test_hex_colours_are_normalised():
    assert normalise_hex("#ABC") == "#aabbcc"
    assert normalise_hex("#12ab9F") == "#12ab9f"
    assert normalise_hex("12ab9f") is None and normalise_hex("#12ab9") is None


def test_agent_view_has_a_ruler_frame():
    canvas = Canvas()
    png = agent_view_png(canvas.to_svg(), canvas.width, canvas.height)
    img = Image.open(io.BytesIO(png))
    assert img.size == (canvas.width + 2 * FRAME, canvas.height + 2 * FRAME)


def test_landscape_and_portrait_canvases_render_at_their_own_size():
    for shape, size in (("square", (512, 512)), ("landscape", (768, 512)), ("portrait", (512, 768))):
        canvas = Canvas(shape)
        canvas.add([make(start=(0, 0), end=(size[0], size[1]), colours=["#00ff00"])])
        img = Image.open(io.BytesIO(render_png(canvas.to_svg()))).convert("RGB")
        assert img.size == size
        assert img.getpixel((size[0] - 1, size[1] - 1)) == (0, 255, 0)  # the far corner is painted
    assert Canvas("hexagon").info() == {"shape": "square", "width": 512, "height": 512}


def test_off_canvas_allowance_follows_each_side():
    # Half a side past either edge: x on a landscape canvas reaches further than on a square one.
    good, errors, _ = check_batch(["rectangle 600 10 900 60 #ff0000"], 768, 512)
    assert len(good) == 1 and not errors
    good, errors, _ = check_batch(["rectangle 600 10 900 60 #ff0000"], 512, 512)
    assert not good and "x2 = 900 is too far off the canvas (allowed -256 to 768)" in errors[0]
    errors, _ = check_shape(make(end=(110, 900)), 768, 512)
    assert errors == ["y2 = 900 is too far off the canvas (allowed -256 to 768)"]
    errors, _ = check_shape(make(end=(110, 900)), 512, 768)
    assert errors == []
    errors, _ = check_shape(make(end=(110, 1200)), 512, 768)
    assert errors == ["y2 = 1200 is too far off the canvas (allowed -384 to 1152)"]


def test_portrait_agent_view_has_a_ruler_down_its_long_side():
    canvas = Canvas("portrait")
    img = Image.open(io.BytesIO(agent_view_png(canvas.to_svg(), canvas.width, canvas.height))).convert("RGB")
    assert img.size == (512 + 2 * FRAME, 768 + 2 * FRAME)
    # A grid line at y = 704 (only on the tall canvas) runs across the painting...
    assert img.getpixel((FRAME + 100, FRAME + 704)) != (255, 255, 255)
    assert img.getpixel((FRAME + 100, FRAME + 700)) == (255, 255, 255)
    # ...and the ruler labels it in the left margin (dark text pixels beside the tick).
    label = img.crop((0, FRAME + 698, FRAME - 7, FRAME + 711))
    assert min(lo for lo, _ in label.getextrema()) < 130


@pytest.mark.parametrize("raw, field, expected", [
    ({"shape_budget": 99999}, "shape_budget", 500),
    ({"shape_budget": "12"}, "shape_budget", 12),
    ({"look_budget": -3}, "look_budget", 0),
    ({"model": "not-offered"}, "model", "m1"),
    ({"model": "m2"}, "model", "m2"),
    ({"reasoning_effort": "extreme"}, "reasoning_effort", "medium"),
])
def test_settings_are_clamped_and_validated(raw, field, expected):
    assert getattr(clean(raw, ("m1", "m2")), field) == expected


def test_settings_survive_a_restart(tmp_path):
    store = SettingsStore(tmp_path / "s.json", ("m1",))
    store.update({"shape_budget": 42, "vision": False})
    again = SettingsStore(tmp_path / "s.json", ("m1",))
    assert again.current == StudioSettings(shape_budget=42, vision=False, model="m1")
