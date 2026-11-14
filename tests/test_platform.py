"""Connectors, builder, evals, self-improvement, and the TUI."""
import http.server
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agh import connectors, evals, improve, release, scaffold
from agh.agents import load_agent, load_skills
from agh.engine import Engine
from agh.hooks import fire, load_hooks
from agh.tooling import load_workspace_tools
from agh.llm import ScriptedChatModel
from agh.memory import MemoryStore
from agh.workspace import WorkspaceError, write_yaml


@pytest.fixture
def ws(ws):
    from conftest import with_toys

    return with_toys(ws)


def call(tool, /, **args):
    return {"tool_calls": [{"name": tool, "args": args}]}


async def yes(_):
    return "once"


@pytest.fixture
def api():
    """A real local HTTP server recording requests, with a flaky endpoint."""
    seen = []
    flaky = {"left": 2}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code, body):
            self.send_response(code)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_GET(self):
            seen.append(("GET", self.path, dict(self.headers)))
            if self.path.startswith("/flaky") and flaky["left"]:
                flaky["left"] -= 1
                return self._reply(503, {"e": 1})
            if self.path.startswith("/missing"):
                return self._reply(404, {"e": "nope"})
            self._reply(200, {"ok": True, "path": self.path})

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/oauth":
                seen.append(("POST", self.path, body.decode()))
                return self._reply(200, {"access_token": "tok-1", "expires_in": 3600})
            seen.append(("POST", self.path, json.loads(body)))
            self._reply(503 if self.path == "/fail" else 201, {"id": 1})

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()


def rest(base, **extra):
    return {"name": "svc", "type": "rest", "base_url": base, "retry": {"attempts": 3}, "operations": {
        "get_item": {"method": "GET", "path": "/items/{id}", "params": {
            "id": {"in": "path", "required": True}, "q": {"in": "query", "type": "string"}}},
        "flaky": {"method": "GET", "path": "/flaky"},
        "missing": {"method": "GET", "path": "/missing"},
        "make": {"method": "POST", "path": "/items", "params": {"body": {"in": "body", "type": "object"}}},
        "fail": {"method": "POST", "path": "/fail", "params": {"body": {"in": "body", "type": "object"}}},
    }, **extra}


def tools_by_name(conn):
    return {t.name: t for t in conn.tools()}


async def test_rest_connector_auth_path_encoding_and_query(api, monkeypatch):
    base, seen = api
    monkeypatch.setenv("SVC_TOKEN", "abc12345")
    conn = connectors.Connector("svc", rest(base, auth={"type": "bearer", "token_env": "SVC_TOKEN"}))
    out = await conn.call("get_item", {"id": "a/../b", "q": "x y"})
    assert json.loads(out)["ok"]
    method, path, headers = seen[0]
    assert path == "/items/a%2F..%2Fb?q=x+y"
    assert headers["Authorization"] == "Bearer abc12345"


async def test_read_retries_but_write_does_not(api):
    base, seen = api
    conn = connectors.Connector("svc", rest(base))
    assert json.loads(await conn.call("flaky", {}))["ok"]
    assert sum(s[1] == "/flaky" for s in seen) == 3
    with pytest.raises(connectors.ConnectorError):
        await conn.call("fail", {"body": {"a": 1}})
    assert sum(s[1] == "/fail" for s in seen) == 1


async def test_http_error_raises_with_status(api):
    conn = connectors.Connector("svc", rest(api[0]))
    with pytest.raises(connectors.ConnectorError, match="404"):
        await conn.call("missing", {})


async def test_missing_secret_is_clear(api):
    conn = connectors.Connector("svc", rest(api[0], auth={"type": "bearer", "token_env": "NOPE_NOT_SET"}))
    with pytest.raises(connectors.ConnectorError, match="NOPE_NOT_SET"):
        await conn.call("flaky", {})


async def test_oauth_client_credentials(api, monkeypatch):
    base, seen = api
    monkeypatch.setenv("CID", "id")
    monkeypatch.setenv("CSEC", "secret")
    connectors._tokens.clear()
    conn = connectors.Connector("svc2", rest(base, auth={
        "type": "oauth2_client_credentials", "token_url": f"{base}/oauth",
        "client_id_env": "CID", "client_secret_env": "CSEC"}))
    await conn.call("get_item", {"id": "1"})
    await conn.call("get_item", {"id": "2"})
    assert seen[-1][2]["Authorization"] == "Bearer tok-1"
    token_requests = [s for s in seen if s[1] == "/oauth"]
    assert len(token_requests) == 1  # cached
    assert "grant_type=client_credentials" in token_requests[0][2] and "client_secret=secret" in token_requests[0][2]


async def test_connector_tools_through_agent_with_permissions(ws, api):
    base, seen = api
    connectors.write_connector(ws, "svc", rest(base))
    path = ws.writable("agents", "researcher") / "agent.yaml"  # reads no files, so readonly calls run unattended
    path.write_text(path.read_text().replace("connectors: []", "connectors: [svc]"))
    engine = Engine(ws, "researcher", model=ScriptedChatModel(responses=[
        call("svc__get_item", id="7"), call("svc__make", body={"a": 1}), "done"]))
    result = await engine.run("go")
    engine.close()
    assert result.tool_calls[0]["error"] is None
    assert result.tool_calls[1]["error"].startswith("denied")  # POST needs approval; nobody to ask
    assert not any(s[0] == "POST" for s in seen)
    tool_msgs = [m for m in engine.session.messages if m.type == "tool"]
    assert tool_msgs[0].content.startswith('<untrusted_data source="svc__get_item">')


