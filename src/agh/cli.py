"""Command line entry point. `agh` with no arguments opens the terminal UI."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Confirm
from rich.text import Text

from agh import __version__, commands, connectors, evals, improve, keys, llm, profiles, scaffold
from agh.engine import Engine
from agh.memory import MemoryStore
from agh.agents import load_agent
from agh.policy import MODES
from agh.sessions import (Session, delete_session, export_markdown, latest_session, list_sessions, read_trace,
                          rename_session, search_sessions, trace_lines)
from agh.workspace import CONFIG_NAME, Workspace, WorkspaceError

console = Console()


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agh", description="AGH agent harness")
    p.add_argument("-w", "--workspace", type=Path, help="workspace directory")
    p.add_argument("-V", "--version", action="version", version=f"agh {__version__}")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("init", help="create a workspace")
    s.add_argument("path", nargs="?", default=".", type=Path)

    p.add_argument("-c", "--continue", dest="continue_", action="store_true",
                   help="open the UI on the most recent session")
    p.add_argument("-p", "--profile", help="use this profile for this command only, without switching")

    s = sub.add_parser("profile", help="list, switch, save, show or delete profiles")
    ps = s.add_subparsers(dest="action")
    ps.add_parser("list")
    c = ps.add_parser("switch", help="make a profile active")
    c.add_argument("name")
    c = ps.add_parser("save", help="save what is active (with unsaved changes on default) as a new profile")
    c.add_argument("name")
    c.add_argument("description", nargs="*")
    c = ps.add_parser("show", help="what a profile adds or changes")
    c.add_argument("name", nargs="?")
    c = ps.add_parser("delete", help="move a profile and its data to the backups folder")
    c.add_argument("name")
    ps.add_parser("discard", help="drop unsaved changes made on default")
    s = sub.add_parser("tui", help="open the terminal UI (default)")
    s.add_argument("-a", "--agent")
    s.add_argument("--resume")
    s.add_argument("-c", "--continue", dest="continue_", action="store_true", help="resume the most recent session")

    s = sub.add_parser("run", help="run one prompt and print the answer")
    s.add_argument("prompt", nargs="?", default="-", help="the prompt; - or omitted reads it from stdin")
    s.add_argument("-a", "--agent")
    s.add_argument("--resume", help="continue a session")
    s.add_argument("-c", "--continue", dest="continue_", action="store_true", help="continue the most recent session")
    s.add_argument("--stream-json", action="store_true", help="print every event as a JSON line, then the result")
    s.add_argument("--mode", choices=list(MODES), default="default")
    s.add_argument("--yes", action="store_true", help="auto-approve tool calls that would ask")
    s.add_argument("--json", action="store_true", help="print the result as JSON")
    s.add_argument("-m", "--model", help="use this model or level for this run, without fallbacks")

    s = sub.add_parser("key", help="add, list or remove provider API keys")
    ks = s.add_subparsers(dest="action")
    ks.add_parser("list", help="show which keys are set and where (default)")
    c = ks.add_parser("set", help="save a provider's key; prompts if KEY is omitted")
    c.add_argument("provider", choices=list(keys.FIELDS))
    c.add_argument("key", nargs="?", help="the key; omit to type it hidden")
    c.add_argument("extra", nargs="*", metavar="NAME=VALUE",
                   help="other settings, e.g. endpoint=... deployment=... url=...")
    c.add_argument("--workspace-only", dest="scope", action="store_const", const="workspace", default="user",
                   help="save in this workspace's .env instead of the user-wide ~/.agh/.env")
    c.add_argument("--no-check", action="store_true", help="don't test the key against the provider")
    c = ks.add_parser("remove", help="delete a provider's saved settings")
    c.add_argument("provider", choices=list(keys.FIELDS))
    c.add_argument("--workspace-only", dest="scope", action="store_const", const="workspace", default="user")

    s = sub.add_parser("models", help="show or change model selection and priority")
    ms = s.add_subparsers(dest="action")
    c = ms.add_parser("set", help="set a level's models, best first")
    c.add_argument("level", choices=llm.LEVELS)
    c.add_argument("models", nargs="+")
    c = ms.add_parser("priority", help="set the order `auto` tries providers")
    c.add_argument("providers", nargs="+")

    s = sub.add_parser("provider", help="list or select the LLM provider")
    s.add_argument("name", nargs="?")
    s = sub.add_parser("model", help="list or select a model from the selected provider")
    s.add_argument("name", nargs="?")

    sub.add_parser("ls", help="list workspace elements")
    sub.add_parser("doctor", help="check configuration")
    s = sub.add_parser("sessions", help="list, search, rename, delete or export sessions")
    ss = s.add_subparsers(dest="action")
    c = ss.add_parser("search", help="sessions containing every word")
    c.add_argument("query", nargs="+")
    c = ss.add_parser("rename")
    c.add_argument("session")
    c.add_argument("title", nargs="+")
    c = ss.add_parser("delete", help="remove a session with its trace and checkpoints")
    c.add_argument("session")
    c = ss.add_parser("export", help="print a session as Markdown")
    c.add_argument("session")

    s = sub.add_parser("trace", help="show a session's timeline of tool calls, notices and outcomes")
    s.add_argument("session", nargs="?", help="defaults to the most recent")

    s = sub.add_parser("serve", help="local HTTP API (POST /v1/runs) and scheduled triggers from agh.yaml")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--yes", action="store_true", help="auto-approve tool calls that would ask (default: deny)")

    s = sub.add_parser("new", help="create an element from a template, or with --ai by asking the architect")
    s.add_argument("kind", choices=["agent", "skill", "tool", "connector", "hook", "eval"])
    s.add_argument("name")
    s.add_argument("description", nargs="?", default="")
    s.add_argument("--ai", action="store_true", help="have the architect agent build it from the description")
    s.add_argument("--yes", action="store_true", help="auto-approve the architect's create_* calls")

    s = sub.add_parser("connector", help="manage API connectors")
    cs = s.add_subparsers(dest="action", required=True)
    c = cs.add_parser("import", help="create from an OpenAPI URL or file")
    c.add_argument("name")
    c.add_argument("source")
    c.add_argument("--include", help="regex of operation ids to keep")
    c = cs.add_parser("test", help="load and list tools")
    c.add_argument("name")
    c = cs.add_parser("login", help="sign in to an oauth2_authorization_code connector in the browser")
    c.add_argument("name")
    c = cs.add_parser("logout", help="forget a connector's sign-in")
    c.add_argument("name")

    s = sub.add_parser("eval", help="run an eval suite")
    s.add_argument("suite")
    s.add_argument("--baseline", action="store_true", help="save the result as the baseline")

    s = sub.add_parser("improve", help="eval-gated prompt improvement")
    s.add_argument("agent")
    s.add_argument("suite")
    s.add_argument("--rounds", type=int, default=2)
    s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("rollback", help="restore an earlier prompt version")
    s.add_argument("agent")
    s.add_argument("--version", type=int)

    s = sub.add_parser("feedback", help="record feedback on an agent")
    s.add_argument("agent")
    s.add_argument("rating", choices=["good", "bad"])
    s.add_argument("note", nargs="?", default="")

    s = sub.add_parser("learn", help="draft a skill from a session")
    s.add_argument("session")
    s = sub.add_parser("promote", help="activate a drafted skill")
    s.add_argument("agent")
    s.add_argument("name")

    s = sub.add_parser("memory", help="inspect or edit agent memory")
    s.add_argument("agent")
    s.add_argument("query", nargs="?")
    s.add_argument("--add")
    s.add_argument("--forget", type=int)
    return p


async def _cli_approver(request: dict[str, Any]) -> str:
    args = json.dumps(request["args"], default=str)[:1000]
    console.print(Text(f"{request['agent']} wants to run {request['tool']} {args}", style="yellow"))
    if request.get("diff"):
        from rich.syntax import Syntax

        console.print(Syntax(request["diff"], "diff"))
    if not sys.stdin.isatty():
        console.print(Text("Denied: no terminal to ask on. Use --yes to allow.", style="red"))
        return "deny"
    allowed = await asyncio.to_thread(Confirm.ask, "Allow?", default=False)
    return "once" if allowed else "deny"


async def _auto_yes(_: dict[str, Any]) -> str:
    return "once"


async def _run(ws: Workspace, prompt: str, agent: str | None, resume: str | None, mode: str, yes: bool,
               as_json: bool, model: str | None = None, stream_json: bool = False) -> int:
    from agh.serve import event_json, result_json

    session = Session.load(ws, resume) if resume else None
    name = (session.agent if session else None) or agent or ws.default_agent
    engine = Engine(ws, name, session=session, approver=_auto_yes if yes else _cli_approver, mode=mode)
    engine.model_override = model
    streamed = False
    try:
        async for event in engine.stream(prompt):
            if stream_json:
                if event.kind != "usage":
                    print(json.dumps(event_json(event), default=str), flush=True)
                continue
            if as_json:
                continue
            if event.agent != name and event.kind in ("text", "text_end"):
                continue  # parallel subagents would interleave; each one's answer prints when it finishes
            if event.kind == "subagent_end":
                if event.data["text"]:
                    console.print(Text(f"{event.agent}: {event.data['text']}", style="dim"))
            elif event.kind == "text":
                console.print(Text(event.data["text"]), end="")
                streamed = True
            elif event.kind == "text_end":
                console.print()
            elif event.kind == "tool_start":
                console.print(Text(f"> {event.agent}: {event.data['tool']} {json.dumps(event.data['args'], default=str)[:160]}", style="dim"))
            elif event.kind == "tool_end" and event.data.get("error"):
                console.print(Text(f"  failed: {event.data['error']}", style="red"))
            elif event.kind == "notice":
                console.print(Text(event.data["text"], style="dim italic"))
            elif event.kind == "error":
                console.print(Text(f"Stopped: {event.data['text']}", style="red"))
    finally:
        await engine.mcp.aclose()
        engine.close()
    r = engine.result
    if stream_json:
        print(json.dumps({**result_json(engine), "tool_calls": r.tool_calls}, default=str), flush=True)
    elif as_json:
        print(json.dumps({"text": r.text, "stop_reason": r.stop_reason, "error": r.error, "steps": r.steps,
                          "session": engine.session.id, "input_tokens": r.input_tokens,
                          "output_tokens": r.output_tokens, "cost_usd": r.cost_usd, "tool_calls": r.tool_calls}, default=str))
    else:
        if not streamed and r.text:
            console.print(Markdown(r.text))
        console.print(Text(f"[{engine.session.id} | {r.steps} steps | {r.input_tokens + r.output_tokens} tokens]", style="dim"))
    return 1 if r.error else 0


async def _serve(ws: Workspace, host: str, port: int, yes: bool) -> int:
    from agh.serve import Server, new_token

    token = os.environ.get("AGH_SERVE_TOKEN") or new_token()
    server = Server(ws, token, yes=yes, log=lambda text: console.print(Text(text, style="dim")))
    bound = await server.start(host, port)
    console.print(f"AGH API on http://{host}:{bound}  (workspace {ws.root})")
    if not os.environ.get("AGH_SERVE_TOKEN"):
        console.print(f"Token for this run: {token}   (set AGH_SERVE_TOKEN to choose one)")
    console.print(Text(f"{len(ws.config.get('triggers') or [])} trigger(s); tool calls that would ask are "
                       f"{'approved' if yes else 'denied'}. Ctrl+C stops.", style="dim"))
    try:
        await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await server.close()
    return 0


async def _eval(ws: Workspace, suite: str, baseline: bool) -> int:
    result = await evals.run_suite(ws, suite, on_case=lambda c: console.print(
        Text(f"{'PASS' if c.passed else 'FAIL'} {c.id}", style="green" if c.passed else "red")))
    console.print(commands.eval_table(result))
    if baseline:
        evals.set_baseline(ws, suite, result)
        console.print("Saved as baseline.")
    elif diff := evals.compare_baseline(ws, suite, result):
        console.print(Text(f"Baseline delta {diff['delta']:+.2f}; regressions {diff['regressions']}; fixed {diff['fixed']}"))
    return 0 if result.passed else 1


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        sys.exit(_dispatch(args) or 0)
    except (WorkspaceError, connectors.ConnectorError, llm.ModelConfigError, keys.KeySetupError) as exc:
        console.print(Text(f"Error: {exc}", style="red"))
        sys.exit(2)
    except (KeyboardInterrupt, EOFError):
        sys.exit(130)


def _parse_extras(items: list[str]) -> dict[str, str]:
    out = {}
    for item in items:
        if "=" not in item:
            raise keys.KeySetupError(f"expected NAME=VALUE, got {item!r}")
        name, value = item.split("=", 1)
        out[name.strip()] = value.strip()
    return out


def _prompt_missing(provider: str, values: dict[str, str]) -> None:
    """At a terminal, ask for whatever the provider still needs. Keys are typed hidden."""
    fields = keys.fields_for(provider)
    main = next((f for f in fields if f.secret), None)
    for field in fields:
        if field.var in values:
            continue
        if field is main:  # setting a provider means setting its key, even if one exists
            if typed := getpass.getpass(f"{provider} {field.label}: ").strip():
                values[field.var] = typed
        elif not os.environ.get(field.var) and (field.required or field.default):
            hint = field.default
            typed = input(f"{provider} {field.label}" + (f" [{hint}]" if hint else "") + ": ").strip()
            if typed or hint:
                values[field.var] = typed or hint


def _key_command(args: argparse.Namespace) -> int:
    root = Workspace.locate(args.workspace)
    action = args.action or "list"
    if action == "list":
        console.print(commands.keys_table(root))
        return 0
    if action == "remove":
        path, removed = keys.remove_provider(root, args.provider, args.scope)
        console.print(f"Removed {', '.join(removed)} from {path}." if removed
                      else f"Nothing saved for {args.provider} in {path}.")
        return 0
    values = keys.build_values(args.provider, args.key, _parse_extras(args.extra))
    if sys.stdin.isatty():
        _prompt_missing(args.provider, values)
    missing = [f.label for f in keys.fields_for(args.provider)
               if f.required and not (values.get(f.var) or os.environ.get(f.var))]
    if missing:
        raise keys.KeySetupError(f"{args.provider} still needs: {', '.join(missing)}")
    if not values:
        console.print("Nothing to save.")
        return 0
    console.print(commands.save_keys(root, args.provider, values, args.scope, check=not args.no_check))
    return 0


def _workspace_here(args: argparse.Namespace, cmd: str) -> Workspace:
    """Find the workspace: one at or above the current directory, else the user-wide one in AGH_HOME
    (~/.agh/workspace, beside the user-wide .env). `agh`/`agh run` create it on first use."""
    root = Workspace.locate(args.workspace)
    if root:
        return Workspace(root)
    if os.environ.get("AGH_WORKSPACE"):
        return Workspace.find(args.workspace)
    target = Path(args.workspace).resolve() if args.workspace else (keys.agh_home() / "workspace").resolve()
    if (target / CONFIG_NAME).exists():
        return Workspace(target)
    if cmd not in ("tui", "run"):
        return Workspace.find(args.workspace)
    if sys.stdin.isatty() and not Confirm.ask(f"Make {target} an AGH workspace?", default=True):
        raise WorkspaceError("no workspace; nothing created")
    ws = Workspace.init(target)
    console.print(Text(f"Created workspace at {ws.root}.", style="green"))
    if not keys.any_cloud_key():
        console.print(Text("No provider key found. Add one with `agh key set anthropic` (or openai, azure_openai, "
                           "gemini, openrouter), or `/key set PROVIDER KEY` in the UI.", style="yellow"))
    return ws


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "init":
        ws = Workspace.init(args.path)
        console.print(f"Workspace ready at {ws.root}. Add a key with `agh key set PROVIDER`, then run: agh")
        return 0
    if args.command == "key":
        return _key_command(args)
    cmd = args.command or "tui"
    ws = _workspace_here(args, cmd)
    ws.project = Path.cwd().resolve()
    if args.profile:
        if args.profile not in profiles.list_profiles(ws):
            raise WorkspaceError(f"no profile {args.profile!r}; have: {', '.join(profiles.list_profiles(ws))}")
        ws.config["profile"] = args.profile  # this process only; agh.yaml is untouched
    if cmd == "profile":
        action = args.action or "list"
        if action == "switch":
            profiles.switch(ws, args.name)
            console.print(f"Active profile: {args.name}")
        elif action == "save":
            source = ws.profile
            profiles.save(ws, args.name, " ".join(args.description))
            console.print(f"Saved profile {args.name} (from {source}) and switched to it.")
        else:
            console.print(commands.profile_text(ws, action, [args.name] if getattr(args, "name", None) else [], ""))
        return 0
    if cmd == "tui":
        from agh.tui.app import AghApp

        resume = getattr(args, "resume", None)
        if getattr(args, "continue_", False) and not resume:
            resume = latest_session(ws)
        AghApp(ws, agent=getattr(args, "agent", None), session=Session.load(ws, resume) if resume else None).run()
        return 0
    if cmd == "run":
        prompt = args.prompt
        if prompt == "-":
            if sys.stdin.isatty():
                raise WorkspaceError("no prompt: pass one, or pipe it in (echo ... | agh run)")
            prompt = sys.stdin.read().strip()
        resume = args.resume or (latest_session(ws, args.agent) if args.continue_ else None)
        return asyncio.run(_run(ws, prompt, args.agent, resume, args.mode, args.yes, args.json, args.model,
                                stream_json=args.stream_json))
    if cmd == "serve":
        return asyncio.run(_serve(ws, args.host, args.port, args.yes))
    if cmd == "trace":
        sid = args.session or latest_session(ws)
        if not sid:
            raise WorkspaceError("no sessions yet")
        console.print(Text("\n".join(trace_lines(read_trace(ws, sid))) or "(empty trace)"))
        return 0
    if cmd in ("provider", "model"):
        run = commands.provider_command if cmd == "provider" else commands.model_command
        try:
            output = run(ws, [args.name] if args.name else [])
        except ValueError as exc:
            console.print(Text(f"Error: {exc}", style="red"))
            return 2
        console.print(Text(output) if isinstance(output, str) else output)
    elif cmd == "models":
        if args.action == "set":
            commands.set_level_models(ws, args.level, args.models)
        elif args.action == "priority":
            commands.set_provider_priority(ws, args.providers)
        console.print(commands.models_table(ws))
    elif cmd == "ls":
        console.print(commands.overview(ws))
    elif cmd == "doctor":
        console.print(commands.doctor(ws))
    elif cmd == "sessions":
        action = args.action
        if action == "rename":
            rename_session(ws, args.session, " ".join(args.title))
            console.print("Renamed.")
        elif action == "delete":
            console.print(f"Deleted {', '.join(delete_session(ws, args.session))}.")
        elif action == "export":
            print(export_markdown(ws, args.session))
        else:
            found = search_sessions(ws, " ".join(args.query)) if action == "search" else list_sessions(ws)
            console.print(commands.table("Sessions", ["id", "agent", "messages", "title"],
                                         [[s["id"], s["agent"], s["messages"], s["title"]] for s in found]))
    elif cmd == "new":
        if args.ai:
            request = (f"Create a new {args.kind} named {args.name}. Requirements: "
                       f"{args.description or 'infer sensible defaults from the name'}.")
            return asyncio.run(_run(ws, request, "architect", None, "default", args.yes, False))
        console.print(f"Created {scaffold.create_element(ws, args.kind, args.name, args.description)}")
    elif cmd == "connector":
        if args.action in ("login", "logout"):
            console.print(Text(asyncio.run(commands.connector_login(ws, args.name, logout=args.action == "logout"))))
            return 0
        if args.action == "import":
            console.print(f"Created {scaffold.create_connector(ws, args.name, openapi=args.source, include=args.include)}")
        console.print(Text(asyncio.run(connectors.probe(ws, args.name))))
    elif cmd == "eval":
        return asyncio.run(_eval(ws, args.suite, args.baseline))
    elif cmd == "improve":
        out = asyncio.run(improve.improve_prompt(ws, args.agent, args.suite, rounds=args.rounds, apply=not args.dry_run))
        console.print(Text(f"Baseline {out.baseline:.2f}, best {out.best:.2f}. {out.reason}\nRecord: {out.record}"))
    elif cmd == "rollback":
        console.print(f"Restored {improve.rollback(ws, args.agent, args.version)}")
    elif cmd == "feedback":
        improve.record_feedback(ws, args.agent, args.rating, args.note)
        console.print("Feedback recorded.")
    elif cmd == "learn":
        console.print(f"Draft at {asyncio.run(improve.mine_skill(ws, args.session))}")
    elif cmd == "promote":
        console.print(f"Activated {improve.promote_skill(ws, args.agent, args.name)}")
    elif cmd == "memory":
        load_agent(ws, args.agent)
        store = MemoryStore(ws.memory_dir(args.agent))
        try:
            if args.add:
                console.print(f"Saved #{store.add(args.add, source='user', importance=0.8)}")
            elif args.forget is not None:
                console.print("Forgotten." if store.forget(args.forget) else "No such memory.")
            else:
                items = store.search(args.query, k=20) if args.query else store.all(limit=20)
                console.print(commands.table("Memory", ["id", "kind", "text"], [[i.id, i.kind, i.text] for i in items]))
        finally:
            store.close()
    return 0
