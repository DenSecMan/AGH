"""Slash commands, shared by the TUI. Each returns a Rich renderable or text for the transcript."""
from __future__ import annotations

import asyncio
import os
import re
import shlex
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import yaml
from langchain_core.messages import HumanMessage
from rich.table import Table
from rich.text import Text

from agh import connectors, evals, improve, keys, llm, profiles, scaffold, ui_prefs
from agh.agents import load_agent
from agh.policy import MODES
from agh.sessions import (Session, delete_session, export_markdown, list_sessions, read_trace, rename_session,
                          search_sessions, trace_lines)
from agh.workspace import BACKUPS_DIR, CONFIG_NAME, KINDS, Workspace, WorkspaceError, validate_name


@dataclass
class Host:
    """What a command may do to the app."""
    ws: Workspace
    engine: Any
    switch_agent: Callable[[str, Session | None], Awaitable[None]]
    send_to_agent: Callable[[str, str], Awaitable[None]]  # (agent, text)
    log: Callable[[Any], None]
    quit: Callable[[], None]
    busy: Callable[[], bool] = lambda: False
    apply_ui: Callable[[dict[str, Any]], None] = lambda prefs: None
    open_setup: Callable[[], None] | None = None
    open_search_setup: Callable[[], None] | None = None
    goal: Callable[[str], None] | None = None  # /goal's text after the command word


def table(title: str, columns: list[str], rows: list[list[Any]]) -> Table:
    t = Table(title=title, title_justify="left", expand=True, border_style="dim")
    for c in columns:
        t.add_column(c, overflow="fold")
    for r in rows:
        t.add_row(*[Text(str(v)) for v in r])
    return t


def eval_table(result: evals.SuiteResult) -> Table:
    rows = [[("PASS" if c.passed else "FAIL") + (" (holdout)" if c.holdout else ""), c.id, f"{c.score:.2f}", c.steps,
             c.tokens, c.error or "; ".join(x["detail"] for x in c.checks if not x["passed"])] for c in result.cases]
    verdict = "PASSED" if result.passed else "FAILED"
    return table(f"{result.suite}: {verdict} {result.score:.0%} (threshold {result.threshold:.0%})",
                 ["", "case", "score", "steps", "tokens", "details"], rows)


def overview(ws: Workspace) -> Table:
    return table("Workspace", ["kind", "elements"], [[k, ", ".join(ws.names(k)) or "(none)"] for k in KINDS])


HELP = sorted([
    ("/help", "this list"),
    ("/agents, /agent NAME", "list or switch agents"),
    ("/init", "look at the folder agh started in and write its AGENTS.md (improves an existing one)"),
    ("/ls", "everything in the workspace"),
    ("/status", "current provider, model, level and speed"),
    ("/key [set PROVIDER KEY [NAME=VALUE...]|remove PROVIDER]", "list, add or remove API keys (user-wide; add "
                                                                "--workspace-only for this workspace)"),
    ("/provider [NAME|auto]", "list or select the LLM provider; auto clears the selection"),
    ("/model [NAME|NUMBER]", "list or select a model from the selected provider"),
    ("/models", "show model priorities used when no provider is selected"),
    ("/models set LEVEL MODEL...", "save a level's models, best first"),
    ("/models priority PROVIDER...", "save the order auto tries providers"),
    ("/tools, /skills", "what this agent has loaded"),
    ("/new KIND NAME [description]", "template for agent|skill|tool|connector|hook|eval"),
    ("/build new|edit|delete|restore|list ...", "add, change or remove agents, skills, tools, connectors, hooks and "
                                                 "evals in this profile (/build alone shows how)"),
    ("/connector import NAME SOURCE [REGEX]", "create a connector from an OpenAPI URL or file"),
    ("/connector test NAME", "load a connector and list its tools"),
    ("/connector login|logout NAME", "sign in to (or out of) an OAuth connector in the browser"),
    ("/mcp", "MCP servers: which are running and their tools"),
    ("/memory [QUERY], /remember TEXT, /forget ID", "long-term memory"),
    ("/reflect", "distill this conversation into memories"),
    ("/goal [OBJECTIVE|stop]", "keep working until the objective is done, within a token budget you set; "
                               "/goal alone shows progress"),
    ("/mode default|edits|plan|auto|full-auto", "edits (the UI's start mode) lets file edits in the project through "
                                                "and asks for the rest; plan is read-only; auto skips approval asks; "
                                                "full-auto never asks, even for agent-rule changes"),
    ("/compact", "summarize the conversation now"),
    ("/undo [list]", "put back the files the last turn changed (file tools only, not shell)"),
    ("/sessions [QUERY], /resume ID, /fork, /clear", "list or search sessions; switch, branch or start fresh"),
    ("/rename TITLE, /delete ID, /export [ID]", "name this session; remove one; save one as Markdown in exports/"),
    ("/usage, /trace [ID]", "tokens and cost; timeline of tool calls and outcomes"),
    ("/eval SUITE [--baseline]", "run an eval suite"),
    ("/improve SUITE [--dry-run]", "eval-gated prompt improvement"),
    ("/feedback good|bad [NOTE]", "rate the agent; feeds /improve"),
    ("/rollback [VERSION]", "restore an earlier prompt"),
    ("/learn", "draft a skill from this session"),
    ("/promote NAME", "activate a drafted skill"),
    ("/doctor", "check configuration and load errors"),
    ("/profile [NAME]", "list profiles, or switch to one (default is the starter, as installed)"),
    ("/profile save NAME [description]", "save what you've added as a new profile and switch to it"),
    ("/profile show|delete NAME, /profile discard", "what a profile adds; remove one; drop unsaved changes on default"),
    ("/reset [confirm]", "back to a fresh install; keeps .env, backs up the rest"),
    ("/theme [NAME]", "list colour themes, or switch to one (or ask the agent: \"make it look like nord\")"),
    ("/layout [PANE POSITION|COLUMN...|reset]", "show or change panes, e.g. /layout chat left, /layout explorer "
                                                "below profile, /layout chat editor profile/explorer"),
    ("/setup", "choose a model provider, enter its details, test them and pick a model"),
    ("/search", "set up web search: Tavily or Brave API key (else the free DuckDuckGo fallback)"),
    ("/quit", "leave (Ctrl+Q)"),
], key=lambda pair: pair[0].lower())
COMMAND_WORDS = sorted({w.split()[0].rstrip(",") for pair in HELP for w in pair[0].split(", ")})
COMMAND_HELP = {w.split()[0].rstrip(","): pair[1] for pair in HELP for w in pair[0].split(", ")}

