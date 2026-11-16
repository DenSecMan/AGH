"""The Textual interface: chat, workspace tree, live plan, approvals and slash commands."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, DirectoryTree, Footer, Header, Input, OptionList, Static, TextArea, Tree
from textual.widgets.option_list import Option

from agh import commands, goals, llm, sounds, ui_prefs
from agh.engine import Engine, Event
from agh.policy import MODES
from agh.tui import clipboard_image
from agh.session_log import FILE_NAME as SESSION_LOG, SessionLog
from agh.sessions import Session
from agh.tui.prompt import PromptArea
from agh.workspace import KINDS, Workspace


class Message(Static):
    """A transcript entry. Rendered Markdown isn't plain text, so selecting any part of an agent reply copies
    the reply's Markdown source."""

    def __init__(self, content: Any = "", **kwargs: Any):
        super().__init__(content, **kwargs)
        self.source = content.markup if isinstance(content, Markdown) else None

    def update(self, content: Any = "", *, layout: bool = True) -> None:
        self.source = content.markup if isinstance(content, Markdown) else None
        super().update(content, layout=layout)

    def get_selection(self, selection: Any) -> tuple[str, str] | None:
        if self.source is not None:
            return self.source, "\n"
        return super().get_selection(selection)


class ApprovalScreen(ModalScreen[str]):
    """Ask the human whether a tool call may run."""

    BINDINGS = [Binding("y", "answer('once')", "Allow"), Binding("a", "answer('session')", "Always"),
                Binding("p", "answer('always')", "Save rule"), Binding("n,escape", "answer('deny')", "Deny")]

    def __init__(self, request: dict[str, Any]):
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        args = json.dumps(self.request["args"], indent=2, default=str)
        with Vertical(id="approval"):
            yield Static(Text(f"{self.request['agent']} wants to run {self.request['tool']}", style="bold"))
            yield Static(Text(self.request.get("description", ""), style="dim"))
            with VerticalScroll(id="approval-args"):
                diff = self.request.get("diff")
                yield Static(Syntax(diff, "diff", word_wrap=True) if diff else Syntax(args, "json", word_wrap=True))
            grant, rule = self.request.get("grant"), self.request.get("rule")
            note = f"Always (a) allows: {grant}" if grant else "This call changes agent rules, so it asks every time."
            if rule:
                note += f"\nSave rule (p) keeps it in permissions.yaml: allow {rule}"
            yield Static(Text(note, style="dim italic"))
            with Horizontal(id="approval-buttons"):
                yield Button("Allow once (y)", id="once", variant="success")
                if grant:
                    yield Button("Always this session (a)", id="session", variant="warning")
                if rule:
                    yield Button("Save rule (p)", id="always", variant="primary")
                yield Button("Deny (n)", id="deny", variant="error")

    def action_answer(self, answer: str) -> None:
        if (answer == "session" and not self.request.get("grant")) or (answer == "always" and not self.request.get("rule")):
            return
        self.dismiss(answer)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)


class ConfirmCloseScreen(ModalScreen[str]):
    """Ask what to do with unsaved editor changes: save, discard or cancel."""

    BINDINGS = [Binding("s", "answer('save')", "Save"), Binding("d", "answer('discard')", "Discard"),
                Binding("escape,c", "answer('cancel')", "Cancel")]

    def __init__(self, name: str):
        super().__init__()
        self.file_name = name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(Text(f"{self.file_name} has unsaved changes.", style="bold"))
            with Horizontal(id="approval-buttons"):
                yield Button("Save (s)", id="save", variant="success")
                yield Button("Discard (d)", id="discard", variant="error")
                yield Button("Cancel (c)", id="cancel")

    def action_answer(self, answer: str) -> None:
        self.dismiss(answer)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id)


LANGS = {".py": "python", ".md": "markdown", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
         ".js": "javascript", ".html": "html", ".css": "css", ".sql": "sql", ".sh": "bash", ".rs": "rust",
         ".go": "go", ".java": "java", ".xml": "xml"}
NO_FILE = "No file open. Pick one in the Explorer."


class Splitter(Widget):
    """Vertical bar between columns: drag it to resize the neighbouring column that isn't the editor's."""

    MIN_WIDTH = 16

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self._drag: tuple[str, int, int, int] | None = None  # (column key, sign, start x, start width)

    def render(self) -> str:
        return "\u2502\n" * self.size.height

    def on_mouse_down(self, event: events.MouseDown) -> None:
        found = self.app.splitter_target(self)
        if found:
            key, sign, width = found
            self._drag = (key, sign, event.screen_x, width)
            self.capture_mouse()
            self.add_class("dragging")

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag is None:
            return
        key, sign, start_x, start_w = self._drag
        limit = max(self.MIN_WIDTH, self.screen.size.width - 30)
        self.app.set_column_width(key, max(self.MIN_WIDTH, min(limit, start_w + sign * (event.screen_x - start_x))))

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._drag is not None:
            self._drag = None
            self.release_mouse()
            self.remove_class("dragging")


