from rich.text import Text

from agh import commands, project_init
from agh.engine import Engine
from agh.llm import ScriptedChatModel


def _project(tmp_path):
    project = tmp_path / "poe"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text("print('hi')\n")
    (project / "node_modules" / "junk").mkdir(parents=True)
    (project / "pyproject.toml").write_text('[project]\nname = "poe"\n')
    return project


def test_snapshot_has_tree_and_key_files_but_skips_noise(tmp_path):
    snap = project_init.snapshot(_project(tmp_path))
    assert "src/main.py" in snap and 'name = "poe"' in snap
    assert "node_modules" not in snap


def test_claude_md_is_read_when_there_is_no_agents_md(tmp_path):
    project = _project(tmp_path)
    assert project_init.instructions_file(project) is None
    (project / "CLAUDE.md").write_text("use tabs")
    assert project_init.instructions_file(project).name == "CLAUDE.md"
    (project / "AGENTS.md").write_text("use spaces")
    assert project_init.instructions_file(project).name == "AGENTS.md"


async def test_init_command_writes_agents_md_that_agents_then_read(ws, tmp_path):
    project = _project(tmp_path)
    ws.project = project
    model = ScriptedChatModel(responses=["```markdown\n# Poe\n\n- Run `python src/main.py`.\n```",
                                         "# Poe v2\n"])
    engine = Engine(ws, ws.default_agent, model=model)
    logs = []
    host = commands.Host(ws=ws, engine=lambda: engine, switch_agent=None, send_to_agent=None,
                         log=logs.append, quit=lambda: None)
    await commands.dispatch(host, "/init")
    assert (project / "AGENTS.md").read_text() == "# Poe\n\n- Run `python src/main.py`.\n"
    assert "Run `python src/main.py`" in engine._project_instructions()
    await commands.dispatch(host, "/init")
    assert (project / "AGENTS.md").read_text() == "# Poe v2\n"
    assert (project / "AGENTS.md.bak").read_text().startswith("# Poe\n")
    assert not any(isinstance(m, Text) and m.plain.startswith("Error") for m in logs)
