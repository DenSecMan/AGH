"""Create agents, skills, tools, connectors, hooks and eval suites, validating before anything goes live.

Used by the `create_*` agent tools, by `agh new`, and by slash commands.
"""
from __future__ import annotations

import ast
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from agh.agents import selected, snapshot_prompt
from agh.connectors import from_openapi, load_openapi_source, validate_config, write_connector
from agh.policy import FILE_READERS
from agh.release import UsageSink, record as _record, run_test as _release_test
from agh.workspace import FLAT_MARKER, KINDS, Workspace, WorkspaceError, read_yaml, validate_name, write_yaml

DEFAULT_SKILLS = ["*", "!build-*", "!write-evals"]  # those guide the builder tools, which most agents don't have

TOOL_TEMPLATE = '''"""{description}"""


def run(text: str) -> str:
    """{description}

    Args:
        text: Input to process.
    """
    return text
'''

HOOK_TEMPLATE = '''"""{description}"""


def handle(event: dict) -> dict | None:
    return None
'''


def _fresh(path: Path, overwrite: bool) -> Path:
    if path.exists():
        if not overwrite:
            raise WorkspaceError(f"{path.name} already exists; pass overwrite to replace it")
        shutil.rmtree(path)
    return path


def _check_python(code: str, func: str, kind: str) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise WorkspaceError(f"{kind} code has a syntax error on line {exc.lineno}: {exc.msg}") from exc
    if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func for n in tree.body):
        raise WorkspaceError(f"{kind} code must define a top-level function `{func}`")


def _guard(path: Path, overwrite: bool) -> None:
    """Fail before running any test; _fresh removes the old version only after the new one has passed."""
    if path.exists() and not overwrite:
        raise WorkspaceError(f"{path.name} already exists; pass overwrite to replace it")


def _check_cases(cases: list[dict[str, Any]]) -> None:
    ids = [c.get("id") for c in cases]
    if len(set(ids)) != len(ids) or not all(ids):
        raise WorkspaceError("every eval case needs a unique id")
    empty = [c["id"] for c in cases if not c.get("input") or not c.get("expect")]
    if empty:
        raise WorkspaceError(f"eval cases need an input and at least one expect check: {empty}")


def create_tool(ws: Workspace, name: str, description: str, code: str | None = None, readonly: bool = False,
                permission: str | None = None, timeout: int = 120, untrusted: bool = False,
                overwrite: bool = False, tests: list[dict[str, Any]] | None = None) -> Path:
    validate_name(name)
    _guard(ws.element("tools", name), overwrite)
    if code is None and tests is None:  # the template echoes its input, so it can test itself
        tests = [{"args": {"text": "ping"}, "expect": "ping"}]
    code = code or TOOL_TEMPLATE.format(description=description.replace('"', "'"))
    _check_python(code, "run", "tool")
    meta: dict[str, Any] = {"name": name, "description": description, "readonly": readonly,
                            "timeout": timeout, "untrusted": untrusted, "tests": tests or []}
    if permission:
        meta["permission"] = permission
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "t" / name
        probe.mkdir(parents=True)
        (probe / "tool.py").write_text(code, encoding="utf-8")
        write_yaml(probe / "tool.yaml", meta)
        _release_test("tool", probe, timeout=30 + timeout * max(len(tests or []), 1))
    target = _fresh(ws.dir("tools") / name, overwrite)
    target.mkdir(parents=True)
    (target / "tool.py").write_text(code, encoding="utf-8")
    write_yaml(target / "tool.yaml", meta)
    _record(ws, target)
    return target


def create_skill(ws: Workspace, name: str, description: str, body: str | None = None,
                 resources: dict[str, str] | None = None, overwrite: bool = False,
                 base: Path | None = None) -> Path:
    validate_name(name)
    if not description.strip():
        raise WorkspaceError("a skill needs a description; it is how agents decide to load it")
    final = (base or ws.dir("skills")) / name
    _guard(ws.element("skills", name) if base is None else final, overwrite)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / name
        stage.mkdir()
        front = yaml.safe_dump({"name": name, "description": description}, sort_keys=False).strip()
        content = body or f"# {name}\n\n{description}\n\n## Steps\n\n1. Describe the procedure here.\n"
        (stage / "SKILL.md").write_text(f"---\n{front}\n---\n\n{content}\n", encoding="utf-8")
        for rel, text in (resources or {}).items():
            dest = (stage / rel).resolve()
            if not dest.is_relative_to(stage.resolve()) or dest.name == "SKILL.md":
                raise WorkspaceError(f"bad resource path {rel}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        _release_test("skill", stage, timeout=60)
        target = _fresh(final, overwrite)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, target)
    if base is None:  # drafts stay unreleased until promoted
        _record(ws, target)
    return target