BUILD_KINDS = ("agent", "skill", "tool", "connector", "hook", "eval")
BUILD_ACTIONS = {
    "delete": "remove an element from this profile: /build delete KIND NAME",
    "edit": "change an element: /build edit KIND NAME [FIELD] what to change",
    "list": "list this profile's elements: /build list [KIND]",
    "new": "create an element: /build new KIND NAME what it should do",
    "restore": "bring back a default element deleted in this profile: /build restore KIND NAME",
}
BUILD_ALIASES = {"add": "new", "create": "new", "amend": "edit", "change": "edit", "update": "edit",
                 "modify": "edit", "remove": "delete", "rm": "delete", "ls": "list", "undelete": "restore"}
EDIT_FIELDS = {
    "agent": ["connectors", "description", "model", "permissions", "prompt", "skills", "subagents", "system_tools",
              "tools"],
    "skill": ["body", "description", "resources"],
    "tool": ["code", "description", "permission", "tests"],
    "connector": ["auth", "config", "operations"],
    "hook": ["code", "description", "events"],
    "eval": ["agent", "cases", "threshold"],
}
PROTECTED = {("agent", "architect"): "the architect runs /build"}
BUILD_USAGE = Text("\n".join([
    "/build new KIND NAME DETAILS       e.g. /build new agent pentester You are an experienced principal pentester",
    "/build edit KIND NAME [FIELD] WHAT e.g. /build edit agent pentester prompt Focus on web applications",
    "/build delete KIND NAME            removes it from this profile (asks you to confirm)",
    "/build restore KIND NAME           brings back a deleted default element",
    "/build list [KIND]                 what this profile has",
    "/build ANY REQUEST                 free-form request to the architect",
    f"KIND is one of: {', '.join(BUILD_KINDS)}. Tab or the / menu completes each word.",
]))


def _save_models(ws: Workspace, key: str, value: Any) -> None:
    """Rewrites only the models block, so comments elsewhere in agh.yaml survive."""
    models = {**(ws.config.get("models") or {}), key: value}
    if value is None:
        del models[key]
    block = yaml.safe_dump({"models": models}, sort_keys=False)
    path = ws.root / CONFIG_NAME
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r"^models:.*\n(?:(?:[ \t].*|[ \t]*)(?:\n|\Z))*", re.M)
    text = pattern.sub(lambda _: block, text, count=1) if pattern.search(text) else text.rstrip() + "\n" + block
    path.write_text(text, encoding="utf-8")
    ws.config["models"] = models


def set_level_models(ws: Workspace, level: str, models: list[str]) -> None:
    if level not in llm.LEVELS:
        raise ValueError(f"unknown level {level!r} (have: {', '.join(llm.LEVELS)})")
    _save_models(ws, level, models[0] if len(models) == 1 else models)


def set_provider_priority(ws: Workspace, providers: list[str]) -> None:
    llm.provider_priority({"models": {"providers": providers}})  # raises on an unknown provider
    _save_models(ws, "providers", providers)


def models_table(ws: Workspace) -> Table:
    rows = []
    for level in llm.LEVELS:
        try:
            rows.append([level, " > ".join(llm.candidates(None, ws.config, level))])
        except llm.ModelConfigError:
            rows.append([level, "none available"])
    try:
        for provider in llm.provider_priority(ws.config):
            rows.append([f"provider {provider}", "ready" if llm.has_credentials(provider) else "no credentials"])
    except llm.ModelConfigError as exc:
        rows.append(["providers", f"ERROR: {exc}"])
    return table("Models, best first", ["", "models"], rows)


def provider_command(ws: Workspace, args: list[str]) -> Any:
    current, _ = llm.selection(ws.config)
    if not args:
        rows = []
        for name, spec in llm.PROVIDERS.items():
            status = "local server" if spec.get("local") else "ready" if llm.has_credentials(name) else f"set {spec['key']}"
            rows.append([("* " if name == current else "") + name, status])
        return table("Providers (* selected)", ["provider", "status"], rows)
    name = args[0].lower()
    if name in ("auto", "none"):
        _save_models(ws, "selected", None)
        return "Provider selection cleared. Using models from agh.yaml in priority order."
    if name not in llm.PROVIDERS:
        raise ValueError(f"unknown provider {name!r} (have: {', '.join(llm.PROVIDERS)})")
    if not llm.has_credentials(name):
        raise ValueError(f"{name} has no credentials; set {llm.PROVIDERS[name]['key']} in .env")
    if name != current:
        _save_models(ws, "selected", {"provider": name})
    return f"Provider: {name}. Use /model to see its models and pick one."


