"""Lifecycle hooks: hooks/<name>/{hook.yaml,hook.py}, where hook.py defines handle(event) -> dict | None.

Events: session_start, pre_tool, post_tool, turn_end.
pre_tool may return {"deny": "reason"} or {"args": {...}}; post_tool may return {"result": "..."}.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Callable

from agh import release
from agh.tooling import import_file
from agh.workspace import Workspace, read_yaml


@dataclass
class Hook:
    name: str
    events: list[str]
    tools: str
    handle: Callable[[dict[str, Any]], Any]


def load_hooks(ws: Workspace) -> list[Hook]:
    hooks = []
    for name in ws.names("hooks"):
        d = ws.element("hooks", name)
        meta = read_yaml(d / "hook.yaml")
        if meta.get("enabled", True) is False or not (d / "hook.py").exists():
            continue
        events, tools = meta.get("events") or [], meta.get("tools", "*")
        if not release.admit(ws, "hook", d):
            if "pre_tool" in events:
                hooks.append(_fail_closed(name, tools, ws.load_errors.get(f"hook:{name}", "not released")))
            continue
        try:
            module = import_file(d / "hook.py", f"agh_hook_{name}")
            hooks.append(Hook(name, events, tools, module.handle))
        except Exception as exc:  # user code at import time
            ws.load_errors[f"hook:{name}"] = f"{type(exc).__name__}: {exc}"
            if "pre_tool" in events:
                hooks.append(_fail_closed(name, tools, str(exc)))
    return hooks


def _fail_closed(name: str, tools: str, why: str) -> Hook:
    """A pre_tool hook may be a guard, so losing it must block what it guards rather than let it through."""
    def handle(_: dict[str, Any]) -> None:
        raise RuntimeError(why)

    return Hook(name, ["pre_tool"], tools, handle)


async def fire(hooks: list[Hook], event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run matching hooks in order and merge their results.

    A crash in a pre_tool hook denies the call: a broken guard must fail closed.
    """
    import fnmatch

    merged: dict[str, Any] = {}
    for hook in hooks:
        if event not in hook.events:
            continue
        if "tool" in payload and not fnmatch.fnmatchcase(payload["tool"], hook.tools):
            continue
        try:
            data = {"event": event, **payload, **merged}
            result = hook.handle(data) if inspect.iscoroutinefunction(hook.handle) else \
                await asyncio.to_thread(hook.handle, data)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            if event == "pre_tool":
                return {"deny": f"hook {hook.name} failed: {exc}"}
            merged.setdefault("hook_errors", []).append(f"{hook.name}: {exc}")
            continue
        if isinstance(result, dict):
            merged.update(result)
            if "deny" in result:
                return merged
    return merged
