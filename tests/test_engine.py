"""The agent loop end to end, using a scripted model and the real starter workspace."""
import asyncio
import os

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agh.agents import load_agent
from agh.engine import Engine
from agh.llm import ScriptedChatModel
from agh.memory import MemoryStore
from agh.sessions import Session, repair_messages
from agh.workspace import write_yaml


@pytest.fixture
def ws(ws):
    from conftest import with_toys

    return with_toys(ws)


def call(tool, /, **args):
    return {"tool_calls": [{"name": tool, "args": args}]}


async def turn(engine, text="go"):
    try:
        return await engine.run(text)
    finally:
        engine.close()


async def yes(_):
    return "once"


async def no(_):
    return "deny"


async def test_text_streams_and_persists(ws):
    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=["Hello world"]))
    kinds = [e async for e in engine.stream("hi")]
    assert "".join(e.data["text"] for e in kinds if e.kind == "text") == "Hello world"
    assert kinds[-1].kind == "done"
    engine.close()
    loaded = Session.load(ws, engine.session.id)
    assert [m.type for m in loaded.messages] == ["human", "ai"]


async def test_trace_records_steps_not_streamed_chunks(ws):
    from agh.sessions import read_trace

    engine = Engine(ws, "assistant", model=ScriptedChatModel(
        responses=[call("calculator", expression="6*7"), "A long enough answer to arrive in several chunks."]))
    shown = [e.kind async for e in engine.stream("go")]
    engine.close()
    assert shown.count("text") > 1  # the screen still gets every chunk
    events = read_trace(ws, engine.session.id)
    kinds = [e["kind"] for e in events]
    assert not {"text", "thinking", "text_end"} & set(kinds)
    assert {"tool_start", "tool_end", "usage", "done"} <= set(kinds)
    assert events[-1]["text"] == "A long enough answer to arrive in several chunks."


async def test_tool_loop_runs_real_tool(ws):
    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=[call("calculator", expression="6*7"), "42"]))
    result = await turn(engine)
    assert result.text == "42" and result.steps == 2
    tool_msg = next(m for m in engine.session.messages if isinstance(m, ToolMessage))
    assert tool_msg.content == "42"


async def test_ask_tool_denied_without_approver(ws):
    engine = Engine(ws, "assistant", model=ScriptedChatModel(
        responses=[call("write_file", path="x.txt", content="y"), "ok"]))
    result = await turn(engine)
    assert result.tool_calls[0]["error"].startswith("denied")
    assert not (ws.root / "x.txt").exists()


async def test_ask_tool_runs_after_approval(ws):
    engine = Engine(ws, "assistant", approver=yes, model=ScriptedChatModel(
        responses=[call("write_file", path="x.txt", content="y"), "ok"]))
    await turn(engine)
    assert (ws.root / "x.txt").read_text() == "y"


async def test_session_grant_skips_second_prompt(ws):
    asked = []

    async def always(req):
        asked.append(req["tool"])
        return "session"

    engine = Engine(ws, "assistant", approver=always, model=ScriptedChatModel(responses=[
        call("write_file", path="a.txt", content="1"), call("write_file", path="b.txt", content="2"), "ok"]))
    await turn(engine)
    assert asked == ["write_file"] and (ws.root / "b.txt").exists()


async def test_plan_mode_blocks_writes_even_when_approved(ws):
    engine = Engine(ws, "assistant", approver=yes, mode="plan", model=ScriptedChatModel(
        responses=[call("write_file", path="x.txt", content="y"), "ok"]))
    result = await turn(engine)
    assert "plan mode" in result.tool_calls[0]["error"] and not (ws.root / "x.txt").exists()