def _check_agent_refs(ws: Workspace, name: str, tools: list[str] | None, skills: list[str] | None,
                      connectors: list[str] | None, subagents: list[str] | None) -> None:
    def missing(wanted: list[str] | None, kind: str, also_ok: tuple[str, ...] = ()) -> list[str]:
        names = {*ws.names(kind), *also_ok, "*"}
        return [w for w in wanted or [] if not w.startswith("!") and w not in names]

    if unknown := missing(tools, "tools"):
        raise WorkspaceError(f"unknown tools: {unknown}. Create them first. Available: {ws.names('tools')}")
    if unknown := missing(connectors, "connectors"):
        raise WorkspaceError(f"unknown connectors: {unknown}")
    if unknown := missing(skills, "skills"):
        raise WorkspaceError(f"unknown skills: {unknown}")
    if unknown := missing(subagents, "agents", (name,)):
        raise WorkspaceError(f"unknown subagents: {unknown}")


def _stage_workspace(ws: Workspace, stage_root: Path) -> None:
    """Copy what an agent needs to run into a throwaway workspace. Memory stays out: evals don't use it."""
    stage_root.mkdir()
    ignore = shutil.ignore_patterns("__pycache__", "results", "memory.db*")
    for kind in KINDS:  # the active profile flattened: what this profile sees, at the root of the copy
        for name in ws.names(kind):
            shutil.copytree(ws.element(kind, name), stage_root / kind / name, ignore=ignore)
    (stage_root / FLAT_MARKER).write_text("staged copy for a release test\n", encoding="utf-8")
    if (ws.root / "agh.yaml").exists():
        shutil.copy2(ws.root / "agh.yaml", stage_root)
    (stage_root / "state").mkdir()
    if (ws.state / "releases.json").exists():
        shutil.copy2(ws.state / "releases.json", stage_root / "state")


def _default_permissions(ws: Workspace, tools: list[str] | None) -> dict[str, Any]:
    """Read-only tools run unattended, as an agent's release eval can't approve anything. Tools returning outside
    content ask if the agent also reads files, since a malicious page could otherwise send those files anywhere."""
    wanted = selected(tools if tools is not None else FILE_READERS, ws.names("tools"))
    metas = {t: read_yaml(ws.element("tools", t) / "tool.yaml") for t in wanted}
    safe = [t for t, m in metas.items() if m.get("readonly") and not m.get("untrusted") and t not in FILE_READERS]
    outside = [t for t, m in metas.items() if m.get("untrusted")]
    reads_files = any(t in FILE_READERS for t in wanted)
    return {"default": "ask", "allow": [*FILE_READERS, *safe], "ask": outside if reads_files else [], "deny": []}


def _release_suite(ws: Workspace, name: str) -> Path:
    return ws.writable("evals", f"{name[:40]}-release")  # stays with the workspace, so the agent can be re-checked


def create_agent(ws: Workspace, name: str, description: str, prompt: str | None = None,
                 tools: list[str] | None = None, system_tools: list[str] | None = None,
                 skills: list[str] | None = None, connectors: list[str] | None = None,
                 subagents: list[str] | None = None, model: str = "auto",
                 permissions: dict[str, Any] | None = None, overwrite: bool = False,
                 evals: list[dict[str, Any]] | None = None, on_usage: UsageSink | None = None) -> Path:
    """Build the agent in a copy of the workspace, run its eval there, and release only if every case passes."""
    validate_name(name)
    _guard(ws.element("agents", name), overwrite)
    _check_agent_refs(ws, name, tools, skills, connectors, subagents)
    if not evals:
        raise WorkspaceError('a new agent needs evals: at least one {"id", "input", "expect": [...]} case')
    _check_cases(evals)
    suite = {"name": "release-gate", "agent": name, "threshold": 1.0, "approve": "deny", "concurrency": 4, "cases": evals}
    fallback = _default_permissions(ws, tools)
    with tempfile.TemporaryDirectory() as tmp:
        stage_root = Path(tmp) / "ws"
        _stage_workspace(ws, stage_root)
        _write_agent(stage_root / "agents" / name, name, description, prompt, tools, system_tools, skills, connectors,
                     subagents, model, permissions, fallback)
        write_yaml(stage_root / "evals" / "release-gate" / "suite.yaml", suite)
        _release_test("agent", stage_root, timeout=600, on_usage=on_usage)
    target = ws.writable("agents", name)  # replacing a default agent copies it into the profile first
    if (target / "prompt.md").exists():
        snapshot_prompt(target)
    _write_agent(target, name, description, prompt, tools, system_tools, skills, connectors, subagents, model,
                 permissions, fallback)
    keep = _release_suite(ws, name)
    write_yaml(keep / "suite.yaml", {**suite, "name": keep.name})
    return target


