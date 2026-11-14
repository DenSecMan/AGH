"""Plain-text log of the visible chat session, so an agent (or a human) can read it back to debug the harness."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

FILE_NAME = "session.log"


class SessionLog:
    def __init__(self, path: Path):
        self.path = path

    def reset(self, header: str = "") -> None:
        """Start an empty log; called when agh starts and when a fresh session begins (/clear)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(f"# AGH session log  {time.strftime('%Y-%m-%d %H:%M:%S')}  {header}\n",
                                 encoding="utf-8")
        except OSError:
            pass

    def write(self, tag: str, content: Any) -> None:
        text = plain(content).rstrip()
        if not text:
            return
        stamp = time.strftime("%H:%M:%S")
        body = text.replace("\n", "\n    ")
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {tag}: {body}\n")
        except OSError:
            pass  # logging must never break the UI


def plain(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    markup = getattr(content, "markup", None)  # rich Markdown keeps its source here
    if isinstance(markup, str):
        return markup
    text = getattr(content, "plain", None)  # rich Text
    if isinstance(text, str):
        return text
    return str(content)
