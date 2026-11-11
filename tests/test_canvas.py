"""The canvas and renderer: plain Python, no AI."""

import base64
import io

import pytest
from PIL import Image

from app.canvas import (Canvas, Shape, check_batch, check_shape, kind_of, normalise_hex,
                        parse_shape, shape_svg, split_batch)
from app.settings_store import SettingsStore, StudioSettings, clean
from app.exemplar import COLUMNS, ROWS, SHEET, SHEETS, exemplar_text, exemplar_views
from app.view import FRAME, agent_view_png, framed_png, reference_png, render_png


def image_url(img: Image.Image, fmt: str = "PNG") -> str:
    """An image as the data URL the browser sends."""
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return f"data:image/{fmt.lower()};base64," + base64.b64encode(buf.getvalue()).decode()


def test_reference_is_centre_cropped_to_the_canvas():
    # 3:1 stripes: red | green | blue. A 3:2 crop from the middle keeps only the green.
    img = Image.new("RGB", (1200, 400), (0, 200, 0))
    img.paste((255, 0, 0), (0, 0, 290, 400))
    img.paste((0, 0, 255), (910, 0, 1200, 400))
    out = Image.open(io.BytesIO(reference_png(image_url(img), 768, 512))).convert("RGB")
    assert out.size == (768, 512)
    for x, y in ((0, 0), (767, 511), (384, 256)):
        r, g, b = out.getpixel((x, y))
        assert g > 150 and r < 60 and b < 60


def test_a_full_size_photo_is_scaled_down_to_the_canvas():
    # A 12-megapixel phone photo becomes canvas-sized, so it costs no more tokens than a look.
    photo = image_url(Image.new("RGB", (4032, 3024), (90, 140, 200)), "JPEG")
    out = Image.open(io.BytesIO(reference_png(photo, 768, 512)))
    assert out.size == (768, 512)


def test_reference_transparency_is_laid_on_white():
    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    img.paste((255, 0, 0, 255), (0, 0, 256, 512))
    out = Image.open(io.BytesIO(reference_png(image_url(img), 512, 512))).convert("RGB")
    assert out.getpixel((100, 100)) == (255, 0, 0) and out.getpixel((400, 100)) == (255, 255, 255)


@pytest.mark.parametrize("url, message", [
    ("data:text/plain;base64,aGVsbG8=", "isn't an image"),
    ("data:image/png;base64,not base64!", "couldn't be read"),
    ("data:image/png;base64,AAAAAAAA", "couldn't be read"),
    (image_url(Image.new("RGB", (10, 10)), "GIF"), "PNG, JPEG or WebP"),
    (image_url(Image.new("1", (8000, 8000))), "too big"),
])
def test_bad_references_are_refused_with_a_reason(url, message):
    with pytest.raises(ValueError, match=message):
        reference_png(url, 512, 512)


def test_reference_view_has_the_same_ruler_frame_as_a_look():
    ref = reference_png(image_url(Image.new("RGB", (512, 768), (10, 20, 30))), 512, 768)
    view = Image.open(io.BytesIO(framed_png(ref, 512, 768))).convert("RGB")
    canvas = Canvas("portrait")
    look = Image.open(io.BytesIO(agent_view_png(canvas.to_svg(), 512, 768, "frame"))).convert("RGB")
    assert view.size == look.size == (512 + 2 * FRAME, 768 + 2 * FRAME)
    assert view.getpixel((5, 5)) == look.getpixel((5, 5))  # the frame
    assert view.getpixel((FRAME + 10, FRAME + 10)) == (10, 20, 30)  # the picture inside it


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


def test_painting_svg_holds_each_shape_as_its_own_fragment_for_replay():
    # The gallery's replay splits painting.svg into its top-level <g data-n> fragments
    # and paints them one by one, so each must stand alone (its own gradients and filters).
    import re
    import xml.etree.ElementTree as ET
    canvas = Canvas()
    texts = ["rectangle 0 0 512 300 linear #4a90d9 #cfe8ff 180",
             "ellipse 100 100 200 200 #ff0000 spray=0.4 soft=4",
             "line 10 400 300 420 #222222 thickness=8 bend=20@180 taper=end"]
    good, errors, _ = check_batch(texts, 512, 512)
    assert not errors
    canvas.add([sh for _, sh in good], texts)
    root = ET.fromstring(canvas.to_svg())
    children = list(root)
    assert children[0].tag.endswith("rect")  # the white background
    frags = children[1:]
    assert [f.get("data-n") for f in frags] == ["1", "2", "3"]
    assert all(f.tag.endswith("}g") for f in frags)
    for ps in canvas.shapes:
        used = set(re.findall(r"url\(#([^)]+)\)", ps.svg))
        defined = set(re.findall(r'id="([^"]+)"', ps.svg))
        assert used <= defined, f"shape {ps.n} uses paint defined outside its fragment"


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


