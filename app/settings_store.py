"""The on-screen settings panel's values, saved to a small JSON file.

These are the knobs you turn while experimenting, so they live in the UI
rather than in .env. They are app-wide on purpose (every browser tab sees
the same settings) and survive restarts via DATA_DIR/settings.json.

A drawing takes a copy of the settings when it starts, so changing them
mid-drawing never changes the model, vision or budgets under the agent.
Keep going top-ups aren't settings: you type them on the paused card each time.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from .view import DEFAULT_GRID, GRID_MODES

log = logging.getLogger("painty")

# "" sends no effort, so the model does what it does by default. Which of these a model
# takes comes from OpenRouter's catalogue (model_library.reasoning_options).
REASONING_EFFORTS = ("", "none", "minimal", "low", "medium", "high", "xhigh", "max")

# (min, max) for each whole-number setting.
LIMITS: dict[str, tuple[int, int]] = {
    "shape_budget": (1, 500),
    "look_budget": (0, 50),
    "max_shapes_per_call": (1, 100),
}


@dataclass(frozen=True)
class StudioSettings:
    shape_budget: int = 60          # shapes the agent may paint per drawing
    look_budget: int = 5            # times it may look at the canvas per drawing
    max_shapes_per_call: int = 15   # stops it dumping everything down in one blind batch
    vision: bool = True             # eyes on: the agent gets the look tool
    model: str = ""                 # blank = first of PAINTY_MODELS
    reasoning_effort: str = "medium"
    grid: str = DEFAULT_GRID        # how the agent's view marks positions: frame, overlay or none
    exemplar: bool = True           # show the agent the exemplar sheet in its first message
    planning: bool = False          # a forced first turn where the agent writes a plan
    planning_effort: str = "high"   # reasoning effort for that planning turn ("" = model default)


def clean(raw: dict, models: tuple[str, ...], base: StudioSettings | None = None) -> StudioSettings:
    """Merge raw values (from the browser or the file) into valid settings.

    Unknown keys are ignored, numbers are clamped to LIMITS, and an unknown
    model or effort falls back to the current value.
    """
    current = base or StudioSettings(model=models[0])
    known = {f.name for f in fields(StudioSettings)}
    updates = {}
    for key, value in (raw or {}).items():
        if key not in known:
            continue
        if key in LIMITS:
            lo, hi = LIMITS[key]
            try:
                updates[key] = max(lo, min(hi, int(value)))
            except (TypeError, ValueError):
                continue
        elif key in ("vision", "exemplar", "planning"):
            updates[key] = bool(value)
        elif key == "model":
            if value in models:
                updates[key] = value
        elif key in ("reasoning_effort", "planning_effort"):
            if value in REASONING_EFFORTS:
                updates[key] = value
        elif key == "grid":
            if value in GRID_MODES:
                updates[key] = value
    result = replace(current, **updates)
    if result.model not in models:
        result = replace(result, model=models[0])
    return result


class SettingsStore:
    """Loads and saves StudioSettings as JSON. A missing or broken file means defaults."""

    def __init__(self, path: Path, models: tuple[str, ...]) -> None:
        self.path = path
        self.models = models
        self.current = self._load()

    def _load(self) -> StudioSettings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable settings file %s: %s", self.path, exc)
            raw = {}
        return clean(raw, self.models)

    def update(self, raw: dict) -> StudioSettings:
        self.current = clean(raw, self.models, self.current)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(asdict(self.current), indent=2), encoding="utf-8")
        except OSError as exc:  # still works for this run, just not saved
            log.warning("could not save settings to %s: %s", self.path, exc)
        return self.current

    def set_models(self, models: tuple[str, ...]) -> None:
        """The models on offer changed. If the chosen one has gone, the first is chosen."""
        self.models = models
        self.current = clean({}, models, self.current)

    def options(self) -> dict:
        """What the settings panel needs to build its controls."""
        return {"models": list(self.models), "reasoning_efforts": list(REASONING_EFFORTS),
                "grids": list(GRID_MODES),
                "limits": {k: list(v) for k, v in LIMITS.items()}}
