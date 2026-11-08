"""The route a model takes: OpenAI directly, or OpenRouter. No network or key needed."""

import dataclasses

import pytest
from agents import OpenAIResponsesModel
from fastapi.testclient import TestClient

import app.main as main_module
from app import providers
from app.gallery import Gallery
from app.model_library import CatalogueError, ModelLibrary, parse_catalogue, parse_model
from app.painter import build_painter_agent
from app.pricing import cost, price_var
from app.settings_store import SettingsStore, StudioSettings


def entry(model_id="google/gemma-4-31b-it", **changes):
    """One model as OpenRouter's catalogue lists it."""
    raw = {
        "id": model_id, "name": "Google: Gemma 4 31B", "description": "A model.", "created": 100,
        "context_length": 262144,
        "architecture": {"input_modalities": ["text", "image"]},
        "pricing": {"prompt": "0.0000002", "completion": "0.0000004"},
        "supported_parameters": ["tools", "tool_choice", "reasoning"],
        "endpoints": ["/api/v1/chat/completions", "/api/v1/responses"],
    }
    return {**raw, **changes}


# ------------------------------------------------------------------ routing
@pytest.mark.parametrize("model, routed", [
    ("gpt-6-luna", False), ("gpt-5.4-mini", False), ("", False),
    ("openai/gpt-6-luna", True), ("anthropic/claude-sonnet-5.5", True),
    ("google/gemma-4-31b-it:free", True), ("~anthropic/claude-sonnet-latest", True),
])
def test_a_slash_in_the_id_means_openrouter(model, routed):
    assert providers.is_openrouter(model) is routed


def test_native_models_stay_exactly_as_they_were():
    agent = build_painter_agent(StudioSettings(model="gpt-6-luna"), cache_key="d1")
    assert agent.model == "gpt-6-luna"
    ms = agent.model_settings
    assert ms.extra_args == {"prompt_cache_key": "d1"} and ms.parallel_tool_calls is False
    assert ms.extra_body is None and ms.preserve_raw_usage is None


def test_openrouter_models_use_the_responses_api_at_openrouter():
    agent = build_painter_agent(StudioSettings(model="google/gemma-4-31b-it"), cache_key="d1")
    assert isinstance(agent.model, OpenAIResponsesModel)
    assert agent.model.model == "google/gemma-4-31b-it"  # the whole id, prefix included
    assert str(agent.model._get_client().base_url).startswith(providers.OPENROUTER_BASE_URL)
    ms = agent.model_settings
    assert ms.preserve_raw_usage is True and ms.extra_args == {"prompt_cache_key": "d1"}
    assert ms.extra_body is None  # this model's cache needs no asking


@pytest.mark.parametrize("model", ["anthropic/claude-haiku-5.5", "~anthropic/claude-sonnet-latest"])
def test_only_anthropic_models_opt_in_to_the_prompt_cache(model):
    assert providers.provider_settings(model)["extra_body"] == {"cache_control": {"type": "ephemeral"}}


def test_the_key_never_shows_in_the_settings_repr():
    from app.config import Settings
    assert "secret-value" not in repr(dataclasses.replace(Settings(), openrouter_api_key="secret-value"))


# ------------------------------------------------------------------ the catalogue
def test_prices_become_dollars_per_million_tokens():
    m = parse_model(entry(pricing={"prompt": "0.000001", "completion": "0.000005",
                                   "input_cache_read": "0.00000005", "input_cache_write": "0.00000125",
                                   "overrides": [{"min_prompt_tokens": 272000}]}))
    assert m["price"] == [1.0, 0.05, 5.0, 1.25]


def test_missing_cached_and_write_prices_are_the_input_price():
    assert parse_model(entry())["price"] == [0.2, 0.2, 0.4, 0.2]


def test_free_models_are_kept_at_zero():
    m = parse_model(entry("google/gemma-4-31b-it:free", pricing={"prompt": "0", "completion": "0"}))
    assert m["price"] == [0.0, 0.0, 0.0, 0.0]


@pytest.mark.parametrize("changes", [
    {"architecture": {"input_modalities": ["text"]}},         # can't see
    {"supported_parameters": ["reasoning"]},                  # can't call tools
    {"endpoints": ["/api/v1/chat/completions"]},              # :batch models have no Responses endpoint
    {"pricing": {"prompt": "-1", "completion": "-1"}},        # a router, priced by what it picks
    {"pricing": {"completion": "0.000001"}},                  # no input price
    {"architecture": None},                                   # malformed
])
def test_models_painty_cant_use_are_left_out(changes):
    assert parse_model(entry(**changes)) is None


def test_a_broken_catalogue_says_so():
    with pytest.raises(CatalogueError):
        parse_catalogue({"oops": []})


# ------------------------------------------------------------------ the library
def library(tmp_path, *models, env_prices=None, base=("gpt-6-luna",), fetch=None):
    return ModelLibrary(tmp_path / "models.json", base, env_prices,
                        fetch=fetch or (lambda: {"data": list(models) or [entry()]}))


def run(coro):
    import asyncio
    return asyncio.run(coro)