async def test_sandbox_blocks_escape(ws, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("hidden")
    engine = Engine(ws, "assistant", model=ScriptedChatModel(
        responses=[call("read_file", path=str(outside)), "ok"]))
    result = await turn(engine)
    assert "outside sandbox" in result.tool_calls[0]["error"]


async def test_hook_blocks_dangerous_shell_even_in_auto_mode(ws):
    engine = Engine(ws, "assistant", mode="auto", model=ScriptedChatModel(
        responses=[call("shell", command="rm -rf /"), "ok"]))
    result = await turn(engine)
    assert "blocked dangerous" in result.tool_calls[0]["error"]


async def test_broken_pre_tool_hook_fails_closed(ws):
    hook = ws.writable("hooks", "block_dangerous_shell") / "hook.py"
    hook.write_text("def handle(event):\n    raise RuntimeError('boom')\n")
    engine = Engine(ws, "assistant", approver=yes, model=ScriptedChatModel(
        responses=[call("shell", command="echo hi"), "ok"]))
    result = await turn(engine)
    assert "hook block_dangerous_shell failed" in result.tool_calls[0]["error"]


async def test_tool_exception_goes_back_to_model(ws):
    engine = Engine(ws, "assistant", model=ScriptedChatModel(
        responses=[call("read_file", path="missing.txt"), "sorry"]))
    result = await turn(engine)
    assert result.text == "sorry" and "FileNotFoundError" in result.tool_calls[0]["error"]


async def test_unknown_tool_and_bad_args_are_recoverable(ws):
    engine = Engine(ws, "assistant", model=ScriptedChatModel(
        responses=[call("nope"), call("calculator", wrong=1), "ok"]))
    result = await turn(engine)
    assert "unknown tool" in result.tool_calls[0]["error"]
    assert "invalid arguments" in result.tool_calls[1]["error"]
    assert result.text == "ok"


async def test_max_steps_stops_runaway(ws):
    (ws.writable("agents", "assistant") / "agent.yaml").write_text(
        (ws.writable("agents", "assistant") / "agent.yaml").read_text().replace("max_steps: 0", "max_steps: 2"))
    engine = Engine(ws, "assistant", model=ScriptedChatModel(
        responses=[call("calculator", expression=f"{i}") for i in range(9)]))
    result = await turn(engine)
    assert result.stop_reason == "max_steps" and result.steps == 2


async def test_loop_detection_nudges_model(ws):
    model = ScriptedChatModel(responses=[call("calculator", expression="1")] * 3 + ["done"])
    engine = Engine(ws, "assistant", model=model)
    await turn(engine)
    assert any("repeated the same tool call" in str(m.content) for m in engine.session.messages)


async def test_retry_then_fallback_on_model_failure(ws):
    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=[RuntimeError("flaky"), "recovered"]))
    result = await turn(engine)
    assert result.text == "recovered"


async def test_secrets_are_redacted_from_tool_output_and_answer(ws, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "supersecretvalue123")
    (ws.root / "f.txt").write_text("token=supersecretvalue123")
    engine = Engine(ws, "assistant", model=ScriptedChatModel(
        responses=[call("read_file", path="f.txt"), "it is supersecretvalue123"]))
    result = await turn(engine)
    assert "supersecretvalue123" not in result.text
    assert all("supersecretvalue123" not in str(m.content) for m in engine.session.messages)


async def test_untrusted_tool_output_is_fenced(ws):
    from agh.tooling import ToolSpec

    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=[call("calculator", expression="1"), "ok"]))
    await engine.refresh()
    await engine._run_tool({"name": "calculator", "args": {"expression": "1"}, "id": "a"})
    engine.tools["calculator"].untrusted = True
    msg = await engine._run_tool({"name": "calculator", "args": {"expression": "1"}, "id": "b"})
    assert msg.content.startswith("<untrusted_data")
    engine.close()


async def test_oversized_output_spills_to_disk(ws):
    (ws.root / "big.txt").write_text("x" * 40000)
    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=[call("read_file", path="big.txt"), "ok"]))
    await turn(engine)
    tool_msg = next(m for m in engine.session.messages if isinstance(m, ToolMessage))
    assert "full output at state/outputs/" in tool_msg.content
    assert len(tool_msg.content) < 21000


