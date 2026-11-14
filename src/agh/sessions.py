"""Durable sessions: resume, fork, and transcript repair. Traces are append-only JSONL with secrets redacted."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage, messages_from_dict, messages_to_dict

from agh.workspace import Workspace, WorkspaceError

SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)


def secret_values() -> list[str]:
    return [v for k, v in os.environ.items() if SECRET_NAME.search(k) and len(v) >= 8]


def redact(text: str) -> str:
    for value in secret_values():
        text = text.replace(value, "[REDACTED]")
    return text


class StreamRedactor:
    """Redacts text that arrives in pieces. A secret can be split across two chunks, where redacting each
    chunk alone would miss it, so the end of what has arrived is held back for as long as it could still be
    the start of a secret."""

    def __init__(self) -> None:
        self.secrets = secret_values()
        self.held = ""

    def feed(self, text: str) -> str:
        """Add a chunk; returns what is now safe to show (possibly nothing yet)."""
        if not self.secrets:
            return text
        buffer = self.held + text
        for value in self.secrets:
            buffer = buffer.replace(value, "[REDACTED]")
        longest = max(len(s) for s in self.secrets) - 1
        keep = 0
        for size in range(min(longest, len(buffer)), 0, -1):
            tail = buffer[-size:]
            if any(s.startswith(tail) for s in self.secrets):
                keep = size
                break
        self.held = buffer[len(buffer) - keep:]
        return buffer[:len(buffer) - keep]

    def flush(self) -> str:
        """The stream ended: whatever was held back was not a secret after all."""
        rest, self.held = self.held, ""
        return rest


def repair_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Providers reject a tool call with no result; an interrupted run can leave exactly that."""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    fixed: list[BaseMessage] = []
    for m in messages:
        fixed.append(m)
        if isinstance(m, AIMessage):
            for call in m.tool_calls:
                if call["id"] not in answered:
                    fixed.append(ToolMessage(content="[interrupted before this tool finished]",
                                             tool_call_id=call["id"], name=call["name"]))
    return fixed


@dataclass
class Session:
    ws: Workspace
    agent: str
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    messages: list[BaseMessage] = field(default_factory=list)
    title: str = ""
    ephemeral: bool = False
    usage: dict[str, float] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
    todos: list[dict[str, str]] = field(default_factory=list)
    profile: str = ""  # the profile it ran in; resuming switches back to it

    @property
    def path(self):
        return self.ws.state / "sessions" / f"{self.id}.json"

    def save(self) -> None:
        if self.ephemeral:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "id": self.id, "agent": self.agent, "title": self.title, "usage": self.usage,
            "todos": self.todos, "updated": time.time(), "profile": self.profile or self.ws.profile,
            "messages": messages_to_dict(self.messages)}), encoding="utf-8")
        os.replace(tmp, self.path)

    def archive(self, messages: list[BaseMessage]) -> None:
        """Compaction drops messages from context, not from disk."""
        if self.ephemeral:
            return
        path = self.ws.state / "sessions" / f"{self.id}.archive.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            for item in messages_to_dict(messages):
                fh.write(json.dumps(item) + "\n")

    @classmethod
    def load(cls, ws: Workspace, session_id: str) -> "Session":
        if not re.fullmatch(r"[\w-]+", session_id or ""):
            raise WorkspaceError(f"bad session id {session_id!r}")
        path = ws.state / "sessions" / f"{session_id}.json"
        if not path.exists():
            raise WorkspaceError(f"no session {session_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(ws=ws, agent=data["agent"], id=data["id"], title=data.get("title", ""),
                   usage=data.get("usage", {}), todos=data.get("todos", []), profile=data.get("profile", ""),
                   messages=repair_messages(messages_from_dict(data["messages"])))

    def fork(self) -> "Session":
        new = Session(ws=self.ws, agent=self.agent, title=f"fork of {self.title or self.id}",
                      messages=list(self.messages), todos=list(self.todos))
        new.save()
        if self.path.with_suffix(".archive.jsonl").exists():
            shutil.copy(self.path.with_suffix(".archive.jsonl"), new.path.with_suffix(".archive.jsonl"))
        return new


