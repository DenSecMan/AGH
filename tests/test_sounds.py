"""Notification sounds: a chime when a run finishes, a brief beep when AGH needs the user."""
from agh import sounds
from agh.tui import app as tui_app
from agh.tui.app import AghApp

from agh.llm import ScriptedChatModel


def test_sounds_are_silent_under_pytest_and_when_disabled(monkeypatch):
    assert not sounds.enabled()  # PYTEST_CURRENT_TEST is set
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    assert sounds.enabled()
    monkeypatch.setenv("AGH_SOUND", "0")
    assert not sounds.enabled()


def test_done_and_attention_play_different_tones(monkeypatch):
    played = []
    monkeypatch.setattr(sounds, "enabled", lambda: True)
    monkeypatch.setattr(sounds.threading, "Thread", lambda target, args, daemon: type(
        "T", (), {"start": lambda self: played.append(args[0])})())
    sounds.done()
    sounds.attention()
    assert played == [sounds.DONE_TONES, sounds.ATTENTION_TONES]
    assert len(sounds.DONE_TONES) > len(sounds.ATTENTION_TONES)


async def test_finishing_a_run_chimes_once(ws, monkeypatch):
    calls = []
    monkeypatch.setattr(tui_app.sounds, "done", lambda: calls.append("done"))
    app = AghApp(ws, model=ScriptedChatModel(responses=["hi"]))
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#prompt").value = "hello"
        await pilot.press("enter")
        await app._run_task
        await pilot.pause(0.2)
    assert calls == ["done"]
