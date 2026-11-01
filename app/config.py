"""Settings read from environment variables (see .env.example).

Only deployment details live here. Everything you tune while experimenting
(budgets, model, vision) lives in the on-screen settings panel instead
(see app/settings_store.py).
"""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    # `or default` so a blank value in .env (e.g. `PAINTY_MODELS=`) uses the default.
    return (os.getenv(name) or default).strip()


def _models() -> tuple[str, ...]:
    raw = _env("PAINTY_MODELS", "gpt-6-luna,gpt-6.1-terra,gpt-6.1-sol,gpt-5.4-nano,gpt-5.4-mini,gpt-5.4")
    models = tuple(m.strip() for m in raw.split(",") if m.strip())
    return models or ("gpt-6-luna",)


@dataclass(frozen=True)
class Settings:
    # Models offered in the settings panel. The first is the default.
    models: tuple[str, ...] = field(default_factory=_models)
    # Name shown for each drawing run in the OpenAI Traces dashboard.
    trace_workflow_name: str = _env("TRACE_WORKFLOW_NAME", "Painty drawing")
    # Where the gallery and saved settings live. In the container: /srv/data.
    data_dir: Path = Path(_env("DATA_DIR", "data"))


settings = Settings()
