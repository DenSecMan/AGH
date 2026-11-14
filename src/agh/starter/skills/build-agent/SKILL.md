---
name: build-agent
description: "How to design or change an AGH agent. Use before calling create_agent or update_agent."
---

# Build an agent

1. One clear job per agent; the description tells other agents when to delegate to it.
2. Prompt: role, how to work, and what done looks like. Avoid generic filler.
3. Least privilege: only the tools it needs. The default is ask for anything not on the allow list.
4. An agent that reads untrusted content (web, external APIs) gets no private data: leave out file tools and memory,
   and have a parent pass it excerpts. Reading private files and auto-allowed web access in one agent lets a malicious
   page leak data.
5. system_tools groups: memory, skills, plan, delegate, builder, ui (theme and pane layout of the terminal UI). Grant builder rarely.
6. List `subagents` to let it delegate, and include the delegate group.
7. `skills`: omit for everything except the builder skills. `"*"` means all and `"!glob"` excludes.
8. `create_agent` requires `evals`: cases shaped `{"id", "input", "expect": [...]}`, no `mock`. They run on the real
   model in a copy of the workspace, and every case must pass before the agent is released. Keep them few, since each
   costs model calls and counts against the run's budget. Prefer checks that a correct answer can't miss.
9. Those evals can't approve anything, so a tool that asks is denied there. Read-only tools are allowed by default. If
   a case needs a tool that writes, pass `permissions` allowing it or test it another way. A failed release names any
   denied tool.

To change an agent, call `update_agent` with only what changes. A list you pass replaces the old list, so include what
stays. It keeps the agent's memory and history, reruns its `<name>-release` suite and adds any `evals` you pass. New
tools get default permissions unless you pass `permissions`, which replaces the old set.
