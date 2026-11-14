"""/reset restores the starter workspace without losing anything."""
import shutil

import pytest
from agh.tui.prompt import PromptArea as Input

from agh import commands, scaffold
from agh.llm import ScriptedChatModel
from agh.tui.app import AghApp
from agh.workspace import BACKUPS_DIR, CONFIG_NAME, STARTER, WorkspaceError


def _customize(ws):
    scaffold.create_element(ws, "skill", "mine", "custom")
    (ws.root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (ws.root / "notes.txt").write_text("mine", encoding="utf-8")
    (ws.root / CONFIG_NAME).write_text("default_agent: gone\n", encoding="utf-8")
    (ws.state / "sessions" / "old.json").write_text("{}", encoding="utf-8")


def test_reset_restores_defaults_and_backs_up(ws):
    _customize(ws)
    backup = ws.reset()
    assert (ws.root / CONFIG_NAME).read_text(encoding="utf-8") == (STARTER / CONFIG_NAME).read_text(encoding="utf-8")
    assert ws.config.get("default_agent") != "gone"
    assert "mine" not in ws.names("skills") and not (ws.state / "sessions" / "old.json").exists()
    assert (ws.state / "traces").is_dir()
    assert (backup / "profiles" / ".draft" / "skills" / "mine").is_dir()
    assert (backup / "state" / "sessions" / "old.json").exists()
    assert (ws.root / ".env").read_text(encoding="utf-8") == "SECRET=1\n"
    assert (ws.root / "notes.txt").exists()


def test_failed_reset_changes_nothing(ws, monkeypatch):
    _customize(ws)
    real_move, calls = shutil.move, []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 3:
            raise PermissionError("locked")
        return real_move(src, dst)

    monkeypatch.setattr(shutil, "move", flaky)
    with pytest.raises(WorkspaceError, match="nothing changed"):
        ws.reset()
    assert "mine" in ws.names("skills")
    assert (ws.root / CONFIG_NAME).read_text(encoding="utf-8") == "default_agent: gone\n"
    assert not any((ws.root / BACKUPS_DIR).iterdir())


async def _submit(app, pilot, line):
    app.query_one("#prompt", Input).value = line
    await pilot.press("enter")
    # The Explorer's directory loader is a worker that never finishes, so wait only for the command.
    # An empty list would mean "all workers", so only call it when a command worker exists.
    busy = [w for w in app.workers if w.group != "_loader"]
    if busy:
        await app.workers.wait_for_complete(busy)
    await pilot.pause()


async def test_reset_command_needs_confirm_then_works_with_open_memory(ws):
    scaffold.create_element(ws, "skill", "mine", "custom")
    app = AghApp(ws, model=ScriptedChatModel())
    async with app.run_test() as pilot:
        await app.engine.refresh()  # opens memory.db, which Windows won't let reset move
        app.engine.mode = "plan"
        await _submit(app, pilot, "/reset")
        assert "mine" in ws.names("skills")
        before = app.engine
        await _submit(app, pilot, "/reset confirm")
        assert "mine" not in ws.names("skills")
        assert app.engine is not before and app.engine.mode == "edits"
        await app.engine.refresh()  # the new engine works on the restored files


async def test_reset_refused_while_running(ws):
    scaffold.create_element(ws, "skill", "mine", "custom")
    logged = []
    host = commands.Host(ws=ws, engine=lambda: type("E", (), {"agent_name": "assistant"})(), switch_agent=None,
                         send_to_agent=None, log=logged.append, quit=None, busy=lambda: True)
    await commands.dispatch(host, "/reset confirm")
    assert "mine" in ws.names("skills") and "cancel" in str(logged[0])