OPENAPI = {
    "openapi": "3.0.0", "info": {"title": "Pets"}, "servers": [{"url": "https://pets.example/v1"}],
    "components": {
        "securitySchemes": {"k": {"type": "apiKey", "in": "header", "name": "X-Key"}},
        "parameters": {"Limit": {"name": "limit", "in": "query", "schema": {"type": "integer"}}}},
    "paths": {"/pets/{id}": {
        "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
        "get": {"operationId": "getPet", "summary": "Get a pet", "parameters": [{"$ref": "#/components/parameters/Limit"}]},
        "delete": {"operationId": "deletePet"},
    }, "/pets": {"post": {"operationId": "addPet", "requestBody": {"required": True}}}},
}


def test_openapi_import(ws, tmp_path, monkeypatch):
    monkeypatch.setattr(scaffold, "_release_test", lambda *a, **k: None)  # the spec's server doesn't exist
    source = tmp_path / "spec.json"
    source.write_text(json.dumps(OPENAPI))
    scaffold.create_connector(ws, "pets", openapi=str(source))
    conn = connectors.load_connector(ws, "pets")
    assert conn.config["auth"] == {"type": "api_key", "in": "header", "name": "X-Key", "key_env": "PETS_API_KEY"}
    tools = tools_by_name(conn)
    assert tools["pets__getpet"].permission == "allow" and tools["pets__getpet"].readonly
    assert tools["pets__deletepet"].permission == "ask"
    assert conn.config["operations"]["getpet"]["params"]["limit"]["type"] == "integer"
    assert conn.config["operations"]["addpet"]["params"]["body"]["in"] == "body"


def test_openapi_include_filter(ws, tmp_path, monkeypatch):
    monkeypatch.setattr(scaffold, "_release_test", lambda *a, **k: None)  # the spec's server doesn't exist
    source = tmp_path / "spec.yaml"
    source.write_text(json.dumps(OPENAPI))
    scaffold.create_connector(ws, "pets", openapi=str(source), include="^get")
    assert list(connectors.load_connector(ws, "pets").config["operations"]) == ["getpet"]


MCP_SERVER = """
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("t")

@mcp.tool()
def shout(text: str) -> str:
    "Uppercase text."
    return text.upper()

mcp.run()
"""


async def test_mcp_connector_via_stdio(ws, tmp_path):
    script = tmp_path / "server.py"
    script.write_text(MCP_SERVER)
    connectors.write_connector(ws, "m", {"name": "m", "type": "mcp", "command": sys.executable,
                                         "args": [str(script)], "readonly_tools": ["shout"]})
    path = ws.writable("agents", "researcher") / "agent.yaml"  # reads no files, so readonly calls run unattended
    path.write_text(path.read_text().replace("connectors: []", "connectors: [m]"))
    engine = Engine(ws, "researcher", model=ScriptedChatModel(responses=[call("m__shout", text="hey"), "ok"]))
    await engine.run("go")
    engine.close()
    assert ws.load_errors.get("connector:m") is None
    tool_msg = next(m for m in engine.session.messages if m.type == "tool")
    assert "HEY" in tool_msg.content and tool_msg.content.startswith("<untrusted_data")


# ---- builder ---------------------------------------------------------------------------------

GOOD_TOOL = '''
def run(a: int, b: int = 2) -> int:
    """Multiply.

    Args:
        a: first.
        b: second.
    """
    return a * b
'''


GOOD_TESTS = [{"args": {"a": 3}, "expect": "6"}]


