"""Azure OpenAI: level resolution, and the real client against a local server."""
import pytest
from test_providers import openai_sse, serve, set_model

from agh import llm
from agh.engine import Engine


@pytest.fixture
def ws(ws):
    from conftest import with_toys

    return with_toys(ws)

VARS = {"AZURE_OPENAI_API_KEY": "azure-test-not-real", "AZURE_OPENAI_API_VERSION": "2025-04-01-preview",
        "AZURE_OPENAI_DEPLOYMENT_NAME_CHAT": "dep-chat", "AZURE_OPENAI_DEPLOYMENT_NAME_FAST": "dep-fast",
        "AZURE_OPENAI_DEPLOYMENT_NAME_REASONING": "dep-reason"}


@pytest.fixture
def azure(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/")
    for key, value in VARS.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_levels_map_to_deployments_and_beat_other_keys(azure):
    azure.setenv("OPENAI_API_KEY", "other")
    assert llm.resolve_name("auto", {}) == "azure_openai:dep-chat"
    assert llm.resolve_name(None, {}, level="fast") == "azure_openai:dep-fast"
    assert llm.resolve_name("reasoning", {}) == "azure_openai:dep-reason"
    assert llm.resolve_name("openai:explicit", {}) == "openai:explicit"
    assert llm.resolve_name("auto", {"models": {"default": "ollama:x"}}) == "ollama:x"


def test_missing_level_falls_back_to_chat(azure):
    azure.delenv("AZURE_OPENAI_DEPLOYMENT_NAME_REASONING")
    assert llm.resolve_name("reasoning", {}) == "azure_openai:dep-chat"


def test_incomplete_azure_config_is_ignored(azure):
    azure.delenv("AZURE_OPENAI_API_KEY")
    azure.delenv("OPENAI_API_KEY", raising=False)
    azure.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.ModelConfigError):
        llm.resolve_name("auto", {})


async def test_real_client_uses_deployment_url_version_and_key(ws, azure):
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    server, requests = serve([
        openai_sse({"role": "assistant", "content": None, "tool_calls": [{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "calculator", "arguments": '{"expression": "6*7"}'}}]}, usage=usage),
        openai_sse({"role": "assistant", "content": "It is 42."}, usage=usage),
    ])
    azure.setenv("AZURE_OPENAI_ENDPOINT", f"http://127.0.0.1:{server.server_port}/")
    set_model(ws, "reasoning", use_responses_api=False)
    engine = Engine(ws, "assistant")
    result = await engine.run("6 times 7?")
    engine.close()
    server.shutdown()
    assert result.error is None and result.text == "It is 42."
    assert result.tool_calls[0]["name"] == "calculator"
    assert result.input_tokens == 20  # usage arrives while streaming
    assert engine.model_name == "azure_openai:dep-reason"
    path = requests[0][0]
    assert path.startswith("/openai/deployments/dep-reason/chat/completions")
    assert "api-version=2025-04-01-preview" in path


def test_dotenv_beats_stale_environment_but_empty_does_not_clobber(tmp_path, monkeypatch):
    from agh.workspace import load_dotenv

    monkeypatch.setenv("AGH_T_KEY", "stale")
    monkeypatch.setenv("AGH_T_KEEP", "kept")
    env = tmp_path / ".env"
    env.write_text("AGH_T_KEY = fresh" + chr(10) + "AGH_T_KEEP=" + chr(10))
    load_dotenv(env)
    import os
    assert os.environ["AGH_T_KEY"] == "fresh" and os.environ["AGH_T_KEEP"] == "kept"
    monkeypatch.delenv("AGH_T_KEY")
