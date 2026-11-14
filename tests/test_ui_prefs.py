"""Theme and pane-layout preferences, and the set_ui system tool the agent uses to change them."""
import pytest

from agh import ui_prefs
from agh.sdk import ToolContext
from agh.system_tools import set_ui


def test_move_positions():
    base = ui_prefs.DEFAULT_LAYOUT
    assert ui_prefs.move(base, "chat", "left") == [["chat"], ["profile", "explorer"], ["editor"]]
    assert ui_prefs.move(base, "explorer", "right") == [["profile"], ["editor"], ["chat"], ["explorer"]]
    assert ui_prefs.move(base, "profile", "after chat") == [["explorer"], ["editor"], ["chat"], ["profile"]]
    assert ui_prefs.move(base, "files", "below chat") == [["profile"], ["editor"], ["chat", "explorer"]]
    assert ui_prefs.move(base, "explorer", "above editor") == [["profile"], ["explorer", "editor"], ["chat"]]
    with pytest.raises(ValueError):
        ui_prefs.move(base, "editor", "below profile")  # that column is full
    with pytest.raises(ValueError):
        ui_prefs.move(base, "chat", "upstairs")
    with pytest.raises(ValueError):
        ui_prefs.move(base, "terminal", "left")


def test_full_layout_and_theme_names():
    assert ui_prefs.full_layout(["chat", "editor/profile"]) == [["chat"], ["editor", "profile"], ["explorer"]]
    assert ui_prefs.theme_name("Tokyo Night") == "tokyo-night"
    assert ui_prefs.theme_name("mocha") == "catppuccin-mocha"
    with pytest.raises(ValueError):
        ui_prefs.theme_name("catppuccin")  # ambiguous


def test_bad_file_falls_back_to_defaults(tmp_path):
    (tmp_path / "ui.json").write_text('{"theme": "nope", "layout": ["chat"]}', encoding="utf-8")
    assert ui_prefs.load(tmp_path) == {"theme": ui_prefs.DEFAULT_THEME, "layout": ui_prefs.DEFAULT_LAYOUT}


def test_old_flat_layout_still_loads(tmp_path):
    (tmp_path / "ui.json").write_text('{"layout": ["chat", "profile", "editor", "explorer"]}', encoding="utf-8")
    assert ui_prefs.load(tmp_path)["layout"] == [["chat"], ["profile"], ["editor"], ["explorer"]]


class FakeEngine:
    def __init__(self, ws):
        self.ws, self.events = ws, []

    def emit(self, kind, **data):
        self.events.append((kind, data))


async def test_set_ui_tool_saves_and_emits(ws):
    engine = FakeEngine(ws)
    ctx = ToolContext(root=ws.root, engine=engine)
    out = await set_ui(ctx, theme="gruvbox", pane="chat", position="left")
    assert "UI updated" in out
    assert engine.events == [("ui", {"theme": "gruvbox", "layout": [["chat"], ["profile", "explorer"], ["editor"]]})]
    assert ui_prefs.load(ws.state)["theme"] == "gruvbox"
    assert "not changed" in await set_ui(ctx, theme="sparkly")
    assert "Themes:" in await set_ui(ctx)
