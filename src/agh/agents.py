"""Agent definitions and skills. An agent is a directory; each concern has its own file:

agents/<name>/
  agent.yaml        identity, model, which tools/skills/connectors/subagents, limits
  prompt.md         system prompt (managed and versioned by self-improvement)
  permissions.yaml  allow / ask / deny rules
  guardrails.yaml   output redaction, blocked patterns, required JSON schema
  memory/core.md    always-in-context notes (memory.db holds searchable memories)
  tools/ skills/    optional private tools and skills
  versions/         prompt history
  improvements/     feedback and self-improvement run records
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agh import release
from agh.workspace import Workspace, WorkspaceError, read_yaml, validate_name

DEFAULT_LIMITS = {
    "max_steps": 50,
    "timeout_s": 900,
    # context_tokens: unset means what the model is known to take (llm.context_window). Old tool output is
    # cleared from 70% of it and the conversation summarized at 80%.
    "max_tool_result_chars": 20_000,
}


@dataclass
class AgentSpec:
    name: str
    path: Path
    description: str = ""
    model: str | list[str] = "auto"  # a list is tried in order
    fallbacks: list[str] = field(default_factory=list)
    model_params: dict[str, Any] = field(default_factory=dict)
    tools: list[str] = field(default_factory=list)
    system_tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    preload_skills: list[str] = field(default_factory=list)  # of those, the ones always in the prompt
    connectors: list[str] = field(default_factory=list)
    subagents: list[str] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""
    permissions: dict[str, Any] = field(default_factory=dict)
    guardrails: dict[str, Any] = field(default_factory=dict)
    project_instructions: bool = True  # include the workspace's AGENTS.md in the prompt


def load_agent(ws: Workspace, name: str) -> AgentSpec:
    validate_name(name)
    path = ws.element("agents", name)
    if not (path / "agent.yaml").exists():
        raise WorkspaceError(f"no agent named {name!r} (have: {', '.join(ws.names('agents')) or 'none'})")
    data = read_yaml(path / "agent.yaml")
    prompt = path / "prompt.md"
    return AgentSpec(
        name=name, path=path,
        description=data.get("description", ""),
        model=data.get("model", "auto"),
        fallbacks=data.get("fallbacks") or [],
        model_params=data.get("model_params") or {},
        tools=data.get("tools") or [],
        system_tools=data.get("system_tools") or [],
        skills=data.get("skills") or [],
        preload_skills=data.get("preload_skills") or [],
        connectors=data.get("connectors") or [],
        subagents=data.get("subagents") or [],
        limits={**DEFAULT_LIMITS, **(data.get("limits") or {})},
        memory={"enabled": True, "recall_k": 5, "reflect_every": 0, **(data.get("memory") or {})},
        prompt=prompt.read_text(encoding="utf-8") if prompt.exists() else "",
        permissions=read_yaml(path / "permissions.yaml"),
        guardrails=read_yaml(path / "guardrails.yaml"),
        project_instructions=data.get("project_instructions", True) is not False,
    )


def selected(wanted: list[str], available: list[str]) -> list[str]:
    """Resolve a yaml list. "*" means everything, and "!glob" removes the names it matches."""
    chosen = list(available) if "*" in wanted else [w for w in wanted if w in available]
    excluded = [w[1:] for w in wanted if w.startswith("!")]
    return [c for c in chosen if not any(fnmatch.fnmatchcase(c, pattern) for pattern in excluded)]


def snapshot_prompt(agent_dir: Path) -> Path:
    """Save the current prompt as the next version, so any change to it can be rolled back."""
    versions = agent_dir / "versions"
    versions.mkdir(exist_ok=True)
    numbers = [int(m[1]) for p in versions.glob("prompt.v*.md") if (m := re.search(r"v(\d+)", p.name))]
    target = versions / f"prompt.v{max(numbers, default=0) + 1}.md"
    target.write_text((agent_dir / "prompt.md").read_text(encoding="utf-8"), encoding="utf-8")
    return target


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path

    def resources(self) -> list[str]:
        return sorted(str(p.relative_to(self.path)).replace("\\", "/")
                      for p in self.path.rglob("*") if p.is_file() and p.name != "SKILL.md")


FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


def load_skill_dir(path: Path) -> Skill:
    text = (path / "SKILL.md").read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    if not match:
        raise WorkspaceError(f"{path / 'SKILL.md'} needs YAML frontmatter with name and description")
    meta = yaml.safe_load(match[1]) or {}
    return Skill(name=meta.get("name", path.name), description=meta.get("description", ""),
                 body=match[2].strip(), path=path)


def load_skills(ws: Workspace, agent: AgentSpec) -> dict[str, Skill]:
    """Workspace skills chosen by the agent, plus all of its private skills."""
    found: dict[str, Skill] = {}
    for name in selected(agent.skills, ws.names("skills")):
        _try_skill(ws, ws.element("skills", name), found)
    private = agent.path / "skills"
    if private.is_dir():
        for d in sorted(p for p in private.iterdir() if p.is_dir()):
            _try_skill(ws, d, found)
    return found


def _try_skill(ws: Workspace, path: Path, found: dict[str, Skill]) -> None:
    if not release.admit(ws, "skill", path):
        return
    try:
        skill = load_skill_dir(path)
        found[skill.name] = skill
    except (OSError, yaml.YAMLError, WorkspaceError) as exc:
        ws.load_errors[f"skill:{path.name}"] = str(exc)
