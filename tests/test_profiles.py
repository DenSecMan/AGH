"""Profiles: default stays as installed; saved profiles add to it; memory is per profile."""
import shutil

import pytest

from agh import profiles, release, scaffold
from agh.commands import Host, dispatch
from agh.engine import Engine
from agh.llm import ScriptedChatModel
from agh.memory import MemoryStore
from agh.sessions import Session
from agh.workspace import STARTER, Workspace, WorkspaceError, tree_digest


def skill(ws, name):
    return scaffold.create_skill(ws, name, f"{name} skill", f"# {name}\n")


def test_fresh_workspace_starts_on_default(ws):
    assert ws.profile == "default" and profiles.list_profiles(ws) == ["default"]
    assert not (ws.root / "agents").exists()  # elements live under profiles/, not at the root
    assert tree_digest(ws.default_root / "agents") == tree_digest(STARTER / "agents")
    assert "assistant" in ws.names("agents") and not profiles.has_draft(ws)


def test_changes_on_default_are_a_draft_until_saved(ws):
    default_before = tree_digest(ws.default_root)
    path = skill(ws, "port-scan")
    assert path.is_relative_to(ws.root / "profiles" / ".draft")
    assert tree_digest(ws.default_root) == default_before  # default is never written
    assert "port-scan" in ws.names("skills") and profiles.has_draft(ws)

    profiles.save(ws, "cyber", "Security work")
    assert ws.profile == "cyber" and not profiles.has_draft(ws)
    assert "port-scan" in ws.names("skills")
    assert profiles.describe(ws, "cyber").added == {"skills": ["port-scan"]}

    profiles.switch(ws, "default")
    assert "port-scan" not in ws.names("skills")  # default is uncluttered again
    profiles.switch(ws, "cyber")
    assert "port-scan" in ws.names("skills")


def test_saved_profile_survives_a_restart_and_release_records_follow(ws):
    skill(ws, "recon")
    profiles.save(ws, "cyber")
    again = Workspace(ws.root)
    assert again.profile == "cyber" and "recon" in again.names("skills")
    assert release.admit(again, "skill", again.element("skills", "recon"))  # no retest needed after the move


def test_default_cannot_be_overwritten_or_deleted(ws):
    with pytest.raises(WorkspaceError, match="can't be overwritten"):
        profiles.save(ws, "default")
    with pytest.raises(WorkspaceError, match="can't be deleted"):
        profiles.delete(ws, "default")
    blocked = release.blocked_write("write_file", {"path": "profiles/default/agents/assistant/prompt.md"}, False,
                                    ws.root)
    assert "can't be changed" in blocked


def test_tampered_default_is_put_back_on_start(ws):
    prompt = ws.default_root / "agents" / "assistant" / "prompt.md"
    original = prompt.read_text()
    prompt.write_text("evil")
    Workspace(ws.root)
    assert prompt.read_text() == original


def test_changing_a_default_agent_in_a_profile_copies_it(ws):
    profiles.save(ws, "web")
    scaffold.update_agent(ws, "assistant", {"limits": {"timeout_s": 42}},
                          evals=[{"id": "c", "input": "hi", "expect": [{"type": "contains", "value": "ok"}],
                                  "mock": ["ok"]}])
    assert ws.element("agents", "assistant").is_relative_to(ws.profile_path("web"))
    assert "timeout_s: 42" not in (ws.default_root / "agents" / "assistant" / "agent.yaml").read_text()
    assert profiles.describe(ws, "web").changed == {"agents": ["assistant"], }


def test_memory_is_per_profile_and_a_new_profile_starts_with_a_copy(ws):
    home = MemoryStore(ws.memory_dir("assistant"))
    home.add("user prefers short answers")
    home.close()
    profiles.save(ws, "cyber")
    cyber = MemoryStore(ws.memory_dir("assistant"))
    assert [i.text for i in cyber.search("short answers")] == ["user prefers short answers"]
    cyber.add("target network is 10.0.0.0/8")
    cyber.close()
    profiles.switch(ws, "default")
    back = MemoryStore(ws.memory_dir("assistant"))
    assert back.search("target network") == []
    back.close()


def test_profile_can_hide_default_elements_and_add_instructions(ws):
    profiles.save(ws, "focused")
    meta = ws.overlay / "profile.yaml"
    meta.write_text(meta.read_text() + "exclude:\n  tools: [web_search]\n")
    (ws.overlay / "AGENTS.md").write_text("Think like a pentester.")
    (ws.root / "AGENTS.md").write_text("Be concise.")
    assert "web_search" not in ws.names("tools")
    engine = Engine(ws, "assistant", model=ScriptedChatModel())
    text = engine._project_instructions()
    assert "Be concise." in text and "Think like a pentester." in text
    assert release.changes_rules("write_file", {"path": "profiles/focused/profile.yaml"}, False, ws.root)