async def test_parallel_read_only_calls_all_answered(ws):
    both = {"tool_calls": [{"name": "calculator", "args": {"expression": "1+1"}},
                           {"name": "calculator", "args": {"expression": "2+2"}}]}
    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=[both, "ok"]))
    await turn(engine)
    assert [m.content for m in engine.session.messages if isinstance(m, ToolMessage)] == ["2", "4"]


async def test_subagent_has_isolated_context_and_shared_budget(ws):
    model = ScriptedChatModel(responses=[call("delegate", agent="researcher", task="find x"), "sub says x", "final"])
    engine = Engine(ws, "assistant", model=model)
    result = await turn(engine)
    assert result.text == "final"
    assert "sub says x" in str([m.content for m in engine.session.messages if isinstance(m, ToolMessage)])
    sub_call_messages = model._seen[1]
    assert not any("go" == m.content for m in sub_call_messages if isinstance(m, HumanMessage))


async def test_delegate_to_unlisted_agent_refused(ws):
    engine = Engine(ws, "researcher", model=ScriptedChatModel(responses=["x"]))
    await engine.refresh()
    assert "cannot delegate" in await engine.run_subagent("architect", "t")
    engine.close()


async def test_skill_catalog_then_progressive_load(ws):
    model = ScriptedChatModel(responses=[call("load_skill", name="build-tool"), "ok"])
    engine = Engine(ws, "architect", model=model)
    await turn(engine)
    system_text = str(model._seen[0][0].content)
    assert "build-tool" in system_text and "A tool is a typed Python function" not in system_text
    assert any("A tool is a typed Python function" in str(m.content) for m in engine.session.messages)


async def test_memory_saved_recalled_and_fenced_next_session(ws):
    engine = Engine(ws, "assistant", model=ScriptedChatModel(
        responses=[call("remember", text="Favourite colour is teal"), "saved"]))
    await turn(engine)
    model = ScriptedChatModel(responses=["teal"])
    second = Engine(ws, "assistant", model=model)
    await turn(second, "what colour do I like, my favourite colour?")
    system, asked = (str(m.content) for m in model._seen[0][:2])
    assert "<recalled_memories>" in asked and "teal" in asked and "teal" not in system
    assert "teal" not in second.session.messages[0].content  # stored beside the user's words, not in them


async def test_turn_context_stays_put_so_the_conversation_cache_survives(ws):
    store = MemoryStore(ws.memory_dir("assistant"))
    store.add("Deploys go out on Fridays")
    store.close()
    model = ScriptedChatModel(responses=[call("update_todos", todos=[{"content": "a", "status": "pending"}]),
                                         "one", "two"])
    engine = Engine(ws, "assistant", model=model)
    await engine.run("when are deploys?")
    await engine.run("something unrelated entirely")
    engine.close()
    first, second, third = model._seen
    assert first[0].content == second[0].content == third[0].content  # system prompt: no plan, no recall
    assert first[1].content == third[1].content and "Fridays" in third[1].content  # turn one, unchanged in turn two
    assert "Session mode: default" in third[-1].content and "Fridays" not in third[-1].content
    reloaded = Session.load(ws, engine.session.id)
    assert "Fridays" in reloaded.messages[0].additional_kwargs["agh_context"]


async def test_automatic_run_notes_are_not_recalled_on_their_own(ws):
    model = ScriptedChatModel(responses=[call("calculator", expression="1"), "the deploy pipeline is fine",
                                         "second answer", call("recall", query="deploy pipeline"), "third"])
    engine = Engine(ws, "assistant", model=model)
    await engine.run("check the deploy pipeline for me")  # leaves a "User asked / Outcome" note
    assert [i.kind for i in engine.memory.all()] == ["episodic"]
    engine.memory.add("The deploy pipeline needs VPN access", "semantic")
    await engine.run("how is the deploy pipeline?")
    asked = str(model._seen[2][-1].content)
    assert "VPN access" in asked and "User asked" not in asked
    await engine.run("look it up")  # asking for it still finds the note
    engine.close()
    assert "User asked: check the deploy pipeline" in next(
        m.content for m in reversed(engine.session.messages) if isinstance(m, ToolMessage))