@pytest.fixture
def local_api():
    """A real HTTP server: /ok answers 200, anything else 404. Records each request."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200 if self.path == "/ok" else 404)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()


def rest_config(url, path="/ok", method="GET", **extra):
    return {"type": "rest", "base_url": url, "retry": {"attempts": 1},
            "operations": {"probe": {"method": method, "path": path}}, **extra}


async def test_agent_builds_tool_then_new_agent_uses_it(ws, local_api):
    model = ScriptedChatModel(responses=[
        call("create_tool", name="multiply", description="Multiply numbers", code=GOOD_TOOL, tests=GOOD_TESTS, readonly=True, permission="allow"),
        call("create_agent", name="mathbot", description="Does maths", prompt="You do maths.", tools=["multiply"], system_tools=[],
             evals=[{"id": "m", "input": "hi", "mock": ["6"], "expect": [{"type": "contains", "value": "6"}]}]),
        call("create_skill", name="mental-maths", description="Tricks", body="Double and halve."),
        call("create_connector", name="acme", config=rest_config(local_api[0])),
        call("create_hook", name="logger", description="x", events=["turn_end"], code="def handle(event):\n    return None\n"),
        call("create_eval", name="mathbot-suite", agent="mathbot", cases=[{"id": "c", "input": "i", "expect": [{"type": "contains", "value": "6"}]}]),
        "All built."])
    engine = Engine(ws, "architect", approver=yes, model=model)
    result = await engine.run("build it")
    engine.close()
    assert result.text == "All built."
    assert all(c["error"] is None for c in result.tool_calls), result.tool_calls
    for rel in ("tools/multiply/tool.py", "tools/multiply/tool.yaml", "agents/mathbot/prompt.md",
                "agents/mathbot/permissions.yaml", "agents/mathbot/memory/core.md", "skills/mental-maths/SKILL.md",
                "connectors/acme/connector.yaml", "hooks/logger/hook.py", "evals/mathbot-suite/suite.yaml"):
        assert (ws.overlay / rel).exists(), rel  # new elements land in the active profile (the draft on default)
    bot = Engine(ws, "mathbot", model=ScriptedChatModel(responses=[call("multiply", a=3), "6"]))
    out = await bot.run("3 times 2")
    bot.close()
    assert [m.content for m in bot.session.messages if m.type == "tool"] == ["6"]
    assert out.text == "6"


async def test_builder_requires_approval(ws):
    engine = Engine(ws, "architect", model=ScriptedChatModel(responses=[
        call("create_tool", name="x", description="d", code=GOOD_TOOL), "no"]))
    result = await engine.run("go")
    engine.close()
    assert result.tool_calls[0]["error"].startswith("denied") and not (ws.writable("tools", "x")).exists()


@pytest.mark.parametrize("code,match", [
    ("def run(:\n", "syntax"), ("x = 1\n", "must define"),
    ("import nope_nope\ndef run() -> str:\n    return ''\n", "failed to load")])
def test_bad_tool_never_goes_live(ws, code, match):
    with pytest.raises(WorkspaceError, match=match):
        scaffold.create_tool(ws, "bad", "d", code)
    assert not (ws.writable("tools", "bad")).exists()


@pytest.mark.parametrize("tests,match", [
    (None, "needs tests"),
    ([{"args": {"a": 3}}], "non-empty `expect`"),
    ([{"args": {"a": 3}, "expect": "7"}], "expected '7'"),
    ([{"args": {"wrong": 1}, "expect": "6"}], "raised ValueError"),
])
def test_tool_must_pass_its_tests(ws, tests, match):
    with pytest.raises(WorkspaceError, match=match):
        scaffold.create_tool(ws, "mul", "d", GOOD_TOOL, tests=tests)
    assert not (ws.writable("tools", "mul")).exists()


def test_failed_replacement_keeps_live_version(ws):
    live = scaffold.create_tool(ws, "mul", "d", GOOD_TOOL, tests=GOOD_TESTS)
    with pytest.raises(WorkspaceError, match="expected"):
        scaffold.create_tool(ws, "mul", "d", GOOD_TOOL, tests=[{"args": {"a": 1}, "expect": "nope"}], overwrite=True)
    assert (live / "tool.py").read_text() == GOOD_TOOL
    scaffold.create_skill(ws, "s", "d", "keep me")
    with pytest.raises(WorkspaceError, match="bad resource"):
        scaffold.create_skill(ws, "s", "d", "new", resources={"../x": "y"}, overwrite=True)
    assert "keep me" in (ws.writable("skills", "s") / "SKILL.md").read_text()


@pytest.mark.parametrize("code,events,match", [
    ("import nope_nope\ndef handle(event):\n    return None\n", ["turn_end"], "failed to load"),
    ("def handle(event):\n    raise ValueError('boom')\n", ["turn_end"], "boom"),
    ("def handle(event):\n    return 1\n", ["turn_end"], "dict or None"),
    ("def handle(event):\n    return event['nope']\n", ["pre_tool"], "KeyError"),
    ("def handle(event):\n    return None\n", ["typo"], "needs events"),
    ("def handle(event):\n    return None\n", [], "needs events"),
])
def test_bad_hook_never_goes_live(ws, code, events, match):
    with pytest.raises(WorkspaceError, match=match):
        scaffold.create_hook(ws, "bad", "d", events, code)
    assert not (ws.writable("hooks", "bad")).exists()


def test_connector_released_only_after_real_call(ws, local_api):
    url, seen = local_api
    scaffold.create_connector(ws, "good", rest_config(url))
    assert seen == ["/ok"] and (ws.writable("connectors", "good") / "connector.yaml").exists()


@pytest.mark.parametrize("config,match", [
    (lambda u: rest_config(u, "/missing"), "HTTP 404"),
    (lambda u: rest_config("http://127.0.0.1:1"), "test call probe failed"),
    (lambda u: rest_config(u, auth={"type": "bearer", "token_env": "AGH_NO_SUCH_TOKEN"}), "AGH_NO_SUCH_TOKEN"),
    (lambda u: rest_config(u, method="POST"), "no read operation"),
    (lambda u: rest_config(u, method="POST", test={"operation": "probe"}), "must be a read"),
    (lambda u: {"type": "mcp", "command": sys.executable, "args": ["-c", "raise SystemExit(1)"]}, "failed"),
])
def test_bad_connector_never_goes_live(ws, local_api, config, match):
    with pytest.raises(WorkspaceError, match=match):
        scaffold.create_connector(ws, "bad", config(local_api[0]))
    assert not (ws.writable("connectors", "bad") / "connector.yaml").exists()


def test_connector_needs_real_config(ws):
    with pytest.raises(WorkspaceError, match="untested"):
        scaffold.create_element(ws, "connector", "fresh")


def test_agent_and_eval_references_checked(ws):
    with pytest.raises(WorkspaceError, match="unknown skills"):
        scaffold.create_agent(ws, "a", "d", skills=["nope"])
    with pytest.raises(WorkspaceError, match="unknown subagents"):
        scaffold.create_agent(ws, "a", "d", subagents=["nope"])
    with pytest.raises(WorkspaceError, match="unknown agent"):
        scaffold.create_eval(ws, "e", "nope")
    with pytest.raises(WorkspaceError, match="at least one expect"):
        scaffold.create_eval(ws, "e", "assistant", [{"id": "c", "input": "i", "expect": []}])


EVAL_OK = [{"id": "m", "input": "hi", "mock": ["6"], "expect": [{"type": "contains", "value": "6"}]}]


def test_agent_needs_a_passing_eval(ws):
    with pytest.raises(WorkspaceError, match="needs evals"):
        scaffold.create_agent(ws, "bot", "d")
    bad = [{"id": "m", "input": "hi", "mock": ["5"], "expect": [{"type": "contains", "value": "6"}]}]
    with pytest.raises(WorkspaceError, match="eval cases failed"):
        scaffold.create_agent(ws, "bot", "d", evals=bad)
    assert "bot" not in ws.names("agents")
    assert scaffold.create_agent(ws, "bot", "d", evals=EVAL_OK).exists()
    assert (ws.writable("evals", "bot-release") / "suite.yaml").exists()


def test_production_refuses_mock_evals(ws, monkeypatch):
    monkeypatch.delenv("AGH_ALLOW_MOCK_EVALS")
    with pytest.raises(WorkspaceError, match="can't use `mock`"):
        scaffold.create_agent(ws, "bot", "d", evals=EVAL_OK)


def test_failed_agent_replacement_keeps_live_version(ws):
    live = scaffold.create_agent(ws, "bot", "d", prompt="original", evals=EVAL_OK)
    bad = [{"id": "m", "input": "hi", "mock": ["5"], "expect": [{"type": "contains", "value": "6"}]}]
    with pytest.raises(WorkspaceError):
        scaffold.create_agent(ws, "bot", "d", prompt="new", evals=bad, overwrite=True)
    assert "original" in (live / "prompt.md").read_text()


def tool_names(ws):
    return load_workspace_tools(ws)


def hand_write_tool(ws, name, tests):
    target = ws.writable("tools", name)
    target.mkdir()
    (target / "tool.py").write_text(GOOD_TOOL)
    write_yaml(target / "tool.yaml", {"name": name, "description": "d", "tests": tests})
    return target


def test_hand_written_tool_without_passing_tests_does_not_load(ws):
    hand_write_tool(ws, "sneaky", [])
    assert "sneaky" not in tool_names(ws) and "needs tests" in ws.load_errors["tool:sneaky"]


def test_hand_written_tool_with_passing_tests_loads(ws):
    hand_write_tool(ws, "ok_tool", GOOD_TESTS)
    assert "ok_tool" in tool_names(ws)


def test_edit_after_release_is_retested(ws):
    target = scaffold.create_tool(ws, "mul", "d", GOOD_TOOL, tests=GOOD_TESTS)
    assert "mul" in tool_names(ws)
    (target / "tool.py").write_text(GOOD_TOOL.replace("a * b", "a + b + 100"))
    assert "mul" not in tool_names(ws) and "expected '6'" in ws.load_errors["tool:mul"]


def test_edited_starter_tool_is_no_longer_trusted(ws):
    assert "calculator" in tool_names(ws)
    (ws.writable("tools", "calculator") / "tool.py").write_text(GOOD_TOOL)
    assert "calculator" not in tool_names(ws)


def test_unreleased_hook_and_skill_do_not_load(ws):
    hook = ws.writable("hooks", "sneaky")
    hook.mkdir()
    write_yaml(hook / "hook.yaml", {"events": ["turn_end"]})
    (hook / "hook.py").write_text("def handle(event):\n    raise ValueError('boom')\n")
    assert "sneaky" not in [h.name for h in load_hooks(ws)]
    skill = ws.writable("skills", "sneaky")
    skill.mkdir()
    (skill / "SKILL.md").write_text("no frontmatter")
    assert "sneaky" not in load_skills(ws, load_agent(ws, "assistant"))


async def test_refused_guard_hook_blocks_rather_than_vanishes(ws):
    (ws.writable("hooks", "block_dangerous_shell") / "hook.py").write_text("import nope_nope\ndef handle(event):\n    return None\n")
    hooks = load_hooks(ws)
    verdict = await fire(hooks, "pre_tool", {"tool": "shell", "args": {}, "agent": "a"})
    assert "block_dangerous_shell failed" in verdict["deny"]
    assert not (await fire(hooks, "pre_tool", {"tool": "read_file", "args": {}, "agent": "a"})).get("deny")


def test_connector_is_retried_after_failure(ws, local_api, monkeypatch):
    url, _ = local_api
    connectors.write_connector(ws, "later", rest_config(url, auth={"type": "bearer", "token_env": "AGH_LATER"}))
    assert not release.admit(ws, "connector", ws.writable("connectors", "later"))
    monkeypatch.setenv("AGH_LATER", "token")
    monkeypatch.setattr(release, "CONNECTOR_RETRY_S", 0)
    assert release.admit(ws, "connector", ws.writable("connectors", "later"))


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "tools/x/tool.py"}),
    ("edit_file", {"path": "./hooks/x/hook.py"}),
    ("write_file", {"path": "skills/x/SKILL.md"}),
    ("write_file", {"path": "connectors/x/connector.yaml"}),
    ("write_file", {"path": "agents/assistant/tools/x/tool.py"}),
    ("write_file", {"path": "state/releases.json"}),
    ("shell", {"command": "echo x > tools/y/tool.py"}),
    ("shell", {"command": "rm -rf hooks/guard"}),
    ("shell", {"command": "python -c \"open('state/releases.json','w')\""}),
])
def test_writes_to_released_elements_are_blocked(tmp_path, tool, args):
    assert release.blocked_write(tool, args, False, tmp_path)


@pytest.mark.parametrize("tool,args,readonly", [
    ("write_file", {"path": "notes/tools.md"}, False),
    ("write_file", {"path": "src/tools/a.py"}, False),
    ("shell", {"command": "ls tools/"}, False),
    ("shell", {"command": "echo hi > notes.txt"}, False),
    ("read_file", {"path": "tools/x/tool.py"}, True),
])
def test_normal_work_is_not_blocked(tmp_path, tool, args, readonly):
    assert release.blocked_write(tool, args, readonly, tmp_path) is None


async def test_engine_denies_blocked_write(ws):
    engine = Engine(ws, "assistant", approver=yes, model=ScriptedChatModel(responses=[
        call("write_file", path="tools/evil/tool.py", content="x"), "done"]))
    result = await engine.run("go")
    engine.close()
    assert "released element" in result.tool_calls[0]["error"] and not (ws.writable("tools", "evil")).exists()


async def test_build_stops_after_three_failures_then_resets_next_run(ws):
    bad = call("create_tool", name="mul", description="d", code=GOOD_TOOL, tests=[{"args": {"a": 1}, "expect": "nope"}])
    good = call("create_tool", name="mul", description="d", code=GOOD_TOOL, tests=GOOD_TESTS)
    engine = Engine(ws, "architect", approver=yes, model=ScriptedChatModel(responses=[bad, bad, bad, good, "gave up", good, "built"]))
    first = await engine.run("build")
    errors = [c["error"] for c in first.tool_calls]
    assert all("expected 'nope'" in e for e in errors[:3])
    assert "attempt limit reached" in errors[3] and "expected 'nope'" in errors[3]
    assert not (ws.writable("tools", "mul")).exists()
    engine.result = type(first)()
    second = await engine.run("again")
    engine.close()
    assert second.tool_calls[-1]["error"] is None and (ws.writable("tools", "mul")).exists()


def test_no_default_token_or_cost_budget():
    from agh.agents import DEFAULT_LIMITS
    assert "max_tokens" not in DEFAULT_LIMITS and "max_cost_usd" not in DEFAULT_LIMITS


def test_promote_refuses_broken_draft(ws):
    drafts = load_agent(ws, "assistant").path / "improvements" / "skill_drafts" / "broken"
    drafts.mkdir(parents=True)
    (drafts / "SKILL.md").write_text("no frontmatter")
    with pytest.raises(WorkspaceError):
        improve.promote_skill(ws, "assistant", "broken")
    assert not (ws.writable("skills", "broken")).exists()


@pytest.mark.parametrize("name", ["../evil", "UPPER", "a/b", "", "..", "x" * 60])
def test_names_cannot_traverse(ws, name):
    with pytest.raises(WorkspaceError):
        scaffold.create_skill(ws, name, "d")


def test_no_silent_overwrite_and_unknown_refs(ws):
    with pytest.raises(WorkspaceError, match="already exists"):
        scaffold.create_tool(ws, "calculator", "d", GOOD_TOOL, tests=GOOD_TESTS)
    with pytest.raises(WorkspaceError, match="unknown tools"):
        scaffold.create_agent(ws, "a", "d", tools=["nope"])
    with pytest.raises(WorkspaceError, match="bad resource"):
        scaffold.create_skill(ws, "s", "d", resources={"../../x": "y"})


@pytest.mark.parametrize("kind", ["agent", "skill", "tool", "hook", "eval"])
def test_template_for_each_kind(ws, kind, monkeypatch):
    if kind == "agent":  # its smoke eval needs a real model
        monkeypatch.setattr(scaffold, "_release_test", lambda *a, **k: None)
    assert scaffold.create_element(ws, kind, "fresh", "Something new").exists()


# ---- evals -----------------------------------------------------------------------------------

async def test_starter_offline_suite_passes(ws):
    result = await evals.run_suite(ws, "assistant-offline")
    assert result.passed, [(c.id, c.checks, c.error) for c in result.cases]
    assert not (ws.root / "eval_output.txt").exists()  # approve: deny blocks real writes
    assert result.path


def suite(ws, cases, **extra):
    scaffold.create_eval(ws, "s", "assistant", cases, overwrite=True)
    data = evals.load_suite(ws, "s")
    data.update(extra)
    write_yaml(ws.writable("evals", "s") / "suite.yaml", data)


async def test_graders(ws):
    (ws.dir("evals")).mkdir(exist_ok=True)
    suite(ws, [
        {"id": "fail-contains", "input": "x", "mock": ["hi"], "expect": [{"type": "contains", "value": "bye"}]},
        {"id": "regex", "input": "x", "mock": ["abc 123"], "expect": [{"type": "regex", "value": r"\d+"}]},
        {"id": "schema", "input": "x", "mock": ['{"a": 1}'], "expect": [
            {"type": "json_schema", "schema": {"type": "object", "required": ["a"]}}]},
        {"id": "judge", "input": "x", "mock": ["good"], "expect": [{"type": "llm_judge", "criteria": "be good"}]},
        {"id": "py", "input": "x", "mock": ["z"], "expect": [{"type": "python", "function": "check"}]},
        {"id": "bad-grader", "input": "x", "mock": ["z"], "expect": [{"type": "python", "function": "missing"}]},
        {"id": "partial", "input": "x", "mock": ["hi"], "expect": [
            {"type": "contains", "value": "hi"}, {"type": "contains", "value": "no"}]},
    ], threshold=0.5)
    (ws.writable("evals", "s") / "graders.py").write_text(
        "def check(text, result, case):\n    return text == 'z', 'is z'\n")
    judge = ScriptedChatModel(responses=['{"pass": true, "reason": "fine"}'])
    result = await evals.run_suite(ws, "s", judge=judge)
    by = {c.id: c for c in result.cases}
    assert not by["fail-contains"].passed and by["regex"].passed and by["schema"].passed
    assert by["judge"].passed and by["py"].passed and not by["bad-grader"].passed
    assert by["partial"].score == 0.5 and not by["partial"].passed
    assert result.passed


async def test_baseline_detects_regression(ws):
    suite(ws, [{"id": "a", "input": "x", "mock": ["yes"], "expect": [{"type": "contains", "value": "yes"}]}])
    evals.set_baseline(ws, "s", await evals.run_suite(ws, "s"))
    data = evals.load_suite(ws, "s")
    data["cases"][0]["mock"] = ["no"]
    write_yaml(ws.writable("evals", "s") / "suite.yaml", data)
    diff = evals.compare_baseline(ws, "s", await evals.run_suite(ws, "s"))
    assert diff["regressions"] == ["a"] and diff["delta"] == -1


# ---- self-improvement ------------------------------------------------------------------------

def prompt_sensitive(case, prompt):
    """Stand-in for a real model: answers well only if the prompt tells it to."""
    prompt = prompt or ""
    answers = {"polite": "Hello friend" if "greet" in prompt else "what",
               "short": "ok" if "BADRULE" not in prompt else "x" * 50}
    return ScriptedChatModel(responses=[answers[case["id"]]])


CASES = [
    {"id": "polite", "input": "hi", "expect": [{"type": "contains", "value": "hello"}]},
    {"id": "short", "input": "hi", "holdout": True, "expect": [{"type": "regex", "value": "^.{1,10}$"}]},
]


async def test_improvement_applies_only_with_gain_then_rolls_back(ws):
    suite(ws, CASES)
    original = (ws.writable("agents", "assistant") / "prompt.md").read_text()
    seen = []

    def propose(messages):
        seen.append(str(messages[-1].content))
        return "Always greet the user."

    out = await improve.improve_prompt(ws, "assistant", "s", proposer=ScriptedChatModel(responses=[propose]),
                                       factory=prompt_sensitive)
    assert out.accepted and out.best == 1.0 and out.baseline == 0.5
    assert "greet" in (ws.writable("agents", "assistant") / "prompt.md").read_text()
    assert "case polite" in seen[0]
    assert (ws.writable("agents", "assistant") / "versions" / "prompt.v1.md").read_text() == original
    assert improve.rollback(ws, "assistant") == "prompt.v1.md"
    assert (ws.writable("agents", "assistant") / "prompt.md").read_text() == original


async def test_improvement_rejects_regression_on_holdout(ws):
    suite(ws, CASES)
    before = (ws.writable("agents", "assistant") / "prompt.md").read_text()
    out = await improve.improve_prompt(
        ws, "assistant", "s", rounds=1, factory=prompt_sensitive,
        proposer=ScriptedChatModel(responses=["Always greet the user. BADRULE"]))
    assert not out.accepted and out.rounds[0]["regressions"] == ["short"]
    assert (ws.writable("agents", "assistant") / "prompt.md").read_text() == before


async def test_holdout_failures_are_hidden_from_proposer(ws):
    suite(ws, [{"id": "secret-holdout", "input": "hi", "mock": ["no"], "holdout": True,
                "expect": [{"type": "contains", "value": "yes"}]}])
    seen = []

    def propose(messages):
        seen.append(str(messages[-1].content))
        return "New prompt"

    await improve.improve_prompt(ws, "assistant", "s", rounds=1, proposer=ScriptedChatModel(responses=[propose]))
    assert "secret-holdout" not in seen[0]


async def test_dry_run_leaves_prompt(ws):
    suite(ws, CASES)
    before = (ws.writable("agents", "assistant") / "prompt.md").read_text()
    out = await improve.improve_prompt(ws, "assistant", "s", apply=False, factory=prompt_sensitive,
                                       proposer=ScriptedChatModel(responses=["Always greet"]))
    assert out.accepted and (ws.writable("agents", "assistant") / "prompt.md").read_text() == before


async def test_feedback_reaches_proposer(ws):
    suite(ws, CASES)
    improve.record_feedback(ws, "assistant", "bad", "too rude to users")
    seen = []

    def propose(messages):
        seen.append(str(messages[-1].content))
        return "x"

    await improve.improve_prompt(ws, "assistant", "s", rounds=1, factory=prompt_sensitive,
                                 proposer=ScriptedChatModel(responses=[propose]))
    assert "too rude" in seen[0]


async def test_skill_mining_is_inactive_until_promoted(ws):
    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=["done"]))
    await engine.run("deploy")
    engine.close()
    draft = await improve.mine_skill(ws, engine.session.id, model=ScriptedChatModel(
        responses=['{"name": "deploy-app", "description": "Deploy", "body": "1. ship"}']))
    assert "skill_drafts" in str(draft) and "deploy-app" not in ws.names("skills")
    improve.promote_skill(ws, "assistant", "deploy-app")
    assert "deploy-app" in ws.names("skills")


# ---- memory ----------------------------------------------------------------------------------

def test_memory_search_forget_dedupe_and_hostile_query(ws):
    store = MemoryStore(ws.memory_dir("assistant"))
    a = store.add("The deploy target is staging")
    assert store.add("The deploy target is staging") == a
    store.add("Lunch was pizza", kind="episodic")
    assert store.search("deploy")[0].id == a
    store.search('" OR 1=1; DROP TABLE items --')  # must not raise or be read as FTS syntax
    assert store.search("deploy")
    assert store.search("pizza", kind="semantic") == []
    assert store.forget(a) and store.search("deploy") == []
    store.close()


def test_memory_embedding_rerank(ws):
    class Fake:
        def embed_query(self, text):
            return [1.0, 0.0] if "cat" in text else [0.0, 1.0]

    store = MemoryStore(ws.memory_dir("assistant"), embedder=Fake())
    store.add("shared word about a cat")
    store.add("shared word about a dog")
    assert "cat" in store.search("shared cat")[0].text
    store.close()


# ---- TUI -------------------------------------------------------------------------------------

async def test_tui_chat_slash_approval_flow(ws):
    from agh.tui.app import AghApp, ApprovalScreen

    model = ScriptedChatModel(responses=["Hello **there**", call("write_file", path="t.txt", content="1"), "wrote it"])
    app = AghApp(ws, model=model)
    assert app.engine.mode == "edits"  # the TUI starts letting project file edits through; the rest asks
    app.engine.mode = "default"  # this test exercises the approval prompt
    async with app.run_test(size=(120, 40)) as pilot:
        async def say(text):
            app.query_one("#prompt").value = text
            await pilot.press("enter")
            await pilot.pause(0.2)

        await say("hi")
        await app._run_task
        await pilot.pause(0.2)
        assert app.engine.result.text == "Hello **there**"
        assert "ready" in str(app.query_one("#status").render())
        screen = app.export_screenshot().replace("&#160;", " ")
        assert "there" in screen and "**" not in screen  # markdown was rendered, not shown raw

        await say("write it")
        for _ in range(50):
            if isinstance(app.screen, ApprovalScreen):
                break
            await pilot.pause(0.05)
        assert isinstance(app.screen, ApprovalScreen)
        await pilot.press("y")
        await app._run_task
        assert (ws.root / "t.txt").read_text() == "1"

        await say("/help")
        await say("/ls")
        await say("/new skill tui-made A skill from the TUI")
        await pilot.pause(0.3)
        assert "tui-made" in ws.names("skills")
        await say("/remember tea is good")
        await say("/memory tea")
        await say("/mode plan")
        await pilot.pause(0.2)
        assert app.engine.mode == "plan"
        await say("/eval assistant-offline")
        await pilot.pause(1.0)
        await say("/bogus")
        await say("/agent nope")
        await say("/sessions")
        await say("/doctor")
        await pilot.pause(0.5)
        await pilot.press("ctrl+b")
        assert app.query_one("#sidebar").has_class("hidden")
        session = app.engine.session.id
        await say("/fork")
        await pilot.pause(0.3)
        assert app.engine.session.id != session
        await say("/agent researcher")
        await pilot.pause(0.3)
        assert app.engine.agent_name == "researcher"


def test_suggested_grants():
    from agh.policy import suggest_grant

    g = suggest_grant("shell", {"command": "git status --short"})
    assert g.matches("shell", {"command": "git status"}) and not g.matches("shell", {"command": "git push"})
    assert not g.matches("shell", {"command": "git status; rm -rf /"})
    assert suggest_grant("shell", {"command": "ls | wc"}).kind == "exact"
    w = suggest_grant("web_fetch", {"url": "https://Example.com/a?b"})
    assert w.matches("web_fetch", {"url": "https://example.com/other"})
    assert not w.matches("web_fetch", {"url": "https://example.com.evil.io/"})
    assert not w.matches("http_request", {"url": "https://example.com/"})
    assert suggest_grant("calculator", {"x": 1}).kind == "any"


STATEFUL_MCP_SERVER = """
import os
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("t")
count = 0

