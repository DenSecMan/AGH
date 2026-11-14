"""Tools that reach into the harness itself. Agents opt in by group via `system_tools:` in agent.yaml."""
from __future__ import annotations

import functools
from typing import Any

from agh import scaffold
from agh.sdk import ToolContext
from agh.sessions import redact
from agh.tooling import ToolSpec, spec_from_function
from agh.workspace import WorkspaceError


def _shown(ctx: ToolContext, path: Any) -> str:
    """A path the agent can pass straight back to file tools: relative to the tool root, else absolute."""
    path = path.resolve()
    try:
        return path.relative_to(ctx.root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()

MAX_BUILD_ATTEMPTS = 3


async def remember(text: str, ctx: ToolContext, kind: str = "semantic", tags: str = "",
                   importance: float = 0.5) -> str:
    """Save something worth knowing in future sessions.

    Args:
        text: One self-contained fact, event or procedure.
        kind: semantic (fact), episodic (event) or procedural (how-to).
        tags: Optional space-separated tags.
        importance: 0 to 1.
    """
    if not ctx.engine.memory:
        return "memory is disabled for this run"
    return f"saved memory #{ctx.engine.memory.add(text, kind, tags, importance, source=ctx.session_id)}"


async def pin_memory(text: str, ctx: ToolContext) -> str:
    """Add a note to core memory, which is in context for every task in every session. Use for standing user
    preferences and project rules, which remember would only surface on related topics. Each pin needs approval.

    Args:
        text: One short, self-contained note.
    """
    if not ctx.engine.memory:
        return "memory is disabled for this run"
    return "pinned" if ctx.engine.memory.pin(redact(text)) else "already pinned"


async def recall(query: str, ctx: ToolContext, k: int = 5, kind: str | None = None) -> str:
    """Search long-term memory.

    Args:
        query: Search terms.
        k: How many results.
        kind: Optional filter: semantic, episodic or procedural.
    """
    if not ctx.engine.memory:
        return "memory is disabled for this run"
    items = ctx.engine.memory.search(query, k=k, kind=kind)
    return "\n".join(f"#{i.id} [{i.kind}] {i.text}" for i in items) or "no matching memories"


async def forget_memory(memory_id: int, ctx: ToolContext) -> str:
    """Delete a memory that is wrong or outdated.

    Args:
        memory_id: The number shown by recall.
    """
    if not ctx.engine.memory:
        return "memory is disabled for this run"
    return "forgotten" if ctx.engine.memory.forget(memory_id) else f"no memory #{memory_id}"


async def load_skill(name: str, ctx: ToolContext, resource: str | None = None) -> str:
    """Load the full instructions of a skill from the catalog, or one of its resource files.

    Args:
        name: Skill name.
        resource: Optional relative path of a file inside the skill.
    """
    skill = ctx.engine.skills.get(name)
    if not skill:
        return f"unknown skill {name}; available: {sorted(ctx.engine.skills)}"
    if resource:
        target = (skill.path / resource).resolve()
        if not target.is_relative_to(skill.path.resolve()) or not target.is_file():
            return f"no resource {resource}; available: {skill.resources()}"
        return target.read_text(encoding="utf-8")
    extra = f"\n\nResources (load with `resource`): {skill.resources()}" if skill.resources() else ""
    return skill.body + extra


async def update_todos(todos: list[dict[str, str]], ctx: ToolContext) -> str:
    """Replace the working plan for this session. Use for tasks of three or more steps.

    Args:
        todos: Items shaped {"content": "...", "status": "pending|in_progress|done"}.
    """
    ctx.engine.session.todos = todos
    ctx.engine.emit("todos", todos=todos)
    done = sum(t.get("status") == "done" for t in todos)
    return f"plan updated: {done}/{len(todos)} done"


async def set_ui(ctx: ToolContext, theme: str | None = None, pane: str | None = None,
                 position: str | None = None, layout: list[str] | None = None, reset: bool = False) -> str:
    """Change the terminal UI: colour theme and where panes sit. Use when the user asks for a different look
    (e.g. "dark blue theme", "something light") or wants a pane moved ("chat on the left", "explorer on the
    right", "explorer under the profile"). Call with no arguments to see the current settings and the theme list.

    Args:
        theme: A theme name, e.g. nord, dracula, gruvbox, tokyo-night, catppuccin-mocha, solarized-light,
            textual-light, textual-dark. Pick the closest match to what the user describes.
        pane: The pane to move: profile (profile/agent tree), explorer (file browser), editor, or chat.
        position: Where pane goes. Its own column: left, right, center, a column number, "before PANE" or
            "after PANE". Stacked in another pane's column: "above PANE" or "below PANE" (e.g. explorer
            "below profile"). A column holds at most two panes.
        layout: Instead of pane/position, the columns left to right; "a/b" stacks a above b, e.g.
            ["profile/explorer", "editor", "chat"] (the default) or ["chat", "editor", "profile/explorer"].
            Unnamed panes get their own columns at the end.
        reset: Back to the default theme and layout before applying anything else.
    """
    from agh import ui_prefs

    state = ctx.engine.ws.state
    if not any((theme, pane, position, layout, reset)):
        return f"{ui_prefs.describe(ui_prefs.load(state))}. Themes: {', '.join(ui_prefs.themes())}"
    try:
        prefs = ui_prefs.change(state, theme, pane, position, layout, reset)
    except ValueError as exc:
        return f"not changed: {exc}"
    ctx.engine.emit("ui", **prefs)
    return f"UI updated: {ui_prefs.describe(prefs)}"


async def delegate(agent: str, task: str, ctx: ToolContext, conversation: str | None = None,
                   background: bool = False) -> str:
    """Hand a self-contained task to a subagent with its own clean context; returns its final answer.
    Multiple delegate calls in one turn run in parallel.

    Args:
        agent: Subagent name from the catalog.
        task: Complete instructions. The subagent sees nothing else from this conversation, except earlier
            turns of the same `conversation`.
        conversation: The id from an earlier answer (such as c1) to follow up with the same subagent, which
            remembers that exchange. Omit to start fresh.
        background: Return at once with a job id and keep working; collect the answer with delegate_result.
    """
    return await ctx.engine.run_subagent(agent, task, conversation=conversation, background=background)


async def delegate_result(job: str, ctx: ToolContext, wait_s: float = 0) -> str:
    """Collect the answer of a background delegation.

    Args:
        job: The job id from delegate, such as j1.
        wait_s: Seconds to wait if it is still running. 0 just checks.
    """
    return await ctx.engine.job_result(job, wait_s=min(float(wait_s), 1500))


async def create_agent(name: str, description: str, prompt: str, ctx: ToolContext,
                       tools: list[str] | None = None, system_tools: list[str] | None = None,
                       skills: list[str] | None = None, connectors: list[str] | None = None,
                       subagents: list[str] | None = None, model: str = "auto",
                       permissions: dict[str, Any] | None = None, overwrite: bool = False,
                       evals: list[dict[str, Any]] | None = None) -> str:
    """Create a new agent directory (agent.yaml, prompt.md, permissions.yaml, guardrails.yaml, memory/).
    Nothing goes live until its eval passes against the real model in a copy of the workspace.
    To change an existing agent, use update_agent.

    Args:
        evals: Required. Eval cases shaped {"id": "...", "input": "...", "expect": [{"type": "contains",
            "value": "..."}]}. Every case must pass, and they are saved as <name>-release. These cost model
            calls, so keep them few.
        name: lowercase-name.
        description: When to use this agent; shown to agents that may delegate to it.
        prompt: Complete system prompt.
        tools: Tool names; must already exist.
        system_tools: Groups from memory, skills, plan, delegate, builder, ui.
        skills: Skill names, or "*" for all and "!glob" to exclude. Omit for all except the builder skills.
        connectors: Connector names.
        subagents: Agents it may delegate to.
        model: Model id, a level (default, fast, reasoning) or auto.
        permissions: {"default": "ask", "allow": [...], "ask": [...], "deny": [...]}. Omit for read-only allows.
        overwrite: Replace the definition of an existing agent, keeping its memory, prompt history and private
            tools and skills.
    """
    path = scaffold.create_agent(ctx.engine.ws, name, description, prompt, tools, system_tools, skills,
                                 connectors, subagents, model, permissions, overwrite=overwrite, evals=evals or [],
                                 on_usage=ctx.engine.add_usage)
    return f"created agent at {_shown(ctx, path)}"


async def update_agent(name: str, ctx: ToolContext, description: str | None = None, prompt: str | None = None,
                       tools: list[str] | None = None, system_tools: list[str] | None = None,
                       skills: list[str] | None = None, connectors: list[str] | None = None,
                       subagents: list[str] | None = None, model: str | None = None,
                       permissions: dict[str, Any] | None = None, limits: dict[str, Any] | None = None,
                       evals: list[dict[str, Any]] | None = None) -> str:
    """Change an existing agent, keeping its memory and history. Only the fields you pass change, and a list
    you pass replaces the old list, so include what should stay. Goes live only if its release eval passes.

    Args:
        name: The agent to change.
        description: New description.
        prompt: New complete system prompt. The old one stays in versions/ for rollback.
        tools: New complete tool list.
        system_tools: New complete list of groups.
        skills: New complete skill list.
        connectors: New complete connector list.
        subagents: New complete subagent list.
        model: New model id or level.
        permissions: New complete permissions.
        limits: Limits to change; others stay.
        evals: Cases added to the agent's <name>-release suite, replacing any with the same id. Required if the
            agent has no such suite yet.
    """
    changes = {"description": description, "prompt": prompt, "tools": tools, "system_tools": system_tools,
               "skills": skills, "connectors": connectors, "subagents": subagents, "model": model,
               "permissions": permissions, "limits": limits}
    path = scaffold.update_agent(ctx.engine.ws, name, {k: v for k, v in changes.items() if v is not None},
                                 evals=evals, on_usage=ctx.engine.add_usage)
    return f"updated agent at {_shown(ctx, path)}"


async def create_skill(name: str, description: str, body: str, ctx: ToolContext,
                       resources: dict[str, str] | None = None, overwrite: bool = False) -> str:
    """Create skills/<name>/SKILL.md, an on-demand procedure loaded with load_skill.

    Args:
        name: lowercase-name.
        description: What it does and when to use it. This is the trigger, so be specific.
        body: Markdown instructions.
        resources: Optional {relative_path: text} supporting files.
        overwrite: Replace an existing skill.
    """
    path = scaffold.create_skill(ctx.engine.ws, name, description, body, resources, overwrite)
    await ctx.engine.refresh()
    return f"created skill at {_shown(ctx, path)}"


async def create_tool(name: str, description: str, code: str, ctx: ToolContext, readonly: bool = False,
                      permission: str | None = None, timeout: int = 120, untrusted: bool = False,
                      overwrite: bool = False, tests: list[dict[str, Any]] | None = None) -> str:
    """Create tools/<name>/{tool.yaml,tool.py}. Nothing goes live until its tests pass.

    Args:
        name: lowercase_name.
        description: What the tool does, for the model.
        code: Python defining a typed `def run(...)` with a Google-style docstring. Add a `ctx` parameter
            to get agh.sdk.ToolContext; use ctx.resolve(path) for any file access.
        tests: Required. Items shaped {"args": {...}, "expect": "text in the output"}. They really call
            `run`, so choose arguments that are safe to execute.
        readonly: True if it has no side effects.
        permission: allow, ask or deny. Omit to use each agent's default.
        timeout: Seconds.
        untrusted: True if it returns external content.
        overwrite: Replace an existing tool.
    """
    path = scaffold.create_tool(ctx.engine.ws, name, description, code, readonly, permission, timeout,
                                untrusted, overwrite, tests)
    return f"created tool at {_shown(ctx, path)}; give it to an agent with update_agent or create_agent"


async def create_connector(name: str, ctx: ToolContext, config: dict[str, Any] | None = None,
                           openapi: str | None = None, include: str | None = None,
                           overwrite: bool = False) -> str:
    """Create connectors/<name>/connector.yaml for an API service or MCP server. Nothing goes live until one
    real read-only call succeeds (MCP: the server starts and lists tools), so credentials must already be set.

    Args:
        name: lowercase-name.
        config: Full connector config. Load the `build-connector` skill for the format. Never put secrets
            here; reference environment variables by name. Add test: {operation: NAME, args: {...}} if no
            read operation works without arguments.
        openapi: URL or path of an OpenAPI spec to import instead of config.
        include: Regex limiting which OpenAPI operation ids are imported.
        overwrite: Replace an existing connector.
    """
    path = scaffold.create_connector(ctx.engine.ws, name, config, openapi, include, overwrite)
    return f"created connector at {_shown(ctx, path)}"


async def create_hook(name: str, description: str, events: list[str], code: str, ctx: ToolContext,
                      tools: str = "*", overwrite: bool = False) -> str:
    """Create hooks/<name>/{hook.yaml,hook.py}. Called once per event as a test, then active from the next
    turn for all agents.

    Args:
        name: lowercase-name.
        description: Purpose.
        events: From session_start, pre_tool, post_tool, turn_end.
        code: Python defining `def handle(event: dict) -> dict | None`. Test calls set event["dry_run"] to
            True; skip side effects when it is.
        tools: Glob of tool names to match.
        overwrite: Replace an existing hook.
    """
    path = scaffold.create_hook(ctx.engine.ws, name, description, events, code, tools, overwrite)
    return f"created hook at {_shown(ctx, path)}"


async def create_eval(name: str, agent: str, cases: list[dict[str, Any]], ctx: ToolContext,
                      threshold: float = 0.8, overwrite: bool = False) -> str:
    """Create evals/<name>/suite.yaml.

    Args:
        name: lowercase-name.
        agent: Agent under test.
        cases: Items like {"id": "...", "input": "...", "expect": [{"type": "contains", "value": "..."}]}.
        threshold: Minimum pass rate.
        overwrite: Replace an existing suite.
    """
    path = scaffold.create_eval(ctx.engine.ws, name, agent, cases, threshold, overwrite)
    return f"created eval suite at {_shown(ctx, path)}"


async def list_elements(ctx: ToolContext) -> str:
    """List every agent, skill, tool, connector, hook and eval suite in the workspace."""
    from agh.workspace import KINDS

    ws = ctx.engine.ws
    return "\n".join(f"{kind}: {', '.join(ws.names(kind)) or '(none)'}" for kind in KINDS)


async def read_element(kind: str, name: str, ctx: ToolContext) -> str:
    """Show an existing element's files (agent.yaml, prompt.md, permissions.yaml, tool code, SKILL.md...).
    Elements live in the AGH profile, outside the project, so file tools cannot reach them; use this instead.

    Args:
        kind: One of agents, skills, tools, connectors, hooks, evals.
        name: The element's name, as list_elements shows it.
    """
    from agh.workspace import KINDS

    if kind not in KINDS:
        return f"unknown kind {kind}; use one of {', '.join(KINDS)}"
    ws = ctx.engine.ws
    if name not in ws.names(kind):
        return f"no {kind[:-1]} named {name}; {kind}: {', '.join(ws.names(kind)) or '(none)'}"
    root = ws.element(kind, name)
    parts = [f"{kind[:-1]} {name} at {root.as_posix()}"]
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            parts.append(f"--- {rel} (binary, {path.stat().st_size} bytes)")
            continue
        parts.append(f"--- {rel}\n{redact(text)[:8000]}")
    return "\n\n".join(parts)


def _limit_attempts(fn: Any) -> Any:
    """Stop a build after MAX_BUILD_ATTEMPTS failures on one element in a run. A success clears the count."""
    @functools.wraps(fn)
    async def wrapper(**kwargs: Any) -> str:
        failures = kwargs["ctx"].engine.root.build_failures
        key = (fn.__name__, kwargs["name"])
        count, last = failures.get(key, (0, ""))
        if count >= MAX_BUILD_ATTEMPTS:
            raise WorkspaceError(f"attempt limit reached: {kwargs['name']} failed {count} times this run. Do not try "
                                 f"again or build it under another name. Tell the user this last error: {last}")
        try:
            result = await fn(**kwargs)
        except Exception as exc:
            failures[key] = (count + 1, str(exc))
            raise
        failures.pop(key, None)
        return result
    return wrapper


# (function, readonly, permission)
GROUPS: dict[str, list[tuple[Any, bool, str | None]]] = {
    # pin_memory asks because core memory is trusted and always in context, unlike the fenced recalled memories.
    "memory": [(remember, False, "allow"), (pin_memory, False, "ask"), (recall, True, "allow"),
               (forget_memory, False, "allow")],
    "skills": [(load_skill, True, "allow")],
    "plan": [(update_todos, True, "allow")],
    "ui": [(set_ui, True, "allow")],  # only touches the user's display preferences
    "delegate": [(delegate, True, "allow"), (delegate_result, True, "allow")],
    "builder": [(_limit_attempts(create_agent), False, "ask"), (_limit_attempts(update_agent), False, "ask"),
                (_limit_attempts(create_skill), False, "ask"),
                (_limit_attempts(create_tool), False, "ask"), (_limit_attempts(create_connector), False, "ask"),
                (_limit_attempts(create_hook), False, "ask"), (_limit_attempts(create_eval), False, "ask"),
                (list_elements, True, "allow"), (read_element, True, "allow")],
}


def system_tool_specs(groups: list[str]) -> list[ToolSpec]:
    specs = []
    for group in groups:
        for fn, readonly, permission in GROUPS.get(group, []):
            # A subagent may have read the web, so its report must not reach the parent as trusted text.
            specs.append(spec_from_function(fn, readonly=readonly, permission=permission, timeout=1800,
                                            untrusted=fn in (delegate, delegate_result), source=f"system:{group}"))
    return specs