class Editor(Vertical):
    """Middle pane: view and edit the file chosen in the Explorer or profile tree."""

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.path: Path | None = None
        self._saved = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="editor-bar"):
            yield Static(Text(NO_FILE, style="dim"), id="editor-title")
            yield Button("Save", id="editor-save", variant="success", disabled=True)
            yield Button("Close", id="editor-close", disabled=True)
        yield TextArea("", id="editor-text", show_line_numbers=True, read_only=True)

    @property
    def text_area(self) -> TextArea:
        return self.query_one("#editor-text", TextArea)

    @property
    def dirty(self) -> bool:
        return self.path is not None and not self.text_area.read_only and self.text_area.text != self._saved

    def load(self, path: Path) -> None:
        try:
            text, read_only = path.read_text(encoding="utf-8"), False
        except (OSError, UnicodeDecodeError) as exc:
            text, read_only = f"Cannot display this file: {exc}", True
        self.path, self._saved = path, text
        area = self.text_area
        area.load_text(text)
        area.read_only = read_only
        try:
            area.language = LANGS.get(path.suffix.lower())
        except Exception:  # tree-sitter grammar not installed: plain text is fine
            area.language = None
        self.query_one("#editor-save", Button).disabled = read_only
        self.query_one("#editor-close", Button).disabled = False
        self.refresh_title()

    def refresh_title(self) -> None:
        if self.path is None:
            return
        mark = " [modified]" if self.dirty else ""
        self.query_one("#editor-title", Static).update(
            Text(f"{self.path.name}{mark}  ", style="bold") + Text(str(self.path.parent), style="dim"))

    def save(self) -> bool:
        if self.path is None or self.text_area.read_only:
            return False
        self.path.write_text(self.text_area.text, encoding="utf-8", newline="")
        self._saved = self.text_area.text
        self.refresh_title()
        return True

    def close(self) -> None:
        self.path, self._saved = None, ""
        self.text_area.load_text("")
        self.text_area.read_only = True
        self.query_one("#editor-save", Button).disabled = True
        self.query_one("#editor-close", Button).disabled = True
        self.query_one("#editor-title", Static).update(Text(NO_FILE, style="dim"))

    @on(TextArea.Changed, "#editor-text")
    def _changed(self) -> None:
        self.refresh_title()


