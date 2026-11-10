"""The route a model takes: OpenAI directly, or OpenRouter. No network or key needed."""

import dataclasses
import json

import pytest
from agents import OpenAIResponsesModel
from fastapi.testclient import TestClient

import app.main as main_module
from app import providers
from app.gallery import Gallery
from app.benchmark_score import SCORER_VERSION, best_scores
from app.model_library import CATALOGUE_URL, CatalogueError, ModelLibrary, parse_catalogue, parse_model, reasoning_options
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
    {"supported_parameters": ["reasoning"]},                  # can't call tools
    {"endpoints": ["/api/v1/chat/completions"]},              # :batch models have no Responses endpoint
    {"pricing": {"prompt": "-1", "completion": "-1"}},        # a router, priced by what it picks
    {"pricing": {"completion": "0.000001"}},                  # no input price
    {"architecture": None},                                   # malformed
])
def test_models_painty_cant_use_are_left_out(changes):
    assert parse_model(entry(**changes)) is None


def test_the_scores_painty_sorts_by_are_kept():
    m = parse_model(entry(benchmarks={
        "artificial_analysis": {"intelligence_index": 59.1, "coding_index": 70, "agentic_index": 57.2},
        "design_arena": [{"arena": "agents", "category": "webapps", "elo": 1300},
                         {"arena": "models", "category": "website", "elo": 1400},
                         {"arena": "models", "category": "svg", "elo": 1468}]}))
    assert m["benchmarks"] == {"intelligence": 59.1, "agentic": 57.2, "design_arena_svg": 1468}


@pytest.mark.parametrize("changes", [
    {},                                                                  # omitted when there's no data
    {"benchmarks": None},
    {"benchmarks": {"design_arena": [], "artificial_analysis": {"intelligence_index": None}}},
    {"benchmarks": {"design_arena": "nope", "artificial_analysis": {"agentic_index": "57"}}},
    {"benchmarks": {"design_arena": [{"arena": "agents", "category": "svg", "elo": 1300}]}},  # the wrong arena
    {"benchmarks": {"artificial_analysis": {"intelligence_index": True}}},
])
def test_a_missing_or_strange_score_is_none(changes):
    assert parse_model(entry(**changes))["benchmarks"] == {
        "intelligence": None, "agentic": None, "design_arena_svg": None}


def test_popularity_is_a_models_place_among_those_painty_can_use():
    models = parse_catalogue({"data": [entry("a/first"), entry("b/batch-only", endpoints=["/api/v1/batches"]),
                                       entry("c/second")]})
    assert [(m["id"], m["popularity"]) for m in models] == [("a/first", 1), ("c/second", 2)]


def test_the_catalogue_is_asked_for_in_order_of_popularity():
    assert CATALOGUE_URL.endswith("?sort=most-popular")


def _run(model, score, card="combined", scorer=SCORER_VERSION):
    return {"settings": {"model": model}, "benchmark": card, "benchmark_score": {"score": score, "scorer": scorer}}


def test_best_scores_keeps_each_models_best_on_the_combined_card():
    items = [_run("a/one", 0.4), _run("a/one", 0.9), _run("a/one", 0.7), _run("b/two", 0.0),
             _run("a/one", 1.0, card="basic"),         # cards differ in difficulty
             _run("c/old", 0.8, scorer=SCORER_VERSION - 1),  # not comparable
             {"settings": {"model": "d/plain"}},        # not a benchmark
             {"benchmark": "combined", "benchmark_score": {"score": 0.5, "scorer": SCORER_VERSION}}]  # no model
    assert best_scores(items) == {"a/one": {"best": 0.9, "runs": 3}, "b/two": {"best": 0.0, "runs": 1}}


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


def test_the_api_lists_painty_scores(client, tmp_path):
    folder = tmp_path / "gallery" / "20261108-120000-run"
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps(_run("google/gemma-4-31b-it", 0.5)))
    assert client.get("/api/models/openrouter").json()["painty_scores"] == {
        "google/gemma-4-31b-it": {"best": 0.5, "runs": 1}}


def test_the_api_refuses_unknown_models(client):
    assert client.post("/api/models", json={"id": "made/up"}).status_code == 404
    assert client.delete("/api/models/gpt-6-luna").status_code == 404


def test_the_api_reports_when_openrouter_cant_be_reached(client):
    def down():
        raise CatalogueError("Couldn't fetch OpenRouter's model list: no route")
    main_module.app.state.library.fetch = down
    res = client.get("/api/models/openrouter")
    assert res.status_code == 502 and "no route" in res.json()["detail"]


