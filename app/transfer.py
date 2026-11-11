"""Exporting and importing paintings.

A painting exports as a zip of its whole gallery folder, inside one folder named
like the download, and a zip like that imports back as a new gallery painting,
here or in another Painty. Every download is named from the painting's title and
model, like "Sunny Face - gpt-6-luna.zip".

An import is someone else's file, so it is checked before anything is written:

- Only the files a painting folder holds are taken (see _ALLOWED), anything else
  (such as a __MACOSX folder) is skipped. Paths can't leave the folder.
- Size and file count are capped, read as they're unpacked, so a zip can't claim
  to be small and unpack huge.
- meta.json must be a painting's meta; every PNG must be a PNG; painting.svg may
  hold only the elements and attributes Painty's own renderer writes. The gallery
  serves these files from Painty's own address, so a script in an SVG would run there.
- The painting is unpacked beside the gallery and moved in once it's complete,
  so the gallery never lists a half-imported painting.

A painting that is already in the gallery (the same title, saved at the same moment)
is refused, with the id of the one already there.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import quote

from PIL import Image, UnidentifiedImageError

from .gallery import _ID, Gallery, _slug

MAX_IMPORT_BYTES = 100 * 1024 * 1024    # the zip as uploaded (the largest painting so far is ~7 MB)
MAX_UNPACKED_BYTES = 200 * 1024 * 1024  # everything in it, unpacked
MAX_FILES = 3000

# The files a painting folder holds (gallery.py), as paths inside it.
_ALLOWED = re.compile(
    r"(meta\.json|painting\.(png|svg)|shapes\.(txt|json)|plan\.txt|benchmark\.json"
    r"|benchmark-diff\.png|reference(-view)?\.png|exemplar-\d{1,3}\.png"
    r"|looks/look-\d{1,4}\.png|stages/stage-\d{1,4}\.png)")
_REQUIRED = ("meta.json", "painting.png", "painting.svg")

# Everything Painty's renderer writes in an SVG (canvas.shape_svg and Canvas.to_svg).
_SVG = "{http://www.w3.org/2000/svg}"
_SVG_TAGS = {
    "svg", "g", "defs", "rect", "ellipse", "circle", "line", "polyline", "polygon", "path",
    "linearGradient", "radialGradient", "stop", "filter", "feGaussianBlur", "feTurbulence",
    "feComponentTransfer", "feFuncA", "feComposite",
}
_SVG_ATTRS = {
    "baseFrequency", "color-interpolation-filters", "cx", "cy", "d", "data-n", "fill", "filter",
    "filterUnits", "gradientUnits", "height", "id", "in", "in2", "intercept", "numOctaves",
    "offset", "opacity", "operator", "points", "r", "result", "rx", "ry", "seed", "slope",
    "stdDeviation", "stop-color", "stroke", "stroke-linecap", "stroke-linejoin", "stroke-width",
    "transform", "type", "viewBox", "width", "x", "x1", "x2", "y", "y1", "y2",
}
_URL = re.compile(r"url\(\s*([^)]*)\)")


class NotAPainting(ValueError):
    """The zip isn't a painting Painty can import. The message says why, for the person."""


class AlreadyImported(Exception):
    """The painting is already in the gallery. id is the one already there."""

    def __init__(self, painting_id: str) -> None:
        super().__init__(painting_id)
        self.id = painting_id


# ------------------------------------------------------------------ names
_UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", _UNSAFE.sub(" ", text or "")).strip(" .")
    return text[:limit].rstrip(" .")


def export_name(meta: dict, ext: str, kind: str = "") -> str:
    """A download's file name, from the painting's title and model:
    "Sunny Face - gpt-6-luna.png", "Sunny Face - anthropic-claude-sonnet-5.5 - card.png"."""
    title = _clean(meta.get("title") or "", 80) or "Untitled"
    model = _clean(str((meta.get("settings") or {}).get("model") or "").replace("/", "-"), 60)
    parts = [title, model or "unknown model", *([kind] if kind else [])]
    return f"{' - '.join(parts)}.{ext}"


def attachment(name: str) -> str:
    """A Content-Disposition header that downloads as name, with a plain ASCII fallback
    for browsers that don't read the UTF-8 one."""
    plain = name.encode("ascii", "ignore").decode().strip() or f"painting.{name.rsplit('.', 1)[-1]}"
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"


# ------------------------------------------------------------------ export
def export_zip(folder: Path, meta: dict) -> bytes:
    """Every file in a painting's folder, zipped inside one folder named like the download."""
    top = export_name(meta, "zip")[:-4]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(folder).as_posix()
            if path.is_file() and _ALLOWED.fullmatch(rel):
                # PNGs are already compressed: storing them saves time and costs nothing.
                method = zipfile.ZIP_STORED if rel.endswith(".png") else zipfile.ZIP_DEFLATED
                zf.write(path, f"{top}/{rel}", compress_type=method)
    return buf.getvalue()