def model_command(ws: Workspace, args: list[str]) -> Any:
    """Blocking: asks the provider for its models."""
    provider, current = llm.selection(ws.config)
    if not provider:
        return "No provider selected, so no models are available. Use /provider to pick one."
    models = llm.list_models(provider)
    if not args:
        if not models:
            return f"{provider} offers no models."
        return table(f"{provider} models (* selected)", ["#", "model"],
                     [[i, ("* " if m == current else "") + m] for i, m in enumerate(models, 1)])
    choice = args[0]
    if choice.isdigit() and 1 <= int(choice) <= len(models):
        choice = models[int(choice) - 1]
    if choice not in models:
        raise ValueError(f"{choice!r} is not available from {provider}. Use /model to list its models.")
    _save_models(ws, "selected", {"provider": provider, "model": choice})
    return f"Model: {provider} / {choice}"


def status_table(engine: Any) -> Table:
    now = engine.describe()
    speed = f"{now['first_token_s']}s first token, {now['tokens_per_s']} tok/s" if "first_token_s" in now else "no calls yet"
    return table("Current", ["", ""], [["provider", now["provider"]], ["model", now["model"]],
                                       ["level", now["level"]], ["last call", speed]])


def _forget_model(engine: Any) -> None:
    engine.model_name, engine.active_model, engine.perf = "", None, {}


def _argument_choices(ws: Workspace, words: list[str]) -> list[str]:
    """What may come next after a command and its earlier arguments. Only cheap, local lookups."""
    cmd = words[0].lower()
    if len(words) == 1:
        return {"/agent": ws.names("agents"), "/eval": ws.names("evals"), "/improve": ws.names("evals"),
                "/provider": [*llm.PROVIDERS, "auto"], "/resume": [s["id"] for s in list_sessions(ws)],
                "/key": ["set", "remove", "list"], "/mode": list(MODES), "/undo": ["list"],
                "/profile": ["save", "show", "delete", "discard", *profiles.list_profiles(ws)],
                "/new": list(BUILD_KINDS), "/build": list(BUILD_ACTIONS),
                "/theme": ui_prefs.themes(), "/layout": [*ui_prefs.PANES, "reset"]}.get(cmd, [])
    if cmd == "/layout" and len(words) == 2 and words[1] in ui_prefs.PANES:
        return ["left", "right", "center", *(f"{r} {p}" for r in ("before", "after", "above", "below")
                                             for p in ui_prefs.PANES if p != words[1])]
    if cmd == "/build" and len(words) >= 2:
        action = BUILD_ALIASES.get(words[1].lower(), words[1].lower())
        if len(words) == 2 and action in BUILD_ACTIONS:
            return list(BUILD_KINDS)
        kind = words[2].lower() if len(words) > 2 else ""
        if len(words) == 3 and kind in BUILD_KINDS:
            if action in ("edit", "delete"):
                return ws.names(kind + "s")
            if action == "restore":
                return sorted(ws.excluded(kind + "s"))
        if len(words) == 4 and kind in BUILD_KINDS:
            if action == "edit":
                return EDIT_FIELDS[kind]
            if action == "delete":
                return ["confirm"]
        return []
    if cmd == "/key" and len(words) == 2 and words[1] in ("set", "remove"):
        return list(keys.FIELDS)
    return []


def suggestions(text: str, ws: Workspace) -> list[tuple[str, str, str]]:
    """What the slash menu offers for the word being typed: (text after choosing, label, description)."""
    if not text.startswith("/") or "\n" in text:
        return []
    if " " not in text:
        return [(w, w, COMMAND_HELP.get(w, "")) for w in COMMAND_WORDS if w.startswith(text.lower())]
    words = text.split()
    partial = "" if text.endswith(" ") else words.pop()
    prefix = text[:len(text) - len(partial)]
    try:
        options = _argument_choices(ws, words)
    except (OSError, WorkspaceError):
        return []
    notes = BUILD_ACTIONS if [w.lower() for w in words] == ["/build"] else {}
    return [(prefix + o, o, notes.get(o, "")) for o in options if o.lower().startswith(partial.lower())]


def _cycle(text: str, prefix: str, partial: str, options: list[str]) -> tuple[str, list[str]]:
    matches = [prefix + o for o in options if o.lower().startswith(partial.lower())]
    if not matches:
        return text, []
    if len(matches) == 1:
        return matches[0] + " ", []
    shared = os.path.commonprefix(matches)
    return (shared, matches if shared in matches else []) if len(shared) > len(text) else (matches[0], matches)


def complete(text: str, cycle: list[str], ws: Workspace | None = None) -> tuple[str, list[str]]:
    """Tab completion for a command word, or with a workspace, its arguments (agents, suites, sessions,
    providers): returns the new text and the matches being cycled through.

    One match completes fully. Several extend to their shared prefix, then each press moves to the next.
    """
    if text in cycle:
        return cycle[(cycle.index(text) + 1) % len(cycle)], cycle
    if not text.startswith("/"):
        return text, []
    if " " not in text:
        return _cycle(text, "", text, COMMAND_WORDS)
    if ws is None:
        return text, []
    words = text.split()
    partial = "" if text.endswith(" ") else words.pop()
    prefix = text[:len(text) - len(partial)]
    return _cycle(text, prefix, partial, _argument_choices(ws, words))


