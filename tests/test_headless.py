"""agh run from stdin and as JSON lines, agh serve, triggers, traces and session management."""
import asyncio
import io
import json

import httpx
import pytest

from agh import cli
from agh.llm import ScriptedChatModel
from agh.serve import Server, parse_every
from agh.sessions import (delete_session, export_markdown, latest_session, list_sessions, read_trace,
                          rename_session, search_sessions, trace_lines)


@pytest.fixture
def scripted(monkeypatch):
    """Make every Engine use a scripted model."""
    from agh import engine as engine_mod

    real = engine_mod.Engine.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("model", None)
        kwargs["model"] = kwargs["model"] or ScriptedChatModel(responses=["hello from agh"] * 5)
        real(self, *args, **kwargs)

    monkeypatch.setattr(engine_mod.Engine, "__init__", init)


def test_run_reads_stdin_and_streams_json(ws, scripted, monkeypatch, capsys):
    monkeypatch.chdir(ws.root)
    monkeypatch.setattr("sys.stdin", io.StringIO("say hi\n"))
    assert cli._dispatch(cli._parser().parse_args(["run", "--stream-json"])) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert lines[-1]["kind"] == "result" and lines[-1]["text"] == "hello from agh"
    assert any(e["kind"] == "done" for e in lines[:-1])
    sid = lines[-1]["session"]
    assert latest_session(ws) == sid
    monkeypatch.setattr("sys.stdin", io.StringIO("again"))
    cli._dispatch(cli._parser().parse_args(["run", "--continue", "--stream-json"]))
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["session"] == sid


def test_session_management_and_trace(ws, scripted, monkeypatch, capsys):
    monkeypatch.chdir(ws.root)
    cli._dispatch(cli._parser().parse_args(["run", "find the purple elephant", "--json"]))
    sid = list_sessions(ws)[0]["id"]
    assert [s["id"] for s in search_sessions(ws, "purple elephant")] == [sid]
    assert search_sessions(ws, "zebra") == []
    rename_session(ws, sid, "Elephant hunt")
    assert list_sessions(ws)[0]["title"] == "Elephant hunt"
    md = export_markdown(ws, sid)
    assert md.startswith("# Elephant hunt") and "## User" in md and "hello from agh" in md
    assert any("done" in line for line in trace_lines(read_trace(ws, sid)))
    assert "json" in " ".join(delete_session(ws, sid))
    assert list_sessions(ws) == []


async def test_serve_runs_prompts_with_a_token(ws):
    server = Server(ws, "secret-token", model=ScriptedChatModel(responses=["served answer"]), log=lambda _: None)
    port = await server.start("127.0.0.1", 0)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            assert (await client.get("/health")).json() == {"ok": True}
            assert (await client.post("/v1/runs", json={"prompt": "hi"})).status_code == 401
            resp = await client.post("/v1/runs", json={"prompt": "hi"},
                                     headers={"Authorization": "Bearer secret-token"})
            events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
            assert events[-1]["kind"] == "result" and events[-1]["text"] == "served answer"
            sessions = (await client.get("/v1/sessions", headers={"Authorization": "Bearer secret-token"})).json()
            assert sessions["sessions"][0]["id"] == events[-1]["session"]
    finally:
        await server.close()


async def test_triggers_run_on_schedule(ws, monkeypatch):
    import agh.serve as serve_mod

    ws.config["triggers"] = [{"name": "tick", "every": "1s", "prompt": "status?"}]
    monkeypatch.setattr(serve_mod, "parse_every", lambda _: 0.05)
    server = Server(ws, "t", model=ScriptedChatModel(responses=["all good"] * 50), log=lambda _: None)
    await server.start("127.0.0.1", 0)
    out = ws.state / "triggers" / "tick.jsonl"
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            if out.exists() and out.read_text().strip():
                break
    finally:
        await server.close()
    record = json.loads(out.read_text().splitlines()[0])
    assert record["trigger"] == "tick" and record["text"] == "all good"


def test_parse_every():
    assert parse_every("30m") == 1800 and parse_every("2h") == 7200 and parse_every("1.5s") == 1.5
    with pytest.raises(Exception):
        parse_every("soon")
