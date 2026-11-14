"""Tools work in the folder agh was started in, not the workspace directory."""
from agh.engine import Engine
from agh.llm import ScriptedChatModel


def test_tools_resolve_paths_in_launch_folder(ws, tmp_path):
    project = tmp_path / "proj"
    (project / "scripts").mkdir(parents=True)
    (project / "scripts" / "vps.ps1").write_text("echo hi", encoding="utf-8")
    ws.project = project
    ctx = Engine(ws, ws.default_agent, model=ScriptedChatModel()).ctx()
    assert ctx.root == project.resolve()
    assert ctx.resolve("scripts/vps.ps1") == (project / "scripts" / "vps.ps1").resolve()
    assert ctx.resolve(str(ws.root / "agh.yaml")) == (ws.root / "agh.yaml").resolve()  # harness files still reachable


def test_reported_workspace_paths_work_from_launch_folder(ws, tmp_path):
    from agh.system_tools import _shown

    ws.project = tmp_path / "proj"
    ws.project.mkdir()
    ctx = Engine(ws, ws.default_agent, model=ScriptedChatModel()).ctx()
    target = ws.root / "profiles"
    assert ctx.resolve(_shown(ctx, target)) == target.resolve()
