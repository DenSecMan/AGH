---
name: subagent-driven-development
description: "Coordinate implementation as small, reviewed tasks using AGH's delegate tools. Use when a change has multiple bounded tasks and a suitable implementation subagent is available."
---

# Subagent-driven development

Adapted from [obra/superpowers subagent-driven-development](https://github.com/obra/superpowers/tree/main/skills/subagent-driven-development) (MIT), for AGH's agent catalog and shared workspace.

1. **Plan the slices.** Read the relevant code and split the work into small tasks with clear acceptance criteria. Use `update_todos` for three or more steps. Keep dependencies and overlapping files visible.
2. **Check the agent catalog.** Delegate implementation only to an available agent suited to write code. Use a reviewer for read-only review, and a researcher for external facts. If no suitable implementer is available, work inline; do not ask a read-only agent to make code changes.
3. **Give a complete brief.** Each `delegate` task must include the goal, relevant files or excerpts, constraints, expected result, and checks to run. A subagent has no other conversation context. State any user authorization limits that apply.
4. **Control overlap.** Finish and inspect one implementation task before dispatching another when they may touch the same files or workspace. Run edits in parallel only when the tasks are independent and the environment isolates their changes.
5. **Review each result.** Read the changed files and diff yourself. For meaningful slices, ask the available reviewer to check the task against its acceptance criteria and for regressions. Resolve confirmed issues before marking the task done; do not treat an agent's report as proof.
6. **Close the plan.** After all slices, review the combined change and required project checks. Report completed work, evidence, and unresolved findings. Do not repeat tests a worker just ran unless integration changed the code or project policy requires it.

