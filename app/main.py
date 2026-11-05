"""FastAPI entry point: serves the UI, the gallery and the /ws WebSocket.

WebSocket in one line: a phone call that stays open, so either side can talk
at any time. The browser sends commands (paint, keep going, stop...); the
server streams back shapes, looks, budget changes and activity-feed entries.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import settings
from .gallery import Gallery
from .settings_store import SettingsStore
from .studio import Studio

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


@app.get("/api/gallery")
async def gallery_list() -> list[dict]:
    return app.state.gallery.list()


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
                await studio.start(str(msg.get("prompt", "")), str(msg.get("canvas") or ""))
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
