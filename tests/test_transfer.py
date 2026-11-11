"""Exporting and importing paintings, and the card."""

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app.main as main_module
from app.canvas import Canvas, check_batch
from app.card import FRAME, MOUNT, PAD, PROMPT_LINES, cost_text, render_card, token_groups
from app.context import Look, Stage
from app.gallery import GALLERY_ZOOM, Gallery, SaveRequest
from app.model_library import ModelLibrary
from app.settings_store import SettingsStore
from app.transfer import (AlreadyImported, NotAPainting, _check_svg, attachment, export_name,
                          export_zip, import_zip)
from app.view import render_png

# Every kind of shape and paint option, so the import's SVG check covers all the renderer writes.
EVERYTHING = [
    "rectangle 0 0 512 300 linear #4a90d9 #cfe8ff 180",
    "ellipse 100 100 200 200 radial #ff0000 #ffff00 spray=0.4 soft=4",
    "triangle 300 300 400 400 #00aa00 rotation=30 outline=#000000",
    "diamond 50 350 150 420 #884488 skew=20 opacity=0.6",
    "line 10 400 300 420 #222222 thickness=8 bend=20@180 taper=end",
    "trail 20 20 40@90 30@135 40@90 #333333 thickness=4 smooth=1 taper=both",
    "trail 400 50 40@180 30@90 #333333",
]


def save(gallery, title="Sunny Face", model="openai/gpt-6-luna", shape="square", texts=None,
         prompt="a sunny face"):
    canvas = Canvas(shape)
    texts = texts or ["rectangle 0 0 512 512 #ff0000"]
    good, errors, _ = check_batch(texts, canvas.width, canvas.height)
    assert not errors
    canvas.add([sh for _, sh in good], texts)
    png = render_png(canvas.to_svg())
    req = SaveRequest(canvas=canvas, prompt=prompt, title=title, note="A note.", ending="finished",
                      settings={"model": model, "reasoning_effort": "high", "vision": True},
                      shapes_budget=60, looks_budget=5, looks=[Look(png, "check", len(canvas))],
                      stages=[Stage(1, len(canvas), "everything")], input_tokens=1200, output_tokens=300,
                      reference=png)
    return gallery.save(req, "test")


