"""Public API for tools and hooks written inside a workspace."""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ProcessResult:
    returncode: int | None
    output: str
    timed_out: bool = False


def kill_tree(pid: int) -> None:
    """Kill a process and everything it started. A shell's children would otherwise outlive it."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


async def run_process(command: str, cwd: Path | str, timeout: float,
                      env: dict[str, str] | None = None) -> ProcessResult:
    """Run a shell command, merging stdout and stderr. On timeout or cancellation (Ctrl+C, a run's limit)
    the whole process tree is killed before this returns or re-raises, so nothing keeps running unseen.
    `env` replaces the inherited environment."""
    kwargs: dict[str, Any] = {"cwd": str(cwd), "stdin": asyncio.subprocess.DEVNULL, "env": env,
                              "stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.STDOUT}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True  # its own process group, so killpg reaches grandchildren
    proc = await asyncio.create_subprocess_shell(command, **kwargs)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return ProcessResult(proc.returncode, out.decode("utf-8", errors="replace"))
    except asyncio.TimeoutError:
        await _reap(proc)
        return ProcessResult(None, "", timed_out=True)
    except asyncio.CancelledError:
        await asyncio.shield(_reap(proc))
        raise


async def _reap(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        await asyncio.to_thread(kill_tree, proc.pid)
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        pass


def read_text_eol(path: Path) -> tuple[str, str]:
    """A text file's contents with every line ending as "\\n", plus the ending the file uses ("\\r\\n" or "\\n").
    Pass that to write_text_eol so a change keeps the file's line endings instead of rewriting every line.
    A file that mixes both is treated as using the more common one."""
    with path.open(encoding="utf-8", newline="") as fh:  # newline="": no translation, so endings can be counted
        raw = fh.read()
    crlf = raw.count("\r\n")
    eol = "\r\n" if crlf and crlf >= raw.count("\n") - crlf else "\n"
    return raw.replace("\r\n", "\n"), eol


def write_text_eol(path: Path, text: str, eol: str = "\n") -> None:
    """Write text with the given line ending. With the default, the text is written exactly as given: Python's
    own text mode would turn every "\\n" into "\\r\\n" on Windows."""
    if eol != "\n":
        text = text.replace("\r\n", "\n").replace("\n", eol)
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


class SandboxError(PermissionError):
    """A tool tried to touch a path outside the workspace sandbox."""


@dataclass
class ToolContext:
    """Passed to any tool whose `run` function declares a `ctx` parameter."""

    root: Path
    agent: str = ""
    session_id: str = ""
    extra_paths: tuple[Path, ...] = ()
    engine: Any = None  # the running Engine; used by harness system tools

    def resolve(self, path: str) -> Path:
        """Resolve `path` against the sandbox root and refuse escapes (including symlinks)."""
        target = Path(path)
        if not target.is_absolute():
            target = self.root / target
        target = target.resolve()
        for allowed in (self.root.resolve(), *self.extra_paths):
            if target.is_relative_to(allowed):
                return target
        raise SandboxError(f"path outside sandbox: {path}")

    @property
    def config(self) -> dict[str, Any]:
        """The workspace's agh.yaml."""
        return self.engine.ws.config if self.engine is not None else {}

    def _reads(self) -> dict[str, int] | None:
        return getattr(self.engine.root, "file_reads", None) if self.engine is not None else None

    def note_read(self, target: Path) -> None:
        """Remember the version of a file the agent has seen, so a later edit can tell if it changed since."""
        reads = self._reads()
        if reads is not None and target.is_file():
            reads[str(target)] = target.stat().st_mtime_ns

    def check_fresh(self, target: Path) -> None:
        """Refuse to edit a file that changed on disk after the agent last read it: the edit was planned
        against text that is no longer there."""
        reads = self._reads()
        seen = reads.get(str(target)) if reads is not None else None
        if seen is not None and target.is_file() and target.stat().st_mtime_ns != seen:
            raise ValueError(f"{target.name} changed since you last read it; read it again before editing")

    @property
    def processes(self) -> dict[str, Any]:
        """Background processes started in this session, shared with subagents."""
        if self.engine is None:
            return _LOOSE_PROCESSES
        root = self.engine.root
        if not hasattr(root, "processes"):
            root.processes = {}
        return root.processes


_LOOSE_PROCESSES: dict[str, Any] = {}


def shell_argv(command: str, config: dict[str, Any], root: Path) -> str:
    """The command line to actually run. With `sandbox.shell: docker` in agh.yaml, commands run in a throwaway
    container that sees only the workspace (mounted at /work) and, unless `sandbox.network` is true, no network."""
    box = config.get("sandbox") or {}
    if box.get("shell") != "docker":
        return command
    image = box.get("image", "python:3.12-slim")
    network = [] if box.get("network") else ["--network", "none"]
    quoted = subprocess.list2cmdline if sys.platform == "win32" else __import__("shlex").join
    return quoted(["docker", "run", "--rm", "-i", *network, "-v", f"{root}:/work", "-w", "/work",
                   image, "sh", "-c", command])


@dataclass
class BackgroundProcess:
    id: str
    command: str
    proc: Any
    lines: list[str]
    started: float

    @property
    def running(self) -> bool:
        return self.proc.returncode is None


async def start_background(command: str, cwd: Path | str, store: dict[str, Any]) -> BackgroundProcess:
    """Start a long-running command (a dev server, a watcher) and keep its output in memory."""
    import time

    kwargs: dict[str, Any] = {"cwd": str(cwd), "stdin": asyncio.subprocess.DEVNULL,
                              "stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.STDOUT}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = await asyncio.create_subprocess_shell(command, **kwargs)
    bg = BackgroundProcess(id=f"p{len(store) + 1}", command=command, proc=proc, lines=[], started=time.time())
    store[bg.id] = bg

    async def pump() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            bg.lines.append(raw.decode("utf-8", errors="replace").rstrip("\n"))
            del bg.lines[:-2000]  # keep the most recent output only

    bg.pump = asyncio.create_task(pump())  # type: ignore[attr-defined]
    return bg


def stop_all(store: dict[str, Any]) -> None:
    """Kill every background process. Called when a session ends."""
    for bg in list(store.values()):
        if getattr(bg, "running", False):
            kill_tree(bg.proc.pid)
    store.clear()
