"""The canvas and renderer: plain Python, no AI."""

import io

import pytest
from PIL import Image

from app.canvas import Canvas, Shape, check_shape, normalise_hex, shape_svg
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
    assert any("first colour" in n for n in notes) and any("no outline_color" in n for n in notes)


def test_hex_colours_are_normalised():
    assert normalise_hex("#ABC") == "#aabbcc"
    assert normalise_hex("#12ab9F") == "#12ab9f"
    assert normalise_hex("12ab9f") is None and normalise_hex("#12ab9") is None


def test_agent_view_has_a_ruler_frame():
    canvas = Canvas()
    png = agent_view_png(canvas.to_svg(), canvas.size)
    img = Image.open(io.BytesIO(png))
    assert img.size == (canvas.size + 2 * FRAME, canvas.size + 2 * FRAME)


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
