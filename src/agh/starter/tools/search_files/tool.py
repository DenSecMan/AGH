import re
import shutil
import subprocess

from agh.sdk import ToolContext

SKIP = {".git", ".venv", "venv", "__pycache__", "node_modules", "state", ".agh-backups"}


def run(pattern: str, ctx: ToolContext, path: str = ".", glob: str = "*", max_results: int = 100,
        ignore_case: bool = False) -> str:
    """Search file contents with a regular expression. Uses ripgrep when installed, which skips git-ignored files.

    Args:
        pattern: Regular expression.
        path: Directory to search.
        glob: File name filter such as *.py.
        max_results: Most matches to return.
        ignore_case: Match regardless of case.
    """
    base = ctx.resolve(path)
    only = None  # models often pass a single file as the path; search just that file
    if base.is_file():
        base, only = base.parent, base.name
    root = ctx.root.resolve()
    prefix = base.relative_to(root).as_posix() if base.is_relative_to(root) else str(base)
    if shutil.which("rg"):
        args = ["rg", "--line-number", "--no-heading", "--with-filename", "--color", "never", "--max-columns", "300"]
        # A -g glob overrides ripgrep's ignore rules for whatever it matches, so `-g *` would search every
        # git-ignored file. A file-name glob goes in as a file type instead, which only narrows the search.
        # A glob that names a folder has no such form and stays a -g.
        if glob not in ("", "*", "**", "**/*"):
            args += ["-g", glob] if "/" in glob else ["--type-add", f"aghglob:{glob}", "-t", "aghglob"]
        args += [f"-g!{d}" for d in SKIP]
        if ignore_case:
            args.append("-i")
        proc = subprocess.run([*args, "-e", pattern, only or "."], cwd=base, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=55)
        if proc.returncode == 2 and not proc.stdout:
            raise ValueError(proc.stderr.strip() or "ripgrep failed")
        lines = []
        for line in proc.stdout.splitlines():
            rel = line.replace("\\", "/").removeprefix("./")
            lines.append(rel if prefix == "." else f"{prefix}/{rel}")
        more = f"\n[{len(lines) - max_results} more matches]" if len(lines) > max_results else ""
        return "\n".join(lines[:max_results]) + more if lines else "no matches"
    regex = re.compile(pattern, re.I if ignore_case else 0)
    hits = []
    for file in ([base / only] if only else base.rglob(glob)):
        if not file.is_file() or SKIP & set(file.relative_to(base).parts):
            continue
        try:
            for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{file.resolve().relative_to(root).as_posix()}:{number}: {line[:200]}")
                    if len(hits) >= max_results:
                        return "\n".join(hits)
        except (UnicodeDecodeError, OSError, ValueError):
            continue
    return "\n".join(hits) or "no matches"