def update_agent(ws: Workspace, name: str, changes: dict[str, Any], evals: list[dict[str, Any]] | None = None,
                 on_usage: UsageSink | None = None) -> Path:
    """Change only the given fields, gated like a new agent. Memory, history and private elements are untouched."""
    validate_name(name)
    target = ws.element("agents", name)
    if not (target / "agent.yaml").exists():
        raise WorkspaceError(f"no agent named {name!r}; use create_agent to make one")
    if not changes and not evals:
        raise WorkspaceError("nothing to change")
    _check_agent_refs(ws, name, changes.get("tools"), changes.get("skills"), changes.get("connectors"),
                      changes.get("subagents"))
    changes = _with_permissions_for_new_tools(ws, target, changes)
    keep = _release_suite(ws, name)
    cases = {c.get("id"): c for c in read_yaml(keep / "suite.yaml").get("cases") or []}
    _check_cases(evals or [])
    cases.update({c["id"]: c for c in evals or []})
    if not cases:
        raise WorkspaceError(f"{name} has no release eval yet, so pass evals: at least one "
                             '{"id", "input", "expect": [...]} case that shows the change works')
    suite = {"name": "release-gate", "agent": name, "threshold": 1.0, "approve": "deny", "concurrency": 4,
             "cases": list(cases.values())}
    with tempfile.TemporaryDirectory() as tmp:
        stage_root = Path(tmp) / "ws"
        _stage_workspace(ws, stage_root)
        _apply_agent_changes(stage_root / "agents" / name, changes)
        write_yaml(stage_root / "evals" / "release-gate" / "suite.yaml", suite)
        _release_test("agent", stage_root, timeout=600, on_usage=on_usage)
    target = ws.writable("agents", name)  # only now: a failed change leaves no copy behind
    if "prompt" in changes:
        snapshot_prompt(target)
    _apply_agent_changes(target, changes)
    write_yaml(keep / "suite.yaml", {**suite, "name": keep.name})
    return target


def _with_permissions_for_new_tools(ws: Workspace, target: Path, changes: dict[str, Any]) -> dict[str, Any]:
    """Tools added without explicit permissions follow the rules for a new agent, so giving a file-reading agent a
    web tool can't quietly let it read files and reach the web unattended."""
    if "tools" not in changes or "permissions" in changes:
        return changes
    names = ws.names("tools")
    old = selected(read_yaml(target / "agent.yaml").get("tools") or [], names)
    added = [t for t in selected(changes["tools"], names) if t not in old]
    rules = read_yaml(target / "permissions.yaml")
    defaults = _default_permissions(ws, changes["tools"])
    allow, ask = list(rules.get("allow") or []), list(rules.get("ask") or [])
    allow += [t for t in added if t in defaults["allow"] and t not in allow]
    ask += [t for t in defaults["ask"] if t not in ask]
    return {**changes, "permissions": {**rules, "allow": allow, "ask": ask}}


def _apply_agent_changes(target: Path, changes: dict[str, Any]) -> None:
    fields = {k: v for k, v in changes.items() if k not in ("prompt", "permissions", "limits")}
    data = read_yaml(target / "agent.yaml")
    limits = {**(data.get("limits") or {}), **(changes.get("limits") or {})}
    write_yaml(target / "agent.yaml", {**data, **fields, **({"limits": limits} if limits else {})})
    if "prompt" in changes:
        (target / "prompt.md").write_text(changes["prompt"].rstrip() + "\n", encoding="utf-8")
    if "permissions" in changes:
        write_yaml(target / "permissions.yaml", changes["permissions"])