async def test_secret_split_across_streamed_chunks_never_reaches_the_screen(ws, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "supersecretvalue123")
    engine = Engine(ws, "assistant", model=ScriptedChatModel(
        responses=["Here it is: supersecretvalue123, and supersecret is only its start."]))
    chunks = [e.data["text"] async for e in engine.stream("show it") if e.kind == "text"]
    engine.close()
    assert len(chunks) > 1
    assert "".join(chunks) == "Here it is: [REDACTED], and supersecret is only its start."


def test_stream_redactor_holds_back_only_what_could_be_a_secret(monkeypatch):
    from agh.sessions import StreamRedactor

    monkeypatch.setenv("SOME_TOKEN", "abcdefgh1234")
    r = StreamRedactor()
    assert r.feed("hello ") == "hello "            # nothing secret-like: shown at once
    assert r.feed("x abcdef") == "x "              # could be the start of the token: held
    assert r.feed("gh1234 done") == "[REDACTED] done"
    assert r.feed("abc") == "" and r.flush() == "abc"   # the stream ended; it was not the token
    out = "".join(r.feed(c) for c in "say abcdefgh1234!") + r.flush()
    assert out == "say [REDACTED]!"


async def test_preloaded_skills_are_in_the_prompt_and_out_of_the_catalog(ws):
    engine = Engine(ws, "assistant", model=ScriptedChatModel())
    await engine.refresh()
    prompt = str(engine._system_prompt().content)
    engine.close()
    assert "## Skill: caveman (already loaded" in prompt and "Read every hunk" in prompt
    assert "- caveman:" not in prompt and "- repository-orientation:" in prompt


async def test_reflect_ignores_tool_output(ws):
    captured = []

    def reply(messages):
        captured.append(str(messages[-1].content))
        return '[{"text": "User likes tea", "kind": "semantic", "importance": 0.9}]'

    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=[call("calculator", expression="99999"), "done", reply]))
    await engine.run("I like tea")
    assert await engine.reflect() == ["User likes tea"]
    assert "99999\n" not in captured[0] and "I like tea" in captured[0]
    engine.close()


async def test_compaction_keeps_tool_pairs_valid_and_archives(ws):
    path = ws.writable("agents", "assistant") / "agent.yaml"
    path.write_text(path.read_text().replace("timeout_s: 0 ", "context_tokens: 1000\n  timeout_s: 0 "))
    session = Session(ws=ws, agent="assistant")
    for i in range(12):
        session.messages += [HumanMessage(content="q" * 500), AIMessage(content="", tool_calls=[
            {"name": "calculator", "args": {}, "id": f"c{i}"}]),
            ToolMessage(content="r" * 500, tool_call_id=f"c{i}", name="calculator"), AIMessage(content="a" * 500)]
    engine = Engine(ws, "assistant", session=session, model=ScriptedChatModel(responses=["SUMMARY", "final"]))
    await turn(engine)
    msgs = engine.session.messages
    assert "SUMMARY" in msgs[0].content
    ids = {c["id"] for m in msgs if isinstance(m, AIMessage) for c in m.tool_calls}
    assert {m.tool_call_id for m in msgs if isinstance(m, ToolMessage)} == ids
    assert session.path.with_suffix(".archive.jsonl").exists()


async def test_stale_tool_output_cleared_oldest_first_back_to_half_the_context_limit(ws):
    path = ws.writable("agents", "assistant") / "agent.yaml"
    path.write_text(path.read_text().replace("timeout_s: 0 ", "context_tokens: 10000\n  timeout_s: 0 "))

    def history(results):
        session = Session(ws=ws, agent="assistant")
        for i in range(results):
            session.messages += [AIMessage(content="", tool_calls=[{"name": "calculator", "args": {}, "id": f"c{i}"}]),
                                 ToolMessage(content="r" * 2400, tool_call_id=f"c{i}", name="calculator")]
        return Engine(ws, "assistant", session=session, model=ScriptedChatModel(responses=["final"]))

    engine = history(10)  # ~6,000 tokens: under the 70% point, so nothing the agent read is taken away yet
    assert not await engine.compact()
    engine = history(12)  # ~7,200 tokens: clear the oldest until ~5,000 remain
    assert await engine.compact()
    cleared = ["cleared" in m.content for m in engine.session.messages if isinstance(m, ToolMessage)]
    assert cleared == sorted(cleared, reverse=True) and sum(cleared) in (4, 5)  # the oldest, and only as many as needed
    assert not await engine.compact()  # settled: the next steps leave old messages, and the cache, alone


