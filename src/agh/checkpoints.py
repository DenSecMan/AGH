"""File change previews and undo.

Before a tool writes a file, the file's old contents are saved once per turn under
state/checkpoints/<session>/<turn>/, so /undo can put back everything a turn changed.
Only file tools that take a `path` are covered: what a shell command changes can't be known in advance.
"""
from __future__ import annotations

import difflib
import json
import shutil
from pathlib import Path
from typing import Any

MAX_DIFF_LINES = 400
MAX_PREVIEW_BYTES = 2_000_000


def _read(path: Path) -> str | None:
    if not path.is_file() or path.stat().st_size > MAX_PREVIEW_BYTES:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def proposed_text(args: dict[str, Any], current: str | None) -> str | None:
    """What the file would contain after the call, for argument shapes we recognise."""
    if isinstance(args.get("content"), str):
        return args["content"]
    if current is None:
        return None
    # edit_file's shapes: one old/new pair (every match with replace_all), or several pairs applied in order.
    edits = args.get("edits")
    changes = edits if isinstance(edits, list) and edits else [args]
    many = bool(args.get("replace_all")) and changes is not edits
    text = current
    for change in changes:
        old, new = (change.get("old"), change.get("new", "")) if isinstance(change, dict) else (None, None)
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            return None
        old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")  # as edit_file matches them
        count = text.count(old)
        if count == 0 or (count > 1 and not many):
            return None  # the edit will fail, so there is nothing to preview
        text = text.replace(old, new)
    return text


def diff_preview(args: dict[str, Any], target: Path, shown_path: str) -> str | None:
    """A unified diff of the change, or None when the call isn't a recognisable file write."""
    if not isinstance(args.get("path"), str):
        return None
    exists = target.is_file()
    current = _read(target) if exists else ""
    if current is None:
        return f"(binary or very large file {shown_path}; no preview)"
    after = proposed_text(args, current)
    if after is None:
        return None
    lines = list(difflib.unified_diff(current.splitlines(keepends=True), after.splitlines(keepends=True),
                                      fromfile=f"a/{shown_path}" if exists else "/dev/null",
                                      tofile=f"b/{shown_path}"))
    if not lines:
        return f"(no change to {shown_path})"
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + [f"\n... {len(lines) - MAX_DIFF_LINES} more diff lines\n"]
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


class Checkpoints:
    """One directory per turn that changed files: manifest.json maps each path to its saved copy,
    or to null if the file didn't exist (undo then deletes it)."""

    def __init__(self, state: Path, session_id: str, enabled: bool = True):
        self.base = state / "checkpoints" / session_id
        self.enabled = enabled
        self.turn: Path | None = None
        self._manifest: dict[str, str | None] = {}

    def begin_turn(self) -> None:
        self.turn, self._manifest = None, {}

    def record(self, target: Path) -> None:
        """Save `target` as it is now, unless this turn already saved it."""
        if not self.enabled:
            return
        key = str(target)
        if key in self._manifest:
            return
        if self.turn is None:
            numbers = [int(p.name) for p in self.base.glob("*") if p.name.isdigit()] if self.base.exists() else []
            self.turn = self.base / f"{max(numbers, default=0) + 1:05d}"
            (self.turn / "files").mkdir(parents=True)
        if target.is_file():
            copy = self.turn / "files" / str(len(self._manifest))
            shutil.copy2(target, copy)
            self._manifest[key] = copy.name
        elif target.exists():
            return  # a directory: not something a file tool replaces
        else:
            self._manifest[key] = None
        (self.turn / "manifest.json").write_text(json.dumps(self._manifest, indent=1), encoding="utf-8")

    def history(self) -> list[tuple[str, list[str]]]:
        if not self.base.exists():
            return []
        out = []
        for turn in sorted(p for p in self.base.iterdir() if (p / "manifest.json").exists()):
            out.append((turn.name, list(json.loads((turn / "manifest.json").read_text(encoding="utf-8")))))
        return out

    def undo(self) -> list[str]:
        """Restore the most recent turn's files and drop its checkpoint. Returns what was restored."""
        turns = sorted(p for p in self.base.iterdir() if (p / "manifest.json").exists()) if self.base.exists() else []
        if not turns:
            return []
        turn = turns[-1]
        manifest = json.loads((turn / "manifest.json").read_text(encoding="utf-8"))
        restored = []
        for path, saved in manifest.items():
            target = Path(path)
            if saved is None:
                if target.is_file():
                    target.unlink()
                restored.append(f"removed {target}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(turn / "files" / saved, target)
                restored.append(f"restored {target}")
        shutil.rmtree(turn)
        if self.turn == turn:
            self.begin_turn()
        return restored