def paint_one(text: str) -> str:
    """The canvas SVG after painting one shape written in the notation."""
    shape, errors, _ = parse_shape(text)
    assert errors == [], errors
    assert check_shape(shape)[0] == []
    canvas = Canvas()
    canvas.add([shape])
    return canvas.to_svg()


def dark_share(svg: str, box: tuple[int, int, int, int]) -> float:
    """Share of the pixels in box that are darker than mid grey."""
    grey = Image.open(io.BytesIO(render_png(svg))).convert("L").crop(box)
    data = grey.get_flattened_data()
    return sum(1 for p in data if p < 128) / len(data)


def test_diamond_has_its_corners_at_the_middle_of_each_edge():
    svg = paint_one("diamond 100 100 300 200 #000000")
    assert pixel(svg, 200, 150) == (0, 0, 0)
    assert pixel(svg, 105, 150) == (0, 0, 0) and pixel(svg, 295, 150) == (0, 0, 0)
    assert pixel(svg, 105, 105) == (255, 255, 255) and pixel(svg, 295, 195) == (255, 255, 255)


def test_skew_tilts_top_and_bottom_and_the_gradient_follows():
    # skew=45 around the centre (200, 200): the right edge drops 100 px, the left edge rises 100.
    svg = paint_one("rectangle 100 150 300 250 #000000 skew=45")
    assert pixel(svg, 290, 300) == (0, 0, 0)        # below the unskewed box, on the right
    assert pixel(svg, 110, 100) == (0, 0, 0)        # above it, on the left
    assert pixel(svg, 290, 160) == (255, 255, 255)  # the right side's top dropped away
    # A bottom-to-top blend on a skewed face is skewed with it: the same shade sits 80% of
    # the way down the left edge (y 60 to 160) and the right edge (y 240 to 340).
    svg = paint_one("rectangle 100 150 300 250 linear #000000 #ffffff 0 skew=45")
    left, right = pixel(svg, 110, 140), pixel(svg, 290, 320)
    assert left[0] < 80 and abs(left[0] - right[0]) < 30


def test_skew_comes_before_rotation():
    svg = shape_svg(1, parse("rectangle 0 0 100 100 #000 skew=20 rotation=90"))
    assert 'transform="rotate(90 50 50) translate(50 50) skewY(20) translate(-50 -50)"' in svg


def test_trail_walks_its_legs_by_compass_heading():
    svg = paint_one("trail 100 100 200@90 100@180 #000000 thickness=6")
    assert pixel(svg, 200, 100) == (0, 0, 0)        # first leg heads right
    assert pixel(svg, 300, 150) == (0, 0, 0)        # second leg heads down
    assert pixel(svg, 200, 150) == (255, 255, 255)  # an open trail isn't filled


def test_smooth_curves_through_every_corner():
    sharp = paint_one("trail 100 100 300@90 300@180 #000000 thickness=6")
    smooth = paint_one("trail 100 100 300@90 300@180 #000000 thickness=6 smooth=1")
    assert pixel(sharp, 400, 100) == (0, 0, 0) and pixel(smooth, 400, 100) == (0, 0, 0)
    # Between the corners the curve swings outwards, above the straight first leg at y = 100.
    assert pixel(sharp, 250, 81) == (255, 255, 255) and pixel(smooth, 250, 81) == (0, 0, 0)
    assert "<path" in smooth and " C" in smooth and "Z" not in smooth


def test_bend_pulls_the_middle_of_a_line_towards_its_heading():
    svg = paint_one("line 100 200 400 200 #000000 thickness=6 bend=40@180")
    assert pixel(svg, 250, 240) == (0, 0, 0)          # the middle is pulled 40 px down
    assert pixel(svg, 250, 200) == (255, 255, 255)    # off the straight line
    assert pixel(svg, 102, 200) == (0, 0, 0) and pixel(svg, 398, 200) == (0, 0, 0)  # ends stay put
    up = paint_one("line 100 200 400 200 #000000 thickness=6 bend=40@0")
    assert pixel(up, 250, 160) == (0, 0, 0)


