"""First-run provider setup: the checks, what gets saved, and the wizard in the terminal UI."""
import os

import pytest
from langchain_core.messages import AIMessage

from agh import keys, llm, onboarding
from agh.llm import ScriptedChatModel
from agh.tui.app import AghApp
from agh.tui.setup import SetupScreen


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGH_HOME", str(tmp_path / "home"))
    for field in keys.FIELDS["openai"]:
        monkeypatch.delenv(field.var, raising=False)
    return tmp_path / "home"


async def settle(app):
    """Wait for the wizard's checks (the Explorer's loader worker never finishes, so not all workers)."""
    busy = [w for w in app.workers if w.group == "setup"]
    if busy:
        await app.workers.wait_for_complete(busy)


class Replies:
    def invoke(self, prompt):
        return AIMessage(content="OK")


def fake_provider(monkeypatch, good_key="sk-good"):
    def list_models(provider):
        if os.environ.get("OPENAI_API_KEY") != good_key:
            raise llm.ModelConfigError(f"{provider} refused the model list: HTTP 401")
        return ["gpt-a", "gpt-b"]

    monkeypatch.setattr(llm, "list_models", list_models)
    monkeypatch.setattr(llm, "make_model", lambda name, config, **kw: Replies())


def test_needs_setup_until_provider_model_and_key(ws, home, monkeypatch):
    assert onboarding.needs_setup(ws)
    ws.config["models"] = {"selected": {"provider": "openai", "model": "gpt-a"}}
    assert onboarding.needs_setup(ws)  # no key
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert not onboarding.needs_setup(ws)


def test_bad_details_are_not_kept(ws, home, monkeypatch):
    fake_provider(monkeypatch)
    with pytest.raises(ValueError, match="fill in"):
        onboarding.check_connection("openai", {"OPENAI_API_KEY": ""})
    with pytest.raises(ValueError, match="401"):
        onboarding.check_connection("openai", {"OPENAI_API_KEY": "sk-bad"})
    assert "OPENAI_API_KEY" not in os.environ
    assert onboarding.check_connection("openai", {"OPENAI_API_KEY": "sk-good"}) == ["gpt-a", "gpt-b"]
    assert onboarding.check_model("openai", "gpt-a", ws.config) == "OK"
    onboarding.save(ws, "openai", "gpt-a", {"OPENAI_API_KEY": "sk-good"})
    assert keys.parse_env(keys.global_env_path())["OPENAI_API_KEY"] == "sk-good"
    assert llm.selection(ws.config) == ("openai", "gpt-a")


async def test_wizard_runs_on_first_start_and_saves(ws, home, monkeypatch):
    fake_provider(monkeypatch)
    app = AghApp(ws)  # no injected model: the real first-run path
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        screen = app.screen
        menu = screen.query_one("#step-provider")
        menu.highlighted = [name for name, _ in onboarding.providers()].index("openai")
        await pilot.press("enter")
        await pilot.pause()
        field = screen.query_one("#f-OPENAI_API_KEY")
        field.value = "sk-bad"
        await pilot.press("enter")  # last field: Enter tests the connection
        await settle(app)
        await pilot.pause()
        assert "didn't work" in str(screen.query_one("#setup-status").render())
        field.value = "sk-good"
        screen.query_one("#setup-test").press()
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        assert screen.query_one("#setup-steps").current == "step-model"
        await pilot.press("enter")  # first model
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        assert screen.query_one("#setup-steps").current == "step-done"
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, SetupScreen)
        assert llm.selection(ws.config) == ("openai", "gpt-a")


async def test_no_wizard_when_already_set_up(ws, home, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    ws.config["models"] = {"selected": {"provider": "openai", "model": "gpt-a"}}
    app = AghApp(ws)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        assert not isinstance(app.screen, SetupScreen)


# ---- web search setup ---------------------------------------------------------------------------

@pytest.fixture
def no_search(monkeypatch):
    for var, _ in onboarding.SEARCH_PROVIDERS.values():
        monkeypatch.delenv(var, raising=False)


def test_search_setup_asked_once_until_a_key_exists(ws, home, no_search, monkeypatch):
    assert onboarding.needs_search_setup(ws)
    onboarding.mark_search_asked(ws)
    assert not onboarding.needs_search_setup(ws)
    (ws.state / onboarding.SEARCH_ASKED).unlink()
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    assert onboarding.search_configured() == "tavily" and not onboarding.needs_search_setup(ws)


def test_check_search_rejects_empty_key():
    with pytest.raises(ValueError, match="enter the API key"):
        onboarding.check_search("tavily", "  ")


async def test_search_wizard_tests_and_saves_key(ws, home, no_search, monkeypatch):
    from agh.tui.setup import SearchSetupScreen

    monkeypatch.setattr(onboarding, "check_search", lambda service, key: 1)
    app = AghApp(ws, model=ScriptedChatModel(responses=["hi"]))
    async with app.run_test(size=(160, 45)) as pilot:
        app.open_search_setup()
        await pilot.pause()
        assert isinstance(app.screen, SearchSetupScreen)
        await pilot.press("enter")  # tavily is first
        await pilot.pause()
        app.screen.query_one("#search-key").value = "tvly-test"
        await pilot.press("enter")
        for _ in range(20):  # never wait on all workers: the Explorer's loader never finishes
            await pilot.pause(0.05)
            if not isinstance(app.screen, SearchSetupScreen):
                break
        assert not isinstance(app.screen, SearchSetupScreen)
    assert os.environ["TAVILY_API_KEY"] == "tvly-test"
    assert "TAVILY_API_KEY=tvly-test" in keys.global_env_path().read_text()
