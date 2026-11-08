"""Which service runs a model: OpenAI directly, or OpenRouter.

One rule decides: a model id with a "/" in it (anthropic/claude-sonnet-5.5,
openai/gpt-6-luna) goes through OpenRouter, and anything else (gpt-6-luna) goes
direct to OpenAI. Every OpenRouter id has one and no OpenAI id does, so a model
stays one plain string everywhere else in the app.

Both routes use the Responses API. OpenRouter has its own, which takes images in
tool results, so the look tool and the history work the same on both. The SDK's
Chat Completions route would strip those images by default.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from agents import Model, OpenAIResponsesModel
from openai import AsyncOpenAI

from .config import settings as env

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def is_openrouter(model: str) -> bool:
    return "/" in (model or "")


@lru_cache(maxsize=1)
def _openrouter_client() -> AsyncOpenAI:
    """Its own client, never the SDK's default: traces and native models don't use it."""
    return AsyncOpenAI(base_url=OPENROUTER_BASE_URL, api_key=env.openrouter_api_key or "missing")


def agent_model(name: str) -> str | Model:
    """What to give the Agent. A Model object for OpenRouter, because the SDK reads a
    string's prefix itself: it would strip "openai/" and refuse "anthropic/"."""
    if is_openrouter(name):
        return OpenAIResponsesModel(model=name, openai_client=_openrouter_client())
    return name


def provider_settings(name: str) -> dict[str, Any]:
    """Extra ModelSettings for a model's route. Nothing for a native model.

    preserve_raw_usage keeps OpenRouter's usage.cost, the amount actually billed.
    Anthropic's prompt cache is opt-in, so its models ask for it (without it, every
    call pays full price for the exemplar sheets, the reference and every look).
    """
    if not is_openrouter(name):
        return {}
    settings: dict[str, Any] = {"preserve_raw_usage": True}
    if name.lstrip("~").startswith("anthropic/"):
        settings["extra_body"] = {"cache_control": {"type": "ephemeral"}}
    return settings