def test_delete_and_discard_keep_backups(ws):
    skill(ws, "scratch")
    assert profiles.discard_draft(ws) == ["skills/scratch"] and "scratch" not in ws.names("skills")
    profiles.save(ws, "temp")
    profiles.switch(ws, "default")
    backup = profiles.delete(ws, "temp")
    assert (backup / "profile" / "profile.yaml").exists() and "temp" not in profiles.list_profiles(ws)


async def test_profile_commands_in_the_app(ws):
    switched = []
    engine = Engine(ws, "assistant", model=ScriptedChatModel())

    async def switch_agent(agent, session):
        nonlocal engine
        switched.append(agent)
        engine = Engine(ws, agent, session=session, model=ScriptedChatModel())

    logs = []
    host = Host(ws=ws, engine=lambda: engine, switch_agent=switch_agent, send_to_agent=None,
                log=logs.append, quit=None)
    skill(ws, "xss")
    await dispatch(host, "/profile save webdev Web development")
    assert ws.profile == "webdev" and "Saved profile webdev" in str(logs[-1])
    assert profiles.describe(ws, "webdev").description == "Web development"
    await dispatch(host, "/profile default")
    assert ws.profile == "default" and "xss" not in ws.names("skills")
    await dispatch(host, "/profile")
    await dispatch(host, "/profile webdev")
    assert ws.profile == "webdev" and switched[-1] == "assistant"
    await dispatch(host, "/profile save default")
    assert "can't be overwritten" in str(logs[-1])


async def test_switching_profile_starts_on_the_default_agent(ws):
    switched = []
    engine = Engine(ws, "architect", model=ScriptedChatModel())  # as after a /build

    async def switch_agent(agent, session):
        switched.append(agent)

    host = Host(ws=ws, engine=lambda: engine, switch_agent=switch_agent, send_to_agent=None,
                log=lambda _: None, quit=None)
    profiles.save(ws, "custom-security")
    profiles.switch(ws, "default")
    await dispatch(host, "/profile custom-security")
    assert switched[-1] == "assistant"


async def test_resuming_a_session_returns_to_its_profile(ws):
    profiles.save(ws, "cyber")
    session = Session(ws=ws, agent="assistant")
    session.save()
    profiles.switch(ws, "default")
    engine = Engine(ws, "assistant", model=ScriptedChatModel())

    async def switch_agent(agent, s):
        pass

    host = Host(ws=ws, engine=lambda: engine, switch_agent=switch_agent, send_to_agent=None,
                log=lambda _: None, quit=None)
    await dispatch(host, f"/resume {session.id}")
    assert ws.profile == "cyber"


def test_flat_workspace_migrates_into_profiles(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    shutil.copy2(STARTER / "agh.yaml", root / "agh.yaml")
    for kind in ("agents", "skills", "tools", "connectors", "hooks", "evals"):
        if (STARTER / kind).is_dir():  # the starter ships no connectors
            shutil.copytree(STARTER / kind, root / kind)
    (root / "agents" / "assistant" / "memory" / "core.md").write_text("- likes tea\n")
    (root / "skills" / "mine").mkdir()
    (root / "skills" / "mine" / "SKILL.md").write_text("---\nname: mine\ndescription: d\n---\nbody")
    ws = Workspace(root)
    assert ws.profile == "custom" and not (root / "agents").exists()
    assert "mine" in ws.names("skills") and "assistant" in ws.names("agents")
    assert profiles.describe(ws, "custom").added == {"skills": ["mine"]}  # unchanged starter elements were dropped
    assert "likes tea" in (ws.memory_dir("assistant") / "core.md").read_text()
    assert any(p.name.endswith("before-profiles") for p in (root / ".agh-backups").iterdir())


def test_deleted_bundled_profile_stays_deleted(tmp_path, monkeypatch):
    import agh.workspace as workspace_module
    starter = tmp_path / "starter"
    shutil.copytree(STARTER, starter)
    (starter / "profiles" / "demo").mkdir(parents=True)
    (starter / "profiles" / "demo" / "profile.yaml").write_text("name: demo\nbased_on: default\n")
    monkeypatch.setattr(workspace_module, "STARTER", starter)
    ws = Workspace.init(tmp_path / "ws")
    assert "demo" in profiles.list_profiles(ws)
    profiles.delete(ws, "demo")
    assert "demo" not in profiles.list_profiles(Workspace(ws.root))


async def test_assistant_codes_itself_and_reviewer_loads_its_skills(ws):
    assert "coder" not in ws.names("agents")  # the assistant does code work; a second agent would start cold
    coder = Engine(ws, "assistant", model=ScriptedChatModel())
    await coder.refresh()
    assert {"repository-orientation", "verification-before-completion", "code-review"} <= set(coder.skills)
    assert {"edit_file", "shell", "diagnostics"} <= set(coder.tools) and "reviewer" in ws.names("agents")
    assert int(coder.spec.limits["max_steps"]) == 0  # no step cap
    coder.close()
    reviewer = Engine(ws, "reviewer", model=ScriptedChatModel())
    await reviewer.refresh()
    assert set(reviewer.skills) == {"code-review", "repository-orientation"}