def test_context_limit_follows_the_model_unless_the_agent_sets_one(ws):
    engine = Engine(ws, "assistant", model=ScriptedChatModel())
    assert engine.context_limit() == 120_000
    engine.model_name = "anthropic:claude-opus-5-5"
    assert engine.context_limit() == 200_000
    engine.spec.limits["context_tokens"] = 50_000
    assert engine.context_limit() == 50_000


async def test_schema_guardrail_retries_until_valid(ws):
    write_yaml(ws.writable("agents", "assistant") / "guardrails.yaml", {"output_schema": {
        "type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}})
    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=["not json", '```json\n{"a": 1}\n```']))
    result = await turn(engine)
    assert result.error is None and result.steps == 2


async def test_block_pattern_withholds_answer(ws):
    write_yaml(ws.writable("agents", "assistant") / "guardrails.yaml", {"block_patterns": ["forbidden"]})
    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=["this is forbidden"]))
    result = await turn(engine)
    assert result.text == "[response withheld by guardrail]"
    assert engine.session.messages[-1].content == result.text


async def test_blocked_text_never_reaches_the_stream(ws):
    write_yaml(ws.writable("agents", "assistant") / "guardrails.yaml", {"block_patterns": ["forbidden"]})
    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=["this is forbidden"]))
    shown = "".join([e.data["text"] async for e in engine.stream("x") if e.kind == "text"])
    engine.close()
    assert shown == "[response withheld by guardrail]"


async def test_summary_model_falls_back_to_agent_model_without_keys(ws, monkeypatch):
    from agh import llm

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    made = []

    def fake_make(name, config, level="default", **kw):
        if not name:
            raise llm.ModelConfigError("no key")
        made.append(name)
        return "local-model"

    monkeypatch.setattr(llm, "make_model", fake_make)
    engine = Engine(ws, "assistant")
    engine.model_name = "ollama:x"
    assert engine._plain_model() == "local-model" and made == ["ollama:x"]
    engine.close()


async def test_cancel_leaves_resumable_session(ws):
    started = asyncio.Event()

    def slow(path: str = ""):
        started.set()
        import time
        time.sleep(0.3)
        return "x"

    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=[call("calculator", expression="1"), call("calculator", expression="2"), "end"]))

    async def consume():
        async for e in engine.stream("hi"):
            if e.kind == "tool_start":
                raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await consume()
    engine.close()
    loaded = Session.load(ws, engine.session.id)
    answered = {m.tool_call_id for m in loaded.messages if isinstance(m, ToolMessage)}
    assert {c["id"] for m in loaded.messages if isinstance(m, AIMessage) for c in m.tool_calls} <= answered


async def test_web_fetch_strips_scripts_and_styles(ws, monkeypatch):
    import httpx

    from agh.tooling import load_tool_dir

    tool = load_tool_dir(ws.writable("tools", "web_fetch"))
    page = "<html><style>body{color:red}</style><script>evil()</script><p>Visible</p></html>"
    response = httpx.Response(200, text=page, headers={"content-type": "text/html"},
                              request=httpx.Request("GET", "https://example.com"))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: response)
    text = tool.func(url="https://example.com")
    assert "Visible" in text and "color" not in text and "evil" not in text


def test_repair_answers_dangling_calls():
    fixed = repair_messages([AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "1"}])])
    assert isinstance(fixed[1], ToolMessage)


