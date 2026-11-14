"""ToolSpec: one harness-side description of a tool, however it was sourced."""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, get_type_hints

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ValidationError, create_model
from pydantic.fields import FieldInfo

from agh import release
from agh.sdk import ToolContext
from agh.workspace import Workspace, read_yaml


@dataclass
class ToolSpec:
    name: str
    description: str
    args_schema: Any = None
    func: Callable[..., Any] | None = None
    readonly: bool = False       # may run concurrently and is allowed in plan mode
    permission: str | None = None  # allow | ask | deny; None means use the agent's default
    timeout: float = 120.0
    untrusted: bool = False      # output comes from outside and is fenced as data
    source: str = "workspace"
    wants_ctx: bool = False
    lc_tool: BaseTool | None = None  # set for tools that LangChain already wraps (MCP)

    def as_langchain(self) -> BaseTool:
        if self.lc_tool:
            return self.lc_tool

        def _never(**_: Any) -> str:  # the engine calls ToolSpec.invoke, never this
            raise NotImplementedError

        return StructuredTool(name=self.name, description=self.description,
                              args_schema=self.args_schema, func=_never)

    async def invoke(self, args: dict[str, Any], ctx: ToolContext) -> Any:
        if self.lc_tool:
            return await asyncio.wait_for(self.lc_tool.ainvoke(args), self.timeout)
        try:
            parsed = self.args_schema(**args).model_dump(exclude_unset=True)
        except ValidationError as exc:
            raise ValueError(f"invalid arguments: {exc}") from exc
        kwargs = {**parsed, "ctx": ctx} if self.wants_ctx else parsed
        assert self.func is not None
        if inspect.iscoroutinefunction(self.func):
            return await asyncio.wait_for(self.func(**kwargs), self.timeout)
        return await asyncio.wait_for(asyncio.to_thread(self.func, **kwargs), self.timeout)


def _arg_docs(doc: str) -> dict[str, str]:
    """Pull `name: description` lines out of a Google-style Args: section."""
    match = re.search(r"Args:\n(.*?)(?:\n\s*\n|\Z)", doc, re.S)
    if not match:
        return {}
    return {m[0]: m[1].strip() for m in re.findall(r"^\s*(\w+)(?:\s*\(.*?\))?:\s*(.+)$", match[1], re.M)}


def _describe(description: str | None, doc: str) -> str:
    """What the model is told about a tool: the one-line description (tool.yaml) and the docstring's text above
    Args:, which is where a tool explains how to use it well. Whichever repeats the other is left out."""
    body = re.split(r"^\s*Args:\s*$", doc, maxsplit=1, flags=re.M)[0].strip()
    summary = (description or "").strip()
    if not summary or not body:
        return summary or body
    short, long = _plain(summary), _plain(body)
    if short in long:
        return body
    if long in short:
        return summary
    return f"{summary}\n\n{body}"


def _plain(text: str) -> str:
    return " ".join(text.lower().split()).rstrip(".")


def spec_from_function(fn: Callable[..., Any], name: str | None = None, description: str | None = None,
                       **attrs: Any) -> ToolSpec:
    """Derive the argument schema from type hints, so a tool is just a typed function."""
    sig = inspect.signature(fn)
    hints = get_type_hints(fn)
    doc = inspect.getdoc(fn) or ""
    docs = _arg_docs(doc)
    fields: dict[str, Any] = {}
    wants_ctx = False
    for pname, param in sig.parameters.items():
        if pname == "ctx":
            wants_ctx = True
            continue
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[pname] = (hints.get(pname, Any), FieldInfo(default=default, description=docs.get(pname)))
    tool_name = name or fn.__name__
    schema = create_model(f"{tool_name}_args", **fields)
    return ToolSpec(name=tool_name, description=_describe(description, doc) or tool_name, args_schema=schema, func=fn,
                    wants_ctx=wants_ctx, **attrs)


def import_file(path: Path, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_tool_dir(directory: Path) -> ToolSpec:
    """Load tools/<name>/{tool.yaml,tool.py}. tool.py must define `run`."""
    meta = read_yaml(directory / "tool.yaml")
    name = meta.get("name") or directory.name
    module = import_file(directory / "tool.py", f"agh_tool_{directory.parent.parent.name}_{name}")
    return spec_from_function(
        module.run, name=name, description=meta.get("description"),
        readonly=bool(meta.get("readonly", False)), permission=meta.get("permission"),
        timeout=float(meta.get("timeout", 120)), untrusted=bool(meta.get("untrusted", False)),
        source=str(directory),
    )


def load_workspace_tools(ws: Workspace, extra_dirs: tuple[Path, ...] = ()) -> dict[str, ToolSpec]:
    """Every tool in the workspace plus agent-private dirs. A broken tool is recorded, never fatal."""
    tools: dict[str, ToolSpec] = {}
    shared = [ws.element("tools", name) for name in ws.names("tools")]  # this profile's, then default's
    for group in (shared, *([p for p in base.iterdir()] if base.is_dir() else [] for base in extra_dirs)):
        for d in sorted((p for p in group if (p / "tool.py").exists()), key=lambda p: p.name):
            if not release.admit(ws, "tool", d):
                continue
            try:
                spec = load_tool_dir(d)
                tools[spec.name] = spec
                ws.load_errors.pop(f"tool:{d.name}", None)
            except Exception as exc:  # user code: anything can go wrong at import time
                ws.load_errors[f"tool:{d.name}"] = f"{type(exc).__name__}: {exc}"
    return tools