@mcp.tool()
def bump() -> str:
    "Count calls in this server process."
    global count
    count += 1
    return f"{count} pid {os.getpid()}"

mcp.run()
"""


async def test_mcp_server_keeps_state_across_turns_and_subagents(ws, tmp_path):
    script = tmp_path / "stateful.py"
    script.write_text(STATEFUL_MCP_SERVER)
    connectors.write_connector(ws, "s", {"name": "s", "type": "mcp", "command": sys.executable,
                                         "args": [str(script)], "readonly_tools": ["bump"]})
    path = ws.writable("agents", "researcher") / "agent.yaml"  # reads no files, so readonly calls run unattended
    path.write_text(path.read_text().replace("connectors: []", "connectors: [s]"))
    engine = Engine(ws, "researcher", model=ScriptedChatModel(responses=[
        call("s__bump"), "one", call("s__bump"), "two"]))
    await engine.run("first")
    await engine.run("second")
    outputs = [m.content for m in engine.session.messages if m.type == "tool"]
    assert "1 pid" in outputs[0] and "2 pid" in outputs[1]
    assert outputs[0].split("pid")[1].split()[0] == outputs[1].split("pid")[1].split()[0]
    assert engine.mcp.running() == ["s"]
    await engine.mcp.aclose()
    engine.close()
    assert engine.mcp.running() == []


async def test_mcp_server_restarts_when_its_config_changes(ws, tmp_path):
    script = tmp_path / "stateful.py"
    script.write_text(STATEFUL_MCP_SERVER)
    cfg = {"name": "s", "type": "mcp", "command": sys.executable, "args": [str(script)], "readonly_tools": ["bump"]}
    connectors.write_connector(ws, "s", cfg)
    pool = connectors.McpPool()
    try:
        first = await pool.tools(connectors.load_connector(ws, "s"))
        assert await pool.tools(connectors.load_connector(ws, "s")) is first
        connectors.write_connector(ws, "s", {**cfg, "timeout": 30}, overwrite=True)
        assert await pool.tools(connectors.load_connector(ws, "s")) is not first
    finally:
        await pool.aclose()


def test_allow_rules_are_strict_but_deny_rules_are_broad():
    from agh.policy import _matches

    assert _matches("shell(git status*)", "shell", {"command": "git status -s"}, strict=True)
    assert not _matches("shell(git status*)", "shell", {"command": "git status; rm -rf x"}, strict=True)
    assert not _matches("write_file(notes/*)", "write_file", {"path": "x.txt", "content": "notes/hi"}, strict=True)
    assert not _matches("write_file(notes/*)", "write_file", {"path": "notes/../x.txt", "content": ""}, strict=True)
    assert _matches("write_file(notes/*)", "write_file", {"path": "notes/a/b.md", "content": ""}, strict=True)
    assert _matches("shell(*rm -rf*)", "shell", {"command": "echo && rm -rf /"})  # deny side stays broad


def test_saved_rule_round_trips_and_keeps_comments(tmp_path):
    from agh.policy import _matches, grant_rule, save_allow_rule, suggest_grant

    perms = tmp_path / "permissions.yaml"
    perms.write_text("default: ask\n# keep me\nallow: [read_file]\nask: []\n")
    grant = suggest_grant("shell", {"command": "npm test --watch"})
    rule = grant_rule(grant)
    assert save_allow_rule(perms, rule) and not save_allow_rule(perms, rule)
    import yaml
    data = yaml.safe_load(perms.read_text())
    assert "# keep me" in perms.read_text() and data["allow"] == ["read_file", rule]
    assert _matches(rule, "shell", {"command": "npm test"}, strict=True)
    assert not _matches(rule, "shell", {"command": "npm publish"}, strict=True)
    host = grant_rule(suggest_grant("web_fetch", {"url": "https://docs.python.org/3/"}))
    assert _matches(host, "web_fetch", {"url": "https://docs.python.org/3/library"}, strict=True)
    assert not _matches(host, "web_fetch", {"url": "https://docs.python.org.evil.io/"}, strict=True)


def test_allowed_domains_block_other_hosts():
    from agh.policy import domain_block

    cfg = {"sandbox": {"allowed_domains": ["example.com"]}}
    assert domain_block({"url": "https://api.example.com/x"}, cfg) is None
    assert "not in sandbox.allowed_domains" in domain_block({"url": "https://example.com.evil.io/"}, cfg)
    assert domain_block({"url": "https://anything.io"}, {}) is None


async def test_save_rule_answer_writes_permissions(ws):
    asked = []

    async def save(req):
        asked.append(req["rule"])
        return "always"

    engine = Engine(ws, "assistant", approver=save, model=ScriptedChatModel(responses=[
        call("write_file", path="docs/a.md", content="1"), "ok"]))
    await engine.run("go")
    engine.close()
    assert asked == ["write_file(docs/*)"]
    assert "write_file(docs/*)" in (ws.writable("agents", "assistant") / "permissions.yaml").read_text()
    again = Engine(ws, "assistant", approver=None, model=ScriptedChatModel(responses=[
        call("write_file", path="docs/b.md", content="2"), "ok"]))
    await again.run("go")
    again.close()
    assert (ws.root / "docs" / "b.md").exists()  # no approver, so only the saved rule could allow it