async def test_fork_is_independent(ws):
    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=["one"]))
    await turn(engine)
    fork = engine.session.fork()
    fork.messages.append(HumanMessage(content="more"))
    fork.save()
    assert len(Session.load(ws, engine.session.id).messages) == 2
    assert len(Session.load(ws, fork.id).messages) == 3


async def test_broken_tool_is_reported_not_fatal(ws):
    bad = ws.writable("tools", "broken")
    bad.mkdir()
    (bad / "tool.py").write_text("import not_a_real_module\ndef run(): pass\n")
    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=["fine"]))
    assert (await turn(engine)).text == "fine"
    assert "tool:broken" in ws.load_errors


async def test_session_grant_is_scoped_to_the_command_prefix(ws):
    asked = []

    async def always(req):
        asked.append(req["args"]["command"])
        return "session"

    engine = Engine(ws, "assistant", approver=always, model=ScriptedChatModel(responses=[
        call("shell", command="git status"), call("shell", command="git status --short"),
        call("shell", command="git log -1"), call("shell", command="git status && echo four"), "ok"]))
    await turn(engine)
    assert asked == ["git status", "git log -1", "git status && echo four"]


async def test_session_grant_for_a_file_covers_its_directory_only(ws):
    asked = []

    async def always(req):
        asked.append(req["args"]["path"])
        return "session"

    engine = Engine(ws, "assistant", approver=always, model=ScriptedChatModel(responses=[
        call("write_file", path="notes/a.txt", content="1"), call("write_file", path="notes/sub/b.txt", content="2"),
        call("write_file", path="other/c.txt", content="3"), call("write_file", path="notes/../d.txt", content="4"),
        "ok"]))
    await turn(engine)
    assert asked == ["notes/a.txt", "other/c.txt", "notes/../d.txt"]


def _alive(pid: int) -> bool:
    import subprocess
    import sys

    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _spawner(tmp_path):
    """A shell command whose grandchild sleeps for a minute and writes its pid first."""
    import sys

    pidfile = tmp_path / "child.pid"
    script = tmp_path / "sleeper.py"
    script.write_text(f"import os, time\nopen(r'{pidfile}', 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
    return f'"{sys.executable}" "{script}"', pidfile


async def test_shell_timeout_kills_the_process_tree(tmp_path):
    from agh.sdk import run_process

    command, pidfile = _spawner(tmp_path)
    result = await run_process(command, tmp_path, timeout=3)
    assert result.timed_out
    assert not _alive(int(pidfile.read_text()))


async def test_cancelling_a_shell_call_kills_the_process_tree(tmp_path):
    from agh.sdk import run_process

    command, pidfile = _spawner(tmp_path)
    task = asyncio.create_task(run_process(command, tmp_path, timeout=60))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _alive(int(pidfile.read_text()))


async def test_agents_md_reaches_the_prompt_except_for_the_researcher(ws):
    (ws.root / "AGENTS.md").write_text("Always answer in French.")
    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=["ok"]))
    await engine.refresh()
    assert "Always answer in French." in str(engine._system_prompt("hi").content)
    researcher = Engine(ws, "researcher", model=ScriptedChatModel(responses=["ok"]), use_memory=False)
    await researcher.refresh()
    assert "French" not in str(researcher._system_prompt("hi").content)


async def test_writing_agents_md_asks_every_time_despite_a_grant(ws):
    asked = []

    async def grant(req):
        asked.append(req["args"]["path"])
        return "session"

    engine = Engine(ws, "assistant", approver=grant, model=ScriptedChatModel(responses=[
        call("write_file", path="AGENTS.md", content="a"), call("write_file", path="AGENTS.md", content="b"), "ok"]))
    await turn(engine)
    assert asked == ["AGENTS.md", "AGENTS.md"]


