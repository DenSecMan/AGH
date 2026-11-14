"""Tab completes slash commands in the prompt."""
from agh.tui.prompt import PromptArea as Input

from agh.commands import complete
from agh.llm import ScriptedChatModel
from agh.tui.app import AghApp


def test_single_match_completes_with_a_space():
    assert complete("/prov", []) == ("/provider ", [])


def test_no_match_or_plain_text_is_unchanged():
    assert complete("/zzz", []) == ("/zzz", [])
    assert complete("hello", []) == ("hello", [])
    assert complete("/model x", []) == ("/model x", [])


def test_shared_prefix_then_cycle():
    text, cycle = complete("/mo", [])
    assert text == "/mode" and cycle == ["/mode", "/model", "/models"]
    text, cycle = complete(text, cycle)
    assert text == "/model"
    text, cycle = complete(text, cycle)
    assert text == "/models"
    assert complete(text, cycle)[0] == "/mode"


async def test_tab_in_the_app(ws):
    app = AghApp(ws, model=ScriptedChatModel())
    async with app.run_test() as pilot:
        prompt = app.query_one("#prompt", Input)
        await pilot.press("/", "p", "r", "o", "v", "tab")
        assert prompt.value == "/provider "
        assert prompt.cursor_position == len(prompt.value) and app.focused is prompt
        prompt.value = "hello"
        await pilot.press("tab")
        assert app.focused is not prompt  # outside commands, Tab still moves focus


def test_argument_completion(ws):
    assert complete("/agent ass", [], ws) == ("/agent assistant ", [])
    assert complete("/key set anth", [], ws) == ("/key set anthropic ", [])
    text, cycle = complete("/agent ", [], ws)
    assert text in ("/agent architect", "/agent a") or cycle


def test_mentions_attach_files_and_complete(ws):
    from agh.commands import complete_mention, expand_mentions
    from agh.sdk import ToolContext

    (ws.root / "notes").mkdir()
    (ws.root / "notes" / "plan.md").write_text("step one")
    out, attached, problems = expand_mentions("summarise @notes/plan.md and email @bob", ToolContext(root=ws.root))
    assert attached == ["notes/plan.md"] and '<file path="notes/plan.md">\nstep one' in out and not problems
    assert complete_mention("look at @not", [], ws.root) == ("look at @notes/", [])
    assert complete_mention("look at @notes/pl", [], ws.root) == ("look at @notes/plan.md ", [])


async def test_multiline_history_and_queue(ws):
    app = AghApp(ws, model=ScriptedChatModel(responses=["first answer", "second answer"]))
    async with app.run_test() as pilot:
        prompt = app.query_one("#prompt", Input)
        await pilot.press("h", "i", "ctrl+j", "x")
        assert prompt.value == "hi\nx"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert prompt.value == ""
        await pilot.press("up")
        assert prompt.value == "hi\nx"
        await pilot.press("down")
        assert prompt.value == ""


async def test_messages_typed_during_a_run_are_queued_and_sent_after(ws):
    app = AghApp(ws, model=ScriptedChatModel(responses=["one", "two"]))
    async with app.run_test() as pilot:
        app._agent_busy = True  # as if a run were in progress
        app.query_one("#prompt", Input).value = "later please"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert app._pending == ["later please"]
        app._agent_busy = False
        app._start_run("now")
        for _ in range(40):
            await pilot.pause(0.1)
            if not app._busy() and not app._pending:
                break
        humans = [m.content for m in app.engine.session.messages if m.type == "human"]
        assert humans == ["now", "later please"]
