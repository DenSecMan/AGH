"""The provider setup wizard: shown at start when no working provider and model are selected."""
from __future__ import annotations

import asyncio
import os
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, Input, OptionList, Static
from textual.widgets.option_list import Option

from agh import keys, llm, onboarding
from agh.workspace import Workspace


class SetupScreen(ModalScreen[bool]):
    """Step 1 pick a provider, 2 enter its details (checked against the provider), 3 pick a model (sent a test
    prompt), 4 confirm and start. Dismisses True once saved, False if skipped."""

    BINDINGS = [Binding("escape", "back", "Back")]

    def __init__(self, ws: Workspace):
        super().__init__()
        self.ws = ws
        self.provider = ""
        self.values: dict[str, str] = {}
        self.models: list[str] = []
        self.model = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="setup"):
            yield Static(Text("Set up a model provider", style="bold"), id="setup-title")
            yield Static("", id="setup-hint")
            with ContentSwitcher(initial="step-provider", id="setup-steps"):
                yield OptionList(*[Option(Text(name, style="bold") + Text(f"  {status}", style="dim"), id=name)
                                   for name, status in onboarding.providers()], id="step-provider")
                yield VerticalScroll(id="step-details")
                yield OptionList(id="step-model")
                yield Static("", id="step-done")
            yield Static("", id="setup-status")
            with Horizontal(id="setup-buttons"):
                yield Button("Back", id="setup-back")
                yield Button("Skip for now", id="setup-skip")
                yield Button("Test connection", id="setup-test", variant="primary")
                yield Button("Start AGH", id="setup-start", variant="success")

    def on_mount(self) -> None:
        self._show("step-provider")

    # ---- steps -------------------------------------------------------------------------------

    def _show(self, step: str) -> None:
        self.query_one("#setup-steps", ContentSwitcher).current = step
        hints = {
            "step-provider": "Which provider should AGH use? Up/Down and Enter to choose. You can change it "
                             "later with /provider and /model, or add more keys with /key.",
            "step-details": onboarding.HELP.get(self.provider, ""),
            "step-model": f"{self.provider} works. Pick the model to use; it gets a short test message.",
            "step-done": "",
        }
        self.query_one("#setup-hint", Static).update(Text(hints[step], style="dim"))
        self.query_one("#setup-test").display = step == "step-details"
        self.query_one("#setup-start").display = step == "step-done"
        self.query_one("#setup-back").display = step != "step-provider"
        self._status("")
        focus = {"step-provider": "#step-provider", "step-model": "#step-model", "step-done": "#setup-start"}
        if step in focus:
            self.query_one(focus[step]).focus()

    def _status(self, text: str, style: str = "dim") -> None:
        self.query_one("#setup-status", Static).update(Text(text, style=style))

    @on(OptionList.OptionSelected, "#step-provider")
    async def _provider_chosen(self, event: OptionList.OptionSelected) -> None:
        self.provider = str(event.option.id)
        self.values = onboarding.current_values(self.provider)
        box = self.query_one("#step-details", VerticalScroll)
        await box.remove_children()
        widgets = []
        for field in keys.FIELDS[self.provider]:
            label = field.label + ("" if field.required or "optional" in field.label else " (optional)")
            widgets.append(Static(Text(f"{label}  [{field.var}]", style="bold" if field.required else "")))
            widgets.append(Input(self.values.get(field.var, ""), password=field.secret, id=f"f-{field.var}",
                                 placeholder=field.default or ("required" if field.required else "optional")))
        await box.mount(*widgets)
        self._show("step-details")
        inputs = list(box.query(Input))
        (next((i for i in inputs if not i.value), None) or inputs[0]).focus()

    @on(Input.Submitted)
    def _enter_in_field(self, event: Input.Submitted) -> None:
        inputs = list(self.query_one("#step-details").query(Input))
        later = inputs[inputs.index(event.input) + 1:] if event.input in inputs else []
        if later:
            later[0].focus()
        else:
            self._test_connection()

    @on(Button.Pressed, "#setup-test")
    def _test_pressed(self) -> None:
        self._test_connection()

    @work(exclusive=True, group="setup")
    async def _test_connection(self) -> None:
        self.values = {i.id.removeprefix("f-"): i.value for i in self.query_one("#step-details").query(Input)}
        self._status(f"Contacting {self.provider}...")
        try:
            self.models = await asyncio.to_thread(onboarding.check_connection, self.provider, self.values)
        except ValueError as exc:
            self._status(f"That didn't work: {exc}. Check the details and try again.", "red")
            return
        menu = self.query_one("#step-model", OptionList)
        menu.clear_options()
        menu.add_options([Option(m, id=m) for m in self.models])
        _, current = llm.selection(self.ws.config)
        menu.highlighted = self.models.index(current) if current in self.models else 0
        self._show("step-model")

    @on(OptionList.OptionSelected, "#step-model")
    def _model_chosen(self, event: OptionList.OptionSelected) -> None:
        self.model = str(event.option.id)
        self._test_model()

    @work(exclusive=True, group="setup")
    async def _test_model(self) -> None:
        self._status(f"Sending a test message to {self.model}...")
        try:
            reply = await asyncio.to_thread(onboarding.check_model, self.provider, self.model, self.ws.config)
        except ValueError as exc:
            self._status(f"{exc}\nPick another model, or go Back to check the details.", "red")
            return
        where = onboarding.save(self.ws, self.provider, self.model, self.values)
        self.query_one("#step-done", Static).update(
            Text("All set.\n\n", style="bold green") +
            Text(f"Provider  {self.provider}\nModel     {self.model}\nReply     {reply}\n\n{where}.\n\n") +
            Text("Press Enter to start.", style="dim"))
        self._show("step-done")

    # ---- navigation --------------------------------------------------------------------------

    def action_back(self) -> None:
        step = self.query_one("#setup-steps", ContentSwitcher).current
        previous = {"step-details": "step-provider", "step-model": "step-details", "step-done": "step-model"}
        if step in previous:
            self._show(previous[step])
            if previous[step] == "step-details":
                next(iter(self.query_one("#step-details").query(Input))).focus()

    @on(Button.Pressed, "#setup-back")
    def _back_pressed(self) -> None:
        self.action_back()

    @on(Button.Pressed, "#setup-skip")
    def _skip(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#setup-start")
    def _start(self) -> None:
        self.dismiss(True)


class SearchSetupScreen(ModalScreen[str]):
    """Ask whether the user has a web search service (Tavily, Brave), take its key, test it and save it.
    Dismisses with the service name, or "" if declined (web_search then uses the DuckDuckGo fallback)."""

    BINDINGS = [Binding("escape", "back", "Back")]
    NONE = "none"

    def __init__(self, ws: Workspace):
        super().__init__()
        self.ws = ws
        self.service = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="setup"):
            yield Static(Text("Set up web search", style="bold"), id="setup-title")
            yield Static("", id="setup-hint")
            with ContentSwitcher(initial="search-service", id="setup-steps"):
                options = [Option(Text(name, style="bold") + Text(f"  {help_.split('.')[0]}", style="dim"), id=name)
                           for name, (_, help_) in onboarding.SEARCH_PROVIDERS.items()]
                options.append(Option(Text("No, I don't have one", style="bold") +
                                      Text("  use the free DuckDuckGo fallback (can be rate limited)", style="dim"),
                                      id=self.NONE))
                yield OptionList(*options, id="search-service")
                with Vertical(id="search-details"):
                    yield Static("", id="search-label")
                    yield Input(password=True, id="search-key", placeholder="API key")
            yield Static("", id="setup-status")
            with Horizontal(id="setup-buttons"):
                yield Button("Back", id="search-back")
                yield Button("Skip", id="search-skip")
                yield Button("Test and save", id="search-test", variant="primary")

    def on_mount(self) -> None:
        self._show("search-service")

    def _show(self, step: str) -> None:
        self.query_one("#setup-steps", ContentSwitcher).current = step
        hint = ("Agents use the web_search tool. Do you have a search API service? Up/Down and Enter to choose. "
                "Run /search any time to change this." if step == "search-service"
                else onboarding.SEARCH_PROVIDERS[self.service][1])
        self.query_one("#setup-hint", Static).update(Text(hint, style="dim"))
        self.query_one("#search-test").display = step == "search-details"
        self.query_one("#search-back").display = step != "search-service"
        self._status("")
        (self.query_one("#search-key") if step == "search-details" else self.query_one("#search-service")).focus()

    def _status(self, text: str, style: str = "dim") -> None:
        self.query_one("#setup-status", Static).update(Text(text, style=style))

    @on(OptionList.OptionSelected, "#search-service")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == self.NONE:
            onboarding.mark_search_asked(self.ws)
            self.dismiss("")
            return
        self.service = str(event.option.id)
        var = onboarding.SEARCH_PROVIDERS[self.service][0]
        self.query_one("#search-label", Static).update(Text(f"API key  [{var}]", style="bold"))
        self.query_one("#search-key", Input).value = os.environ.get(var, "")
        self._show("search-details")

    @on(Input.Submitted, "#search-key")
    @on(Button.Pressed, "#search-test")
    def _test_pressed(self) -> None:
        self._test()

    @work(exclusive=True, group="search-setup")
    async def _test(self) -> None:
        key = self.query_one("#search-key", Input).value
        self._status(f"Testing {self.service}...")
        try:
            await asyncio.to_thread(onboarding.check_search, self.service, key)
        except ValueError as exc:
            self._status(f"That didn't work: {exc}. Check the key and try again.", "red")
            return
        onboarding.save_search(self.ws, self.service, key)
        self.dismiss(self.service)

    def action_back(self) -> None:
        if self.query_one("#setup-steps", ContentSwitcher).current == "search-details":
            self._show("search-service")

    @on(Button.Pressed, "#search-back")
    def _back_pressed(self) -> None:
        self.action_back()

    @on(Button.Pressed, "#search-skip")
    def _skip(self) -> None:
        onboarding.mark_search_asked(self.ws)
        self.dismiss("")
