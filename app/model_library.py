"""The models Painty offers: PAINTY_MODELS, plus any added from OpenRouter's catalogue.

Models added on screen live in DATA_DIR/models.json with their prices, so they need
nothing in .env. A removed model is hidden, not forgotten: it keeps its price, so
older paintings made with it stay priced.

The catalogue is OpenRouter's public model list (no key needed), kept to the models
Painty can use, and cached for an hour. Each fetch refreshes the saved prices and
capabilities (whether a model can see images, and its reasoning efforts),
because costs always use the current prices, as with those set in .env. It is asked
for in order of popularity, because OpenRouter gives no popularity number: a model's
place in the list is all there is. Scores from Artificial Analysis and Design Arena
come with it, for the models that have them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .pricing import Price, Prices, price_var

log = logging.getLogger("painty")

CATALOGUE_URL = "https://openrouter.ai/api/v1/models?sort=most-popular"
CATALOGUE_TTL = 3600  # seconds
RESPONSES_ENDPOINT = "/api/v1/responses"
MAX_DESCRIPTION = 400
DESIGN_ARENA_RATING = ("models", "svg")  # its arena and category


# Reasoning efforts in OpenRouter's order, lowest first, and how the dialog names them.
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
EFFORT_NAMES = {"none": "Off", "minimal": "Minimal", "low": "Low", "medium": "Medium", "high": "High",
                "xhigh": "Extra high", "max": "Max"}
# For a model that reasons but lists no efforts, OpenRouter turns these into a thinking budget.
BUDGET_EFFORTS = ("low", "medium", "high")


class CatalogueError(Exception):
    """OpenRouter's model list couldn't be fetched or read. The text is fit to show."""


def _per_million(value) -> float | None:
    """OpenRouter's price per token (a string) as dollars per million tokens."""
    try:
        price = round(float(value) * 1_000_000, 6)
    except (TypeError, ValueError):
        return None
    return price if price >= 0 and price == price and price != float("inf") else None


