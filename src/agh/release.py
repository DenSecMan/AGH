"""The release record: nothing loads unless this exact content has passed its release test.

state/releases.json maps each element to a fingerprint of its files. create_* records a pass. At load, anything
unknown or changed (hand edits, shell writes) is retested once per version, and refused if it fails.
Unmodified starter elements ship with the harness and are trusted.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from agh.workspace import (CONFIG_NAME, DEFAULT_PROFILE, PROFILES_DIR, STARTER, Workspace, WorkspaceError,
                           read_yaml)

MANIFEST = "releases.json"
USAGE_MARKER = "AGH_USAGE "  # agh.verify prints one line per finished eval case
UsageSink = Callable[[int, float], None]  # (tokens, cost_usd)
CONNECTOR_RETRY_S = 60  # a connector can start passing without changing: token set, service back up
GATED_DIRS = {"tools", "hooks", "connectors", "skills"}
AGENT_RULE_FILES = {"agent.yaml", "permissions.yaml", "guardrails.yaml"}  # what an agent may do, not what it knows
_SHELL_RULE_FILE = re.compile(r"""(agent|permissions|guardrails|agh)\.yaml|agents\.md|(^|[\s"'/\\=])\.env\b""", re.I)
PROJECT_INSTRUCTIONS = "AGENTS.md"  # in every agent's prompt, so changing it changes what agents do
_SHELL_PATH = re.compile(r"""(^|[\s"'/\\=])(tools|hooks|connectors|skills)[\\/]|releases\.json|profiles[\\/]default\b""")
_SHELL_WRITE = re.compile(r"""(?<![-=])>|\b(rm|del|erase|mv|move|cp|copy|xcopy|robocopy|tee|touch|mkdir|md|rmdir|rd|ln"""
                          r"""|sed|git|curl|wget|python\d?|py|node|pip|set-content|add-content|out-file|new-item"""
                          r"""|remove-item|move-item|copy-item|rename-item|powershell|pwsh|bash|sh|cmd)\b""", re.I)


def _report_usage(stdout: str | bytes | None, on_usage: UsageSink | None) -> None:
    """Pass on what the child's model calls spent, so a release test counts against the run's budget."""
    if not on_usage or not stdout:
        return
    text = stdout.decode("utf-8", "replace") if isinstance(stdout, bytes) else stdout
    rows = [json.loads(line[len(USAGE_MARKER):]) for line in text.splitlines() if line.startswith(USAGE_MARKER)]
    if rows:
        on_usage(sum(r["tokens"] for r in rows), sum(r["cost_usd"] for r in rows))


def run_test(kind: str, directory: Path, timeout: float = 180, on_usage: UsageSink | None = None) -> None:
    """Run agh.verify in a child process and a throwaway cwd, so broken code can't hurt the running harness."""
    src = str(Path(__file__).parent.parent)
    env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    with tempfile.TemporaryDirectory() as cwd:
        try:
            proc = subprocess.run([sys.executable, "-m", "agh.verify", kind, str(Path(directory).resolve())],
                                  capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
        except subprocess.TimeoutExpired as exc:
            _report_usage(exc.stdout, on_usage)  # cases finished before the timeout still cost money
            raise WorkspaceError(f"{kind} failed release test: timed out after {timeout:.0f}s") from exc
    _report_usage(proc.stdout, on_usage)
    if proc.returncode:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        if tail[0].startswith("failed to load"):
            raise WorkspaceError(f"{kind} {tail[0]}")
        raise WorkspaceError(f"{kind} failed release test: {tail[0]}")


def digest(directory: Path) -> str:
    sha = hashlib.sha256()
    for path in sorted(p for p in Path(directory).rglob("*") if p.is_file()):
        rel = path.relative_to(directory).as_posix()
        if "__pycache__" not in rel:
            sha.update(rel.encode() + b"\0" + path.read_bytes() + b"\0")
    return sha.hexdigest()


def _load(ws: Workspace) -> dict[str, Any]:
    try:
        return json.loads((ws.state / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(ws: Workspace, key: str, entry: dict[str, Any]) -> None:
    data = _load(ws)
    data[key] = entry
    (ws.state / MANIFEST).write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")


def _key(ws: Workspace, directory: Path) -> str:
    return Path(directory).resolve().relative_to(ws.root).as_posix()


def record(ws: Workspace, directory: Path) -> None:
    """Mark this exact content as having passed."""
    _save(ws, _key(ws, directory), {"sha": digest(directory), "passed": True, "at": time.time()})


def _is_starter(kind: str, directory: Path) -> bool:
    shipped = STARTER / f"{kind}s" / Path(directory).name
    return shipped.is_dir() and digest(shipped) == digest(directory)


def admit(ws: Workspace, kind: str, directory: Path) -> bool:
    """May this element load? Retests anything the record doesn't vouch for."""
    directory = Path(directory)
    key, sha = _key(ws, directory), digest(directory)
    label = f"{kind}:{directory.name}"
    entry = _load(ws).get(key) or {}
    if entry.get("sha") == sha and entry.get("passed"):
        return True
    if _is_starter(kind, directory):  # byte-for-byte the shipped element, in default or copied into a profile
        ws.load_errors.pop(label, None)
        return True
    fresh = entry.get("sha") == sha and (kind != "connector" or time.time() - entry.get("at", 0) < CONNECTOR_RETRY_S)
    if fresh:
        ws.load_errors[label] = f"not released: {entry.get('error')}"
        return False
    try:
        timeout = 180.0
        if kind == "tool":
            meta = read_yaml(directory / "tool.yaml")
            timeout = 30 + float(meta.get("timeout", 120)) * max(len(meta.get("tests") or []), 1)
        run_test(kind, directory, timeout)
    except WorkspaceError as exc:
        _save(ws, key, {"sha": sha, "passed": False, "error": str(exc), "at": time.time()})
        ws.load_errors[label] = f"not released: {exc}"
        return False
    _save(ws, key, {"sha": sha, "passed": True, "at": time.time()})
    ws.load_errors.pop(label, None)
    return True


def blocked_write(tool: str, args: dict[str, Any], readonly: bool, root: Path) -> str | None:
    """Why this tool call may not run. Elements must go through create_*, and the record is off limits.

    The shell check is best effort, since a command can hide its target. admit() is the actual guarantee.
    """
    if readonly:
        return None
    message = "use the create_* tools, which test before releasing"
    if tool == "shell":
        command = str(args.get("command", ""))
        if _SHELL_PATH.search(command) and _SHELL_WRITE.search(command):
            return f"shell may not change released elements; {message}"
        return None
    if not args.get("path"):
        return None
    try:
        parts = (root / str(args["path"])).resolve().relative_to(root.resolve()).parts
    except ValueError:
        return None
    if parts[:2] == (PROFILES_DIR, DEFAULT_PROFILE):
        return f"{args['path']} is in the default profile, which can't be changed; work in another profile"
    inner = _element_parts(parts)
    gated = bool(inner) and (inner[0] in GATED_DIRS or (len(inner) > 2 and inner[0] == "agents" and inner[2] in GATED_DIRS))
    if gated or parts[:2] == ("state", MANIFEST):
        return f"{args['path']} is a released element; {message}"
    return None


def _element_parts(parts: tuple[str, ...]) -> tuple[str, ...]:
    """A path's parts below its profile folder (profiles/<name>/...), or as given for a flat workspace."""
    return parts[2:] if len(parts) > 2 and parts[0] == PROFILES_DIR else parts


def changes_rules(tool: str, args: dict[str, Any], readonly: bool, root: Path) -> str | None:
    """Why this call needs its own approval: it changes what agents may do. Session grants and auto mode
    don't cover it (only full-auto does, see Engine), or an agent granted edit_file could quietly loosen its own permissions.

    The shell check is best effort, since a command can hide its target.
    """
    if readonly:
        return None
    if tool == "update_agent" or (tool == "create_agent" and (args.get("overwrite") or args.get("permissions"))):
        return "it changes an agent's definition"
    if tool == "shell":
        command = str(args.get("command", ""))
        return "it may change agent rules" if _SHELL_RULE_FILE.search(command) and _SHELL_WRITE.search(command) else None
    if not args.get("path"):
        return None
    try:
        parts = tuple(p.lower() for p in (root / str(args["path"])).resolve().relative_to(root.resolve()).parts)
    except ValueError:
        return None
    inner = _element_parts(parts)
    profile_file = len(parts) == 3 and parts[0] == PROFILES_DIR and parts[2] in (PROJECT_INSTRUCTIONS.lower(), "profile.yaml")
    core_memory = len(parts) == 5 and parts[0] == "data" and parts[2] == "memory" and parts[4] == "core.md"
    if (parts in ((CONFIG_NAME,), (".env",), (PROJECT_INSTRUCTIONS.lower(),)) or profile_file or core_memory
            or (len(inner) == 3 and inner[0] == "agents" and inner[2] in AGENT_RULE_FILES)):
        return f"{args['path']} controls what agents may do"
    return None


def is_spilled_output(tool: str, args: dict[str, Any], root: Path) -> bool:
    """Oversized tool output is saved to disk. Reading it back must not lift the fence it had."""
    if tool != "read_file" or not args.get("path"):
        return False
    return (root / str(args["path"])).resolve().is_relative_to((root / "state" / "outputs").resolve())
