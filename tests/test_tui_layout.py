"""Explorer, middle editor pane and Claude Code style tool blocks in the chat column."""
from agh import commands
from agh.engine import Event
from agh.llm import ScriptedChatModel
from agh.tui.app import AghApp, ConfirmCloseScreen, Editor


async def test_explorer_file_opens_in_editor_and_saves(ws):
    path = ws.root / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    app = AghApp(ws, model=ScriptedChatModel())
    async with app.run_test(size=(160, 45)) as pilot:
        app._open_in_editor(path)
        await pilot.pause()
        editor = app.query_one("#editor", Editor)
        assert editor.path == path and editor.text_area.text == "hello" and not editor.dirty
        editor.text_area.text = "hello world"
        await pilot.pause()
        assert editor.dirty
        app.action_save_file()
        assert path.read_text(encoding="utf-8") == "hello world" and not editor.dirty


async def test_closing_edited_file_asks_and_discard_keeps_disk(ws):
    path = ws.root / "notes.txt"
    path.write_text("keep", encoding="utf-8")
    app = AghApp(ws, model=ScriptedChatModel())
    async with app.run_test(size=(160, 45)) as pilot:
        app._open_in_editor(path)
        await pilot.pause()
        editor = app.query_one("#editor", Editor)
        editor.text_area.text = "changed"
        await pilot.pause()
        app.action_close_file()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmCloseScreen)
        await pilot.press("c")  # cancel keeps the file open
        await pilot.pause()
        assert editor.path == path and editor.dirty
        app.action_close_file()
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert editor.path is None and path.read_text(encoding="utf-8") == "keep"


async def test_tool_block_shows_input_then_output(ws):
    app = AghApp(ws, model=ScriptedChatModel())
    async with app.run_test(size=(160, 45)) as pilot:
        agent = app.engine.agent_name
        app._handle(Event("tool_start", agent, {"tool": "shell", "id": "1",
                                                "args": {"command": "ls", "description": "List files"}}))
        await pilot.pause()
        block, head, out, _ = app._tool_rows["1"]
        assert "List files" in str(head.render()) and not out.display
        app._handle(Event("tool_end", agent, {"tool": "shell", "id": "1", "error": None,
                                              "output": "a.txt\nb.txt", "duration": 0.1}))
        await pilot.pause()
        assert "a.txt" in str(out.render())
        assert not out.display  # compact by default: one line per call
        app.action_toggle_details()
        await pilot.pause()
        assert out.display


async def test_compact_tool_block_still_shows_errors(ws):
    app = AghApp(ws, model=ScriptedChatModel())
    async with app.run_test(size=(160, 45)) as pilot:
        agent = app.engine.agent_name
        app._handle(Event("tool_start", agent, {"tool": "edit_file", "id": "1", "args": {"path": "README.md"}}))
        app._handle(Event("tool_end", agent, {"tool": "edit_file", "id": "1", "error": "no match",
                                              "output": None, "duration": 0.0}))
        await pilot.pause()
        blocks = list(app.query(".tool"))
        assert "README.md" in str(blocks[-1].children[0].render())
        assert blocks[-1].children[2].display and not blocks[-1].children[1].display


async def test_transcript_follows_new_output_until_user_scrolls_up(ws):
    app = AghApp(ws, model=ScriptedChatModel())
    async with app.run_test(size=(160, 45)) as pilot:
        agent = app.engine.agent_name
        transcript = app.query_one("#transcript")
        for i in range(40):
            app._handle(Event("tool_start", agent, {"tool": "shell", "id": str(i), "args": {"command": f"c{i}"}}))
            app._handle(Event("tool_end", agent, {"tool": "shell", "id": str(i), "error": None,
                                                  "output": "x\n" * 20, "duration": 0.1}))
            await pilot.pause()
        assert transcript.max_scroll_y > 0 and transcript.scroll_y == transcript.max_scroll_y
        app.action_toggle_details()  # outputs expand: view must still follow
        await pilot.pause()
        await pilot.pause()
        assert transcript.scroll_y == transcript.max_scroll_y


def regions(app):
    """Each visible pane's (x, y) on screen, to check what sits left of / above what."""
    return {name: app._pane(name).region for name in app.PANE_IDS}


async def test_default_stacks_explorer_under_profile_and_splitter_resizes(ws):
    app = AghApp(ws, model=ScriptedChatModel())
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        r = regions(app)
        assert r["profile"].x == r["explorer"].x and r["explorer"].y > r["profile"].y
        assert r["profile"].x < r["editor"].x < r["chat"].x
        before = r["profile"].width
        await pilot.mouse_down("#split-1")
        await pilot.hover("#split-1", offset=(11, 5))
        await pilot.mouse_up("#split-1", offset=(11, 5))
        await pilot.pause()
        assert app._pane("profile").region.width > before
        assert app._pane("explorer").region.width == app._pane("profile").region.width
        app.action_move_pane()
        await pilot.pause()
        r = regions(app)
        assert r["chat"].x < r["editor"].x
        app.action_reset_layout()
        await pilot.pause()
        assert app._layout == [["profile", "explorer"], ["editor"], ["chat"]]
        app.action_toggle_sidebar()
        await pilot.pause()
        assert not app._pane("profile").display and app._pane("editor").region.x < 3


async def test_layout_and_theme_commands_apply_and_persist(ws):
    from agh import ui_prefs

    app = AghApp(ws, model=ScriptedChatModel())
    async with app.run_test(size=(160, 45)) as pilot:
        await commands.dispatch(app.host, "/layout chat left")
        await commands.dispatch(app.host, "/layout explorer right")
        await commands.dispatch(app.host, "/theme nord")
        await pilot.pause()
        r = regions(app)
        assert r["chat"].x < r["profile"].x < r["editor"].x < r["explorer"].x
        assert app.theme == "nord"
        await commands.dispatch(app.host, "/layout explorer below chat")
        await pilot.pause()
        r = regions(app)
        assert r["explorer"].x == r["chat"].x and r["explorer"].y > r["chat"].y
    saved = ui_prefs.load(ws.state)
    assert saved == {"theme": "nord", "layout": [["chat", "explorer"], ["profile"], ["editor"]]}
    app = AghApp(ws, model=ScriptedChatModel())
    async with app.run_test(size=(160, 45)) as pilot:  # restored on the next start
        assert app.theme == "nord" and app._layout == [["chat", "explorer"], ["profile"], ["editor"]]


async def test_agent_ui_event_moves_panes(ws):
    app = AghApp(ws, model=ScriptedChatModel())
    async with app.run_test(size=(160, 45)) as pilot:
        app._handle(Event("ui", app.engine.agent_name, {"theme": "dracula",
                                                        "layout": [["editor"], ["chat"], ["explorer", "profile"]]}))
        await pilot.pause()
        assert app.theme == "dracula" and app._layout == [["editor"], ["chat"], ["explorer", "profile"]]


def test_markdown_message_copies_its_source():
    from rich.markdown import Markdown
    from agh.tui.app import Message

    msg = Message(Markdown("**bold** text"))
    assert msg.get_selection(None) == ("**bold** text", "\n")
    msg.update("plain")
    assert msg.source is None