MENTION = re.compile(r"(?<!\S)@([^\s@]+)")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")  # the types view_image accepts
MENTION_MAX_CHARS = 50_000


def complete_mention(text: str, cycle: list[str], root: Any) -> tuple[str, list[str]]:
    """Complete the @path being typed at the end of the message, one directory level at a time."""
    if text in cycle:
        return cycle[(cycle.index(text) + 1) % len(cycle)], cycle
    head, _, typed = text.rpartition("@")
    folder, _, stem = typed.replace("\\", "/").rpartition("/")
    base = (root / folder) if folder else root
    if not base.is_dir():
        return text, []
    options = sorted(p.name + ("/" if p.is_dir() else "") for p in base.iterdir()
                     if not p.name.startswith((".", "__pycache__")) and p.name.lower().startswith(stem.lower()))
    stems = [f"{head}@{folder + '/' if folder else ''}{o}" for o in options]
    if not stems:
        return text, []
    if len(stems) == 1:
        return stems[0] + ("" if stems[0].endswith("/") else " "), []
    shared = os.path.commonprefix(stems)
    return (shared, []) if len(shared) > len(text) else (stems[0], stems)


def expand_mentions(text: str, ctx: Any) -> tuple[str, list[str], list[str]]:
    """Attach each @path that names a file in the sandbox: its contents follow the message, so the agent
    doesn't need a tool call to read what the user pointed at. Returns (text for the agent, attached, problems)."""
    blocks, attached, problems = [], [], []
    for path in dict.fromkeys(MENTION.findall(text)):
        try:
            target = ctx.resolve(path)
        except Exception:
            continue  # an @handle or an outside path: leave the text as typed
        if not target.is_file():
            continue
        if target.suffix.lower() in IMAGE_SUFFIXES:
            blocks.append(f'<image path="{path}">An image the user attached. Call view_image on this path to look at it.</image>')
            attached.append(path)
            continue
        try:
            body = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            problems.append(f"@{path} is not a text file; ask the agent to use view_image or read_file instead")
            continue
        if len(body) > MENTION_MAX_CHARS:
            body = body[:MENTION_MAX_CHARS] + f"\n[truncated; {len(body) - MENTION_MAX_CHARS} more characters]"
        ctx.note_read(target)
        blocks.append(f'<file path="{path}">\n{body}\n</file>')
        attached.append(path)
    return (text + "\n\n" + "\n\n".join(blocks) if blocks else text), attached, problems


def profiles_table(ws: Workspace) -> Table:
    t = table("Profiles", ["profile", "description", "contents"], profiles.summary_rows(ws))
    t.caption = "Switch with /profile NAME. Changes on default stay unsaved until /profile save NAME."
    return t


def profile_text(ws: Workspace, action: str, args: list[str], rest: str) -> Any:
    """The profile commands that don't need the app: list, show, save, delete, discard. Returns what to show
    and, for save, the profile to switch the app to."""
    if action == "show":
        info = profiles.describe(ws, args[0] if args else ws.profile)
        rows = [[f"adds {k}", ", ".join(v)] for k, v in info.added.items()]
        rows += [[f"changes {k}", ", ".join(v)] for k, v in info.changed.items()]
        title = f"Profile {info.name}" + (f" (from {info.based_on})" if info.based_on else "")
        return table(title, ["", "elements"], rows or [["", "nothing beyond default"]])
    if action == "delete":
        return Text(f"Deleted profile {args[0]}; it is in {profiles.delete(ws, args[0]).relative_to(ws.root)}.")
    if action == "discard":
        gone = profiles.discard_draft(ws)
        return Text("Discarded unsaved changes: " + ", ".join(gone) if gone else "No unsaved changes on default.")
    return profiles_table(ws)


async def profile_command(host: Host, args: list[str], rest: str) -> None:
    ws, log = host.ws, host.log
    action = args[0] if args else "list"
    if action in ("list", "show", "delete", "discard"):
        log(profile_text(ws, action, args[1:], rest))
        return
    if host.busy():
        raise ValueError("still working; press Ctrl+C first, or wait for the run to finish")
    engine = host.engine()
    if action == "save":
        if len(args) < 2:
            raise ValueError("usage: /profile save NAME [description]")
        description = rest.split(None, 2)[2] if len(rest.split(None, 2)) > 2 else ""
        source = ws.profile
        engine.close()  # memory databases are copied with the profile
        profiles.save(ws, args[1], description)
        await host.switch_agent(engine.agent_name, None)
        log(Text(f"Saved profile {args[1]} (from {source}) and switched to it. Changes here are saved as you go; "
                 f"/profile default goes back to the starter."))
        return
    name = args[1] if action == "switch" and len(args) > 1 else action
    if name == ws.profile:
        log(f"Already on {name}.")
        return
    leaving_draft = ws.profile == "default" and profiles.has_draft(ws)
    engine.close()
    profiles.switch(ws, name)
    agent = ws.default_agent  # a new profile starts on its default agent, not whoever /build last switched to
    await host.switch_agent(agent, None)
    note = " Unsaved changes on default are kept for when you come back (/profile save NAME keeps them)." \
        if leaving_draft else ""
    log(Text(f"Switched to profile {name}; talking to {agent} in a new session.{note}"))


