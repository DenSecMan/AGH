"""Choosing models and providers, and failing over between them in priority order."""
import pytest
from langchain_core.messages import SystemMessage

from agh import commands, llm
from agh.cli import main
from agh.engine import Engine
from agh.llm import ScriptedChatModel
from agh.workspace import Workspace, read_yaml

KEYS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT_NAME_CHAT",
        "AZURE_OPENAI_DEPLOYMENT_NAME_FAST", "AZURE_OPENAI_DEPLOYMENT_NAME_REASONING",
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "OLLAMA_API_KEY",
        "OLLAMA_BASE_URL", "LLMSTUDIO_API_KEY", "LLMSTUDIO_BASE_URL", "VLLM_API_KEY", "VLLM_BASE_URL",
        "GEMINI_BASE_URL", "OPENROUTER_BASE_URL")


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def both(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("OPENAI_API_KEY", "o")


def test_auto_follows_default_priority(both):
    assert llm.candidates("auto", {}) == ["anthropic:claude-opus-5-5", "openai:gpt-5"]


def test_auto_follows_configured_priority(both):
    config = {"models": {"providers": ["openai", "anthropic"]}}
    assert llm.candidates("auto", config) == ["openai:gpt-5", "anthropic:claude-opus-5-5"]
    assert llm.resolve_name(None, config, level="fast") == "openai:gpt-5-mini"


def test_unknown_priority_provider_is_an_error(both):
    with pytest.raises(llm.ModelConfigError, match="unknown provider"):
        llm.candidates("auto", {"models": {"providers": ["opnai"]}})


def test_list_skips_missing_keys_and_expands_auto(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    config = {"models": {"default": ["anthropic:x", "ollama:y", "auto", "openai:gpt-5"]}}
    assert llm.candidates(None, config) == ["ollama:y", "openai:gpt-5"]


def test_list_with_nothing_usable_is_an_error():
    with pytest.raises(llm.ModelConfigError):
        llm.candidates(["anthropic:x", "openai:y"], {})


def test_single_named_model_is_not_filtered():
    assert llm.candidates("anthropic:x", {}) == ["anthropic:x"]


def test_chain_appends_usable_fallbacks(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    chain = llm.model_chain("openai:a", ["anthropic:no-key", "ollama:b", "openai:a", "reasoning"], {})
    assert chain == ["openai:a", "ollama:b", "openai:gpt-5"]


def fake_models(monkeypatch, behaviours):
    made = []

    def make(name, config, level="default", **params):
        made.append((name, params))
        return ScriptedChatModel(responses=[behaviours[name]] * 3)  # enough for every retry

    monkeypatch.setattr(llm, "make_model", make)
    return made


async def test_engine_fails_over_and_keeps_params_with_their_provider(ws, both, monkeypatch):
    made = fake_models(monkeypatch, {"anthropic:claude-opus-5-5": ValueError("401 authentication"),
                                     "openai:gpt-5": "from openai"})
    path = ws.writable("agents", "assistant") / "agent.yaml"
    path.write_text(path.read_text().replace("model_params: {}", "model_params: {base_url: 'http://x'}"))
    engine = Engine(ws, "assistant")
    result = await engine.run("hi")
    engine.close()
    assert result.error is None and result.text == "from openai"
    assert made[0] == ("anthropic:claude-opus-5-5", {"base_url": "http://x", "output_config": {"effort": "low"}})
    assert made[1] == ("openai:gpt-5", {"reasoning_effort": "low"})


async def test_system_prompt_loses_cache_markers_on_other_providers(ws, both, monkeypatch):
    seen = ScriptedChatModel(responses=["ok"])
    monkeypatch.setattr(llm, "make_model", lambda *a, **k: seen)
    engine = Engine(ws, "assistant")
    await engine.refresh()
    system = SystemMessage(content=[{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}},
                                    {"type": "text", "text": "b"}])
    await engine._stream_once("openai:gpt-5", system)
    engine.close()
    assert seen._seen[0][0].content == "a\n\nb"


async def test_override_pins_one_model(ws, both, monkeypatch):
    made = fake_models(monkeypatch, {"openai:gpt-5": "pinned"})
    engine = Engine(ws, "assistant")
    engine.model_override = "openai:gpt-5"
    result = await engine.run("hi")
    engine.close()
    assert result.text == "pinned" and engine.model_chain == ["openai:gpt-5"]
    assert [m[0] for m in made] == ["openai:gpt-5"]


async def test_slash_commands_pin_and_save(ws, both):
    engine = Engine(ws, "assistant")
    logged = []
    host = commands.Host(ws=ws, engine=lambda: engine, switch_agent=None, send_to_agent=None,
                         log=logged.append, quit=None)
    await commands.dispatch(host, "/models priority openai anthropic")
    assert read_yaml(ws.root / "agh.yaml")["models"]["providers"] == ["openai", "anthropic"]
    await commands.dispatch(host, "/models priority nope")
    assert "unknown provider" in str(logged[-1])
    engine.close()


def test_saving_keeps_comments_outside_models_block(tmp_path, both):
    ws = Workspace.init(tmp_path / "w")
    commands.set_level_models(ws, "default", ["openai:gpt-5", "ollama:y"])
    commands.set_provider_priority(ws, ["openai", "anthropic"])
    commands.set_level_models(ws, "fast", ["openai:gpt-5-mini"])
    text = (ws.root / "agh.yaml").read_text(encoding="utf-8")
    assert "# how deep delegation may nest" in text
    saved = read_yaml(ws.root / "agh.yaml")
    assert saved["models"]["default"] == ["openai:gpt-5", "ollama:y"]
    assert saved["models"]["providers"] == ["openai", "anthropic"]
    assert saved["models"]["fast"] == "openai:gpt-5-mini"
    assert saved["agents"]["max_depth"] == 3
    assert saved == Workspace(ws.root).config


def test_saving_rejects_bad_input(tmp_path):
    ws = Workspace.init(tmp_path / "w")
    with pytest.raises(ValueError):
        commands.set_level_models(ws, "turbo", ["openai:x"])
    with pytest.raises(llm.ModelConfigError):
        commands.set_provider_priority(ws, ["nope"])


def test_cli_sets_priority_and_shows_order(tmp_path, both, capsys):
    ws = Workspace.init(tmp_path / "w")
    with pytest.raises(SystemExit) as done:
        main(["-w", str(ws.root), "models", "priority", "openai", "anthropic"])
    assert done.value.code == 0
    assert read_yaml(ws.root / "agh.yaml")["models"]["providers"] == ["openai", "anthropic"]
    assert "openai:gpt-5" in capsys.readouterr().out


def test_effort_params_per_provider():
    assert llm.effort_params("anthropic:claude-opus-5-5", None) == {}
    assert llm.DEFAULT_EFFORT == "low"
    assert llm.effort_params("anthropic:claude-opus-5-5", "high") == {"output_config": {"effort": "high"}}
    assert llm.effort_params("openai:gpt-5", "low") == {"reasoning_effort": "low"}
    assert llm.effort_params("azure_openai:dep", "medium") == {"reasoning_effort": "medium"}
    with pytest.raises(llm.ModelConfigError):
        llm.effort_params("openai:gpt-5", "max")


def test_effort_reaches_built_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    model = llm.make_model("openai:gpt-5", {}, **llm.effort_params("openai:gpt-5", "low"))
    assert model.reasoning_effort == "low"