async def test_approval_shows_a_diff_for_file_edits(ws):
    (ws.root / "notes.txt").write_text("alpha\nbeta\n")
    seen = []

    async def look(req):
        seen.append(req.get("diff"))
        return "once"

    engine = Engine(ws, "assistant", approver=look, model=ScriptedChatModel(responses=[
        call("edit_file", path="notes.txt", old="beta", new="gamma"),
        call("write_file", path="new.txt", content="hello\n"), "ok"]))
    await turn(engine)
    assert "-beta" in seen[0] and "+gamma" in seen[0] and "a/notes.txt" in seen[0]
    assert "/dev/null" in seen[1] and "+hello" in seen[1]


def test_diff_preview_covers_multi_edits_and_replace_all(ws):
    from agh.checkpoints import diff_preview

    target = ws.root / "notes.txt"
    target.write_text("a a\nbeta\n")
    several = diff_preview({"path": "notes.txt", "edits": [{"old": "beta", "new": "gamma"}, {"old": "a a", "new": "b"}]},
                           target, "notes.txt")
    assert "+gamma" in several and "+b" in several
    assert "+z z" in diff_preview({"path": "notes.txt", "old": "a", "new": "z", "replace_all": True}, target, "notes.txt")
    assert "-beta" in diff_preview({"path": "notes.txt", "old": "beta\n"}, target, "notes.txt")  # new defaults to ""
    # A call that will fail (two matches, no replace_all) has nothing to preview.
    assert diff_preview({"path": "notes.txt", "old": "a", "new": "z"}, target, "notes.txt") is None


async def test_edits_mode_lets_project_file_edits_through_and_asks_for_the_rest(ws):
    asked = []

    async def refuse(req):
        asked.append(req["tool"])
        return "deny"

    (ws.root / ".git").mkdir()
    engine = Engine(ws, "assistant", approver=refuse, mode="edits", model=ScriptedChatModel(responses=[
        call("write_file", path="made.txt", content="1"), call("edit_file", path="made.txt", old="1", new="2"),
        call("shell", command="echo hi"), call("http_request", method="GET", url="https://example.com"),
        call("write_file", path=".git/hooks/pre-commit", content="x"), "ok"]))
    result = await turn(engine)
    assert (ws.root / "made.txt").read_text() == "2"
    assert asked == ["shell", "http_request", "write_file"]  # a hook under .git is a program, so it still asks
    assert [bool(c["error"]) for c in result.tool_calls] == [False, False, True, True, True]


async def test_edits_mode_still_asks_before_changing_rule_files_and_obeys_deny_rules(ws):
    async def refuse(_):
        return "deny"

    rules = ws.writable("agents", "assistant") / "permissions.yaml"
    rules.write_text(rules.read_text().replace("deny: []", 'deny: ["write_file(secret*)"]'))
    engine = Engine(ws, "assistant", approver=refuse, mode="edits", model=ScriptedChatModel(responses=[
        call("write_file", path="AGENTS.md", content="new rules\n"), call("write_file", path="secret.txt", content="x"),
        "ok"]))
    result = await turn(engine)
    assert all(c["error"].startswith("denied") for c in result.tool_calls)
    assert not (ws.root / "AGENTS.md").exists() and not (ws.root / "secret.txt").exists()


async def test_oversized_output_keeps_its_start_and_end(ws):
    engine = Engine(ws, "assistant", model=ScriptedChatModel())
    await engine.refresh()
    text = "first line\n" + "x" * 50_000 + "\nFAILED the summary at the end"
    cut = engine._limit(text, "shell", "c1")
    engine.close()
    assert cut.startswith("first line") and cut.endswith("FAILED the summary at the end")
    assert "full output at state/outputs/" in cut and len(cut) < 21_000


