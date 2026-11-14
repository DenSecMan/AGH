import fnmatch
import shutil
import subprocess

from agh.sdk import ToolContext

SKIP = {".git", ".venv", "venv", "__pycache__", "node_modules", "state", ".agh-backups"}


def run(pattern: str, ctx: ToolContext, path: str = ".", max_results: int = 500) -> str:
    """Find files whose path matches a glob, newest first.

    Args:
        pattern: Glob such as *.py, src/**/*.ts or *test*. Matched against the path relative to `path`
            and against the file name.
        path: Directory to search.
        max_results: Most paths to return.
    """
    base = ctx.resolve(path)
    if shutil.which("rg"):  # ripgrep honours .gitignore and is fast on big trees
        out = subprocess.run(["rg", "--files", "--hidden", "-g", "!.git"], cwd=base, capture_output=True,
                             text=True, encoding="utf-8", errors="replace").stdout.splitlines()
        candidates = [base / line for line in out if not SKIP & set(line.replace("\\", "/").split("/"))]
    else:
        candidates = [p for p in base.rglob("*") if p.is_file() and not SKIP & set(p.relative_to(base).parts)]
    flat = pattern.replace("**/", "*")
    hits = [p for p in candidates
            if fnmatch.fnmatch(p.relative_to(base).as_posix(), flat) or fnmatch.fnmatch(p.name, pattern)]
    hits.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    root = ctx.root.resolve()
    shown = [p.relative_to(root).as_posix() if p.is_relative_to(root) else str(p) for p in hits[:max_results]]
    more = f"\n[{len(hits) - max_results} more]" if len(hits) > max_results else ""
    return "\n".join(shown) + more if shown else "no files match"
