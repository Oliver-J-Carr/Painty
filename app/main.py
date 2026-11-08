"""FastAPI entry point: serves the UI, the gallery and the /ws WebSocket.

WebSocket in one line: a phone call that stays open, so either side can talk
at any time. The browser sends commands (paint, keep going, stop...); the
server streams back shapes, looks, budget changes and activity-feed entries.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .config import settings
from .benchmark import CARDS, card_png, card_text
from .benchmark_score import best_scores
from .exemplar import SHEETS, exemplar_text, exemplar_views
from .gallery import Gallery, settings_dict
from .model_library import CatalogueError, ModelLibrary
from .pricing import cost
from .settings_store import SettingsStore
from .studio import Studio
from .view import GRID_MODES

log = logging.getLogger("painty")
STATIC = Path(__file__).parent / "static"
GALLERY_DIR = settings.data_dir / "gallery"

try:
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
except OSError as exc:  # the app still runs; saving will report the problem in the UI
    log.warning("can't create %s: %s", GALLERY_DIR, exc)

app = FastAPI(title="Painty", version=__version__)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/gallery", StaticFiles(directory=GALLERY_DIR, check_dir=False), name="gallery")

# App-wide on purpose: every tab shares the settings and the gallery.
# Everything about a drawing lives in its connection's Studio.
app.state.library = ModelLibrary(settings.data_dir / "models.json", settings.models, settings.prices)
app.state.store = SettingsStore(settings.data_dir / "settings.json", app.state.library.models())
app.state.gallery = Gallery(GALLERY_DIR)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "version": __version__}


@app.get("/exemplar.png")
async def exemplar_png(sheet: int = 1, grid: str = "frame") -> Response:
    """An exemplar sheet exactly as the agent sees it, for checking it by eye."""
    if grid not in GRID_MODES or not 1 <= sheet <= len(SHEETS):
        raise HTTPException(404, f"Use sheet 1 to {len(SHEETS)} and grid {', '.join(GRID_MODES)}.")
    return Response(exemplar_views(grid)[sheet - 1], media_type="image/png")


@app.get("/exemplar.txt")
async def exemplar_txt() -> Response:
    """The text sent with the exemplar sheets."""
    return Response(exemplar_text(), media_type="text/plain; charset=utf-8")


@app.get("/benchmark/{name}.png")
async def benchmark_png(name: str) -> Response:
    """A benchmark card, clean, exactly as Painty is given it to reproduce."""
    if name not in CARDS:
        raise HTTPException(404, f"There's no benchmark card called {name}. Try {', '.join(CARDS)}.")
    return Response(card_png(name), media_type="image/png")


@app.get("/benchmark/{name}.txt")
async def benchmark_txt(name: str) -> Response:
    """A benchmark card's shapes in the notation, item by item."""
    if name not in CARDS:
        raise HTTPException(404, f"There's no benchmark card called {name}. Try {', '.join(CARDS)}.")
    return Response(card_text(name), media_type="text/plain; charset=utf-8")


@app.get("/api/gallery")
async def gallery_list() -> list[dict]:
    """Every painting's meta, newest first, each with its estimated cost at today's
    prices (worked out here, never saved, so a price change reaches old paintings too)."""
    items = app.state.gallery.list()
    prices = app.state.library.prices()
    for m in items:
        m["cost"] = cost((m.get("settings") or {}).get("model", ""), m.get("input_tokens", 0),
                         m.get("cached_tokens", 0), m.get("output_tokens", 0), prices,
                         m.get("cache_write_tokens", 0), m.get("billed_usd"))
    return items


class ModelId(BaseModel):
    id: str


def _model_list() -> dict:
    """The models on offer after a change, with the settings (the chosen one may have gone)."""
    library, store = app.state.library, app.state.store
    store.set_models(library.models())
    return {"models": list(library.models()), "added": library.offered(),
            "settings": settings_dict(store.current)}


@app.get("/api/models/openrouter")
async def openrouter_models() -> dict:
    """OpenRouter's models that Painty can use, and which of them are on offer."""
    try:
        models = await app.state.library.catalogue()
    except CatalogueError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"models": models, "added": app.state.library.offered(),
            "has_key": bool(settings.openrouter_api_key),
            "painty_scores": best_scores(app.state.gallery.list())}


@app.post("/api/models")
async def add_model(body: ModelId) -> dict:
    """Offer a model from OpenRouter's catalogue, with its price."""
    try:
        await app.state.library.add(body.id)
    except CatalogueError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except OSError as exc:
        log.warning("could not save the model list: %s", exc)
        raise HTTPException(500, "Couldn't save that. Check that DATA_DIR is writable.") from exc
    return _model_list()


@app.delete("/api/models/{model_id:path}")
async def remove_model(model_id: str) -> dict:
    """Stop offering a model added on screen. It keeps its price, for older paintings."""
    try:
        removed = app.state.library.remove(model_id)
    except OSError as exc:
        log.warning("could not save the model list: %s", exc)
        raise HTTPException(500, "Couldn't save that. Check that DATA_DIR is writable.") from exc
    if not removed:
        raise HTTPException(404, f"{model_id} wasn't added here. Models in PAINTY_MODELS stay on offer.")
    return _model_list()


class Favourite(BaseModel):
    favourite: bool


@app.put("/api/gallery/{painting_id}/favourite")
async def gallery_favourite(painting_id: str, body: Favourite) -> dict:
    """Star or unstar a painting. Saved in its meta.json."""
    try:
        meta = app.state.gallery.set_favourite(painting_id, body.favourite)
    except OSError as exc:
        log.warning("could not save favourite for %s: %s", painting_id, exc)
        raise HTTPException(500, "Couldn't save that. Check that DATA_DIR is writable.") from exc
    if meta is None:
        raise HTTPException(404, "There's no painting with that id.")
    return {"id": painting_id, "favourite": meta["favourite"]}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()

    async def send(event: dict) -> None:
        await ws.send_json(event)

    studio = Studio(send, app.state.store, app.state.gallery, library=app.state.library)
    await send(studio.hello())

    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type") if isinstance(msg, dict) else None
            if kind == "paint":
                reference, benchmark = msg.get("reference"), msg.get("benchmark")
                await studio.start(str(msg.get("prompt", "")), str(msg.get("canvas") or ""),
                                   reference if isinstance(reference, str) else None,
                                   benchmark if isinstance(benchmark, str) and benchmark else None)
            elif kind == "keep_going":
                await studio.keep_going(msg.get("shapes", 0), msg.get("looks", 0))
            elif kind == "stop":
                await studio.stop()
            elif kind == "save":
                await studio.save()
            elif kind == "settings":
                await studio.update_settings(msg.get("settings") or {})
    except WebSocketDisconnect:
        log.info("client disconnected")
    finally:
        await studio.stop()
