"""Self-improvement: feedback, eval-gated prompt evolution, versioning and rollback, skill mining.

A change only ships if the eval suite scores higher with no regressions, including on holdout cases
the proposer never saw. Failures are data to learn from; the eval gate is what grants trust.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agh import llm, release, scaffold
from agh.agents import load_agent, snapshot_prompt
from agh.engine import text_of
from agh.evals import ModelFactory, SuiteResult, load_suite, run_suite
from agh.sessions import Session, redact
from agh.workspace import Workspace, WorkspaceError, validate_name


def record_feedback(ws: Workspace, agent: str, rating: str, note: str = "", session_id: str = "") -> None:
    load_agent(ws, agent)
    path = ws.data_dir("improvements", agent) / "feedback.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": time.time(), "rating": rating, "note": note, "session": session_id}) + "\n")


def read_feedback(ws: Workspace, agent: str, limit: int = 30) -> list[dict[str, Any]]:
    load_agent(ws, agent)
    path = ws.data_dir("improvements", agent) / "feedback.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()][-limit:]


def rollback(ws: Workspace, agent: str, version: int | None = None) -> str:
    load_agent(ws, agent)
    agent_dir = ws.writable("agents", agent)  # a change in a profile never touches default
    versions = agent_dir / "versions"
    found = sorted(((int(m[1]), p) for p in versions.glob("prompt.v*.md") if (m := re.search(r"v(\d+)", p.name))))
    if not found:
        raise WorkspaceError(f"{agent} has no saved prompt versions")
    chosen = dict(found).get(version) if version is not None else found[-1][1]
    if chosen is None:
        raise WorkspaceError(f"no version {version}; have {[n for n, _ in found]}")
    text = chosen.read_text(encoding="utf-8")
    snapshot_prompt(agent_dir)  # rollback itself is undoable
    (agent_dir / "prompt.md").write_text(text, encoding="utf-8")
    return chosen.name


def _failure_digest(result: SuiteResult) -> str:
    """Only non-holdout cases reach the proposer, so holdouts can catch overfitting."""
    rows = []
    for case in result.cases:
        if case.passed or case.holdout:
            continue
        failed = [c["detail"] for c in case.checks if not c["passed"]]
        rows.append(f"- case {case.id}\n  failed checks: {failed}\n  error: {case.error}\n"
                    f"  output: {case.output[:800]}")
    return "\n".join(rows) or "(no visible failures)"


def _strip_fence(text: str) -> str:
    match = re.fullmatch(r"\s*```(?:\w+)?\n(.*)\n```\s*", text, re.S)
    return match[1] if match else text.strip()


@dataclass
class ImproveOutcome:
    agent: str
    suite: str
    accepted: bool
    baseline: float
    best: float
    rounds: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    record: str = ""


async def improve_prompt(ws: Workspace, agent: str, suite_name: str, rounds: int = 2, min_gain: float = 0.01,
                         apply: bool = True, proposer: Any = None, factory: ModelFactory | None = None,
                         judge: Any = None) -> ImproveOutcome:
    spec = load_agent(ws, agent)
    suite = load_suite(ws, suite_name)
    if suite["agent"] != agent:
        raise WorkspaceError(f"suite {suite_name} tests {suite['agent']}, not {agent}")
    model = proposer or llm.make_model(spec.model, ws.config)
    current = spec.prompt
    base = await run_suite(ws, suite_name, factory=factory, judge=judge, save=False)
    best, best_prompt = base, None
    log: list[dict[str, Any]] = []
    feedback = read_feedback(ws, agent)
    note_lines = "\n".join(f"- {f['rating']}: {f['note']}" for f in feedback if f.get("note")) or "(none)"
    for number in range(1, rounds + 1):
        reply = await model.ainvoke([
            SystemMessage(content="You improve system prompts for AI agents. Change as little as needed to fix the "
                                  "failures. State general rules: don't hard-code test answers. Keep every existing "
                                  "safety rule. Reply with only the complete new system prompt."),
            HumanMessage(content=f"<current_prompt>\n{best_prompt or current}\n</current_prompt>\n\n"
                                 f"<eval_failures>\n{redact(_failure_digest(best))}\n</eval_failures>\n\n"
                                 f"<user_feedback>\n{redact(note_lines)}\n</user_feedback>")])
        candidate = _strip_fence(text_of(reply))
        if not candidate or candidate == (best_prompt or current).strip():
            log.append({"round": number, "score": best.score, "accepted": False, "note": "no change proposed"})
            break
        result = await run_suite(ws, suite_name, prompt_override=candidate, factory=factory, judge=judge, save=False)
        regressions = sorted(best.passing_ids() - result.passing_ids())
        better = result.score >= best.score + min_gain and not regressions
        log.append({"round": number, "score": result.score, "accepted": better, "regressions": regressions})
        if better:
            best, best_prompt = result, candidate
        if best.score >= 1.0:
            break
    accepted = best_prompt is not None
    outcome = ImproveOutcome(agent, suite_name, accepted, base.score, best.score, log)
    if accepted and apply:
        agent_dir = ws.writable("agents", agent)
        snapshot = snapshot_prompt(agent_dir)
        (agent_dir / "prompt.md").write_text(best_prompt.rstrip() + "\n", encoding="utf-8")
        outcome.reason = f"applied; previous prompt saved as {snapshot.name}"
    elif accepted:
        outcome.reason = "candidate passed the gate but was not applied (dry run)"
    else:
        outcome.reason = "no candidate beat the baseline without regressions; prompt unchanged"
    records = ws.data_dir("improvements", agent)
    records.mkdir(exist_ok=True)
    record = records / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    record.write_text(json.dumps({
        "suite": suite_name, "baseline": base.score, "best": best.score, "accepted": accepted, "applied": accepted and apply,
        "rounds": log, "candidate": best_prompt}, indent=2), encoding="utf-8")
    outcome.record = str(record)
    return outcome


async def mine_skill(ws: Workspace, session_id: str, model: Any = None) -> Path:
    """Draft a reusable skill from a finished session. Drafts stay inactive until promoted."""
    session = Session.load(ws, session_id)
    spec = load_agent(ws, session.agent)
    transcript = "\n".join(
        f"{m.type}: {text_of(m)[:1200]}" + (f" tool_calls={getattr(m, 'tool_calls', '')}" if getattr(m, "tool_calls", None) else "")
        for m in session.messages if m.type != "tool")
    reply = await (model or llm.make_model(spec.model, ws.config)).ainvoke([
        SystemMessage(content="Turn the procedure this session worked out into a reusable skill. Reply with only "
                              'JSON: {"name": "lowercase-name", "description": "what and when to use", '
                              '"body": "markdown steps"}.'),
        HumanMessage(content=redact(transcript))])
    match = re.search(r"\{.*\}", text_of(reply), re.S)
    if not match:
        raise WorkspaceError("model did not return a skill")
    data = json.loads(match[0])
    drafts = ws.data_dir("improvements", session.agent, "skill_drafts")
    return scaffold.create_skill(ws, validate_name(data["name"]), data["description"], data["body"],
                                 base=drafts, overwrite=True)


def promote_skill(ws: Workspace, agent: str, name: str) -> Path:
    draft = ws.data_dir("improvements", agent, "skill_drafts") / validate_name(name)
    if not draft.exists():
        raise WorkspaceError(f"no draft skill {name}")
    target = ws.dir("skills") / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if ws.element("skills", name).exists():
        raise WorkspaceError(f"skill {name} already exists")
    release.run_test("skill", draft)
    draft.replace(target)
    release.record(ws, target)
    return target