def test_taper_thins_a_stroke_to_a_point():
    svg = paint_one("line 100 256 400 256 #000000 thickness=40 taper=end")
    assert pixel(svg, 110, 256 - 15) == (0, 0, 0)          # near the start: full width
    assert pixel(svg, 390, 256 - 15) == (255, 255, 255)    # near the end: almost a point
    assert pixel(svg, 370, 256) == (0, 0, 0)
    both = paint_one("line 100 256 400 256 #000000 thickness=40 taper=both")
    assert pixel(both, 250, 256 - 15) == (0, 0, 0)         # full width in the middle
    assert pixel(both, 110, 256 - 15) == (255, 255, 255)   # pointed at both ends
    assert pixel(both, 390, 256 - 15) == (255, 255, 255)
    curved = paint_one("trail 100 300 150@90 150@0 #000000 thickness=30 smooth=1 taper=end")
    assert pixel(curved, 102, 300) == (0, 0, 0)            # starts round and wide


def test_deep_bends_say_where_the_curve_runs():
    _, notes = check_shape(parse("line 389 345 426 232 #000 bend=66@90"))
    assert notes == ["curves through (448, 317), (474, 288), (466, 260) between its ends"]
    _, notes = check_shape(parse("line 0 0 100 0 #000 bend=4@180"))
    assert notes == []                           # a gentle bend is close enough to straight


def test_bend_and_taper_note_where_they_do_nothing():
    _, notes = check_shape(parse("ellipse 0 0 40 40 #000 bend=10@90 taper=end"))
    assert any("bend only curves lines" in n for n in notes)
    assert any("taper only thins lines and trails" in n for n in notes)


def test_spray_lands_only_the_share_asked_for():
    svg = paint_one("rectangle 0 0 512 512 #000000 spray=0.3")
    assert 0.2 < dark_share(svg, (0, 0, 512, 512)) < 0.4
    assert paint_one("rectangle 0 0 512 512 #000000 spray=0.3") == svg  # same speckle every time


def test_spray_still_paints_a_level_line():
    # A level line's box is 0 px tall, which would hide a filter sized to the box.
    svg = paint_one("line 50 256 450 256 #000000 thickness=40 spray=0.5")
    assert 0.3 < dark_share(svg, (60, 240, 440, 272)) < 0.7


def test_soft_feathers_the_edge():
    hard = pixel(paint_one("rectangle 100 100 400 400 #000000"), 100, 250)
    soft = pixel(paint_one("rectangle 100 100 400 400 #000000 soft=20"), 100, 250)
    assert hard[0] < 135 and 100 < soft[0] < 160      # half way at the edge either way...
    inside = pixel(paint_one("rectangle 100 100 400 400 #000000 soft=20"), 108, 250)
    assert 20 < inside[0] < 120                        # ...but only the soft one fades inside it


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
    assert (s.skew, s.smooth, s.spray, s.soft, s.legs) == (0, False, 1, 0, [])
    assert parse("ellipse 10 10 50 50 #ff0000 outline=#000").thickness == 2
    assert parse("line 10 10 50 50 #000").thickness == 3
    assert parse("trail 10 10 40@90 #000").thickness == 3
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
    s = parse("Trail 10, 10, 40 @ 90, 20@180 #000 smooth=yes")
    assert [(leg.distance, leg.heading) for leg in s.legs] == [(40, 90), (20, 180)] and s.smooth


def test_notation_reads_walks_and_the_new_extras():
    s = parse("trail 200 200 80@60 80@180.5 linear #6b8e23 #a9c46c 0 smooth=1 spray=0.4 soft=3")
    assert s.kind == "trail" and (s.start.x, s.start.y) == (200, 200)
    assert [(leg.distance, leg.heading) for leg in s.legs] == [(80, 60), (80, 180.5)]
    assert round(s.end.x, 1) == 268.6 and round(s.end.y, 1) == 240  # where the walk ends
    assert s.fill.style == "linear" and (s.smooth, s.spray, s.soft) == (True, 0.4, 3)
    assert parse("diamond 0 0 40 20 #fff skew=-30").skew == -30
    s = parse("line 0 0 50 50 #000 bend=20@180 taper=both")
    assert (s.bend.distance, s.bend.heading, s.taper) == (20, 180, "both")