def _write_agent(target: Path, name: str, description: str, prompt: str | None, tools: list[str] | None,
                 system_tools: list[str] | None, skills: list[str] | None, connectors: list[str] | None,
                 subagents: list[str] | None, model: str, permissions: dict[str, Any] | None,
                 default_permissions: dict[str, Any]) -> None:
    """Writes the definition. When replacing an agent, what it has accumulated stays: memory, prompt history,
    private tools and skills (memory and feedback are kept per profile in data/), plus any permissions and guardrails not explicitly replaced."""
    (target / "memory").mkdir(parents=True, exist_ok=True)  # only the seed core.md; what it learns is in data/
    (target / "versions").mkdir(exist_ok=True)
    write_yaml(target / "agent.yaml", {
        "name": name, "description": description, "model": model, "fallbacks": [],
        "tools": tools if tools is not None else FILE_READERS,
        "system_tools": system_tools if system_tools is not None else ["memory", "skills", "plan"],
        "skills": skills if skills is not None else DEFAULT_SKILLS,
        "connectors": connectors or [], "subagents": subagents or [],
        "limits": {"max_steps": 50}, "memory": {"enabled": True, "recall_k": 5, "reflect_every": 0},
    })
    (target / "prompt.md").write_text(
        (prompt or f"You are {name}. {description}\n\nWork step by step, verify results, and be concise.") + "\n",
        encoding="utf-8")
    if permissions or not (target / "permissions.yaml").exists():
        write_yaml(target / "permissions.yaml", permissions or default_permissions)
    if not (target / "guardrails.yaml").exists():
        write_yaml(target / "guardrails.yaml", {"redact_patterns": [], "block_patterns": [], "output_schema": None})
    if not (target / "memory" / "core.md").exists():
        (target / "memory" / "core.md").write_text(f"# Core memory for {name}\n", encoding="utf-8")


def create_connector(ws: Workspace, name: str, config: dict[str, Any] | None = None,
                     openapi: str | None = None, include: str | None = None, overwrite: bool = False) -> Path:
    """Build from an OpenAPI URL or file or an explicit config, then prove it with one real read-only call."""
    validate_name(name)
    if (ws.element("connectors", name) / "connector.yaml").exists() and not overwrite:
        raise WorkspaceError(f"connector {name} already exists")
    if openapi:
        config = from_openapi(name, load_openapi_source(openapi), include=include)
    elif config is None:
        raise WorkspaceError("a connector can't be released untested, so it needs a real config or an OpenAPI "
                             "source: `agh connector import` or the create_connector tool")
    validate_config(config)
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / name
        probe.mkdir()
        write_yaml(probe / "connector.yaml", config)
        _release_test("connector", probe)
    target = write_connector(ws, name, config, overwrite=overwrite).parent
    _record(ws, target)
    return target


def create_hook(ws: Workspace, name: str, description: str, events: list[str], code: str | None = None,
                tools: str = "*", overwrite: bool = False) -> Path:
    validate_name(name)
    _guard(ws.element("hooks", name), overwrite)
    code = code or HOOK_TEMPLATE.format(description=description.replace('"', "'"))
    _check_python(code, "handle", "hook")
    meta = {"name": name, "description": description, "events": events, "tools": tools, "enabled": True}
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / name
        probe.mkdir()
        write_yaml(probe / "hook.yaml", meta)
        (probe / "hook.py").write_text(code, encoding="utf-8")
        _release_test("hook", probe, timeout=60)
    target = _fresh(ws.dir("hooks") / name, overwrite)
    target.mkdir(parents=True)
    write_yaml(target / "hook.yaml", meta)
    (target / "hook.py").write_text(code, encoding="utf-8")
    _record(ws, target)
    return target


def create_eval(ws: Workspace, name: str, agent: str, cases: list[dict[str, Any]] | None = None,
                threshold: float = 0.8, overwrite: bool = False) -> Path:
    validate_name(name)
    _guard(ws.element("evals", name), overwrite)
    if agent not in ws.names("agents"):
        raise WorkspaceError(f"eval names unknown agent {agent!r}")
    cases = cases or [{"id": "smoke", "input": "Say hello.", "expect": [{"type": "contains", "value": "hello"}]}]
    _check_cases(cases)
    target = _fresh(ws.dir("evals") / name, overwrite)
    write_yaml(target / "suite.yaml", {"name": name, "agent": agent, "threshold": threshold,
                                       "approve": "deny", "concurrency": 4, "cases": cases})
    return target


def create_element(ws: Workspace, kind: str, name: str, description: str = "") -> Path:
    """Template-only creation for `agh new` and `/new`."""
    description = description or f"TODO: describe {name}"
    if kind == "agent":
        return create_agent(ws, name, description, evals=[
            {"id": "smoke", "input": "Say hello.", "expect": [{"type": "contains", "value": "hello"}]}])
    if kind == "skill":
        return create_skill(ws, name, description)
    if kind == "tool":
        return create_tool(ws, name, description)
    if kind == "connector":
        return create_connector(ws, name)
    if kind == "hook":
        return create_hook(ws, name, description, events=["pre_tool"])
    if kind == "eval":
        return create_eval(ws, name, agent=(ws.names("agents") or ["assistant"])[0])
    raise WorkspaceError(f"unknown kind {kind}; use agent, skill, tool, connector, hook or eval")
