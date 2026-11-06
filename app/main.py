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
from .exemplar import SHEETS, exemplar_text, exemplar_views
from .gallery import Gallery
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
app.state.store = SettingsStore(settings.data_dir / "settings.json", settings.models)
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


@app.get("/api/gallery")
async def gallery_list() -> list[dict]:
    """Every painting's meta, newest first, each with its estimated cost at today's
    prices (worked out here, never saved, so a price change reaches old paintings too)."""
    items = app.state.gallery.list()
    for m in items:
        m["cost"] = cost((m.get("settings") or {}).get("model", ""), m.get("input_tokens", 0),
                         m.get("cached_tokens", 0), m.get("output_tokens", 0), settings.prices,
                         m.get("cache_write_tokens", 0))
    return items


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

    studio = Studio(send, app.state.store, app.state.gallery)
    await send(studio.hello())

    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type") if isinstance(msg, dict) else None
            if kind == "paint":
                reference = msg.get("reference")
                await studio.start(str(msg.get("prompt", "")), str(msg.get("canvas") or ""),
                                   reference if isinstance(reference, str) else None)
            elif kind == "keep_going":
                await studio.keep_going()
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