def test_an_added_model_is_offered_and_priced_and_survives_a_restart(tmp_path):
    lib = library(tmp_path)
    run(lib.add("google/gemma-4-31b-it"))
    assert lib.models() == ("gpt-6-luna", "google/gemma-4-31b-it")
    assert lib.prices()[price_var("google/gemma-4-31b-it")] == (0.2, 0.2, 0.4, 0.2)
    assert library(tmp_path).models() == ("gpt-6-luna", "google/gemma-4-31b-it")


def test_a_model_not_in_the_catalogue_cant_be_added(tmp_path):
    lib = library(tmp_path)
    with pytest.raises(ValueError):
        run(lib.add("made/up"))
    assert lib.models() == ("gpt-6-luna",)


def test_a_removed_model_keeps_its_price_for_older_paintings(tmp_path):
    lib = library(tmp_path)
    run(lib.add("google/gemma-4-31b-it"))
    assert lib.remove("google/gemma-4-31b-it") is True
    assert lib.models() == ("gpt-6-luna",)
    reloaded = library(tmp_path)
    assert reloaded.models() == ("gpt-6-luna",)
    assert cost("google/gemma-4-31b-it", 1_000_000, 0, 0, reloaded.prices())["usd"] == pytest.approx(0.2)


def test_only_models_added_on_screen_can_be_removed(tmp_path):
    assert library(tmp_path).remove("gpt-6-luna") is False


def test_a_price_in_env_beats_the_catalogue(tmp_path):
    var = price_var("google/gemma-4-31b-it")
    lib = library(tmp_path, env_prices={var: (9.0, 9.0, 9.0, 9.0)})
    run(lib.add("google/gemma-4-31b-it"))
    assert lib.prices()[var] == (9.0, 9.0, 9.0, 9.0)


def test_a_new_catalogue_updates_saved_prices(tmp_path):
    lib = library(tmp_path)
    run(lib.add("google/gemma-4-31b-it"))
    lib._catalogue = None  # as if the hour was up
    lib.fetch = lambda: {"data": [entry(pricing={"prompt": "0.000001", "completion": "0.000002"})]}
    run(lib.catalogue())
    assert library(tmp_path).prices()[price_var("google/gemma-4-31b-it")] == (1.0, 1.0, 2.0, 1.0)


def test_the_catalogue_is_cached_and_an_old_copy_survives_a_failed_refresh(tmp_path):
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) > 1:
            raise CatalogueError("down")
        return {"data": [entry()]}
    lib = library(tmp_path, fetch=fetch)

    async def go():
        assert len(await lib.catalogue()) == 1
        assert len(await lib.catalogue()) == 1
        assert len(calls) == 1
        lib._fetched_at = -1e9  # as if the hour was up
        assert len(await lib.catalogue()) == 1  # the fetch failed, the old copy is fine
        assert len(calls) == 2
    run(go())


def test_an_unreadable_file_means_no_added_models(tmp_path):
    (tmp_path / "models.json").write_text("not json")
    assert library(tmp_path).models() == ("gpt-6-luna",)


def test_settings_keep_an_added_model_after_a_restart_and_fall_back_when_it_goes(tmp_path):
    lib = library(tmp_path)
    run(lib.add("google/gemma-4-31b-it"))
    store = SettingsStore(tmp_path / "settings.json", lib.models())
    store.update({"model": "google/gemma-4-31b-it"})
    assert SettingsStore(tmp_path / "settings.json", lib.models()).current.model == "google/gemma-4-31b-it"
    lib.remove("google/gemma-4-31b-it")
    store.set_models(lib.models())
    assert store.current.model == "gpt-6-luna"
    assert store.options()["models"] == ["gpt-6-luna"]


# ------------------------------------------------------------------ the API
@pytest.fixture
def client(tmp_path, monkeypatch):
    lib = library(tmp_path)
    monkeypatch.setattr(main_module.app.state, "library", lib)
    monkeypatch.setattr(main_module.app.state, "store", SettingsStore(tmp_path / "settings.json", lib.models()))
    monkeypatch.setattr(main_module.app.state, "gallery", Gallery(tmp_path / "gallery"))
    return TestClient(main_module.app)


def test_the_api_lists_adds_and_removes_models(client):
    listed = client.get("/api/models/openrouter").json()
    assert [m["id"] for m in listed["models"]] == ["google/gemma-4-31b-it"] and listed["added"] == []

    added = client.post("/api/models", json={"id": "google/gemma-4-31b-it"}).json()
    assert added["models"] == ["gpt-6-luna", "google/gemma-4-31b-it"]
    assert added["added"] == ["google/gemma-4-31b-it"]
    assert main_module.app.state.store.models == ("gpt-6-luna", "google/gemma-4-31b-it")

    removed = client.delete("/api/models/google/gemma-4-31b-it").json()
    assert removed["models"] == ["gpt-6-luna"] and removed["settings"]["model"] == "gpt-6-luna"


def test_the_api_refuses_unknown_models(client):
    assert client.post("/api/models", json={"id": "made/up"}).status_code == 404
    assert client.delete("/api/models/gpt-6-luna").status_code == 404


def test_the_api_reports_when_openrouter_cant_be_reached(client):
    def down():
        raise CatalogueError("Couldn't fetch OpenRouter's model list: no route")
    main_module.app.state.library.fetch = down
    res = client.get("/api/models/openrouter")
    assert res.status_code == 502 and "no route" in res.json()["detail"]