def zip_of(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


# ------------------------------------------------------------------ names
def test_export_names_hold_the_title_and_model_and_are_safe():
    meta = {"title": 'Cat: "Ginger" / Sun?', "settings": {"model": "anthropic/claude-sonnet-5.5"}}
    assert export_name(meta, "png") == "Cat Ginger Sun - anthropic-claude-sonnet-5.5.png"
    assert export_name(meta, "png", "card") == "Cat Ginger Sun - anthropic-claude-sonnet-5.5 - card.png"
    assert export_name({"title": "", "settings": {}}, "zip") == "Untitled - unknown model.zip"
    assert export_name({"title": "x" * 300, "settings": {"model": "m"}}, "zip") == "x" * 80 + " - m.zip"


def test_the_download_header_keeps_unicode_with_an_ascii_fallback():
    header = attachment("Café – gpt-6-luna.png")
    assert header.startswith('attachment; filename="Caf  gpt-6-luna.png"')
    assert "filename*=UTF-8''Caf%C3%A9%20%E2%80%93%20gpt-6-luna.png" in header


# ------------------------------------------------------------------ export and import
def test_a_painting_exports_and_imports_into_another_gallery_unchanged(tmp_path):
    here, there = Gallery(tmp_path / "here"), Gallery(tmp_path / "there")
    meta = save(here)
    folder = here.folder(meta["id"])
    data = export_zip(folder, meta)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
    assert all(n.startswith("Sunny Face - openai-gpt-6-luna/") for n in names)
    assert "Sunny Face - openai-gpt-6-luna/stages/stage-1.png" in names

    imported = import_zip(there, data)
    assert imported["id"] == meta["id"] and imported["imported_at"]
    copy = there.folder(meta["id"])
    for path in folder.rglob("*"):
        if path.is_file() and path.name != "meta.json":
            assert (copy / path.relative_to(folder)).read_bytes() == path.read_bytes()
    assert there.list()[0]["title"] == "Sunny Face"
    assert not list(tmp_path.glob(".painty-import-*"))  # the staging folder is gone

    with pytest.raises(AlreadyImported) as dup:  # the same painting again
        import_zip(there, data)
    assert dup.value.id == meta["id"]
    with pytest.raises(AlreadyImported):  # or into the gallery it came from
        import_zip(here, data)


def test_an_import_whose_id_is_taken_gets_a_new_one(tmp_path):
    gallery = Gallery(tmp_path / "g")
    meta = save(gallery)
    data = export_zip(gallery.folder(meta["id"]), meta)
    other = json.loads((gallery.folder(meta["id"]) / "meta.json").read_text())
    other["created_at"] += 100  # a different painting under the same id
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        files = {n.split("/", 1)[1]: zf.read(n) for n in zf.namelist()}
    files["meta.json"] = json.dumps(other)
    imported = import_zip(gallery, zip_of(files))  # also: files at the zip's top level
    assert imported["id"] == f"{meta['id']}-2"
    assert len(gallery.list()) == 2


def _files(gallery):
    meta = save(gallery)
    folder = gallery.folder(meta["id"])
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_imports_skip_files_that_arent_part_of_a_painting(tmp_path):
    gallery = Gallery(tmp_path / "g")
    files = _files(gallery)
    files["../escape.txt"] = "no"
    files["notes/../../escape2.txt"] = "no"
    files["evil.html"] = "<script>alert(1)</script>"
    files["__MACOSX/._meta.json"] = "junk"
    meta = json.loads(files["meta.json"])
    meta["created_at"] += 1
    files["meta.json"] = json.dumps(meta)
    imported = import_zip(gallery, zip_of(files))
    folder = gallery.folder(imported["id"])
    assert not (folder / "evil.html").exists()
    assert not list(tmp_path.rglob("escape*"))


@pytest.mark.parametrize("change, message", [
    (lambda f: f.pop("meta.json"), "exactly one meta.json"),
    (lambda f: f.pop("painting.svg"), "missing painting.svg"),
    (lambda f: f.update({"meta.json": "[1, 2]"}), "isn't a Painty painting's"),
    (lambda f: f.update({"meta.json": "{not json"}), "can't be read"),
    (lambda f: f.update({"painting.png": b"<html>hello</html>"}), "painting.png isn't a PNG"),
    (lambda f: f.update({"looks/look-1.png": b"GIF89a"}), "look-1.png isn't a PNG"),
    (lambda f: f.update({"shapes.json": "{oops"}), "shapes.json can't be read"),
    (lambda f: f.update({"other/meta.json": f["meta.json"]}), "exactly one meta.json"),
])
def test_imports_refuse_what_isnt_a_painting(tmp_path, change, message):
    gallery = Gallery(tmp_path / "g")
    files = _files(gallery)
    change(files)
    with pytest.raises(NotAPainting, match=message):
        import_zip(Gallery(tmp_path / "other"), zip_of(files))
    assert not (tmp_path / "other").exists() or not any((tmp_path / "other").iterdir())


def test_imports_refuse_anything_but_a_zip(tmp_path):
    with pytest.raises(NotAPainting, match="isn't a zip"):
        import_zip(Gallery(tmp_path / "g"), b"just some bytes")


def test_the_svg_check_passes_everything_the_renderer_paints():
    canvas = Canvas("landscape")
    good, errors, _ = check_batch(EVERYTHING, canvas.width, canvas.height)
    assert not errors
    canvas.add([sh for _, sh in good], EVERYTHING)
    _check_svg(canvas.to_svg().encode())  # raises if anything is missing from the allow lists


@pytest.mark.parametrize("svg", [
    '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url(https://example.com/x)"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:x="http://www.w3.org/1999/xlink">'
    '<rect x:href="javascript:alert(1)"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><g>text</g></svg>',
    '<!DOCTYPE svg [<!ENTITY a "b">]><svg xmlns="http://www.w3.org/2000/svg"/>',
    '<html xmlns="http://www.w3.org/1999/xhtml"/>',
])
def test_the_svg_check_refuses_scripts_links_and_strangers(svg):
    with pytest.raises(NotAPainting):
        _check_svg(svg.encode())


# ------------------------------------------------------------------ the card
@pytest.mark.parametrize("shape", ["square", "landscape", "portrait"])
def test_the_card_frames_the_painting_in_its_own_shape(tmp_path, shape):
    gallery = Gallery(tmp_path / "g")
    meta = save(gallery, shape=shape, texts=["rectangle -10 -10 768 768 #ff0000"])
    png = render_card(gallery.folder(meta["id"]), meta, {"usd": 0.0123})
    card = Image.open(io.BytesIO(png)).convert("RGB")
    pw, ph = meta["canvas"]["width"] * GALLERY_ZOOM, meta["canvas"]["height"] * GALLERY_ZOOM
    edge = PAD + FRAME + MOUNT
    assert card.width == pw + 2 * edge  # the same frame on every shape
    assert card.height > edge + ph + 300  # and the label under it
    assert card.getpixel((edge, edge)) == (255, 0, 0)  # the painting, at full size
    assert card.getpixel((edge + pw - 1, edge + ph - 1)) == (255, 0, 0)
    assert card.getpixel((edge + pw + 2, edge + ph // 2)) != (255, 0, 0)


def test_long_prompts_are_cut_short_so_the_card_never_grows(tmp_path):
    gallery = Gallery(tmp_path / "g")
    heights = []
    for words in (PROMPT_LINES * 15, 200):
        meta = save(gallery, title=f"T{words}", prompt=" ".join(["painting"] * words))
        heights.append(Image.open(io.BytesIO(render_card(gallery.folder(meta["id"]), meta))).height)
    short = save(gallery, title="Short", prompt="a cat")
    short_h = Image.open(io.BytesIO(render_card(gallery.folder(short["id"]), short))).height
    assert heights[0] == heights[1] > short_h


def test_the_card_shows_billed_cost_before_the_estimate():
    assert cost_text({"usd": 0.0421, "billed": 0.04}) == "$0.040"
    assert cost_text({"usd": 0.0421}) == "~$0.042"
    assert cost_text({"usd": 1.234}) == "~$1.23"
    assert cost_text({"usd": None, "var": "X"}) == "–"


# ------------------------------------------------------------------ the API
@pytest.fixture
def client(tmp_path, monkeypatch):
    lib = ModelLibrary(tmp_path / "models.json", ("gpt-6-luna",), {}, fetch=lambda: {"data": []})
    monkeypatch.setattr(main_module.app.state, "library", lib)
    monkeypatch.setattr(main_module.app.state, "store", SettingsStore(tmp_path / "settings.json", lib.models()))
    monkeypatch.setattr(main_module.app.state, "gallery", Gallery(tmp_path / "gallery"))
    return TestClient(main_module.app)


def test_the_api_exports_a_painting_three_ways(client):
    meta = save(main_module.app.state.gallery)
    base = f"/api/gallery/{meta['id']}/export"
    png = client.get(f"{base}/png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert 'filename="Sunny Face - openai-gpt-6-luna.png"' in png.headers["content-disposition"]
    card = client.get(f"{base}/card")
    assert card.status_code == 200 and Image.open(io.BytesIO(card.content)).format == "PNG"
    assert 'filename="Sunny Face - openai-gpt-6-luna - card.png"' in card.headers["content-disposition"]
    z = client.get(f"{base}/zip")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    assert 'filename="Sunny Face - openai-gpt-6-luna.zip"' in z.headers["content-disposition"]
    assert client.get(f"{base}/gif").status_code == 404
    assert client.get("/api/gallery/nope/export/png").status_code == 404
    assert client.get("/api/gallery/../export/png").status_code == 404


def test_the_api_imports_a_painting_once(client, tmp_path):
    source = Gallery(tmp_path / "elsewhere")
    meta = save(source, title="Imported")
    data = export_zip(source.folder(meta["id"]), meta)
    res = client.post("/api/gallery/import", content=data, headers={"Content-Type": "application/zip"})
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == meta["id"] and body["title"] == "Imported" and "cost" in body
    assert [m["id"] for m in client.get("/api/gallery").json()] == [meta["id"]]

    again = client.post("/api/gallery/import", content=data)
    assert again.status_code == 409 and again.json()["id"] == meta["id"]
    bad = client.post("/api/gallery/import", content=b"nope")
    assert bad.status_code == 400 and "isn't a zip" in bad.json()["detail"]


def test_the_card_splits_tokens_in_and_out_like_the_gallery():
    meta = {"input_tokens": 120000, "cached_tokens": 98000, "output_tokens": 4321, "reasoning_tokens": 1200}
    assert token_groups(meta) == [("Tokens in", "120,000", "82% cached"),
                                  ("Tokens out", "4,321", "1,200 reasoning")]
    # Older paintings have no cached or reasoning counts.
    assert token_groups({"input_tokens": 10, "output_tokens": 5}) == [
        ("Tokens in", "10", ""), ("Tokens out", "5", "")]


def test_the_card_shows_the_reference_small_over_the_frames_corner(tmp_path):
    gallery = Gallery(tmp_path / "g")
    meta = save(gallery, texts=["rectangle -10 -10 768 768 #ff0000"])  # its reference is the same red
    folder = gallery.folder(meta["id"])
    (folder / "reference.png").write_bytes(render_png(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
        '<rect width="512" height="512" fill="#0000ff"/></svg>'))
    edge = PAD + FRAME + MOUNT
    corner = (edge + 1024 + MOUNT + FRAME - 10, edge + 1024 + MOUNT + FRAME - 10)  # on the frame
    with_ref = Image.open(io.BytesIO(render_card(folder, meta))).convert("RGB")
    assert with_ref.getpixel(corner) == (0, 0, 255)
    without = Image.open(io.BytesIO(render_card(folder, {**meta, "reference": False}))).convert("RGB")
    assert without.getpixel(corner) != (0, 0, 255)


def test_paintings_can_be_deleted_from_the_gallery(tmp_path):
    gallery = Gallery(tmp_path / "g")
    keep, gone = save(gallery, title="Keep"), save(gallery, title="Gone")
    assert gallery.delete(gone["id"]) is True
    assert [m["id"] for m in gallery.list()] == [keep["id"]]
    assert not (tmp_path / "g" / gone["id"]).exists()
    assert not list(tmp_path.glob(".painty-delete-*"))
    assert gallery.delete(gone["id"]) is False
    for bad in ("..", ".", "../g", "", "a/b"):
        assert gallery.delete(bad) is False
    assert gallery.folder(keep["id"]) is not None


def test_the_api_deletes_a_painting(client):
    meta = save(main_module.app.state.gallery)
    assert client.delete(f"/api/gallery/{meta['id']}").json() == {"id": meta["id"], "deleted": True}
    assert client.get("/api/gallery").json() == []
    assert client.delete(f"/api/gallery/{meta['id']}").status_code == 404
