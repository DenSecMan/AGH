"""Release tests. Run as `python -m agh.verify KIND DIR` in a child process, from a throwaway directory.

Exit 0 means the staged element works. Any failure prints one line to stderr, and scaffold refuses to release it.
Tests run real code: tools and hooks are called, connectors make one real read-only request.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

HOOK_PAYLOADS: dict[str, dict[str, Any]] = {
    "session_start": {"agent": "release-test", "session": "release-test"},
    "pre_tool": {"tool": "release_test", "args": {}, "agent": "release-test"},
    "post_tool": {"tool": "release_test", "args": {}, "agent": "release-test", "result": "ok", "error": None},
    "turn_end": {"agent": "release-test", "session": "release-test", "text": "ok", "stop_reason": "end_turn"},
}


class Failed(Exception):
    pass


async def verify_tool(directory: Path) -> None:
    from agh.sdk import ToolContext
    from agh.tooling import load_tool_dir
    from agh.workspace import read_yaml

    try:
        spec = load_tool_dir(directory)
    except Exception as exc:
        raise Failed(f"failed to load: {type(exc).__name__}: {exc}") from exc
    cases = read_yaml(directory / "tool.yaml").get("tests") or []
    if not cases:
        raise Failed('needs tests: at least one {"args": {...}, "expect": "text in the output"}')
    ctx = ToolContext(root=Path.cwd(), agent="release-test", session_id="release-test")
    for number, case in enumerate(cases, 1):
        if not isinstance(case, dict) or not case.get("expect"):
            raise Failed(f"test {number} needs a non-empty `expect`")
        try:
            output = str(await spec.invoke(case.get("args") or {}, ctx))
        except Exception as exc:
            raise Failed(f"test {number} raised {type(exc).__name__}: {exc}") from exc
        if str(case["expect"]) not in output:
            raise Failed(f"test {number} expected {case['expect']!r} in output, got {output[:300]!r}")


async def verify_hook(directory: Path) -> None:
    from agh.tooling import import_file
    from agh.workspace import read_yaml

    events = read_yaml(directory / "hook.yaml").get("events") or []
    unknown = [e for e in events if e not in HOOK_PAYLOADS]
    if not events or unknown:
        raise Failed(f"needs events from {sorted(HOOK_PAYLOADS)}; got {events}")
    try:
        handle = import_file(directory / "hook.py", "agh_hook_release_test").handle
    except Exception as exc:
        raise Failed(f"failed to load: {type(exc).__name__}: {exc}") from exc
    for event in events:
        try:
            result = handle({"event": event, "dry_run": True, **HOOK_PAYLOADS[event]})
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            raise Failed(f"raised {type(exc).__name__} on {event}: {exc}") from exc
        if result is not None and not isinstance(result, dict):
            raise Failed(f"must return a dict or None on {event}, got {type(result).__name__}")


def _rest_probe(cfg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The operation to call: the configured `test`, else the first read needing no arguments."""
    from agh.connectors import READ_METHODS

    operations = cfg.get("operations") or {}
    if not operations:
        raise Failed("has no operations")
    if cfg.get("test"):
        name, args = cfg["test"].get("operation"), cfg["test"].get("args") or {}
        if name not in operations:
            raise Failed(f"test names unknown operation {name!r}")
        if operations[name].get("method", "GET").upper() not in READ_METHODS:
            raise Failed(f"test operation {name} must be a read; release tests never write to a service")
        return name, args
    for name, op in operations.items():
        params = (op.get("params") or {}).values()
        needs_args = any((p or {}).get("required") or (p or {}).get("in") == "path" for p in params)
        if op.get("method", "GET").upper() in READ_METHODS and not needs_args:
            return name, {}
    raise Failed('has no read operation callable without arguments; add test: {operation: NAME, args: {...}}')


async def verify_connector(directory: Path) -> None:
    from agh.connectors import Connector, _mcp_tools, validate_config
    from agh.workspace import read_yaml

    cfg = read_yaml(directory / "connector.yaml")
    conn = Connector(name=directory.name, config=cfg)
    try:
        validate_config(cfg)
        if cfg.get("type") == "mcp":
            if not await _mcp_tools(conn):
                raise Failed("MCP server exposes no tools")
            return
        conn.tools()
    except Failed:
        raise
    except Exception as exc:
        raise Failed(f"failed to load: {type(exc).__name__}: {exc}") from exc
    operation, args = _rest_probe(cfg)
    try:
        await conn.call(operation, args)
    except Exception as exc:
        raise Failed(f"test call {operation} failed: {type(exc).__name__}: {exc}") from exc


async def verify_skill(directory: Path) -> None:
    """A skill has no code to run, so test that the loader reads it back complete."""
    from agh.agents import load_skill_dir

    try:
        skill = load_skill_dir(directory)
    except Exception as exc:
        raise Failed(f"failed to load: {exc}") from exc
    if not skill.description.strip() or not skill.body.strip():
        raise Failed("description and body must both be non-empty")


def _print_usage(case: Any) -> None:
    """Flushed per case, so the parent still sees what was spent if it kills this process on a timeout."""
    from agh.release import USAGE_MARKER

    print(USAGE_MARKER + json.dumps({"tokens": case.tokens, "cost_usd": case.cost_usd}), flush=True)


async def verify_agent(root: Path) -> None:
    """Run the agent's required eval in a staged copy of the workspace. Every case must pass."""
    from agh.evals import load_suite, run_suite
    from agh.workspace import Workspace

    ws = Workspace(root)
    try:
        suite = load_suite(ws, "release-gate")
        if any(c.get("mock") is not None for c in suite["cases"]) and not os.environ.get("AGH_ALLOW_MOCK_EVALS"):
            raise Failed("eval cases can't use `mock`: a scripted reply proves nothing about the agent")
        result = await run_suite(ws, "release-gate", save=False, on_case=_print_usage)
    except Failed:
        raise
    except Exception as exc:
        raise Failed(f"eval could not run: {type(exc).__name__}: {exc}") from exc
    failed = [f"{c.id}: {c.error or [k['detail'] for k in c.checks if not k['passed']]}"
              + (f" tool errors: {c.tool_errors}" if c.tool_errors else "") + f" answered: {c.output[:150]!r}"
              for c in result.cases if not c.passed]
    if failed:
        raise Failed(f"eval cases failed: {'; '.join(failed)}"[:1000])


VERIFIERS = {"tool": verify_tool, "hook": verify_hook, "connector": verify_connector, "skill": verify_skill,
             "agent": verify_agent}


def main(argv: list[str]) -> int:
    kind, directory = argv
    try:
        asyncio.run(VERIFIERS[kind](Path(directory)))
    except Failed as exc:
        print(" ".join(str(exc).split()), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
