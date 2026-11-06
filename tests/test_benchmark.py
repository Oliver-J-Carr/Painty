"""Benchmark cards and their scorer: real renders, no model."""

import io
import json

import pytest
from PIL import Image, ImageChops

from app.benchmark import BENCHMARK_LOOKS, CARDS, LEVELS, card_canvas, card_json, card_png, card_text
from app.benchmark_score import best_offset, main, score_folder, score_images, _paint_mask
from app.canvas import Canvas, check_batch
from app.exemplar import SHEETS
from app.settings_store import LIMITS, StudioSettings
from app.view import render_png


def _img(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


@pytest.mark.parametrize("name", CARDS)
def test_every_card_is_valid_and_fits_the_default_budget(name):
    card = CARDS[name]
    w, h = card.size
    assert card.items and card.shape_count <= StudioSettings().shape_budget
    assert 3 * card.shape_count + 10 <= card.shape_budget <= LIMITS["shape_budget"][1]
    assert card.shape_budget % 5 == 0 and card.look_budget == BENCHMARK_LOOKS
    for item in card.items:
        assert item.level in LEVELS
        _, errors, _ = check_batch(list(item.shapes), w, h)
        assert not errors, (item.name, errors)
    assert len(card_canvas(name)) == card.shape_count
    assert _img(card_png(name)).size == (w, h)


@pytest.mark.parametrize("name", CARDS)
def test_every_item_paints_inside_its_own_cell(name):
    card = CARDS[name]
    w, h = card.size
    boxes = [item.box for item in card.items]
    for i, a in enumerate(boxes):  # cells don't overlap
        for b in boxes[i + 1:]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], (a, b)
    for item in card.items:
        canvas = Canvas(card.shape)
        good, _, _ = check_batch(list(item.shapes), w, h)
        canvas.add([s for _, s in good])
        bbox = _paint_mask(_img(render_png(canvas.to_svg()))).getbbox()
        x0, y0, x1, y1 = item.box
        assert bbox and x0 <= bbox[0] and y0 <= bbox[1] and bbox[2] <= x1 and bbox[3] <= y1, \
            (item.name, bbox, item.box)


def test_no_card_repeats_an_exemplar_example():
    shown = {_norm(text) for _, examples in SHEETS for ex in examples for text in ex.shapes}
    for card in CARDS.values():
        for item in card.items:
            for text in item.shapes:
                assert _norm(text) not in shown, (card.name, text)


def test_every_level_is_on_the_combined_card():
    assert {item.level for item in CARDS["combined"].items} == set(LEVELS)


def test_card_text_and_json_carry_every_shape():
    for name, card in CARDS.items():
        data = card_json(name)
        assert data["name"] == name and [data["width"], data["height"]] == list(card.size)
        assert (data["shape_budget"], data["look_budget"]) == (card.shape_budget, card.look_budget)
        assert sum(len(i["shapes"]) for i in data["items"]) == card.shape_count
        assert all(len(i["box"]) == 4 for i in data["items"])
        text = card_text(name)
        assert all(t in text for item in card.items for t in item.shapes)


# ------------------------------------------------------------------ scoring
@pytest.mark.parametrize("name", ["basic", "letters"])
def test_a_card_scores_perfectly_against_itself(name):
    img = _img(card_png(name))
    result = score_images(img, img, card_json(name))
    assert result.score == 1 and result.canvas == 1 and result.offset == (0, 0)
    assert all(i.score == 1 and i.shape == 1 and i.colour == 1 and i.offset == (0, 0)
               for i in result.items)


def test_a_shifted_copy_reports_its_offset():
    img = _img(card_png("intermediate"))
    shifted = ImageChops.offset(img, 10, 6)  # 10 px right, 6 px down
    result = score_images(img, shifted, card_json("intermediate"))
    assert result.offset == (10, 6)
    for item in result.items:
        assert item.offset == (10, 6) and item.shifted == 1, item
        assert item.score < 1
    assert result.score < 0.9


def test_a_blank_canvas_scores_nothing():
    img = _img(card_png("solids"))
    blank = Image.new("RGB", img.size, "white")
    result = score_images(img, blank, card_json("solids"))
    assert result.score == 0 and result.offset is None
    assert all(i.colour is None and i.offset is None for i in result.items)