# ------------------------------------------------------------------ import
def import_zip(gallery: Gallery, data: bytes) -> dict:
    """Add an exported painting to the gallery and return its meta (with its new id).

    Raises NotAPainting if it isn't a painting Painty can import, AlreadyImported
    if it's already in the gallery, and OSError if it can't be written."""
    if len(data) > MAX_IMPORT_BYTES:
        raise NotAPainting(f"That file is too big to be a painting (over {MAX_IMPORT_BYTES // 2**20} MB).")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise NotAPainting("That isn't a zip file. Import a painting exported from Painty as a zip.") from None
    with zf:
        files = _read_files(zf)
    meta = _check(files)

    for other in gallery.list():
        if other.get("created_at") == meta["created_at"] and other.get("title") == meta.get("title"):
            raise AlreadyImported(str(other.get("id")))

    painting_id = _new_id(gallery, meta)
    meta.pop("cost", None)  # worked out live from today's prices, never saved
    meta.update(id=painting_id, imported_at=time.time())
    files["meta.json"] = json.dumps(meta, indent=2).encode("utf-8")

    gallery.root.mkdir(parents=True, exist_ok=True)
    staging = gallery.root.parent / f".painty-import-{uuid.uuid4().hex}"
    try:
        for rel, content in files.items():
            path = staging / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        staging.rename(gallery.root / painting_id)  # all at once, so it's never listed half done
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return meta


def _read_files(zf: zipfile.ZipFile) -> dict[str, bytes]:
    """The painting's files, by their path inside the painting's folder. The folder is
    the zip's top level, or one folder inside it (as exported, or zipped by hand)."""
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_FILES:
        raise NotAPainting("That zip holds far more files than a painting does.")
    metas = [i.filename for i in infos if i.filename.rsplit("/", 1)[-1] == "meta.json"
             and i.filename.count("/") <= 1 and not i.filename.startswith("__MACOSX/")]
    if len(metas) != 1:
        raise NotAPainting("That zip doesn't hold one painting: there should be exactly one "
                           "meta.json, at the top or in one folder. Export it from Painty's gallery.")
    prefix = metas[0][: -len("meta.json")]

    files: dict[str, bytes] = {}
    total = 0
    for info in infos:
        if not info.filename.startswith(prefix):
            continue
        rel = info.filename[len(prefix):]
        if not _ALLOWED.fullmatch(rel):
            continue  # not part of a painting: skipped, not refused
        with zf.open(info) as f:  # read no more than the cap, whatever the zip claims
            content = f.read(MAX_UNPACKED_BYTES - total + 1)
        total += len(content)
        if total > MAX_UNPACKED_BYTES:
            raise NotAPainting(f"That painting unpacks to over {MAX_UNPACKED_BYTES // 2**20} MB, "
                               "far more than a painting.")
        files[rel] = content
    missing = [name for name in _REQUIRED if name not in files]
    if missing:
        raise NotAPainting(f"That zip is missing {', '.join(missing)}, so it isn't a whole painting.")
    return files


def _check(files: dict[str, bytes]) -> dict:
    """Check every file is what its name says, and return the meta."""
    try:
        meta = json.loads(files["meta.json"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise NotAPainting("Its meta.json can't be read.") from None
    if (not isinstance(meta, dict) or not isinstance(meta.get("title"), str)
            or not isinstance(meta.get("settings"), dict)
            or not isinstance(meta.get("created_at"), (int, float))):
        raise NotAPainting("Its meta.json isn't a Painty painting's (no title, settings or date).")

    for rel, content in files.items():
        if rel.endswith(".png"):
            try:
                with Image.open(io.BytesIO(content)) as im:
                    ok = im.format == "PNG"
                    im.verify()
            except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
                ok = False
            if not ok:
                raise NotAPainting(f"Its {rel} isn't a PNG image.")
        elif rel.endswith(".json") and rel != "meta.json":
            try:
                json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise NotAPainting(f"Its {rel} can't be read.") from None
        elif rel.endswith(".txt"):
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                raise NotAPainting(f"Its {rel} isn't text.") from None
    _check_svg(files["painting.svg"])
    return meta


def _check_svg(content: bytes) -> None:
    """painting.svg may hold only what Painty's renderer writes: no scripts, links or
    outside files, which the gallery would otherwise serve from Painty's own address."""
    refuse = NotAPainting("Its painting.svg holds things Painty never paints with, so it wasn't imported.")
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise refuse
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        raise NotAPainting("Its painting.svg can't be read.") from None
    for node in root.iter():
        if not node.tag.startswith(_SVG) or node.tag[len(_SVG):] not in _SVG_TAGS:
            raise refuse
        if (node.text or "").strip() or (node.tail or "").strip():
            raise refuse
        for name, value in node.attrib.items():
            if name not in _SVG_ATTRS:
                raise refuse
            if any(not ref.strip().startswith("#") for ref in _URL.findall(value)):
                raise refuse


def _new_id(gallery: Gallery, meta: dict) -> str:
    """The painting's own id if it's free here, otherwise one made the same way as a save's."""
    wanted = str(meta.get("id") or "")
    if not _ID.fullmatch(wanted) or wanted in (".", "..") or wanted.startswith("."):
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(meta["created_at"]))
        wanted = f"{stamp}-{_slug(meta.get('title') or meta.get('prompt') or '')}"
    painting_id, n = wanted, 2
    while (gallery.root / painting_id).exists():
        painting_id = f"{wanted}-{n}"
        n += 1
    return painting_id