async def connector_login(ws: Workspace, name: str, logout: bool = False, say: Callable[[Any], Any] = print) -> str:
    from agh import oauth

    if logout:
        return f"Signed out of {name}." if oauth.logout(ws.root, name) else f"{name} was not signed in."
    auth = connectors.load_connector(ws, name).config.get("auth") or {}
    if auth.get("type") != "oauth2_authorization_code":
        raise ValueError(f"{name} doesn't use oauth2_authorization_code; nothing to sign in to")
    try:
        path = await oauth.login(ws.root, name, auth, say=lambda text: say(Text(text)))
    except oauth.OAuthError as exc:
        raise ValueError(str(exc)) from None
    return f"Signed in to {name}. Token saved to {path} (outside the workspace)."


def keys_table(root: Any) -> Table:
    rows = [[r["provider"], r["var"], r["value"], r["source"]] for r in keys.status(root)]
    t = table("API keys", ["provider", "variable", "value", "from"], rows)
    t.caption = f"user-wide: {keys.global_env_path()}" + (f"   workspace: {root / '.env'}" if root else "")
    return t


def check_key(provider: str) -> str:
    """Ask the provider for its model list, which proves the key works without spending tokens."""
    try:
        models = llm.list_models(provider)
    except llm.ModelConfigError as exc:
        return f"Could not verify: {exc}"
    return f"Verified: {provider} lists {len(models)} models." if models else f"{provider} answered but lists no models."


def save_keys(root: Any, provider: str, values: dict[str, str], scope: str = "user", check: bool = True) -> Text:
    path = keys.set_values(root, values, scope)
    out = Text(f"Saved {', '.join(values)} to {path}.")
    if scope == "user" and root and any(keys.parse_env(root / ".env").get(v) for v in values):
        out.append(f"\n{root / '.env'} sets the same variable and wins in this workspace.", style="yellow")
    if check and provider in keys.LLM_PROVIDERS:
        out.append("\n" + check_key(provider), style="dim")
    return out


def mask_key_command(line: str) -> str:
    """What the transcript shows for a /key line: the key itself never appears on screen."""
    parts = line.split()
    if len(parts) >= 4 and parts[0].lower() == "/key" and parts[1] == "set" and "=" not in parts[3]:
        parts[3] = keys.mask(parts[3]) or parts[3]
    return " ".join(parts)


def key_command(ws: Workspace, args: list[str]) -> Any:
    scope = "workspace" if "--workspace-only" in args else "user"
    args = [a for a in args if a != "--workspace-only"]
    no_check = "--no-check" in args
    args = [a for a in args if a != "--no-check"]
    if not args or args[0] == "list":
        return keys_table(ws.root)
    if args[0] == "remove" and len(args) == 2:
        path, removed = keys.remove_provider(ws.root, args[1], scope)
        return f"Removed {', '.join(removed)} from {path}." if removed else f"Nothing saved for {args[1]} in {path}."
    if args[0] == "set" and len(args) >= 2:
        provider, rest = args[1], args[2:]
        key = rest[0] if rest and "=" not in rest[0] else None
        extras = dict(a.split("=", 1) for a in rest[1 if key else 0:] if "=" in a)
        values = keys.build_values(provider, key, extras)
        missing = [f"{f.var} (pass {f.var}=...)" for f in keys.fields_for(provider)
                   if f.required and not (values.get(f.var) or os.environ.get(f.var))]
        if missing:
            raise ValueError(f"{provider} still needs: {', '.join(missing)}. Or run /setup, which asks for "
                             "each detail and tests them")
        if not values:
            raise ValueError("usage: /key set PROVIDER KEY [NAME=VALUE...]")
        return save_keys(ws.root, provider, values, scope, check=not no_check)
    raise ValueError("usage: /key, /key set PROVIDER KEY [NAME=VALUE...], /key remove PROVIDER")


def doctor(ws: Workspace, engine: Any = None) -> Table:
    from agh.tooling import load_workspace_tools

    rows: list[list[str]] = [["workspace", str(ws.root)]]
    configured = [p for p in keys.LLM_PROVIDERS if keys.configured(p)]
    rows.append(["provider keys", ", ".join(configured) if keys.any_cloud_key() else
                 "MISSING: no hosted provider key. Run `agh key set PROVIDER`" +
                 (f" (local only: {', '.join(configured)})" if configured else "")])
    try:
        rows.append(["default model", llm.resolve_name(None, ws.config)])
    except llm.ModelConfigError as exc:
        rows.append(["default model", f"MISSING: {exc}"])
    load_workspace_tools(ws)
    for name in ws.names("agents"):
        try:
            load_agent(ws, name)
        except Exception as exc:
            ws.load_errors[f"agent:{name}"] = str(exc)
    for name in ws.names("connectors"):
        try:
            conn = connectors.load_connector(ws, name)
            for key, value in (conn.config.get("auth") or {}).items():
                if key.endswith("_env") and not os.environ.get(value):
                    rows.append([f"connector {name}", f"environment variable {value} is not set"])
        except Exception as exc:
            ws.load_errors[f"connector:{name}"] = str(exc)
    rows.extend([k, f"ERROR: {v}"] for k, v in ws.load_errors.items())
    return table("Doctor", ["check", "result"], rows)


def _build_target(ws: Workspace, args: list[str], must_exist: bool) -> tuple[str, str]:
    if len(args) < 3 or args[1].lower() not in BUILD_KINDS:
        raise ValueError(f"usage: /build {args[0]} KIND NAME ...; KIND is one of {', '.join(BUILD_KINDS)}")
    kind, name = args[1].lower(), args[2].lower()  # names are lowercase, so "PenTester" means pentester
    if must_exist and name not in ws.names(kind + "s"):
        have = ", ".join(ws.names(kind + "s")) or "none"
        raise ValueError(f"no {kind} named {name!r} in profile {ws.profile} (have: {have})")
    return kind, name


