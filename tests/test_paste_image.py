"""Pasting an image from the clipboard into the prompt box."""
from pathlib import Path

from agh.commands import expand_mentions
from agh.llm import ScriptedChatModel
from agh.sdk import ToolContext
from agh.tui import clipboard_image
from agh.tui.app import AghApp
from agh.tui.prompt import PromptArea

PNG = clipboard_image.PNG_MAGIC + b"fake"


def test_image_mentions_tell_the_agent_to_view_them(ws):
    (ws.root / "shot.png").write_bytes(PNG)
    out, attached, problems = expand_mentions("what is in @shot.png", ToolContext(root=ws.root))
    assert attached == ["shot.png"] and not problems
    assert '<image path="shot.png">' in out and "view_image" in out


def test_save_rejects_a_clipboard_without_a_png(ws, monkeypatch):
    dest = ws.root / "state" / "pasted" / "x.png"

    def fake_run(cmd, dest, capture=False):
        dest.write_bytes(b"not a png")
        return True

    monkeypatch.setattr(clipboard_image, "_run", fake_run)
    monkeypatch.setattr(clipboard_image.shutil, "which", lambda name: name)
    assert clipboard_image.save_clipboard_image(dest) is False and not dest.exists()


async def test_ctrl_v_inserts_a_mention_when_the_clipboard_holds_an_image(ws, monkeypatch):
    def fake_save(dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(PNG)
        return True

    monkeypatch.setattr(clipboard_image, "save_clipboard_image", fake_save)
    app = AghApp(ws, model=ScriptedChatModel(responses=["ok"]))
    async with app.run_test() as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        await pilot.press("ctrl+v")
        mention = prompt.value.strip().removeprefix("@")
        assert "/state/pasted/image-" in mention and mention.endswith(".png")
        assert Path(mention).is_absolute() and Path(mention).read_bytes() == PNG
        # the agent's tools can open it even when the project folder is not the workspace
        assert app.engine.ctx().resolve(mention).read_bytes() == PNG


async def test_ctrl_v_without_an_image_leaves_the_prompt_alone(ws, monkeypatch):
    monkeypatch.setattr(clipboard_image, "save_clipboard_image", lambda dest: False)
    app = AghApp(ws, model=ScriptedChatModel(responses=["ok"]))
    async with app.run_test() as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        await pilot.press("ctrl+v")
        assert prompt.value == ""