# ------------------------------------------------------------------ what each model can do
def options(**reasoning):
    """A model's reasoning efforts as the dialog offers them, for catalogue fields given."""
    params = reasoning.pop("params", ["tools", "reasoning"])
    return reasoning_options({"supported_parameters": params, "reasoning": reasoning or None})


def test_listed_efforts_are_offered_in_order_with_the_default_marked():
    # Claude Sonnet 5.5 as OpenRouter lists it: it must reason, and defaults to high.
    r = options(mandatory=True, supported_efforts=["max", "xhigh", "high", "medium", "low"], default_effort="high")
    assert r == {"default": "high", "options": [
        ["low", "Low"], ["medium", "Medium"], ["high", "High (default)"], ["xhigh", "Extra high"], ["max", "Max"]]}


def test_a_model_that_must_reason_is_never_offered_off():
    r = options(mandatory=True, supported_efforts=["none", "low", "high"], default_effort="high")
    assert [v for v, _ in r["options"]] == ["low", "high"]


def test_a_model_off_by_default_defaults_to_off():
    # gpt-5.4: reasons only when asked, and can be sent "none".
    r = options(mandatory=False, default_enabled=False,
                supported_efforts=["xhigh", "high", "medium", "low", "none"], default_effort="medium")
    assert r["default"] == "none" and r["options"][0] == ["none", "Off (default)"]


def test_off_by_default_with_no_none_to_send_sends_nothing():
    r = options(mandatory=False, default_enabled=False, supported_efforts=["low", "high"], default_effort="low")
    assert r["default"] == "" and r["options"][0] == ["", "Off (default)"]


def test_a_model_with_no_listed_efforts_gets_budget_levels():
    # Gemma 4: reasons only when asked, with no efforts of its own.
    gemma = options(mandatory=False, default_enabled=False)
    assert gemma == {"default": "", "options": [["", "Off (default)"], ["low", "Low"], ["medium", "Medium"],
                                                ["high", "High"]]}
    # Qwen 3.8 Flash: reasons by default and can be turned off.
    qwen = options(mandatory=False, default_enabled=True, supports_max_tokens=True)
    assert [v for v, _ in qwen["options"]] == ["", "none", "low", "medium", "high"]
    assert qwen["options"][:2] == [["", "On (default)"], ["none", "Off"]]


def test_a_model_that_cant_reason_has_no_options():
    assert options(params=["tools"]) == {"options": [], "default": ""}


def test_text_only_models_are_kept_and_marked():
    m = parse_model(entry("z-ai/glm-5.3", architecture={"input_modalities": ["text"]}))
    assert m["vision"] is False
    assert parse_model(entry())["vision"] is True


def test_capabilities_come_from_the_catalogue_including_for_direct_models(tmp_path):
    sonnet = entry("anthropic/claude-sonnet-5.5", reasoning={"mandatory": True, "supported_efforts": ["low", "high"],
                                                              "default_effort": "high"})
    luna = entry("openai/gpt-6-luna", reasoning={"supported_efforts": ["low", "medium"], "default_effort": "medium"})
    text_only = entry("z-ai/glm-5.3", architecture={"input_modalities": ["text"]})
    lib = library(tmp_path, sonnet, luna, text_only)
    assert lib.model_info() == {"gpt-6-luna": None}  # nothing known before the catalogue is fetched
    run(lib.add("anthropic/claude-sonnet-5.5"))
    run(lib.add("z-ai/glm-5.3"))
    info = lib.model_info()
    assert info["gpt-6-luna"]["reasoning"]["default"] == "medium"  # direct, from openai/gpt-6-luna
    assert info["anthropic/claude-sonnet-5.5"]["reasoning"]["default"] == "high"
    assert info["z-ai/glm-5.3"]["vision"] is False
    # Saved with the model, so they're known after a restart before the catalogue is fetched.
    restarted = library(tmp_path)
    assert restarted.capabilities("z-ai/glm-5.3")["vision"] is False
    assert restarted.capabilities("gpt-6-luna") is None


def test_the_api_gives_each_models_capabilities(client):
    info = client.get("/api/models/info").json()["model_info"]
    assert info["gpt-6-luna"] is None  # the test catalogue has no openai/gpt-6-luna
    added = client.post("/api/models", json={"id": "google/gemma-4-31b-it"}).json()
    assert added["model_info"]["google/gemma-4-31b-it"]["vision"] is True