@pytest.mark.parametrize("text, message", [
    ("ellips 0 0 10 10 #fff", "unknown kind 'ellips'"),
    ("ellipse 0 0 10 #fff", "four numbers"),
    ("ellipse 0 0 10 10", "followed by the fill"),
    ("ellipse 0 0 10 10 opacity=0.5", "followed by the fill"),
    ("rectangle 0 0 10 10 linear #fff #000", "needs its angle"),
    ("rectangle 0 0 10 10 #fff wobble=2", "unknown extra 'wobble'"),
    ("rectangle 0 0 10 10 #fff 0.5", "unexpected '0.5'"),
    ("rectangle 0 0 10 10 #fff opacity=half", "opacity=half is not a number"),
    ("rectangle 0 0 10 10 #fff glow=2", "use opacity, rotation, skew, thickness, outline, bend, taper, smooth, spray or soft"),
    ("trail 10 10 #000", "needs its legs straight after the start point"),
    ("trail 10 10 50 50 #000", "needs its legs straight after the start point"),
    ("trail 10 #000", "start point x y"),
    ("trail 10 10 40@east #000", "'40@east' is not a leg"),
    ("trail 10 10 40@90", "the legs must be followed by the fill"),
    ("trail 10 10 40@90 #000 smooth=2", "smooth=2 must be 1"),
    ("line 0 0 50 50 #000 bend=20", "bend=20 must be written distance@heading"),
    ("line 0 0 50 50 #000 taper=start", "taper=start must be end"),
    ("patch 10 10 40@90 40@180 #000", "unknown kind 'patch'"),  # removed: models traced polygons with it
])
def test_notation_rejects_what_it_cannot_read(text, message):
    shape, errors, _ = parse_shape(text)
    assert shape is None and any(message in e for e in errors), errors


@pytest.mark.parametrize("text, message", [
    ("trail 10 10 " + "5@90 " * 13 + "#000", "at most 12 legs"),
    ("trail 10 10 40@90 0.5@180 #000", "leg 2 is 0.5 px long"),
    ("trail 500 500 400@90 #000", "leg 1 ends at (900, 500), too far off"),
    ("rectangle 0 0 10 10 #000 skew=80", "skew 80 must be between -70 and 70"),
    ("rectangle 0 0 10 10 #000 spray=0", "spray 0 must be between 0.05 and 1"),
    ("rectangle 0 0 10 10 #000 soft=50", "soft 50 must be between 0 and 30"),
    ("line 0 0 50 50 #000 bend=300@90", "bend 300 must be between 0 and 256"),
    ("line 0 0 50 50 #000 bend=-20@90", "turn the heading round"),
])
def test_check_rejects_bad_walks_and_extras(text, message):
    shape = parse(text)
    errors, _ = check_shape(shape)
    assert any(message in e for e in errors), errors


def test_trails_report_where_they_ended():
    _, notes = check_shape(parse("trail 100 100 50@90 50@180 #000"))
    assert notes == ["turns at (150, 100), then ends at (150, 150)"]
    _, notes = check_shape(parse("trail 403 365 43@54 52@13 47@351 #000"))
    assert notes == ["turns at (438, 340), (449, 289), then ends at (442, 243)"]
    _, notes = check_shape(parse("trail 100 100 50@90 #000"))
    assert notes == ["ends at (150, 100)"]


def test_new_extras_note_where_they_do_nothing():
    _, notes = check_shape(parse("trail 0 0 40@90 #000 rotation=30"))
    assert any("ignored for a trail" in n for n in notes)
    _, notes = check_shape(parse("ellipse 0 0 40 40 #000 smooth=1"))
    assert any("smooth only bends" in n for n in notes)
    _, notes = check_shape(parse("line 0 0 40 40 #000 skew=10"))
    assert any("skew is ignored for lines" in n for n in notes)


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
    png = agent_view_png(canvas.to_svg(), canvas.width, canvas.height, "frame")
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
    img = Image.open(io.BytesIO(agent_view_png(canvas.to_svg(), canvas.width, canvas.height, "frame"))).convert("RGB")
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
    ({}, "planning", False),
    ({"planning": 1}, "planning", True),
    ({}, "planning_effort", "high"),
    ({"planning_effort": "low"}, "planning_effort", "low"),
    ({"planning_effort": ""}, "planning_effort", ""),
    ({"planning_effort": "extreme"}, "planning_effort", "high"),
])
def test_settings_are_clamped_and_validated(raw, field, expected):
    assert getattr(clean(raw, ("m1", "m2")), field) == expected