async def test_prompt_tells_the_agent_where_it_runs_and_tools_carry_their_usage_notes(ws):
    import sys

    engine = Engine(ws, "assistant", model=ScriptedChatModel())
    await engine.refresh()
    prompt = str(engine._system_prompt().content)
    assert "## Environment" in prompt and ws.project.resolve().as_posix() in prompt
    assert ("cmd.exe" in prompt) == (sys.platform == "win32")
    assert prompt == str(engine._system_prompt().content)  # stable, so the provider's prompt cache keeps hitting
    # tool.yaml's one line and the docstring's guidance both reach the model.
    described = engine.tools["shell"].as_langchain().description
    assert described.startswith("Run a shell command") and "PowerShell" in described and "Args:" not in described
    assert "continue from" in engine.tools["read_file"].as_langchain().description
    assert engine.tools["list_dir"].description == "List a directory."  # the same sentence is not said twice
    engine.close()
    researcher = Engine(ws, "researcher", model=ScriptedChatModel())
    await researcher.refresh()
    assert "## Environment" not in str(researcher._system_prompt().content)  # it gets nothing private
    researcher.close()


async def test_undo_restores_edited_files_and_removes_created_ones(ws):
    from agh.commands import Host, dispatch

    (ws.root / "keep.txt").write_text("original")
    engine = Engine(ws, "assistant", approver=yes, model=ScriptedChatModel(responses=[
        call("write_file", path="keep.txt", content="changed"), call("write_file", path="keep.txt", content="again"),
        call("write_file", path="made.txt", content="new"), "ok"]))
    await turn(engine)
    assert (ws.root / "keep.txt").read_text() == "again"
    logs = []
    host = Host(ws=ws, engine=lambda: engine, switch_agent=None, send_to_agent=None, log=logs.append, quit=None)
    await dispatch(host, "/undo")
    assert (ws.root / "keep.txt").read_text() == "original" and not (ws.root / "made.txt").exists()
    assert "user undid" in engine.session.messages[-1].content
    await dispatch(host, "/undo")
    assert "Nothing to undo" in str(logs[-1])


async def test_rate_limits_wait_longer_and_honour_retry_after(ws, monkeypatch):
    from agh import engine as engine_mod

    class Limited(Exception):
        status_code = 429

        class response:
            status_code = 429
            headers = {"retry-after": "7"}

    waits = []

    async def fake_sleep(s):
        waits.append(s)

    eng = Engine(ws, "assistant", model=ScriptedChatModel(responses=[Limited("slow down")] * 4 + ["ok"]))
    eng.injected_model = None  # make the engine actually sleep (through the fake)
    monkeypatch.setattr(engine_mod.asyncio, "sleep", fake_sleep)
    calls = iter([Limited("x")] * 4 + [None])

    async def stream_once(name, system):
        exc = next(calls)
        if exc:
            raise exc
        return AIMessage(content="ok")

    eng._stream_once = stream_once
    eng.model_chain = ["anthropic:m"]
    msg = await eng._call_model(SystemMessage(content="s"))
    assert msg.content == "ok" and waits == [7.0] * 4


def test_context_estimate_uses_the_provider_count(ws):
    eng = Engine(ws, "assistant", model=ScriptedChatModel())
    eng.session.messages = [HumanMessage(content="x" * 400)]
    assert eng._estimate_tokens() == 100
    eng._context_tokens = (5000, 1)
    eng.session.messages.append(HumanMessage(content="y" * 40))
    assert eng._estimate_tokens() == 5010


async def test_follow_up_continues_the_subagent_conversation(ws):
    seen = []

    def reply(messages):
        seen.append([m.content.split("\n\n[harness]")[0] for m in messages if m.type == "human"])
        return "noted"

    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=[
        call("delegate", agent="researcher", task="remember the word kiwi"), reply, "done one",
        call("delegate", agent="researcher", task="what word?", conversation="c1"), reply, "done two"]))
    await engine.run("first")
    await engine.run("second")
    assert seen[1] == ["remember the word kiwi", "what word?"]
    assert "conversation c1" in next(m.content for m in engine.session.messages if m.type == "tool")


async def test_background_delegation_returns_a_job_to_collect(ws):
    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=["background answer"]))
    await engine.refresh()
    out = await engine.run_subagent("researcher", "dig", background=True)
    assert "job j1" in out
    result = await engine.job_result("j1", wait_s=10)
    assert "background answer" in result and "conversation c1" in result
    assert "no job" in await engine.job_result("j1")