def list_sessions(ws: Workspace, limit: int = 30) -> list[dict[str, Any]]:
    out = []
    files = sorted((ws.state / "sessions").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append({"id": data["id"], "agent": data["agent"], "title": data.get("title", ""), "profile": data.get("profile", ""),
                        "messages": len(data["messages"]), "updated": data.get("updated", 0)})
        except (ValueError, KeyError):
            continue
    return out


def search_sessions(ws: Workspace, query: str, limit: int = 30) -> list[dict[str, Any]]:
    """Sessions whose title or messages contain every word of the query, newest first."""
    words = [w.lower() for w in query.split()]
    out = []
    for path in sorted((ws.state / "sessions").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        haystack = (data.get("title", "") + " " + json.dumps(data.get("messages", []))).lower()
        if all(w in haystack for w in words):
            out.append({"id": data["id"], "agent": data["agent"], "title": data.get("title", ""), "profile": data.get("profile", ""),
                        "messages": len(data["messages"]), "updated": data.get("updated", 0)})
            if len(out) >= limit:
                break
    return out


def latest_session(ws: Workspace, agent: str | None = None) -> str | None:
    for s in list_sessions(ws, limit=200):
        if agent is None or s["agent"] == agent:
            return s["id"]
    return None


def rename_session(ws: Workspace, session_id: str, title: str) -> None:
    session = Session.load(ws, session_id)
    session.title = title.strip()[:120]
    session.save()


def delete_session(ws: Workspace, session_id: str) -> list[str]:
    """Remove a session and everything kept for it: archive, trace, checkpoints."""
    Session.load(ws, session_id)  # validates the id and that it exists
    removed = []
    for path in (ws.state / "sessions" / f"{session_id}.json", ws.state / "sessions" / f"{session_id}.archive.jsonl",
                 ws.state / "traces" / f"{session_id}.jsonl"):
        if path.exists():
            path.unlink()
            removed.append(path.name)
    checkpoints = ws.state / "checkpoints" / session_id
    if checkpoints.exists():
        shutil.rmtree(checkpoints)
        removed.append("checkpoints")
    return removed


def export_markdown(ws: Workspace, session_id: str) -> str:
    """The conversation as Markdown: user and agent messages, tool calls as one line each."""
    session = Session.load(ws, session_id)
    lines = [f"# {session.title or session.id}", "", f"Agent: {session.agent} · Session: {session.id}", ""]
    for m in session.messages:
        text = m.content if isinstance(m.content, str) else " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in m.content)
        if m.type == "human":
            lines += ["## User", "", text, ""]
        elif m.type == "ai":
            if text.strip():
                lines += ["## Agent", "", text, ""]
            for call in getattr(m, "tool_calls", None) or []:
                lines.append(f"- tool `{call['name']}` {json.dumps(call.get('args'), default=str)[:300]}")
            if getattr(m, "tool_calls", None):
                lines.append("")
    return redact("\n".join(lines))


def read_trace(ws: Workspace, session_id: str) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[\w-]+", session_id or ""):
        raise WorkspaceError(f"bad session id {session_id!r}")
    path = ws.state / "traces" / f"{session_id}.jsonl"
    if not path.exists():
        raise WorkspaceError(f"no trace for {session_id}")
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def trace_lines(events: list[dict[str, Any]]) -> list[str]:
    """A readable timeline of a trace: tools, notices, errors and outcomes, with seconds from the start."""
    if not events:
        return []
    start = events[0].get("t", 0)
    out = []
    for e in events:
        kind, at = e.get("kind"), f"{e.get('t', start) - start:7.1f}s"
        who = e.get("agent", "")
        if kind == "tool_start":
            out.append(f"{at} {who} > {e.get('tool')} {json.dumps(e.get('args'), default=str)[:160]}")
        elif kind == "tool_end":
            status = f"failed: {e['error']}" if e.get("error") else f"ok in {e.get('duration')}s"
            out.append(f"{at} {who}   {e.get('tool')} {status}")
        elif kind in ("notice", "error"):
            out.append(f"{at} {who} {kind}: {e.get('text')}")
        elif kind == "done":
            out.append(f"{at} {who} done ({e.get('stop_reason')}, {e.get('steps')} steps)")
        elif kind == "subagent_end":
            out.append(f"{at} {who} finished ({e.get('stop_reason')})")
    return out


class Tracer:
    """One JSON line per event, for later replay and debugging."""

    def __init__(self, ws: Workspace, session_id: str, enabled: bool = True):
        self.path = ws.state / "traces" / f"{session_id}.jsonl" if enabled else None

    def log(self, kind: str, **data: Any) -> None:
        if not self.path:
            return
        line = redact(json.dumps({"t": round(time.time(), 3), "kind": kind, **data}, default=str))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
