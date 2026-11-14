"""Language feedback: run a file's formatter and diagnostics after the agent writes it, so errors come back
in the same tool result and get fixed in the same run.

Configured under `language:` in agh.yaml. Each entry maps file extensions to shell commands; `{file}` is
replaced with the quoted path, relative to the workspace when possible. A command whose program is not
installed is skipped silently, so the shipped defaults cost nothing on machines without those tools.

    language:
      enabled: true
      format_on_write: false      # formatters rewrite files, so they are opt-in
      timeout: 30
      project_tools: false        # true also finds tools in the project's .venv and node_modules/.bin
      checkers:
        python: {extensions: [.py], format: "ruff format {file}", diagnostics: "ruff check --output-format concise {file}"}
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from agh.sdk import ToolContext, run_process, shell_argv

MAX_OUTPUT = 4000

DEFAULT_CHECKERS: dict[str, dict[str, Any]] = {
    "python": {"extensions": [".py"], "format": "ruff format {file}",
               "diagnostics": "ruff check --output-format concise {file}"},
    # No --format: ESLint 9 dropped every compact formatter (unix, compact) from its core.
    "typescript": {"extensions": [".ts", ".tsx", ".js", ".jsx"], "format": "prettier --write {file}",
                   "diagnostics": "eslint {file}"},
    "go": {"extensions": [".go"], "format": "gofmt -w {file}", "diagnostics": "go vet {dir}"},
    "rust": {"extensions": [".rs"], "format": "rustfmt {file}"},
}
# Where a project keeps its own tools. Only searched with `language.project_tools: true`: these are programs
# from the repository, and running them means trusting it.
PROJECT_BINS = (".venv/Scripts", ".venv/bin", "venv/Scripts", "venv/bin", "node_modules/.bin")


def settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("language")
    if raw is None:
        raw = {}
    if raw is False:
        raw = {"enabled": False}
    checkers = raw.get("checkers")
    return {"enabled": raw.get("enabled", True), "format_on_write": bool(raw.get("format_on_write", False)),
            "timeout": float(raw.get("timeout", 30)), "project_tools": bool(raw.get("project_tools", False)),
            "checkers": DEFAULT_CHECKERS if checkers is None else (checkers or {})}


def _search_path(config: dict[str, Any], root: Path) -> str | None:
    """The PATH checker commands run with when project tools are on: the project's tool folders, then the
    usual PATH. None means the usual PATH."""
    if not settings(config)["project_tools"]:
        return None
    bins = [str(root / folder) for folder in PROJECT_BINS if (root / folder).is_dir()]
    return os.pathsep.join([*bins, os.environ.get("PATH", "")]) if bins else None


def checker_for(path: Path, config: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    suffix = path.suffix.lower()
    for name, checker in settings(config)["checkers"].items():
        if suffix in [e.lower() for e in checker.get("extensions") or []]:
            return name, checker
    return None


def _quote(text: str) -> str:
    return subprocess.list2cmdline([text]) if sys.platform == "win32" else shlex.quote(text)


def _program(command: str) -> str:
    try:
        return shlex.split(command, posix=sys.platform != "win32")[0].strip('"')
    except (ValueError, IndexError):
        return ""


def _available(command: str, config: dict[str, Any], root: Path) -> bool:
    if (config.get("sandbox") or {}).get("shell") == "docker":
        return True  # the container decides; a missing tool just reports its own error
    program = _program(command)
    return bool(program) and shutil.which(program, path=_search_path(config, root)) is not None


def missing_programs(target: Path, ctx: ToolContext, fmt: bool = False) -> list[str]:
    """The programs this file's checks need that are not installed. feedback() skips those checks without a
    word, so a caller that reports "clean" should look here first."""
    found = checker_for(target, ctx.config)
    commands = [found[1].get(key) for key in (("format",) if fmt else ()) + ("diagnostics",)] if found else []
    return [_program(c) for c in commands if c and not _available(c, ctx.config, ctx.root)]


def _render(template: str, target: Path, root: Path) -> str:
    try:
        shown = target.relative_to(root.resolve()).as_posix()
        inner = target.parent.relative_to(root.resolve()).as_posix()
        # "./pkg", not "pkg": `go vet pkg` looks for a package of that name instead of the folder.
        folder = "." if inner == "." else f"./{inner}"
    except ValueError:
        shown, folder = str(target), str(target.parent)
    return template.replace("{file}", _quote(shown)).replace("{dir}", _quote(folder))


async def _run(template: str, target: Path, ctx: ToolContext, timeout: float) -> tuple[int | None, str] | None:
    command = _render(template, target, ctx.root)
    if not _available(command, ctx.config, ctx.root):
        return None
    path = _search_path(ctx.config, ctx.root)
    result = await run_process(shell_argv(command, ctx.config, ctx.root), ctx.root, timeout,
                               env={**os.environ, "PATH": path} if path else None)
    if result.timed_out:
        return None, f"timed out after {timeout:g}s"
    return result.returncode, result.output.strip()


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n... ({len(text) - MAX_OUTPUT} more characters)"


async def feedback(target: Path, ctx: ToolContext, fmt: bool | None = None) -> str:
    """Format (if enabled) and check one file. Returns text to append to a tool result, or "" if clean or
    unconfigured. Never raises for a failing checker: the write already happened."""
    conf = settings(ctx.config)
    if not conf["enabled"] or not target.is_file():
        return ""
    found = checker_for(target, ctx.config)
    if not found:
        return ""
    name, checker = found
    notes: list[str] = []
    if (conf["format_on_write"] if fmt is None else fmt) and checker.get("format"):
        before = target.stat().st_mtime_ns
        ran = await _run(checker["format"], target, ctx, conf["timeout"])
        if ran is not None:
            code, out = ran
            if code != 0:
                notes.append(f"formatter ({name}) failed:\n{_clip(out)}")
            elif target.stat().st_mtime_ns != before:
                notes.append("formatter rewrote the file; read it again before your next edit")
            ctx.note_read(target)
    if checker.get("diagnostics"):
        ran = await _run(checker["diagnostics"], target, ctx, conf["timeout"])
        if ran is not None:
            code, out = ran
            if code != 0:
                notes.append(f"diagnostics ({name}) reported problems; fix them:\n{_clip(out) or f'exit code {code}'}")
    return "\n\n".join(notes)


async def with_feedback(message: str, target: Path, ctx: ToolContext) -> str:
    extra = await feedback(target, ctx)
    return f"{message}\n\n{extra}" if extra else message
