---
name: design-discovery
description: "Clarify intent and shape a design before a substantial or ambiguous software change. Use when a feature request leaves important product or technical choices open, or spans several parts of a project."
---

# Design discovery

Adapted from [obra/superpowers brainstorming](https://github.com/obra/superpowers/tree/main/skills/brainstorming) (MIT), for AGH's implementation workflow.

1. **Read first.** Review the request, earlier decisions, repository instructions, and relevant code before asking questions. Do not ask for facts the project already answers or reopen settled choices.
2. **Size the uncertainty.** If the request is clear and local, state any small assumption and proceed. For substantial work, identify only the unresolved choices that could change what gets built.
3. **Ask with purpose.** Ask one focused question at a time when the answer materially changes the result. Offer options only when they capture real tradeoffs; include your recommendation and its cost. Respect the user's choice to proceed with a reasonable default.
4. **Capture the design.** For a substantial feature, summarize the goal, users, constraints, non-goals, chosen approach, and open decisions. Keep this in chat for small changes; write a repository design note only when it will guide later work or needs to persist.
5. **Continue the existing flow.** Once the design is clear, follow `implementation-workflow`; use `update_todos` for work with three or more steps. A request to implement a clear design already authorizes that work; do not add a design-approval gate.

