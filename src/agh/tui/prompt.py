"""The message box: multi-line, with history. Enter sends; Ctrl+J (or Shift+Enter where the terminal reports it)
starts a new line; Up and Down on the first or last line walk through earlier messages."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from textual import events
from textual.message import Message
from textual.widgets import TextArea

HISTORY_MAX = 500


class PromptArea(TextArea):
    class Submitted(Message):
        def __init__(self, area: "PromptArea", value: str):
            super().__init__()
            self.area, self.value = area, value

        @property
        def control(self) -> "PromptArea":
            return self.area

    def __init__(self, history_file: Path | None = None, **kwargs):
        super().__init__(soft_wrap=True, show_line_numbers=False, tab_behavior="focus", **kwargs)
        self.history_file = history_file
        self.sent: list[str] = self._load_history()
        self._index: int | None = None  # position while walking history; None means editing a new message
        self._draft = ""
        # Set by the app while the slash menu is open: gets each key first and returns True if it used it.
        self.menu_handler: Callable[[str], bool] | None = None
        # Set by the app: saves a clipboard image and returns the text to insert for it, or None if there is none.
        self.image_handler: Callable[[], str | None] | None = None

    def _paste_image(self) -> bool:
        reference = self.image_handler() if self.image_handler else None
        if reference:
            self.insert(reference + " ")
        return bool(reference)

    async def _on_paste(self, event: events.Paste) -> None:
        # A terminal pastes nothing for an image-only clipboard, so an empty paste may mean "paste the image".
        if not event.text.strip() and self._paste_image():
            event.stop()
            event.prevent_default()
            return
        await super()._on_paste(event)

    # Input-compatible surface, so callers and tests can treat it like the one-line box it replaced.
    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        self.load_text(text)
        self.cursor_position = len(text)

    @property
    def cursor_position(self) -> int:
        row, col = self.cursor_location
        lines = self.text.split("\n")
        return sum(len(line) + 1 for line in lines[:row]) + col

    @cursor_position.setter
    def cursor_position(self, offset: int) -> None:
        lines = self.text.split("\n")
        row = 0
        while row < len(lines) - 1 and offset > len(lines[row]):
            offset -= len(lines[row]) + 1
            row += 1
        self.move_cursor((row, min(offset, len(lines[row]))))

    async def _on_key(self, event: events.Key) -> None:
        if self.menu_handler is not None and self.menu_handler(event.key):
            event.stop()
            event.prevent_default()
            return
        if event.key in ("ctrl+v", "alt+v") and self._paste_image():  # else Ctrl+V pastes text as before
            event.stop()
            event.prevent_default()
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self._submit()
            return
        if event.key in ("ctrl+j", "shift+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        row = self.cursor_location[0]
        if event.key == "up" and row == 0 and self.sent:
            event.stop()
            event.prevent_default()
            self._walk(-1)
            return
        if event.key == "down" and row == self.document.line_count - 1 and self._index is not None:
            event.stop()
            event.prevent_default()
            self._walk(1)
            return
        await super()._on_key(event)

    def _walk(self, step: int) -> None:
        if self._index is None:
            self._draft, self._index = self.text, len(self.sent)
        self._index = max(0, min(len(self.sent), self._index + step))
        if self._index == len(self.sent):
            text, self._index = self._draft, None
        else:
            text = self.sent[self._index]
        self.value = text

    def _submit(self) -> None:
        text = self.text
        if text.strip() and (not self.sent or self.sent[-1] != text):
            self.sent.append(text)
            del self.sent[:-HISTORY_MAX]
            self._save_history()
        self._index = None
        self.post_message(self.Submitted(self, text))

    def _load_history(self) -> list[str]:
        try:
            data = json.loads(self.history_file.read_text(encoding="utf-8")) if self.history_file else []
            return [str(x) for x in data][-HISTORY_MAX:]
        except (OSError, ValueError):
            return []

    def _save_history(self) -> None:
        if not self.history_file:
            return
        try:  # /key lines carry secrets, so they never reach the history file
            kept = [h for h in self.sent if not h.lstrip().lower().startswith("/key")]
            self.history_file.write_text(json.dumps(kept), encoding="utf-8")
        except OSError:
            pass