def test_settings_survive_a_restart(tmp_path):
    store = SettingsStore(tmp_path / "s.json", ("m1",))
    store.update({"shape_budget": 42, "vision": False})
    again = SettingsStore(tmp_path / "s.json", ("m1",))
    assert again.current == StudioSettings(shape_budget=42, vision=False, model="m1")


# ---------------------------------------------------- grid modes and exemplar
@pytest.mark.parametrize("shape", ["square", "landscape", "portrait"])
def test_grid_modes_set_the_size_of_the_agents_view(shape):
    canvas = Canvas(shape)
    w, h = canvas.width, canvas.height
    for grid, size in (("frame", (w + 2 * FRAME, h + 2 * FRAME)), ("overlay", (w, h)), ("none", (w, h))):
        img = Image.open(io.BytesIO(agent_view_png(canvas.to_svg(), w, h, grid)))
        assert img.size == size, grid


def test_overlay_draws_the_grid_on_the_picture_and_none_leaves_it_clean():
    canvas = Canvas()
    overlay = Image.open(io.BytesIO(agent_view_png(canvas.to_svg(), 512, 512, "overlay"))).convert("RGB")
    plain = Image.open(io.BytesIO(agent_view_png(canvas.to_svg(), 512, 512, "none"))).convert("RGB")
    assert overlay.getpixel((64, 300)) != (255, 255, 255)   # a grid line at x = 64
    assert overlay.getpixel((70, 300)) == (255, 255, 255)
    label = overlay.crop((66, 1, 84, 12))                   # "64" just right of its line, at the top
    assert min(lo for lo, _ in label.getextrema()) < 160
    assert plain.getpixel((64, 300)) == (255, 255, 255)


def test_trail_coordinates_follow_each_side_of_the_canvas():
    shape = parse("trail 700 100 300@90 #000")             # ends at x = 1000
    assert check_shape(shape, 768, 512)[0] == []          # allowed up to 1152 on a landscape canvas
    errors, _ = check_shape(shape, 512, 512)
    assert errors == ["leg 1 ends at (1000, 100), too far off the canvas (allowed x -256 to 768, y -256 to 768)"]


def test_spray_and_soft_reach_the_far_side_of_a_landscape_canvas():
    # Filters are sized to the shape, not clipped to a 512 square.
    canvas = Canvas("landscape")
    canvas.add([parse("rectangle 600 100 760 400 #000000 spray=0.5")])
    assert 0.3 < dark_share(canvas.to_svg(), (610, 110, 750, 390)) < 0.7


def test_every_exemplar_example_is_valid_on_its_sheet():
    w, h = SHEET
    for _, examples in SHEETS:
        assert len(examples) <= COLUMNS * ROWS
        for ex in examples:
            good, errors, _ = check_batch(list(ex.shapes), w, h)
            assert errors == [] and len(good) == len(ex.shapes), (ex.caption, errors)


def test_exemplar_text_lists_every_example_with_its_shapes():
    text = exemplar_text()
    numbered = [ex for _, examples in SHEETS for ex in examples if ex.shapes]
    for n, ex in enumerate(numbered, start=1):
        assert f"{n}. {ex.caption}" in text
        for line in ex.shapes:
            assert f"   {line}" in text
    assert "Sheet 2:" in text and "not your canvas" in text


@pytest.mark.parametrize("grid, size", [("frame", (SHEET[0] + 2 * FRAME, SHEET[1] + 2 * FRAME)),
                                        ("overlay", SHEET), ("none", SHEET)])
def test_exemplar_sheets_render_in_each_grid_mode(grid, size):
    views = exemplar_views(grid)
    assert len(views) == len(SHEETS)
    for png in views:
        img = Image.open(io.BytesIO(png)).convert("RGB")
        assert img.size == size
    assert exemplar_views(grid) is views  # cached: built once per grid mode
    # Example 1 is a blue rectangle from (32, 40) to (160, 130), painted for real.
    first = Image.open(io.BytesIO(views[0])).convert("RGB")
    offset = FRAME if grid == "frame" else 0
    assert first.getpixel((offset + 100, offset + 100)) == (0x4a, 0x90, 0xd9)