def test_wrong_colours_keep_the_shape_but_lose_the_score():
    data = {"name": "t", "items": [{"name": "square", "level": "basic", "box": [0, 0, 200, 200]}]}
    card, copy = Canvas(), Canvas()
    card.add([s for _, s in check_batch(["rectangle 50 50 150 150 #2f6db5"])[0]])
    copy.add([s for _, s in check_batch(["rectangle 50 50 150 150 #e2543b"])[0]])
    result = score_images(_img(render_png(card.to_svg())), _img(render_png(copy.to_svg())), data)
    item = result.items[0]
    assert item.shape == 1 and item.score == 0 and item.colour < 0.6


def test_spray_scores_well_whatever_its_speckle():
    """Spray noise is seeded with the shape's number, so a perfect copy painted in a
    different order speckles differently. The sprayed item is blurred before scoring."""
    copy = Canvas("landscape")
    copy.add([s for _, s in check_batch(["rectangle 0 0 2 2 #ffffff"])[0]])  # shifts every number
    copy.add([ps.shape for ps in card_canvas("effects").shapes])
    result = score_images(_img(card_png("effects")), _img(render_png(copy.to_svg())), card_json("effects"))
    assert all(i.score > 0.95 for i in result.items), result.items


def test_best_offset_prefers_no_shift_on_a_tie():
    """A stripe wider than the search: every sideways shift fits equally well."""
    mask = Image.new("L", (300, 100), 0)
    mask.paste(255, (0, 40, 300, 60))
    assert best_offset(mask, mask, (100, 0, 200, 100)) == ((0, 0), 1.0)


def _saved(tmp_path, name="basic", shapes=None):
    """A gallery folder as a benchmark painting saves it."""
    folder = tmp_path / "gallery" / f"20261106-120000-{name}"
    folder.mkdir(parents=True)
    canvas = card_canvas(name) if shapes is None else Canvas(CARDS[name].shape)
    if shapes is not None:
        canvas.add([s for _, s in check_batch(shapes, *CARDS[name].size)[0]], shapes)
    (folder / "painting.svg").write_text(canvas.to_svg())
    (folder / "reference.png").write_bytes(card_png(name))
    (folder / "benchmark.json").write_text(json.dumps(card_json(name)))
    (folder / "meta.json").write_text(json.dumps({
        "id": folder.name, "benchmark": name, "shapes_used": len(canvas),
        "settings": {"model": "fake-model", "vision": True, "grid": "overlay", "exemplar": True}}))
    return folder


def test_score_folder_and_the_command_line(tmp_path, monkeypatch, capsys):
    folder = _saved(tmp_path)
    assert score_folder(folder).score == 1

    from app.config import Settings
    monkeypatch.setattr("app.config.settings", Settings(data_dir=tmp_path))  # gallery: tmp_path/gallery
    diff = tmp_path / "diff.png"
    assert main([folder.name, "--diff", str(diff)]) == 0
    out = capsys.readouterr().out
    assert "large circle" in out and "score 1.000" in out and "grid overlay" in out
    assert Image.open(diff).size == (512 * 3 + 24, 512 + 24)

    assert main(["--json", str(folder)]) == 0
    assert json.loads(capsys.readouterr().out)["score"] == 1

    _saved(tmp_path, "letters", shapes=["line 46 64 46 196 #22313f thickness=10"])
    assert main([]) == 0
    listing = capsys.readouterr().out
    assert "basic" in listing and "letters" in listing and "fake-model" in listing


def test_a_painting_without_a_card_cant_be_scored(tmp_path, capsys):
    folder = tmp_path / "plain"
    folder.mkdir()
    with pytest.raises(ValueError):
        score_folder(folder)
    assert main([str(folder)]) == 1
    assert "wasn't painted from a benchmark card" in capsys.readouterr().err


def test_cards_are_served_for_the_dialog_and_for_checking_by_eye():
    import asyncio

    from fastapi import HTTPException

    from app import main as web

    png = asyncio.run(web.benchmark_png("effects"))
    assert png.media_type == "image/png" and png.body == card_png("effects")
    txt = asyncio.run(web.benchmark_txt("letters"))
    assert "trail 138 336" in txt.body.decode()
    with pytest.raises(HTTPException) as err:
        asyncio.run(web.benchmark_png("nope"))
    assert err.value.status_code == 404
