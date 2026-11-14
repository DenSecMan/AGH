"""/goal: asks for a token budget, keeps the agent going until it says the goal is done, pauses at the budget."""
import pytest

from agh import goals
from agh.llm import ScriptedChatModel
from agh.tui.app import AghApp
from agh.tui.prompt import PromptArea


def test_parse_tokens():
    assert goals.parse_tokens("200k") == 200_000
    assert goals.parse_tokens("1.5m") == 1_500_000
    assert goals.parse_tokens("50,000 tokens") == 50_000
    assert goals.parse_tokens("none") is None and goals.parse_tokens("") is None
    with pytest.raises(goals.BudgetError):
        goals.parse_tokens("lots")


def test_goal_runs_until_done_marker():
    g = goals.Goal("ship it")
    g.answer("none")
    assert g.state == "running" and g.budget is None
    assert g.after_turn(100, "made progress") == "continue"
    assert g.after_turn(100, "all done\n**GOAL COMPLETE**") == "complete"
    assert g.spent == 200 and g.turns == 2


def test_goal_blocked_and_error_stop():
    g = goals.Goal("x", state="running")
    assert g.after_turn(1, "can't\nGOAL BLOCKED: need a password") == "blocked"
    assert "need a password" in g.note
    assert goals.Goal("x", state="running").after_turn(1, "", "error") == "stopped"


def test_budget_reached_then_more_or_stop():
    g = goals.Goal("x")
    g.answer("1k")
    assert g.after_turn(600, "working") == "continue"
    assert g.after_turn(600, "working") == "budget" and g.state == "awaiting_more"
    assert "1,200 of its 1,000" in g.budget_question()
    with pytest.raises(goals.BudgetError):
        g.answer("plenty")
    g.answer("add 500")
    assert g.state == "running" and g.budget == 1_700
    g.after_turn(600, "working")
    g.answer("stop")
    assert g.state == "stopped" and not g.active


async def _until_idle(app, pilot, done):
    for _ in range(60):
        await pilot.pause(0.1)
        if done():
            return


async def test_goal_in_the_app_asks_budget_then_loops_to_completion(ws):
    app = AghApp(ws, model=ScriptedChatModel(responses=["step one", "step two", "finished\nGOAL COMPLETE"]))
    async with app.run_test() as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        prompt.value = "/goal write the report"
        await pilot.press("enter")
        await _until_idle(app, pilot, lambda: app.goal is not None)
        assert app.goal.state == "awaiting_budget" and not app.engine.session.messages  # nothing sent yet
        prompt.value = "none"
        await pilot.press("enter")
        await _until_idle(app, pilot, lambda: app.goal.state != "running" and not app._busy())
        assert app.goal.state == "complete" and app.goal.turns == 3
        humans = [m.content for m in app.engine.session.messages if m.type == "human"]
        assert len(humans) == 3 and "write the report" in humans[0] and humans[1].startswith("Continue")


async def test_goal_in_the_app_pauses_at_budget(ws):
    app = AghApp(ws, model=ScriptedChatModel(responses=["step", "step", "step", "done\nGOAL COMPLETE"]))
    async with app.run_test() as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        prompt.value = "/goal keep going"
        await pilot.press("enter")
        await _until_idle(app, pilot, lambda: app.goal is not None)
        prompt.value = "1"  # one token: the first turn uses it up
        await pilot.press("enter")
        await _until_idle(app, pilot, lambda: app.goal.state == "awaiting_more" and not app._busy())
        assert app.goal.state == "awaiting_more" and app.goal.turns == 1
        prompt.value = "stop"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.goal.state == "stopped" and not app._busy()
