---
name: write-evals
description: "How to write AGH eval suites that measure an agent. Use when adding or improving evals."
---

# Write evals

Each case has `id`, `input`, and `expect`, a list of checks:

- contains, not_contains, regex, equals: `{type: contains, value: "..."}`
- json_schema: `{type: json_schema, schema: {...}}`
- tool_called, tool_not_called: `{type: tool_called, value: read_file}`
- no_tool_errors; max_steps and max_tokens with `value`
- llm_judge: `{type: llm_judge, criteria: "..."}`
- python: `{type: python, function: name}`, defined in the suite's graders.py as `name(text, result, case)`.
  `result.tool_calls` lists each call's `name`, `args` and `error` in order, so graders can check ordering and who was
  delegated to. It holds this agent's calls only, so test a subagent's tool use with a case aimed at that agent.

`tool_called` counts denied calls too, so `approve: deny` can test what an agent tries without letting it act.

Optional case fields: `mock` (scripted model replies, for offline tests), `holdout: true`
(hidden from self-improvement, to catch overfitting), `agent`.

Suite fields: `agent`, `threshold`, `concurrency`, `repeat` (measures flakiness), and
`approve: deny|allow`. Keep `deny` unless the tools are safe to run for real.
Test behaviour rather than exact wording, and turn each real failure into a case. Leave the expected behaviour out of
the input: "Use the calculator to add these" can't show that the agent would have chosen to.