def _short(n: int) -> str:
    """632700 -> 632.7k, 1_250_000 -> 1.2M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # braille frames shown in the status bar while a run is active
LONG_RUN_S = 15 # runs longer than this ring the bell when they finish


class AghApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "AGH"
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+c", "cancel", "Cancel run", priority=True),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("shift+tab", "cycle_mode", "Mode", priority=True),
        Binding("ctrl+r", "reload_tree", "Reload"),
        Binding("ctrl+s", "save_file", "Save file", priority=True),
        Binding("ctrl+w", "close_file", "Close file", priority=True),
        Binding("f6", "move_pane", "Move panes"),
        Binding("f7", "reset_layout", "Reset layout"),
        Binding("f8", "toggle_details", "Tool details"),
        Binding("ctrl+e", "cycle_effort", "Effort", priority=True),
        # Ctrl+M is the same byte as Enter in terminals, so the model key is Ctrl+O.
        Binding("ctrl+o", "cycle_model", "Model", priority=True),
        Binding("tab", "tab_complete", "Complete", show=False, priority=True),
    ]

    def __init__(self, ws: Workspace, agent: str | None = None, session: Session | None = None, model: Any = None,
                 start_dir: Path | None = None):
        super().__init__()
        self.ws = ws
        self.start_dir = Path(start_dir or ws.project).resolve()  # the Explorer shows where agh was started
        self.injected_model = model
        name = (session.agent if session else None) or agent or ws.default_agent
        # File edits in the project run unasked (/undo reverts them); shell and network calls still ask, so a
        # page or file the agent reads can't get private files sent anywhere unattended. Shift+Tab changes mode.
        self.engine = Engine(ws, name, session=session, approver=self._approve, model=model, mode="edits")
        self._run_task: asyncio.Task | None = None
        self._tab_cycle: list[str] = []
        self._agent_busy = False
        self._live: Static | None = None
        self._live_text = ""
        self._dirty = False
        self._tool_rows: dict[str, tuple[Static, str]] = {}
        self._pending: list[str] = []  # messages typed while the agent was working, sent in order
        self._run_started = 0.0
        self._last_event = 0.0  # when the engine last emitted anything; drives the "waiting" hint
        self._menu: list[tuple[str, str, str]] = []  # (text after choosing, label, description)
        self._menu_closed_for: str | None = None  # Escape hides the menu until the text changes
        self.host = commands.Host(ws=ws, engine=lambda: self.engine, switch_agent=self._switch,
                                  send_to_agent=self._send_to_agent, log=self._log, quit=self.exit, busy=self._busy,
                                  apply_ui=self.apply_ui, open_setup=self.open_setup,
                                  open_search_setup=self.open_search_setup, goal=self._goal_command)
        self.goal: goals.Goal | None = None
        self._layout: list[list[str]] = [list(c) for c in ui_prefs.DEFAULT_LAYOUT]
        self._hidden: set[str] = set()
        self._widths: dict[str, int] = {}  # column key -> width set by dragging a splitter
        self._visible_splitters: list[Splitter] = []
        self.session_log = SessionLog(ws.root / SESSION_LOG)  # readable by agents to debug the harness
        self._reset_session_log()

    def _reset_session_log(self) -> None:
        self.session_log.reset(f"agent={self.engine.agent_name} session={self.engine.session.id}")

    # ---- layout ----------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield Tree("workspace", id="tree")
                yield Static("", id="plan")
            with Vertical(id="explorer-pane"):
                yield Static("Explorer", id="explorer-title")
                yield DirectoryTree(self.start_dir, id="explorer")
            yield Editor(id="editor")
            yield Splitter(id="split-1")
            yield Splitter(id="split-2")
            yield Splitter(id="split-3")
            with Vertical(id="chat"):
                yield VerticalScroll(id="transcript", classes="compact")
                with Vertical(id="bottom"):
                    yield OptionList(id="suggest")
                    yield PromptArea(id="prompt", history_file=self.ws.state / "prompt_history.json",
                                     placeholder="Message the agent (Enter sends, Ctrl+J new line, @path attaches "
                                                 "a file, /help for commands)")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.apply_ui(ui_prefs.load(self.ws.state))
        self.theme_changed_signal.subscribe(self, lambda _theme: self._save_layout())  # Ctrl+P theme picks stick
        self._fill_tree()
        self._update_status()
        self.set_interval(0.08, self._flush_stream)
        self.set_interval(0.2, self._tick_busy)
        # Follow new output; scrolling up releases the anchor, scrolling back to the bottom restores it.
        self.query_one("#transcript", VerticalScroll).anchor()
        prompt = self.query_one("#prompt", PromptArea)
        prompt.menu_handler = self._menu_key
        prompt.image_handler = self._paste_clipboard_image
        menu = self.query_one("#suggest", OptionList)
        menu.can_focus = False
        menu.display = False
        prompt.focus()
        self._log(Text(f"Workspace {self.ws.root}\nTalking to {self.engine.agent_name}. /help lists commands; "
                       "/build asks the architect to create agents, skills, tools or connectors.", style="dim"))
        from agh import onboarding

        if self.injected_model is None and onboarding.needs_setup(self.ws):
            self.open_setup()
        elif self.injected_model is None and onboarding.needs_search_setup(self.ws):
            self.open_search_setup()
        for message in self.engine.session.messages:  # show history when resuming
            if message.type == "human":
                self._mount(Text(f"> {message.content}"), "user")
            elif message.type == "ai" and message.content:
                from agh.engine import text_of
                self._mount(Markdown(text_of(message)))

    def _fill_tree(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.clear()
        tree.root.set_label(f"profile: {self.ws.profile}")
        tree.root.expand()
        for kind in KINDS:
            branch = tree.root.add(f"{kind} ({len(self.ws.names(kind))})", expand=kind == "agents")
            for name in self.ws.names(kind):
                base = self.ws.element(kind, name)
                from_default = self.ws.is_default(base)
                node = branch.add(f"{name} (default)" if from_default and self.ws.profile != "default" else name)
                for file in sorted(p for p in base.rglob("*") if p.is_file() and self._showable(p, base)):
                    node.add_leaf(str(file.relative_to(base)).replace("\\", "/"), data=file)

    @staticmethod
    def _showable(path: Path, base: Path) -> bool:
        parts = path.relative_to(base).parts
        return not any(p.startswith(".") or p in ("__pycache__", "results") for p in parts) and path.suffix != ".db"

    @on(Tree.NodeSelected)
    def _open_file(self, event: Tree.NodeSelected) -> None:
        if isinstance(event.node.data, Path):
            self._open_in_editor(event.node.data)

    @on(DirectoryTree.FileSelected, "#explorer")
    def _explorer_file(self, event: DirectoryTree.FileSelected) -> None:
        event.stop()
        self._open_in_editor(Path(event.path))

    # ---- editor ----------------------------------------------------------------------------

    def _after_unsaved(self, then: Any) -> None:
        """Run then() once the open file is clean, saved or discarded; ask first if it has edits."""
        editor = self.query_one("#editor", Editor)
        if not editor.dirty:
            then()
            return

        def answered(answer: str | None) -> None:
            if answer == "save":
                editor.save()
            if answer in ("save", "discard"):
                then()

        self.push_screen(ConfirmCloseScreen(editor.path.name), answered)

    def _open_in_editor(self, path: Path) -> None:
        editor = self.query_one("#editor", Editor)
        if editor.path != path:
            self._after_unsaved(lambda: editor.load(path))

    @on(Button.Pressed, "#editor-save")
    def _save_pressed(self) -> None:
        self.action_save_file()

    @on(Button.Pressed, "#editor-close")
    def _close_pressed(self) -> None:
        self.action_close_file()

    def action_save_file(self) -> None:
        editor = self.query_one("#editor", Editor)
        if editor.save():
            self.notify(f"Saved {editor.path.name}")

    def action_close_file(self) -> None:
        self._after_unsaved(self.query_one("#editor", Editor).close)

    # ---- transcript ------------------------------------------------------------------------

    def _mount(self, content: Any, *classes: str) -> Static:
        widget = Message(content, classes=" ".join(("msg", *classes)))
        self.session_log.write(classes[0] if classes else "agent", content)
        self.query_one("#transcript", VerticalScroll).mount(widget)  # the anchor keeps the view at the bottom
        return widget

    @staticmethod
    def _tool_head(label: Text, mark: str, color: str, detail: str = "") -> Text:
        return Text(f"{mark} ", style=color) + label + (Text(f"  {detail}", style="dim") if detail else Text(""))

    @staticmethod
    def _io_row(tag: str, body: str) -> Text:
        return Text(f"{tag:<4}", style="bold") + Text(body, style="dim")

    def _mount_tool(self, event: Event, extra: tuple[str, ...]) -> tuple[Vertical, Static, Static, Text]:
        """Status dot + tool name + its main argument on one line. The full input and output rows are shown only in
        detail mode (F8); in compact mode a failed call still shows its output."""
        args = event.data["args"] if isinstance(event.data["args"], dict) else {"input": event.data["args"]}
        label = Text(f"{event.agent}: " if extra else "", style="dim") + Text(event.data["tool"], style="bold")
        main = next((args[k] for k in ("command", "path", "query", "url", "pattern", "name", "agent") if k in args),
                    None)
        shown = str(main) if main is not None else json.dumps(args, default=str)
        summary = str(args.get("description") or shown)
        label += Text("  " + " ".join(summary.split())[:100])
        self.session_log.write(f"tool {event.agent}/{event.data['tool']}", f"IN {json.dumps(args, default=str)}")
        head = Static(self._tool_head(label, "●", "yellow", "running"), classes="tool-head")
        inp = Static(self._io_row("IN", shown[:400]), classes="tool-io")
        out = Static("", classes="tool-io tool-out pending")
        block = Vertical(head, inp, out, classes=" ".join(("msg", "tool", *extra)))
        self.query_one("#transcript", VerticalScroll).mount(block)
        return block, head, out, label

    def action_toggle_details(self) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        transcript.toggle_class("compact")
        self.notify("Tool details " + ("hidden" if transcript.has_class("compact") else "shown"))

    def _log(self, content: Any) -> None:
        self._mount(content if not isinstance(content, str) else Text(content))

    def _flush_stream(self) -> None:
        """Rendering markdown on every token is slow, so stream updates are batched on a timer."""
        if self._dirty and self._live is not None:
            self._live.update(Markdown(self._live_text))
            self._dirty = False

    def _tick_busy(self) -> None:
        """Animate the status bar while a run is active so a slow model never looks frozen."""
        if self._busy():
            self._update_status()

    def _end_stream(self, final: str | None = None) -> None:
        if self._live is not None:
            text = final if final is not None else self._live_text
            self._live.update(Markdown(text))
            self.session_log.write("agent", text)
        self._live, self._live_text, self._dirty = None, "", False

    def _update_status(self) -> None:
        engine = self.engine
        r = engine.result
        if self._busy():
            now_t = time.monotonic()
            frame = SPINNER[int(now_t * 10) % len(SPINNER)]
            state = f"{frame} working {int(now_t - self._run_started)}s (Ctrl+C cancels)"
            quiet = int(now_t - self._last_event)
            if quiet >= 5:
                state += f", waiting on model {quiet}s"
        else:
            state = "ready"
        now = engine.describe()
        effort_colors = {"high": "red", "medium": "yellow", "low": "green"}
        mode_colors = {"default": "green", "plan": "cyan", "edits": "yellow", "auto": "dark_orange", "full-auto": "red"}
        busy = self._busy()
        sep = Text(" | ", style="dim")

        def field(label: str, value: str, style: str) -> Text:
            return Text(f"{label}=", style="bold dark_orange") + Text(value, style=style)

        self.query_one("#status", Static).update(
            field("Provider", str(now["provider"]), "bold")
            + sep + field("Model", str(now["model"]), "bold")
            + sep + field("Effort", str(now["effort"]), effort_colors.get(str(now["effort"]), "white"))
            + sep + field("Mode", engine.mode, mode_colors.get(engine.mode, "white"))
            + sep + self._token_meter()
            + sep + field("Status", state, "yellow" if busy else "green"))

    def _token_meter(self) -> Text:
        """Session ↑sent ↓received $cost."""
        usage = self.engine.session.usage

        def money(value: float) -> str:
            return f"${value:.2f}" if value >= 0.01 or value == 0 else f"${value:.4f}"

        def group(label: str, inp: int, out: int, cost: float) -> Text:
            return (Text(f"{label} ", style="bold dark_orange") + Text(f"↑{_short(inp)} ", style="cyan")
                    + Text(f"↓{_short(out)} ", style="green") + Text(money(cost), style="bold"))

        return (group("Session", int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)),
                      float(usage.get("cost_usd", 0.0))))
        self.sub_title = f"{engine.agent_name} / {engine.session.id}"

    def _busy(self) -> bool:
        return self._agent_busy

    def _handle(self, event: Event) -> None:
        self._last_event = time.monotonic()
        sub = event.agent != self.engine.agent_name
        extra = ("subagent",) if sub else ()
        if sub and event.kind in ("text", "text_end", "todos"):
            return  # parallel subagents would interleave in one stream and overwrite the parent's plan
        if event.kind == "subagent_end":
            if event.data["text"]:
                self._end_stream()
                self._mount(Text(f"{event.agent}: {event.data['text']}"), "subagent")
        elif event.kind == "text":
            if self._live is None:
                self._live = self._mount("", *extra)
                self._live_text = ""
            self._live_text += event.data["text"]
            self._dirty = True
        elif event.kind == "text_end":
            self._end_stream()
        elif event.kind == "thinking":
            if sub:
                return
            self._end_stream()
            think = " ".join(str(event.data.get("text", "")).split())
            if think:
                self._mount(Text("∙ Thought  ", style="bold") +
                            Text(think[:240] + ("..." if len(think) > 240 else ""), style="dim italic"), "thinking")
        elif event.kind == "tool_start":
            self._end_stream()
            self._tool_rows[event.data["id"]] = self._mount_tool(event, extra)
        elif event.kind == "tool_end":
            entry = self._tool_rows.pop(event.data["id"], None)
            if entry is not None:
                block, head, out, label = entry
                error = event.data.get("error")
                head.update(self._tool_head(label, "✗" if error else "●", "red" if error else "green",
                                            f"{event.data.get('duration', '?')}s"))
                body = f"failed: {error}" if error else str(event.data.get("output") or "(no output)")
                self.session_log.write(f"tool {event.agent}/{event.data.get('tool', '?')}",
                                       f"OUT ({event.data.get('duration', '?')}s) {body}")
                lines = body.rstrip().splitlines() or [""]
                if len(lines) > 8:
                    lines = lines[:8] + [f"... {len(lines) - 8} more lines"]
                out.update(self._io_row("OUT", "\n".join(line[:200] for line in lines)))
                out.remove_class("pending")
                if error:
                    block.add_class("tool-error")
        elif event.kind == "todos":
            marks = {"done": "[x]", "in_progress": "[>]"}
            self.query_one("#plan", Static).update(Text("Plan\n" + "\n".join(
                f"{marks.get(t.get('status'), '[ ]')} {t.get('content', '')}" for t in event.data["todos"])))
        elif event.kind == "ui":
            self.apply_ui(event.data)
        elif event.kind == "notice":
            self._mount(Text(event.data["text"]), "notice")
        elif event.kind == "error":
            self._end_stream()
            self._mount(Text(f"Stopped: {event.data['text']}"), "error")
        elif event.kind == "done":
            if sub:
                return
            if self._live is not None:
                self._end_stream(event.data.get("text") or None)
            self.action_reload_tree()  # the turn may have written files
        self._update_status()

    # ---- provider setup --------------------------------------------------------------------

    def open_setup(self) -> None:
        """The provider wizard; the harness carries on with whatever it saves."""
        from agh import onboarding
        from agh.tui.setup import SetupScreen

        def done(saved: bool | None) -> None:
            if saved:
                commands._forget_model(self.engine)
                self.engine.model_override, self.engine.model_chain = None, []
                provider, model = llm.selection(self.ws.config)
                self._log(Text(f"Using {provider} / {model}. /setup runs this again; /provider and /model "
                               "switch without it.", style="green"))
                if onboarding.needs_search_setup(self.ws):
                    self.open_search_setup()
                    return
            else:
                self._log(Text("No provider set up yet. Run /setup when you're ready (or /key and /provider).",
                               style="yellow"))
            self._update_status()
            self.query_one("#prompt", PromptArea).focus()

        self.push_screen(SetupScreen(self.ws), done)

    def open_search_setup(self) -> None:
        """Ask for a web search service (Tavily, Brave) and its key."""
        from agh.tui.setup import SearchSetupScreen

        def done(service: str | None) -> None:
            if service:
                self._log(Text(f"Web search uses {service}. /search changes it.", style="green"))
            else:
                self._log(Text("Web search uses the free DuckDuckGo fallback. Run /search to add a Tavily or Brave "
                               "key later.", style="yellow"))
            self.query_one("#prompt", PromptArea).focus()

        self.push_screen(SearchSetupScreen(self.ws), done)

    # ---- slash menu ------------------------------------------------------------------------

    @on(TextArea.Changed, "#prompt")
    def _prompt_changed(self) -> None:
        self._refresh_menu()

    def _refresh_menu(self) -> None:
        """Show the commands (or a command's next arguments) that fit what is typed after /."""
        text = self.query_one("#prompt", PromptArea).value
        menu = self.query_one("#suggest", OptionList)
        items = [] if text == self._menu_closed_for else commands.suggestions(text, self.ws)
        if text != self._menu_closed_for:
            self._menu_closed_for = None
        if items != self._menu:
            self._menu = items
            menu.clear_options()
            menu.add_options([Option(Text(label, style="bold") + (Text(f"  {note}", style="dim") if note else Text("")))
                              for _, label, note in items])
            # Commands start highlighted; arguments wait for Down, so Enter still sends what was typed.
            menu.highlighted = 0 if items and " " not in text else None
        menu.display = bool(items)

    def _menu_accept(self, index: int) -> None:
        prompt = self.query_one("#prompt", PromptArea)
        prompt.value = self._menu[index][0] + " "
        self._refresh_menu()

    def _menu_key(self, key: str) -> bool:
        menu = self.query_one("#suggest", OptionList)
        self._refresh_menu()
        if not menu.display or not self._menu:
            return False
        if key in ("down", "up"):
            if menu.highlighted is None:
                menu.highlighted = 0 if key == "down" else len(self._menu) - 1
            else:
                menu.highlighted = (menu.highlighted + (1 if key == "down" else -1)) % len(self._menu)
            return True
        if key == "escape":
            self._menu_closed_for = self.query_one("#prompt", PromptArea).value
            menu.display = False
            return True
        if key == "enter" and menu.highlighted is not None:
            chosen = self._menu[menu.highlighted][0]
            typed = self.query_one("#prompt", PromptArea).value.strip()
            # Enter on a whole command sends it, unless it has options to pick next (then show them).
            if typed != chosen or (" " not in typed and commands.suggestions(chosen + " ", self.ws)):
                self._menu_accept(menu.highlighted)
                return True
        return False

    @on(OptionList.OptionSelected, "#suggest")
    def _menu_clicked(self, event: OptionList.OptionSelected) -> None:
        self._menu_accept(event.option_index)
        self.query_one("#prompt", PromptArea).focus()

    # ---- running ---------------------------------------------------------------------------

    @on(PromptArea.Submitted, "#prompt")
    async def _submitted(self, event: PromptArea.Submitted) -> None:
        text = event.value.strip()
        event.area.value = ""
        if not text:
            return
        if text.startswith("/"):
            self._mount(Text(commands.mask_key_command(text)), "user")
            self._run_command(text)
            return
        if self.goal is not None and self.goal.waiting_for_user and not self._busy():
            self._mount(Text(f"> {text}"), "user")
            self._goal_reply(text)
            return
        if self._busy():
            self._pending.append(text)
            self._mount(Text(f"> {text}\n(queued: sent when the current run finishes; Ctrl+C cancels the run "
                             "and the queue)"), "user")
            return
        self._mount(Text(f"> {text}"), "user")
        self._start_run(text)

    def _paste_clipboard_image(self) -> str | None:
        """Save the clipboard's image under state/pasted/ and return the @mention that points the agent at it.

        The mention is an absolute path: tools resolve relative paths against the project folder agh was started
        in, which is usually not the workspace that holds state/."""
        target = self.ws.state / "pasted" / f"image-{time.strftime('%Y%m%d-%H%M%S')}.png"
        if not clipboard_image.save_clipboard_image(target):
            return None
        self.notify("Image pasted. The agent will look at it when you send.", timeout=3)
        return f"@{target.resolve().as_posix()}"

    @work(exclusive=False)
    async def _run_command(self, line: str) -> None:
        await commands.dispatch(self.host, line)
        self.action_reload_tree()  # commands such as /init write files the Explorer should show
        self._update_status()

    def _start_run(self, text: str) -> None:
        expanded, attached, problems = commands.expand_mentions(text, self.engine.ctx())
        for problem in problems:
            self._mount(Text(problem), "notice")
        if attached:
            self._mount(Text("attached: " + ", ".join(attached)), "notice")
        self.query_one("#transcript", VerticalScroll).anchor()  # sending a message jumps back to following
        self._agent_busy = True
        self._run_started = self._last_event = time.monotonic()
        self._run_task = asyncio.create_task(self._consume(expanded))
        self._update_status()

    async def _consume(self, text: str) -> None:
        cancelled = False
        try:
            async for event in self.engine.stream(text):
                self._handle(event)
        except asyncio.CancelledError:
            cancelled = True
            self._end_stream()
            self._pending.clear()
            self._mount(Text("Cancelled."), "notice")
            if self.goal is not None and self.goal.state == "running":
                self.goal.state, self.goal.note = "stopped", "Cancelled with Ctrl+C."
                self._mount(Text("Goal stopped."), "notice")
        finally:
            self._agent_busy = False
            self._update_status()
        goal_next = self._goal_after_turn()
        finished = not cancelled and not self._pending and goal_next != "continue"
        if finished and goal_next != "budget":
            sounds.done()  # the user may have looked away
        if finished and time.monotonic() - self._run_started > LONG_RUN_S:
            self.notify(f"{self.engine.agent_name} finished", timeout=4)
        if self._pending and not self._busy():
            self._start_run(self._pending.pop(0))  # the user's own messages go before the goal's next turn
        elif goal_next == "continue" and not self._busy():
            self._start_run(self.goal.continue_prompt())

    # ---- /goal -----------------------------------------------------------------------------

    def _goal_command(self, rest: str) -> None:
        goal = self.goal
        if not rest:
            self._mount(Text(goal.describe() if goal else "No goal. Start one with /goal OBJECTIVE."), "notice")
        elif rest.lower() in ("stop", "clear", "cancel"):
            if goal is None or not goal.active:
                self._mount(Text("No goal is running."), "notice")
                return
            goal.state, goal.note = "stopped", "Stopped with /goal stop."
            self._mount(Text("Goal stopped" + (" after the current turn." if self._busy() else ".")), "notice")
        elif goal is not None and goal.active:
            self._mount(Text("A goal is already active. /goal stop ends it first.\n" + goal.describe(),
                             style="yellow"), "notice")
        elif self._busy():
            self._mount(Text("Still working. Wait for the run to finish or press Ctrl+C.", style="yellow"), "notice")
        else:
            self.goal = goals.Goal(rest)
            self._mount(Text(goals.ASK_BUDGET, style="bold"), "notice")
            sounds.attention()

    def _goal_reply(self, text: str) -> None:
        goal = self.goal
        try:
            message = goal.answer(text)
        except goals.BudgetError as exc:
            self._mount(Text(str(exc), style="yellow"), "notice")
            return
        self._mount(Text(message), "notice")
        if goal.state == "running":
            self._start_run(goal.first_prompt() if goal.turns == 0 else goal.continue_prompt())

    def _goal_after_turn(self) -> str:
        """Count the turn that just ended against the goal; tell the user when it finishes or needs them."""
        goal = self.goal
        if goal is None or goal.state != "running":
            return ""
        r = self.engine.result
        nxt = goal.after_turn(r.input_tokens + r.output_tokens, r.text, r.stop_reason or "")
        if nxt == "complete":
            self._mount(Text(f"Goal complete in {goal.turns} turns, {goal.spent:,} tokens.", style="green"), "notice")
        elif nxt in ("blocked", "stopped"):
            self._mount(Text(goal.describe(), style="yellow"), "notice")
        elif nxt == "budget":
            self._mount(Text(goal.budget_question(), style="bold"), "notice")
        if nxt == "budget":
            sounds.attention()  # the goal is paused waiting for the user
        return nxt

    async def _approve(self, request: dict[str, Any]) -> str:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        sounds.attention()  # a run is blocked until someone answers
        self.push_screen(ApprovalScreen(request), lambda answer: future.done() or future.set_result(answer or "deny"))
        return await future

    async def _switch(self, agent: str, session: Session | None) -> None:
        if self._busy():
            self.action_cancel()
        mode = self.engine.mode
        self.engine.close()
        self.engine = Engine(self.ws, agent, session=session, approver=self._approve, model=self.injected_model, mode=mode)
        await self.query_one("#transcript", VerticalScroll).remove_children()
        self.query_one("#plan", Static).update("")
        if session is None:  # /clear or a fresh agent: start a new log
            self._reset_session_log()
        else:
            self.session_log.write("notice", f"switched to {agent} / {session.id}")
        self._update_status()

    async def _send_to_agent(self, agent: str, text: str) -> None:
        if self.engine.agent_name != agent:
            await self._switch(agent, None)
            self._mount(Text(f"Switched to {agent}. Use /agent to go back."), "notice")
        self._mount(Text(f"> {text}"), "user")
        self._start_run(text)

    # ---- actions ---------------------------------------------------------------------------

    def action_tab_complete(self) -> None:
        widget = self.focused
        is_prompt = isinstance(widget, PromptArea) and widget.id == "prompt"
        menu = self.query_one("#suggest", OptionList)
        if is_prompt:
            self._refresh_menu()  # the text may have changed since the last Changed event
        if is_prompt and menu.display and self._menu:
            self._menu_accept(menu.highlighted if menu.highlighted is not None else 0)
            return
        last = widget.value.rsplit(None, 1)[-1] if is_prompt and widget.value.strip() else ""
        if is_prompt and last.startswith("@") and not widget.value.endswith(" "):
            done, self._tab_cycle = commands.complete_mention(widget.value, self._tab_cycle, self.ws.root)
            widget.value = done
            return
        if not (is_prompt and widget.value.startswith("/")):
            self.screen.focus_next()  # Tab's usual job everywhere else, including dialogs
            return
        widget.value, self._tab_cycle = commands.complete(widget.value, self._tab_cycle, self.ws)
        widget.cursor_position = len(widget.value)

    def action_cancel(self) -> None:
        """Ctrl+C copies selected text if any (drag with the mouse to select), otherwise cancels the run."""
        selected = self.screen.get_selected_text()
        if selected:
            self.copy_to_clipboard(selected)
            self.screen.clear_selection()
            self.notify("Copied to clipboard.", timeout=2)
            return
        if self._busy():
            self._run_task.cancel()

    def action_toggle_sidebar(self) -> None:
        hide = not self._hidden
        self._hidden = {"profile", "explorer"} if hide else set()
        self.query_one("#sidebar").set_class(hide, "hidden")
        self._arrange(self._layout)

    def action_cycle_mode(self) -> None:
        self.engine.mode = MODES[(MODES.index(self.engine.mode) + 1) % len(MODES)]
        self._update_status()

    def action_cycle_effort(self) -> None:
        """Rotate the reasoning effort (high, medium, low) for this session."""
        if self._busy():
            self.notify("Wait for the run to finish (or Ctrl+C) before changing effort.", timeout=3)
            return
        efforts = llm.EFFORTS
        current = self.engine.effort
        self.engine.set_effort(efforts[(efforts.index(current) + 1) % len(efforts)] if current in efforts else efforts[0])
        self._update_status()

    @work(exclusive=True, group="cycle_model")
    async def action_cycle_model(self) -> None:
        """Step to the next model offered by the selected provider."""
        if self._busy():
            self.notify("Wait for the run to finish (or Ctrl+C) before changing model.", timeout=3)
            return
        provider, current = llm.selection(self.ws.config)
        if not provider:
            self.notify("No provider selected. Use /provider first.", severity="warning", timeout=4)
            return
        try:
            models = await asyncio.to_thread(llm.list_models, provider)
        except Exception as exc:  # network or auth trouble: report, don't crash the UI
            self.notify(f"Cannot list {provider} models: {exc}", severity="error", timeout=4)
            return
        if not models:
            self.notify(f"{provider} offers no models.", severity="warning", timeout=4)
            return
        nxt = models[(models.index(current) + 1) % len(models)] if current in models else models[0]
        await commands.dispatch(self.host, f"/model {nxt}")
        self.engine.model_override = None  # an explicit model choice replaces a level override
        commands._forget_model(self.engine)
        self.engine.model_chain = []
        self._update_status()

    # Pane name (as saved in ui.json and used by /layout and the set_ui tool) -> widget id.
    PANE_IDS = {"profile": "sidebar", "explorer": "explorer-pane", "editor": "editor", "chat": "chat"}
    SPLITTERS = 3  # enough for four columns
    # F6 cycles these; /layout and the agent can set any layout.
    PANE_ORDERS = ((("profile", "explorer"), ("editor",), ("chat",)),
                   (("profile", "explorer"), ("chat",), ("editor",)),
                   (("chat",), ("editor",), ("profile", "explorer")),
                   (("profile",), ("explorer",), ("editor",), ("chat",)))

    def apply_ui(self, prefs: dict[str, Any]) -> None:
        """Apply a theme and layout (from ui.json, a /theme or /layout command, or the set_ui tool)."""
        theme = prefs.get("theme")
        if theme and theme in self.available_themes:
            self.theme = theme
        layout = prefs.get("layout")
        if layout:
            self._arrange(layout)

    def _pane(self, name: str) -> Widget:
        return self.query_one(f"#{self.PANE_IDS[name]}")

    def _columns(self) -> list[list[str]]:
        """The layout's columns without hidden panes."""
        columns = [[p for p in c if p not in self._hidden] for c in self._layout]
        return [c for c in columns if c]

    @staticmethod
    def _column_key(column: list[str]) -> str:
        return "/".join(column)

    def _column_width(self, column: list[str]) -> str:
        if "editor" in column:
            return "1fr"
        default = "40%" if "chat" in column else "30"
        return str(self._widths.get(self._column_key(column), default))

    def _arrange(self, layout: Any) -> None:
        """Lay panes out on #main's grid: one cell per column, two rows so a column can stack two panes.

        The grid fills cells in child order, so the top of every column (with splitters between) comes
        first and the lower panes of stacked columns follow; hidden panes and spare splitters go last."""
        self._layout = [list(c) for c in ui_prefs.normalize([list(c) for c in layout])]
        columns = self._columns()
        main = self.query_one("#main", Horizontal)
        splitters = [self.query_one(f"#split-{i}") for i in range(1, self.SPLITTERS + 1)]
        order: list[Widget] = []
        for i, column in enumerate(columns):
            if i:
                order.append(splitters[i - 1])
            top = self._pane(column[0])
            top.styles.row_span = 1 if len(column) > 1 else 2
            order.append(top)
        for column in columns:
            if len(column) > 1:
                bottom = self._pane(column[1])
                bottom.styles.row_span = 1
                order.append(bottom)
        for splitter in splitters:
            splitter.styles.row_span = 2
            splitter.display = splitter in order
        for name in self.PANE_IDS:
            self._pane(name).display = name not in self._hidden
        rest = [w for w in main.children if w not in order]
        for prev, widget in zip(order + rest, (order + rest)[1:]):
            main.move_child(widget, after=prev)
        main.styles.grid_size_columns = max(1, len(columns) * 2 - 1)
        main.styles.grid_size_rows = 2
        main.styles.grid_columns = " ".join(
            ("1 " if i else "") + self._column_width(c) for i, c in enumerate(columns))
        self._visible_splitters = [w for w in order if isinstance(w, Splitter)]

    def splitter_target(self, splitter: Splitter) -> tuple[str, int, int] | None:
        """For a drag on this splitter: (column key, +1 if it is the left column else -1, current width)."""
        if splitter not in self._visible_splitters:
            return None
        columns = self._columns()
        i = self._visible_splitters.index(splitter)
        left, right = columns[i], columns[i + 1]
        column, sign = (left, 1) if "editor" not in left else (right, -1)
        if "editor" in column:
            return None
        return self._column_key(column), sign, self._pane(column[0]).outer_size.width

    def set_column_width(self, key: str, width: int) -> None:
        self._widths[key] = width
        self._arrange(self._layout)

    def _save_layout(self) -> None:
        prefs = ui_prefs.load(self.ws.state)
        prefs["layout"], prefs["theme"] = self._layout, self.theme
        ui_prefs.save(self.ws.state, prefs)

    def action_move_pane(self) -> None:
        """Cycle through preset layouts."""
        orders = [[list(c) for c in o] for o in self.PANE_ORDERS]
        current = self._layout
        self._arrange(orders[(orders.index(current) + 1) % len(orders)] if current in orders else orders[0])
        self._save_layout()

    def action_reset_layout(self) -> None:
        self._widths.clear()
        self._hidden.clear()
        self.query_one("#sidebar").remove_class("hidden")
        self._arrange(ui_prefs.DEFAULT_LAYOUT)
        self._save_layout()

    def action_reload_tree(self) -> None:
        self._fill_tree()
        self.query_one("#explorer", DirectoryTree).reload()

    async def action_quit(self) -> None:
        editor = self.query_one("#editor", Editor)
        if editor.dirty:
            self._after_unsaved(lambda: self.run_worker(self._shutdown()))
            return
        await self._shutdown()

    async def _shutdown(self) -> None:
        self.action_cancel()
        await self.engine.mcp.aclose()  # let MCP servers shut down cleanly before the loop stops
        self.engine.close()
        self.exit()
