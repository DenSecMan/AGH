import os
import re
import subprocess
from pathlib import Path

from agh.sdk import ToolContext

ALLOWED = {"status", "diff", "log", "show", "blame", "branch", "ls-files", "rev-parse"}
# Flags that write files, run programs, open an editor, or read a file outside the repository.
BLOCKED_FLAGS = {"--output", "-o", "--ext-diff", "--exec", "-c", "--config", "--textconv", "--no-index",
                 "--contents", "--edit-description"}
BRANCH_CHANGES = {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "--set-upstream-to", "-u"}
QUIET = {"GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"}  # never wait on the terminal


def _check_path(arg: str, ctx: ToolContext) -> None:
    """`git diff` compares any two files on disk when one lies outside the repository, so an argument that
    could be such a path (absolute, or climbing with ..) must stay inside the sandbox."""
    if Path(arg).is_absolute() or arg.startswith(("/", "\\")) or ".." in re.split(r"[\\/]", arg):
        try:
            ctx.resolve(arg)
        except PermissionError:
            raise ValueError(f"{arg} points outside the workspace; git here only reads inside it. If it is a "
                             f"value for a flag, attach it to the flag (such as --grep={arg})") from None


def run(subcommand: str, ctx: ToolContext, args: list[str] | None = None) -> str:
    """Run a read-only git command in the workspace. Changes (commit, checkout, push) go through shell.

    Args:
        subcommand: status, diff, log, show, blame, branch, ls-files or rev-parse.
        args: Extra arguments, such as ["--stat"] or ["-n", "5", "--oneline"].
    """
    if subcommand not in ALLOWED:
        raise ValueError(f"git {subcommand} is not read-only here; allowed: {sorted(ALLOWED)}")
    extra = [str(a) for a in args or []]
    if any(a.split("=")[0] in BLOCKED_FLAGS for a in extra):
        raise ValueError("that flag can write files, run programs or read files outside the repository")
    if subcommand == "branch" and any(a in BRANCH_CHANGES or not a.startswith("-") for a in extra):
        raise ValueError("git branch here only lists branches")
    for a in extra:
        if not a.startswith("-"):
            _check_path(a, ctx)
    proc = subprocess.run(["git", "--no-pager", "-c", "core.pager=cat", subcommand, "--no-ext-diff", *extra]
                          if subcommand in ("diff", "show", "log") else
                          ["git", "--no-pager", subcommand, *extra],
                          cwd=ctx.root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=55, stdin=subprocess.DEVNULL, env={**os.environ, **QUIET})
    return (proc.stdout + proc.stderr).strip() or f"(git {subcommand}: no output)"
