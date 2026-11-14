"""Real LangChain provider clients against local servers speaking each provider's streaming format.

No API key or network is needed. This proves streaming, tool-call parsing, usage accounting and
prompt-cache markers work through the actual client libraries, not just the scripted model.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agh.engine import Engine
from agh.workspace import write_yaml


@pytest.fixture
def ws(ws):
    from conftest import with_toys

    return with_toys(ws)


def serve(script):
    requests = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            events = script[min(len(requests) - 1, len(script) - 1)]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(events.encode())

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, requests


def openai_sse(*deltas, usage=None):
    lines = [{"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
              "choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
    if usage:
        lines.append({"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m", "choices": [],
                      "usage": usage})
    return "".join(f"data: {json.dumps(x)}\n\n" for x in lines) + "data: [DONE]\n\n"


def set_model(ws, model, **params):
    path = ws.writable("agents", "assistant") / "agent.yaml"
    path.write_text(path.read_text().replace("model: auto", f"model: {model}").replace(
        "model_params: {}", "model_params: " + json.dumps(params)))


async def test_openai_client_streams_tools_and_usage(ws, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    server, requests = serve([
        openai_sse({"role": "assistant", "content": None, "tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "calculator", "arguments": ""}}]},
                   {"tool_calls": [{"index": 0, "function": {"arguments": "{\"expression\":"}}]},
                   {"tool_calls": [{"index": 0, "function": {"arguments": " \"6*7\"}"}}]}, usage=usage),
        openai_sse({"role": "assistant", "content": "The answer "}, {"content": "is 42."}, usage=usage),
    ])
    set_model(ws, "openai:test-model", base_url=f"http://127.0.0.1:{server.server_port}/v1")
    engine = Engine(ws, "assistant")
    texts = [e.data["text"] async for e in engine.stream("6 times 7?") if e.kind == "text"]
    engine.close()
    server.shutdown()
    assert engine.result.error is None
    assert "".join(texts) == "The answer is 42."
    assert engine.result.tool_calls[0]["name"] == "calculator"
    assert engine.result.input_tokens == 20 and engine.result.output_tokens == 10
    body = requests[0][1]
    assert any(t["function"]["name"] == "calculator" for t in body["tools"])
    second = requests[1][1]["messages"]
    assert second[-1]["role"] == "tool" and second[-1]["content"] == "42"


def anthropic_sse(blocks, stop="end_turn"):
    def ev(kind, **d):
        return f"event: {kind}\ndata: {json.dumps({'type': kind, **d})}\n\n"
    out = ev("message_start", message={"id": "m", "type": "message", "role": "assistant", "content": [], "model": "x",
                                       "stop_reason": None, "stop_sequence": None,
                                       "usage": {"input_tokens": 10, "output_tokens": 1}})
    for i, (start, deltas) in enumerate(blocks):
        out += ev("content_block_start", index=i, content_block=start)
        for d in deltas:
            out += ev("content_block_delta", index=i, delta=d)
        out += ev("content_block_stop", index=i)
    out += ev("message_delta", delta={"stop_reason": stop, "stop_sequence": None}, usage={"output_tokens": 5})
    return out + ev("message_stop")


async def test_anthropic_client_streams_tools_and_marks_cache(ws, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-real")
    server, requests = serve([
        anthropic_sse([({"type": "text", "text": ""}, [{"type": "text_delta", "text": "Calculating."}]),
                       ({"type": "tool_use", "id": "toolu_1", "name": "calculator", "input": {}},
                        [{"type": "input_json_delta", "partial_json": "{\"expression\": \"6*7\"}"}])], stop="tool_use"),
        anthropic_sse([({"type": "text", "text": ""}, [{"type": "text_delta", "text": "It is 42."}])]),
    ])
    set_model(ws, "anthropic:claude-opus-5-5", base_url=f"http://127.0.0.1:{server.server_port}")
    engine = Engine(ws, "assistant")
    result = await engine.run("6 times 7?")
    engine.close()
    server.shutdown()
    assert result.error is None and result.text == "It is 42."
    assert result.tool_calls[0]["name"] == "calculator" and result.tool_calls[0]["error"] is None
    system = requests[0][1]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"} and "Date:" not in str(system)
    # The request-level marker asks Anthropic to cache the conversation up to its newest message.
    assert all(r[1]["cache_control"] == {"type": "ephemeral"} for r in requests)
    asked = requests[1][1]["messages"][0]["content"]
    assert "6 times 7?" in str(asked) and "Date:" in str(asked)
    assert requests[1][1]["system"] == system  # nothing in the prefix moved between steps


async def test_auth_failure_stops_quickly_with_clear_error(ws, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    class H(BaseHTTPRequestHandler):
        count = 0

        def log_message(self, *a):
            pass

        def do_POST(self):
            H.count += 1
            # Read the request first: closing a socket with unread data resets the connection on Windows, and
            # the client then sees a network error (which is retried) instead of the 401.
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "bad key"}}')

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    set_model(ws, "openai:test-model", base_url=f"http://127.0.0.1:{server.server_port}/v1", max_retries=0)
    engine = Engine(ws, "assistant")
    result = await engine.run("hi")
    engine.close()
    server.shutdown()
    assert result.stop_reason == "error" and "AuthenticationError" in result.error
    assert H.count == 1  # no pointless retries