async def init_command(host: Host) -> None:
    """/init: assess the launch folder and write AGENTS.md there; agents read it from the next message."""
    from agh import project_init

    project = host.ws.project
    host.log(Text(f"Looking at {project} to write {project_init.AGENTS_FILE}...", style="dim"))
    text = await project_init.generate(host.engine()._plain_model(level="default"), project)
    path, backup = project_init.write(project, text)
    kept = f" The previous version is in {backup.name}." if backup else ""
    host.log(Text(f"Wrote {path}.{kept} Agents follow it from your next message; edit it any time.",
                  style="green"))


async def build_command(host: Host, args: list[str], rest: str) -> None:
    """/build: new and edit go to the architect as a precise request; delete, restore and list run here."""
    ws, log = host.ws, host.log
    if not args:
        log(BUILD_USAGE)
        return
    action = BUILD_ALIASES.get(args[0].lower(), args[0].lower())
    if action not in BUILD_ACTIONS:
        await host.send_to_agent("architect", rest)  # free-form request
        return
    args = [action, *args[1:]]
    if action == "list":
        kinds = [args[1].lower()] if len(args) > 1 else list(BUILD_KINDS)
        if any(k not in BUILD_KINDS for k in kinds):
            raise ValueError(f"KIND is one of {', '.join(BUILD_KINDS)}")
        rows = [[k, ", ".join(ws.names(k + "s")) or "(none)", ", ".join(sorted(ws.excluded(k + "s")))]
                for k in kinds]
        log(table(f"Profile {ws.profile}", ["kind", "names", "deleted defaults"], rows))
        return
    if action == "new":
        kind, name = _build_target(ws, args, must_exist=False)
        validate_name(name)
        if name in ws.names(kind + "s"):
            raise ValueError(f"{kind} {name!r} already exists; use /build edit {kind} {name} ...")
        details = " ".join(args[3:])
        if not details:
            raise ValueError(f"say what it should do: /build new {kind} {name} DETAILS")
        hidden = (f" A default {kind} of that name was deleted in this profile; build a new one with overwrite."
                  if name in ws.excluded(kind + "s") else "")
        await host.send_to_agent("architect", f"Create a new {kind} named {name} in the active profile.{hidden} "
                                              f"Requirements from the user: {details}")
        return
    if action == "edit":
        kind, name = _build_target(ws, args, must_exist=True)
        field, details = "", args[3:]
        if details and details[0].lower() in EDIT_FIELDS[kind]:
            field, details = details[0].lower(), details[1:]
        if not details:
            raise ValueError(f"say what to change: /build edit {kind} {name} [FIELD] WHAT")
        target = f"the {field} of " if field else ""
        await host.send_to_agent("architect", f"Edit {target}the existing {kind} {name} in the active profile. Read "
                                              f"its current files first and change only what is asked. Change "
                                              f"requested by the user: {' '.join(details)}")
        return
    kind, name = _build_target(ws, args, must_exist=action == "delete")
    if action == "restore":
        ws.restore_element(kind + "s", name)
        log(Text(f"Restored {kind} {name} from default. Changes apply from your next message."))
        return
    if (kind, name) in PROTECTED:
        raise ValueError(f"{kind} {name} can't be deleted: {PROTECTED[(kind, name)]}")
    if kind == "agent" and name in (host.engine().agent_name, ws.default_agent):
        raise ValueError(f"{name} is the agent in use or the profile's default agent; switch agents first")
    if args[3:] != ["confirm"]:
        default = (ws.default_root / f"{kind}s" / name).is_dir()
        note = (" It comes from default, so it is only hidden in this profile; /build restore brings it back."
                if default else " Its files move to the backups folder.")
        log(Text(f"This deletes {kind} {name} from profile {ws.profile}.{note} "
                 f"Run /build delete {kind} {name} confirm to go ahead.", style="yellow"))
        return
    backup = ws.remove_element(kind + "s", name)
    where = f" Files moved to {backup.relative_to(ws.root)}." if backup else ""
    log(Text(f"Deleted {kind} {name} from profile {ws.profile}.{where}"))


def ui_command(host: Host, cmd: str, args: list[str]) -> Any:
    state = host.ws.state
    if not args:
        prefs = ui_prefs.load(state)
        if cmd == "/theme":
            return table("Themes", ["theme"], [[("* " if t == prefs["theme"] else "") + t] for t in ui_prefs.themes()])
        return Text(ui_prefs.describe(prefs))
    if cmd == "/theme":
        prefs = ui_prefs.change(state, theme=" ".join(args))
    elif args == ["reset"]:
        prefs = ui_prefs.change(state, reset=True)
    elif len(args) >= 2 and args[1].lower() not in ui_prefs.PANES and args[1].lower() not in ui_prefs.ALIASES:
        prefs = ui_prefs.change(state, pane=args[0], position=" ".join(args[1:]))
    else:
        prefs = ui_prefs.change(state, layout=args)
    host.apply_ui(prefs)
    return Text(f"UI: {ui_prefs.describe(prefs)}")


async def dispatch(host: Host, line: str) -> None:
    try:
        parts = shlex.split(line, posix=True)
    except ValueError:
        parts = line.split()
    cmd, args = parts[0].lower(), parts[1:]
    rest = line.split(None, 1)[1] if len(parts) > 1 else ""
    try:
        await _run(host, cmd, args, rest)
    except (WorkspaceError, connectors.ConnectorError, llm.ModelConfigError, ValueError, OSError) as exc:
        host.log(Text(f"Error: {exc}", style="red"))


