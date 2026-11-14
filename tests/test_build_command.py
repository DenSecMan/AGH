"""/build subcommands, deleting and restoring profile elements, the slash menu and sorted /help."""
from rich.text import Text

from agh import commands
from agh.llm import ScriptedChatModel
from agh.tui.app import AghApp
from agh.tui.prompt import PromptArea


class FakeEngine:
    agent_name = "assistant"


def make_host(ws, sent, logged):
    async def send(agent, text):
        sent.append((agent, text))

    async def switch(agent, session):
        pass

    return commands.Host(ws=ws, engine=lambda: FakeEngine(), switch_agent=switch, send_to_agent=send,
                         log=logged.append, quit=lambda: None)


def text_of(logged):
    return " ".join(str(x) for x in logged if isinstance(x, (Text, str)))


async def test_build_new_and_edit_send_precise_requests(ws):
    sent, logged = [], []
    host = make_host(ws, sent, logged)
    await commands.dispatch(host, "/build new agent PenTester You are an experienced principal pentester")
    assert sent[-1][0] == "architect"
    assert "new agent named pentester" in sent[-1][1] and "principal pentester" in sent[-1][1]
    await commands.dispatch(host, "/build amend agent reviewer prompt Prefer small diffs")
    assert "the prompt of the existing agent reviewer" in sent[-1][1] and "Prefer small diffs" in sent[-1][1]
    await commands.dispatch(host, "/build edit agent nobody prompt x")
    assert "no agent named 'nobody'" in text_of(logged)
    await commands.dispatch(host, "/build make me something nice")
    assert sent[-1] == ("architect", "make me something nice")


async def test_build_delete_confirms_hides_default_and_restores(ws):
    sent, logged = [], []
    host = make_host(ws, sent, logged)
    await commands.dispatch(host, "/build delete agent reviewer")
    assert "reviewer" in ws.names("agents") and "confirm" in text_of(logged)
    await commands.dispatch(host, "/build delete agent reviewer confirm")
    assert "reviewer" not in ws.names("agents")
    assert (ws.default_root / "agents" / "reviewer").is_dir()  # default is never written to
    await commands.dispatch(host, "/build restore agent reviewer")
    assert "reviewer" in ws.names("agents")
    await commands.dispatch(host, "/build delete agent architect confirm")
    await commands.dispatch(host, "/build delete agent assistant confirm")
    assert {"architect", "assistant"} <= set(ws.names("agents"))


async def test_build_delete_own_element_moves_it_to_backups(ws):
    from agh import scaffold

    scaffold.create_skill(ws, "notes", "Take notes", "Write it down.")
    host = make_host(ws, [], [])
    await commands.dispatch(host, "/build delete skill notes confirm")
    assert "notes" not in ws.names("skills") and not (ws.overlay / "skills" / "notes").exists()


def test_build_completion_walks_each_word(ws):
    assert commands.complete("/bu", [], ws)[0] == "/build "
    assert [s[1] for s in commands.suggestions("/build ", ws)] == sorted(commands.BUILD_ACTIONS)
    assert [s[1] for s in commands.suggestions("/build edit ", ws)] == list(commands.BUILD_KINDS)
    assert "reviewer" in [s[1] for s in commands.suggestions("/build edit agent ", ws)]
    assert "prompt" in [s[1] for s in commands.suggestions("/build edit agent reviewer pr", ws)]
    assert commands.complete("/build delete agent rev", [], ws)[0] == "/build delete agent reviewer "


def test_help_and_menu_are_alphabetical(ws):
    keys = [pair[0].lower() for pair in commands.HELP]
    assert keys == sorted(keys)
    words = [s[1] for s in commands.suggestions("/", ws)]
    assert words == sorted(words) and "/build" in words and "/help" in words


async def test_explorer_shows_the_directory_agh_started_in(ws, tmp_path):
    from textual.widgets import DirectoryTree

    start = tmp_path / "launched-here"
    start.mkdir()
    ws.project = start.resolve()  # cli sets this to the folder agh was started in
    app = AghApp(ws, model=ScriptedChatModel())
    async with app.run_test(size=(160, 45)) as pilot:
        tree = app.query_one("#explorer", DirectoryTree)
        assert tree.path == start.resolve()
        await pilot.pause()
        (start / "AGENTS.md").write_text("# new\n")
        app._run_command("/help")  # any command refreshes the Explorer
        for _ in range(50):
            await pilot.pause(0.05)
            if any(str(n.data.path).endswith("AGENTS.md") for n in tree.root.children):
                break
        assert any(str(n.data.path).endswith("AGENTS.md") for n in tree.root.children)


async def test_slash_menu_opens_navigates_and_drills_into_build(ws):
    app = AghApp(ws, model=ScriptedChatModel())
    async with app.run_test(size=(160, 45)) as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        menu = app.query_one("#suggest")
        await pilot.press("/")
        await pilot.pause()
        assert menu.display and menu.option_count == len(commands.COMMAND_WORDS)
        await pilot.press("b", "u", "i")
        await pilot.pause()
        assert menu.option_count == 1
        await pilot.press("enter")  # /build has options, so Enter opens them instead of sending
        await pilot.pause()
        assert prompt.value == "/build " and menu.display and menu.option_count == len(commands.BUILD_ACTIONS)
        await pilot.press("down", "down")  # delete, edit
        await pilot.press("enter")
        await pilot.pause()
        assert prompt.value == "/build edit "
        await pilot.press("escape")
        await pilot.pause()
        assert not menu.display
