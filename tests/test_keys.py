import os

import pytest

from agh import cli, commands, keys
from agh.workspace import Workspace


@pytest.fixture
def clean_env(monkeypatch):
    for fields in keys.FIELDS.values():
        for f in fields:
            monkeypatch.delenv(f.var, raising=False)
    monkeypatch.delenv("AGH_WORKSPACE", raising=False)


def test_user_key_is_saved_loaded_and_masked(ws, clean_env, monkeypatch):
    path = keys.set_values(ws.root, keys.build_values("anthropic", "sk-ant-1234567890abcdef", {}))
    assert path == keys.global_env_path()
    assert "ANTHROPIC_API_KEY=sk-ant-1234567890abcdef" in path.read_text()
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-1234567890abcdef"
    row = next(r for r in keys.status(ws.root) if r["var"] == "ANTHROPIC_API_KEY")
    assert row["value"] == "sk-a…cdef" and row["source"].startswith("user")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    Workspace(ws.root)  # a new process: the user-wide file is read for every workspace
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-1234567890abcdef"


def test_workspace_key_wins_and_remove_falls_back(ws, clean_env):
    keys.set_values(ws.root, {"OPENAI_API_KEY": "user-key-000000000"})
    keys.set_values(ws.root, {"OPENAI_API_KEY": "local-key-11111111"}, scope="workspace")
    assert os.environ["OPENAI_API_KEY"] == "local-key-11111111"
    keys.remove_provider(ws.root, "openai", scope="workspace")
    assert os.environ["OPENAI_API_KEY"] == "user-key-000000000"
    assert "OPENAI_API_KEY" not in keys.parse_env(ws.root / ".env")


def test_write_keeps_comments_and_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# mine\nOTHER=1\nGEMINI_API_KEY=old\n")
    keys.write_values(env, {"GEMINI_API_KEY": "new value", "TAVILY_API_KEY": "t"})
    assert env.read_text() == '# mine\nOTHER=1\nGEMINI_API_KEY="new value"\nTAVILY_API_KEY=t\n'
    assert keys.parse_env(env)["GEMINI_API_KEY"] == "new value"


def test_azure_short_names_and_unknown_setting():
    values = keys.build_values("azure_openai", "k", {"endpoint": "https://x", "deployment": "gpt"})
    assert values == {"AZURE_OPENAI_API_KEY": "k", "AZURE_OPENAI_ENDPOINT": "https://x",
                      "AZURE_OPENAI_DEPLOYMENT_NAME_CHAT": "gpt"}
    with pytest.raises(keys.KeySetupError):
        keys.build_values("anthropic", "k", {"endpoint": "x"})
    with pytest.raises(keys.KeySetupError):
        keys.build_values("nope", "k", {})


def test_slash_key_command_saves_and_masks(ws, clean_env):
    out = commands.key_command(ws, ["set", "openrouter", "or-abcdefghijklmnop", "--no-check"])
    assert "OPENROUTER_API_KEY" in str(out)
    assert os.environ["OPENROUTER_API_KEY"] == "or-abcdefghijklmnop"
    assert commands.mask_key_command("/key set openrouter or-abcdefghijklmnop") == "/key set openrouter or-a…mnop"
    with pytest.raises(ValueError, match="still needs"):
        commands.key_command(ws, ["set", "azure_openai", "k"])


def test_cli_key_set_without_terminal(ws, clean_env, monkeypatch):
    monkeypatch.chdir(ws.root)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli._dispatch(cli._parser().parse_args(["key", "set", "gemini", "g-123", "--no-check"])) == 0
    assert keys.parse_env(keys.global_env_path())["GEMINI_API_KEY"] == "g-123"
    assert cli._dispatch(cli._parser().parse_args(["key", "remove", "gemini"])) == 0
    assert "GEMINI_API_KEY" not in keys.parse_env(keys.global_env_path())


def test_running_agh_outside_a_workspace_uses_the_user_wide_one(tmp_path, monkeypatch, clean_env):
    folder = tmp_path / "project"
    folder.mkdir()
    monkeypatch.chdir(folder)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    home_ws = (keys.agh_home() / "workspace").resolve()
    ws = cli._workspace_here(cli._parser().parse_args([]), "tui")
    assert ws.root == home_ws and (home_ws / "agh.yaml").exists()
    assert ws.root.parent == keys.global_env_path().parent.resolve()
    assert not (folder / "agh.yaml").exists()
    assert cli._workspace_here(cli._parser().parse_args(["ls"]), "ls").root == home_ws


def test_explicit_workspace_dir_is_created_and_found(tmp_path, monkeypatch, clean_env):
    folder = tmp_path / "project"
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    ws = cli._workspace_here(cli._parser().parse_args(["-w", str(folder)]), "tui")
    assert ws.root == folder.resolve()
    sub = folder / "src"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert cli._workspace_here(cli._parser().parse_args([]), "tui").root == folder.resolve()


def test_other_commands_do_not_create_a_workspace(tmp_path, monkeypatch, clean_env):
    monkeypatch.setenv("AGH_HOME", str(tmp_path / "fresh-home"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Exception, match="no agh.yaml"):
        cli._workspace_here(cli._parser().parse_args(["ls"]), "ls")
    assert not (tmp_path / "agh.yaml").exists()
    assert not (tmp_path / "fresh-home" / "workspace").exists()
