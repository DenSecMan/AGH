"""/provider and /model, live model listing, and the OpenAI-compatible providers on a real client."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agh import commands, llm
from agh.engine import Engine
from test_model_selection import no_keys  # noqa: F401  (autouse: clears provider variables)
from test_providers import openai_sse


@pytest.fixture
def server():
    """A server speaking the OpenAI API: lists models and streams one answer."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(("GET", self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"data": [{"id": "models/m-b"}, {"id": "m-a"}, {"id": "text-embedding-3"}]}).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(("POST", self.path, body["model"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(openai_sse({"role": "assistant", "content": "local answer"},
                                        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}).encode())

    http = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    http.seen = seen
    http.url = f"http://127.0.0.1:{http.server_port}/v1"
    yield http
    http.shutdown()


def host(ws, engine, logged):
    return commands.Host(ws=ws, engine=lambda: engine, switch_agent=None, send_to_agent=None,
                         log=logged.append, quit=None)


async def test_model_shows_nothing_without_a_provider(ws, server, monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", server.url)
    logged = []
    engine = Engine(ws, "assistant")
    await commands.dispatch(host(ws, engine, logged), "/model")
    engine.close()
    assert "No provider selected" in logged[-1] and server.seen == []


def test_listing_filters_non_chat_models_and_sends_key(server, monkeypatch):
    monkeypatch.setenv("OPENROUTER_BASE_URL", server.url)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert llm.list_models("openrouter") == ["m-a", "m-b"]
    assert server.seen[0][2] == "Bearer k"


def test_cloud_provider_without_key_cannot_be_selected(ws):
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        commands.provider_command(ws, ["gemini"])


def test_unreachable_server_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("LLMSTUDIO_BASE_URL", "http://127.0.0.1:9/v1")
    with pytest.raises(llm.ModelConfigError, match="could not list lmstudio"):
        llm.list_models("lmstudio")


def test_azure_lists_deployments_from_env(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME_CHAT", "chat")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME_FAST", "chat")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME_REASONING", "deep")
    assert llm.list_models("azure_openai") == ["chat", "deep"]


async def test_select_then_run_through_real_client(ws, server, monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", server.url)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    logged = []
    engine = Engine(ws, "assistant")
    h = host(ws, engine, logged)

    await commands.dispatch(h, "/provider ollama")
    result = await engine.run("hi")  # provider chosen, model not: refuses rather than using OpenAI
    assert "no model selected" in result.error

    await commands.dispatch(h, "/model not-listed")
    assert "not available" in str(logged[-1])

    await commands.dispatch(h, "/model 1")
    assert llm.selection(ws.config) == ("ollama", "m-a")
    assert engine.describe()["provider"] == "ollama" and engine.describe()["model"] == "m-a"

    engine.result = type(engine.result)()
    result = await engine.run("hi")
    engine.close()
    assert result.error is None and result.text == "local answer"
    assert ("POST", "/v1/chat/completions", "m-a") in server.seen
    now = engine.describe()
    assert now["level"] == "selected" and "first_token_s" in now and "tokens_per_s" in now


async def test_clearing_selection_restores_priorities(ws, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    commands.provider_command(ws, ["openai"])
    commands.provider_command(ws, ["auto"])
    assert llm.selection(ws.config) == (None, None)
    engine = Engine(ws, "assistant")
    assert engine.describe()["model"] == "gpt-5"
    engine.close()