def _number(value) -> float | int | None:
    """A score as sent, or None if it's missing or isn't a real number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _benchmarks(raw: dict) -> dict:
    """The third-party scores Painty sorts by, each None when the model doesn't have it.
    Design Arena rates a model per category with no overall number. SVG is the closest
    to painting from code."""
    scores = raw.get("benchmarks")
    scores = scores if isinstance(scores, dict) else {}
    analysis = scores.get("artificial_analysis")
    analysis = analysis if isinstance(analysis, dict) else {}
    arena = scores.get("design_arena")
    svg = next((e.get("elo") for e in (arena if isinstance(arena, list) else [])
                if isinstance(e, dict) and (e.get("arena"), e.get("category")) == DESIGN_ARENA_RATING), None)
    return {"intelligence": _number(analysis.get("intelligence_index")),
            "agentic": _number(analysis.get("agentic_index")),
            "design_arena_svg": _number(svg)}


def _label(value: str, default: str, reasons_by_default: bool) -> str:
    name = EFFORT_NAMES.get(value) or ("On" if reasons_by_default else "Off")
    return name + (" (default)" if value == default else "")


def reasoning_options(raw: dict) -> dict:
    """The reasoning efforts a model takes, as the dialog offers them:
    {"options": [[value, label], ...], "default": value}. No options means it can't reason.

    Most models list their efforts and a default. A value of "" sends no effort at all,
    so the model does what it does by default: for a model that reasons but lists no
    efforts (OpenRouter turns low, medium and high into a thinking budget for those),
    and for one that doesn't reason unless asked but can't be sent "none". A model
    that must reason is never offered "none".
    """
    params = raw.get("supported_parameters") if isinstance(raw.get("supported_parameters"), list) else []
    if "reasoning" not in params:
        return {"options": [], "default": ""}
    info = raw.get("reasoning") if isinstance(raw.get("reasoning"), dict) else {}
    mandatory = bool(info.get("mandatory"))
    on = mandatory or info.get("default_enabled") is not False
    listed = info.get("supported_efforts") if isinstance(info.get("supported_efforts"), list) else []
    efforts = [e for e in EFFORT_ORDER if e in listed and not (mandatory and e == "none")]
    if efforts:
        default = info.get("default_effort") if on else "none"
        if default not in efforts:  # off by default with no "none" to send, or no default given
            efforts, default = [""] + efforts, ""
    else:
        efforts = [""] + ([] if mandatory or not on else ["none"]) + list(BUDGET_EFFORTS)
        default = ""
    return {"options": [[e, _label(e, default, on)] for e in efforts], "default": default}


def parse_model(raw: dict) -> dict | None:
    """One catalogue entry as Painty keeps it, or None if Painty can't use the model.

    It must call tools and have a Responses endpoint (the :batch variants don't). A
    negative price marks a router such as openrouter/auto, whose price depends on the
    model it picks. A missing cached or cache write price is the input price, as in
    pricing.parse_price. A model that can't take images is kept, marked vision false:
    it can only paint blind, with no exemplar sheets and no reference image.
    """
    try:
        model_id = raw["id"]
        pricing = raw["pricing"]
        takes_images = "image" in raw["architecture"]["input_modalities"]
        calls_tools = "tools" in raw["supported_parameters"]
        has_endpoint = RESPONSES_ENDPOINT in raw["endpoints"]
    except (KeyError, TypeError):
        return None
    if not (isinstance(model_id, str) and "/" in model_id and calls_tools and has_endpoint):
        return None
    p_in, p_out = _per_million(pricing.get("prompt")), _per_million(pricing.get("completion"))
    if p_in is None or p_out is None:
        return None
    p_cached, p_write = _per_million(pricing.get("input_cache_read")), _per_million(pricing.get("input_cache_write"))
    return {
        "id": model_id,
        "name": str(raw.get("name") or model_id),
        "description": str(raw.get("description") or "")[:MAX_DESCRIPTION],
        "created": raw.get("created") or 0,
        "context_length": raw.get("context_length") or 0,
        "price": [p_in, p_in if p_cached is None else p_cached, p_out, p_in if p_write is None else p_write],
        "benchmarks": _benchmarks(raw),
        "vision": takes_images,
        "reasoning": reasoning_options(raw),
    }


def capabilities(entry: dict) -> dict:
    """What the New painting dialog needs to know about a model: whether it can see
    images, and its reasoning efforts. Models saved before these were kept could all see."""
    return {"vision": entry.get("vision", True), "reasoning": entry.get("reasoning")}


def parse_catalogue(raw: dict) -> list[dict]:
    """The usable models, most popular first, each with its place among them (popularity)."""
    data = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data, list):
        raise CatalogueError("OpenRouter's model list wasn't in the expected form.")
    usable = [m for m in map(parse_model, data) if m]
    return [{**m, "popularity": place} for place, m in enumerate(usable, start=1)]


def fetch_catalogue() -> dict:
    """Blocking: call it off the event loop."""
    request = urllib.request.Request(CATALOGUE_URL, headers={"User-Agent": "Painty"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except (OSError, ValueError) as exc:  # URLError and timeouts are OSErrors
        raise CatalogueError(f"Couldn't fetch OpenRouter's model list: {exc}") from exc


class ModelLibrary:
    def __init__(self, path: Path, base_models: tuple[str, ...], env_prices: Prices | None = None,
                 fetch: Callable[[], dict] = fetch_catalogue) -> None:
        self.path = path
        self.base_models = base_models
        self.env_prices = env_prices or {}
        self.fetch = fetch
        self.saved = self._load()  # id -> {"name", "price", "offered", "vision", "reasoning"}
        self._catalogue: list[dict] | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    def _load(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable model list %s: %s", self.path, exc)
            return {}
        models = raw.get("models") if isinstance(raw, dict) else None
        if not isinstance(models, dict):
            return {}
        return {k: v for k, v in models.items()
                if isinstance(v, dict) and isinstance(v.get("price"), list) and len(v["price"]) == 4}

    def _save(self, saved: dict[str, dict]) -> None:
        """Atomic, so a crash never leaves a half-written file. Raises OSError."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"models": saved}, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def offered(self) -> list[str]:
        """The ids added on screen and not removed."""
        return [k for k, v in self.saved.items() if v.get("offered")]

    def models(self) -> tuple[str, ...]:
        """Every model on offer: PAINTY_MODELS first, so the default doesn't change."""
        return self.base_models + tuple(m for m in self.offered() if m not in self.base_models)

    def prices(self) -> Prices:
        """Every known model's price, removed ones too. A price set in .env wins."""
        prices: Prices = {}
        for model, entry in self.saved.items():
            p: Price = tuple(float(x) for x in entry["price"])  # type: ignore[assignment]
            prices[price_var(model)] = p
        return {**prices, **self.env_prices}

    def capabilities(self, model: str) -> dict | None:
        """A model's capabilities, or None if they aren't known yet (the catalogue hasn't
        been fetched, or it isn't on OpenRouter). A model run directly on OpenAI takes
        them from the same model on OpenRouter (gpt-6-luna from openai/gpt-6-luna)."""
        by_id = {m["id"]: m for m in self._catalogue or []}
        found = by_id.get(model) or (None if "/" in model else by_id.get(f"openai/{model}"))
        if found:
            return capabilities(found)
        entry = self.saved.get(model)
        return capabilities(entry) if entry and entry.get("reasoning") else None

    def model_info(self) -> dict[str, dict | None]:
        """The capabilities of every model on offer, as far as they're known."""
        return {m: self.capabilities(m) for m in self.models()}

    async def catalogue(self) -> list[dict]:
        """The usable models, cached. If a refresh fails, an older copy is still fine."""
        async with self._lock:
            if self._catalogue is not None and time.monotonic() - self._fetched_at < CATALOGUE_TTL:
                return self._catalogue
            try:
                raw = await asyncio.to_thread(self.fetch)
                models = parse_catalogue(raw)
            except CatalogueError as exc:
                if self._catalogue is None:
                    raise
                log.warning("using an older copy of the model list: %s", exc)
                return self._catalogue
            self._catalogue, self._fetched_at = models, time.monotonic()
            self._refresh_prices(models)
            return models

    def _refresh_prices(self, models: list[dict]) -> None:
        """Saved models take the catalogue's current price and capabilities."""
        by_id = {m["id"]: m for m in models}
        fresh = {k: {**v, "price": by_id[k]["price"], **capabilities(by_id[k])}
                 for k, v in self.saved.items() if k in by_id}
        changed = {k: v for k, v in fresh.items() if v != self.saved[k]}
        if not changed:
            return
        self.saved = {**self.saved, **changed}
        try:
            self._save(self.saved)
        except OSError as exc:  # the prices still work until a restart
            log.warning("could not save model prices to %s: %s", self.path, exc)

    async def add(self, model_id: str) -> None:
        """Offer a model from the catalogue. ValueError if it isn't there; OSError if
        it can't be saved. CatalogueError if the catalogue can't be fetched."""
        found = next((m for m in await self.catalogue() if m["id"] == model_id), None)
        if found is None:
            raise ValueError(f"{model_id} isn't a model Painty can use on OpenRouter.")
        saved = {**self.saved, model_id: {"name": found["name"], "price": found["price"], "offered": True,
                                          **capabilities(found)}}
        self._save(saved)
        self.saved = saved

    def remove(self, model_id: str) -> bool:
        """Stop offering a model, keeping its price. False if it wasn't added on screen.
        Raises OSError if it can't be saved."""
        if model_id not in self.offered():
            return False
        saved = {**self.saved, model_id: {**self.saved[model_id], "offered": False}}
        self._save(saved)
        self.saved = saved
        return True