async def _run(host: Host, cmd: str, args: list[str], rest: str) -> None:
    ws, engine, log = host.ws, host.engine(), host.log
    agent = engine.agent_name
    if cmd == "/help":
        log(table("Commands", ["command", "what it does"], [list(p) for p in HELP]))
    elif cmd in ("/quit", "/exit"):
        host.quit()
    elif cmd == "/setup":
        if host.open_setup is None:
            log("Setup runs in the terminal UI; from the shell use `agh key set PROVIDER` and `agh provider`.")
        else:
            host.open_setup()
    elif cmd == "/search":
        if host.open_search_setup is None:
            log("Search setup runs in the terminal UI; from the shell use `agh key set tavily` (or brave).")
        else:
            host.open_search_setup()
    elif cmd in ("/theme", "/layout"):
        log(ui_command(host, cmd, args))
    elif cmd == "/models":
        if args[:1] == ["set"] and len(args) > 2:
            set_level_models(ws, args[1], args[2:])
        elif args[:1] == ["priority"] and len(args) > 1:
            set_provider_priority(ws, args[1:])
        elif args:
            raise ValueError("usage: /models, /models set LEVEL MODEL..., or /models priority PROVIDER...")
        log(models_table(ws))
    elif cmd == "/provider":
        log(provider_command(ws, args))
        if args:
            _forget_model(engine)
    elif cmd == "/model":
        log(await asyncio.to_thread(model_command, ws, args))
        if args:
            _forget_model(engine)
    elif cmd == "/status":
        log(status_table(engine))
    elif cmd == "/key":
        log(await asyncio.to_thread(key_command, ws, args))
        if args and args[0] in ("set", "remove"):
            _forget_model(engine)
    elif cmd == "/ls":
        log(overview(ws))
    elif cmd == "/agents":
        rows = [[("* " if n == agent else "") + n, load_agent(ws, n).description] for n in ws.names("agents")]
        log(table("Agents", ["name", "description"], rows))
    elif cmd == "/agent":
        await host.switch_agent(args[0], None)
        log(Text(f"Now talking to {args[0]}. New session started."))
    elif cmd == "/tools":
        await engine.refresh()
        log(table(f"Tools for {agent}", ["tool", "read-only", "source", "description"],
                  [[t.name, t.readonly, t.source.rsplit("\\", 1)[-1], t.description] for t in engine.tools.values()]))
    elif cmd == "/skills":
        await engine.refresh()
        log(table(f"Skills for {agent}", ["skill", "description"],
                  [[s.name, s.description] for s in engine.skills.values()]))
    elif cmd == "/new":
        path = scaffold.create_element(ws, args[0], args[1], " ".join(args[2:]))
        log(Text(f"Created {path.relative_to(ws.root)}. Changes apply from your next message."))
    elif cmd == "/build":
        await build_command(host, args, rest)
    elif cmd == "/init":
        await init_command(host)
    elif cmd == "/connector":
        if args[:1] == ["import"]:
            path = scaffold.create_connector(ws, args[1], openapi=args[2], include=args[3] if len(args) > 3 else None)
            log(await connectors.probe(ws, args[1]))
            log(Text(f"Created {path.relative_to(ws.root)}. Add it to an agent's connectors list."))
        elif args[:1] == ["test"]:
            log(await connectors.probe(ws, args[1]))
        elif args[:1] in (["login"], ["logout"]) and len(args) == 2:
            log(await connector_login(ws, args[1], logout=args[0] == "logout", say=log))
        else:
            log("Usage: /connector import NAME SOURCE [REGEX], /connector test NAME, /connector login|logout NAME")
    elif cmd == "/mcp":
        running = engine.mcp.running()
        rows = []
        for name in ws.names("connectors"):
            try:
                cfg = connectors.load_connector(ws, name).config
            except Exception as exc:
                rows.append([name, "error", str(exc)])
                continue
            if cfg.get("type") == "mcp":
                specs = engine.mcp._servers.get(name).specs if name in running else []
                rows.append([name, "running" if name in running else "not started",
                             ", ".join(t.name.split("__", 1)[-1] for t in specs)])
        log(table("MCP servers", ["connector", "state", "tools"], rows) if rows else "No MCP connectors.")
    elif cmd in ("/memory", "/remember", "/forget", "/reflect"):
        await engine.refresh()
        if not engine.memory:
            log("Memory is disabled for this agent.")
        elif cmd == "/remember":
            log(f"Saved memory #{engine.memory.add(rest, source='user', importance=0.8)}")
        elif cmd == "/forget":
            log("Forgotten." if engine.memory.forget(int(args[0])) else "No such memory.")
        elif cmd == "/reflect":
            saved = await engine.reflect()
            log("Saved:\n" + "\n".join(f"- {s}" for s in saved) if saved else "Nothing worth saving.")
        else:
            items = engine.memory.search(rest, k=20) if rest else engine.memory.all(limit=20)
            log(table(f"Memory for {agent}", ["id", "kind", "text"], [[i.id, i.kind, i.text] for i in items]))
    elif cmd == "/mode":
        if args and args[0] in MODES:
            engine.mode = args[0]
        log(f"Mode: {engine.mode}")
    elif cmd == "/undo":
        if args[:1] == ["list"]:
            rows = [[turn, len(files), ", ".join(os.path.relpath(f, ws.root) for f in files)]
                    for turn, files in engine.checkpoints.history()]
            log(table("Checkpoints (newest last)", ["turn", "files", "paths"], rows) if rows else "No checkpoints.")
        elif host.busy():
            log(Text("Still working. Press Ctrl+C to cancel first.", style="yellow"))
        elif restored := engine.checkpoints.undo():
            shown = [r.replace(str(ws.root) + os.sep, "") for r in restored]
            engine.session.messages.append(HumanMessage(
                content="[harness] The user undid the file changes from the last turn that changed files: "
                        + "; ".join(shown) + ". Shell commands were not undone."))
            engine.session.save()
            log("Undone:\n" + "\n".join(f"- {r}" for r in shown) + "\nShell commands are not undone.")
        else:
            log("Nothing to undo in this session.")
    elif cmd == "/compact":
        log("Compacted." if await engine.compact(force=True) else "Too short to compact.")
    elif cmd == "/sessions":
        found = search_sessions(ws, rest) if rest else list_sessions(ws)
        log(table("Sessions" + (f" matching {rest!r}" if rest else ""), ["id", "agent", "messages", "title"],
                  [[s["id"], s["agent"], s["messages"], s["title"]] for s in found]))
    elif cmd == "/rename":
        engine.session.title = rest.strip()[:120]
        engine.session.save()
        log(f"Renamed to {engine.session.title!r}.")
    elif cmd == "/delete":
        if not args or args[0] == engine.session.id:
            raise ValueError("usage: /delete SESSION_ID (not the current session; /clear first)")
        log(f"Deleted {', '.join(delete_session(ws, args[0]))}.")
    elif cmd == "/export":
        engine.session.save()
        sid = args[0] if args else engine.session.id
        target = ws.root / "exports" / f"{sid}.md"
        target.parent.mkdir(exist_ok=True)
        target.write_text(export_markdown(ws, sid), encoding="utf-8")
        log(f"Exported to {target.relative_to(ws.root).as_posix()}.")
    elif cmd == "/profile":
        await profile_command(host, args, rest)
    elif cmd == "/resume":
        session = Session.load(ws, args[0])
        if session.profile and session.profile != ws.profile and session.profile in profiles.list_profiles(ws):
            profiles.switch(ws, session.profile)
            log(f"Switched to profile {session.profile}, where this session ran.")
        await host.switch_agent(session.agent, session)
        log(f"Resumed {session.id} ({len(session.messages)} messages).")
    elif cmd == "/fork":
        session = engine.session.fork()
        await host.switch_agent(agent, session)
        log(f"Forked into {session.id}.")
    elif cmd == "/goal":
        if host.goal is None:
            log("/goal runs in the terminal UI.")
        else:
            host.goal(rest.strip())
    elif cmd == "/clear":
        await host.switch_agent(agent, None)
    elif cmd == "/usage":
        log(table("Session usage", ["metric", "value"], [[k, round(v, 4)] for k, v in engine.session.usage.items()]))
    elif cmd == "/trace":
        sid = args[0] if args else engine.session.id
        lines = trace_lines(read_trace(ws, sid))
        log(Text("\n".join(lines[-80:])) if lines else f"No events yet. File: {engine.tracer.path}")
    elif cmd == "/eval":
        log(f"Running {args[0]}...")
        result = await evals.run_suite(ws, args[0])
        log(eval_table(result))
        if "--baseline" in args:
            evals.set_baseline(ws, args[0], result)
            log("Saved as baseline.")
        elif diff := evals.compare_baseline(ws, args[0], result):
            log(f"Compared to baseline: {diff['delta']:+.2f}; regressions {diff['regressions']}; fixed {diff['fixed']}")
    elif cmd == "/improve":
        log(f"Improving {agent} against {args[0]}...")
        out = await improve.improve_prompt(ws, agent, args[0], apply="--dry-run" not in args)
        log(f"Baseline {out.baseline:.2f}, best {out.best:.2f}. {out.reason}")
    elif cmd == "/feedback":
        improve.record_feedback(ws, agent, args[0], " ".join(args[1:]), engine.session.id)
        log("Feedback recorded.")
    elif cmd == "/rollback":
        log(f"Restored {improve.rollback(ws, agent, int(args[0]) if args else None)}.")
    elif cmd == "/learn":
        engine.session.save()
        path = await improve.mine_skill(ws, engine.session.id)
        log(f"Draft at {path.relative_to(ws.root)}. Activate it with /promote {path.name}.")
    elif cmd == "/promote":
        log(f"Activated {improve.promote_skill(ws, agent, args[0]).relative_to(ws.root)}.")
    elif cmd == "/doctor":
        log(doctor(ws))
    elif cmd == "/reset":
        if args != ["confirm"]:
            log(Text("This returns the workspace to a fresh install: agh.yaml and the default profile only. Every "
                     "other profile, all memory, sessions and traces are removed from the workspace.\n"
                     f"Kept: .env, AGENTS.md and files the harness doesn't manage. Everything replaced moves to "
                     f"{BACKUPS_DIR}/.\nTo just leave a profile, use /profile default instead.\n"
                     "Run /reset confirm to go ahead.", style="yellow"))
        elif host.busy():
            log(Text("Still working. Press Ctrl+C to cancel first.", style="yellow"))
        else:
            engine.close()  # releases memory.db so it can be moved
            backup = ws.reset()
            await host.switch_agent(ws.default_agent, None)
            host.engine().mode = "edits"  # what the UI starts in
            log(Text(f"Reset to defaults. Previous files are in {backup.relative_to(ws.root)}."))
    else:
        log(Text(f"Unknown command {cmd}. Try /help.", style="yellow"))
