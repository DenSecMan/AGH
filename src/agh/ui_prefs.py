"""Terminal UI preferences: colour theme and pane layout. Saved per workspace in state/ui.json.

A layout is a left-to-right list of columns; each column holds one pane, or two stacked top to bottom.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PANES = ("profile", "explorer", "editor", "chat")
DEFAULT_LAYOUT = [["profile", "explorer"], ["editor"], ["chat"]]
DEFAULT_THEME = "textual-dark"
MAX_STACK = 2
ALIASES = {"sidebar": "profile", "profiles": "profile", "tree": "profile", "workspace": "profile",
           "files": "explorer", "file": "explorer", "conversation": "chat", "transcript": "chat",
           "agent": "chat", "harness": "chat"}
STACK_WORDS = {"above": 0, "over": 0, "on-top-of": 0, "below": 1, "under": 1, "beneath": 1, "underneath": 1}
SIDE_WORDS = {"before": 0, "left-of": 0, "after": 1, "right-of": 1}


def themes() -> list[str]:
    from textual.theme import BUILTIN_THEMES

    return list(BUILTIN_THEMES)


def pane_name(word: str) -> str:
    word = word.strip().lower()
    name = ALIASES.get(word, word)
    if name not in PANES:
        raise ValueError(f"unknown pane {word!r}; choose from {', '.join(PANES)}")
    return name


def normalize(layout: Any) -> list[list[str]]:
    """Accept columns as lists or as "top/bottom" strings; every pane exactly once. Raises ValueError."""
    if not isinstance(layout, list) or not layout:
        raise ValueError("layout must be a list of columns")
    columns = []
    for column in layout:
        names = column.replace("+", "/").split("/") if isinstance(column, str) else column
        if not isinstance(names, list) or not 1 <= len(names) <= MAX_STACK:
            raise ValueError(f"a column holds 1 or {MAX_STACK} panes, got {column!r}")
        columns.append([pane_name(str(n)) for n in names])
    flat = [p for c in columns for p in c]
    if len(set(flat)) != len(flat):
        raise ValueError("a pane is named twice")
    missing = [p for p in PANES if p not in flat]
    if missing:
        raise ValueError(f"layout is missing {', '.join(missing)}")
    return columns


def _file(state: Path) -> Path:
    return state / "ui.json"


def load(state: Path) -> dict[str, Any]:
    try:
        data = json.loads(_file(state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    try:
        layout = normalize(data.get("layout"))
    except ValueError:
        layout = [list(c) for c in DEFAULT_LAYOUT]
    theme = data.get("theme")
    return {"theme": theme if theme in themes() else DEFAULT_THEME, "layout": layout}


def save(state: Path, prefs: dict[str, Any]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    _file(state).write_text(json.dumps(prefs, indent=2), encoding="utf-8")


def theme_name(word: str) -> str:
    want = word.strip().lower().replace(" ", "-").replace("_", "-")
    names = themes()
    if want in names:
        return want
    matches = [n for n in names if want in n]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(f"unknown theme {word!r}; choose from {', '.join(names)}")


def _column_of(layout: list[list[str]], pane: str) -> int:
    return next(i for i, c in enumerate(layout) if pane in c)


def move(layout: list[list[str]], pane: str, position: str) -> list[list[str]]:
    """Move one pane. As its own column: left, right, center, a 1-based column number, before/after PANE.
    Stacked in another pane's column: above/below PANE."""
    pane = pane_name(pane)
    rest = [[p for p in c if p != pane] for c in normalize(layout)]
    rest = [c for c in rest if c]
    words = position.strip().lower().replace("to the ", "").replace("on the ", "").split()
    where = words[0] if words else ""
    if len(words) == 2 and (where in SIDE_WORDS or where in STACK_WORDS):
        other = pane_name(words[1])
        if other == pane:
            raise ValueError("a pane cannot move relative to itself")
        index = _column_of(rest, other)
        if where in SIDE_WORDS:
            rest.insert(index + SIDE_WORDS[where], [pane])
        else:
            if len(rest[index]) >= MAX_STACK:
                raise ValueError(f"{other}'s column already holds {MAX_STACK} panes")
            column = rest[index]
            column.insert(column.index(other) + STACK_WORDS[where], pane)
        return rest
    if len(words) != 1:
        where = "?"
    if where in ("left", "leftmost", "first", "start"):
        rest.insert(0, [pane])
    elif where in ("right", "rightmost", "last", "end"):
        rest.append([pane])
    elif where in ("center", "centre", "middle"):
        rest.insert(len(rest) // 2, [pane])
    elif where.isdigit() and 1 <= int(where) <= len(rest) + 1:
        rest.insert(int(where) - 1, [pane])
    else:
        raise ValueError(f"unknown position {position!r}; use left, right, center, a column number, "
                         "before/after PANE, or above/below PANE")
    return rest


def full_layout(columns: list[Any]) -> list[list[str]]:
    """A layout from the columns named, e.g. ["profile/explorer", "editor", "chat"]; panes not named get
    their own columns at the end, in default order."""
    named = [c.replace("+", "/").split("/") if isinstance(c, str) else c for c in columns]
    seen = {pane_name(p) for c in named for p in c}
    named += [[p] for p in PANES if p not in seen]
    return normalize(named)


def change(state: Path, theme: str | None = None, pane: str | None = None, position: str | None = None,
           layout: list[Any] | None = None, reset: bool = False) -> dict[str, Any]:
    """Validate and save a change; returns the new preferences. Raises ValueError on bad input."""
    prefs = {"theme": DEFAULT_THEME, "layout": [list(c) for c in DEFAULT_LAYOUT]} if reset else load(state)
    if theme:
        prefs["theme"] = theme_name(theme)
    if layout:
        prefs["layout"] = full_layout(layout)
    if pane or position:
        if not (pane and position):
            raise ValueError("moving a pane needs both pane and position")
        prefs["layout"] = move(prefs["layout"], pane, position)
    save(state, prefs)
    return prefs


def describe(prefs: dict[str, Any]) -> str:
    columns = " | ".join(" above ".join(c) for c in prefs["layout"])
    return f"theme {prefs['theme']}; columns left to right: {columns}"
