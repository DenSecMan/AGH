# AGH: Agent Harness

A file-first agent harness for the terminal, built on Python, LangChain, Textual, Rich and PyYAML.
Everything an agent is made of is a plain file in its own directory, so you can read, edit and version it.
The agents can also create more of it for you.

## Quick start

Install the `agh` command once, for your user (uses uv, then pipx, then a private venv in `~/.agh/venv`):

```bash
install.bat                                            # Windows, from Command Prompt or double-click
powershell -ExecutionPolicy Bypass -File install.ps1   # Windows, from PowerShell
./install.sh                                           # macOS / Linux
# add -Editable (Windows) or AGH_EDITABLE=1 (macOS/Linux) to install from this checkout for development
```

Then add a key and run `agh` from any folder. Outside a workspace it uses the user-wide one at
`~/.agh/workspace`, beside `~/.agh/.env` (created on first run, you're asked first). Inside a folder with
`agh.yaml` at or above it, `agh` uses that one; `agh -w DIR` or `agh init DIR` makes a separate workspace.

```bash
agh key set anthropic     # typed hidden, saved to ~/.agh/.env, checked against the provider
agh                       # opens the terminal UI on ~/.agh/workspace (created on first run)
agh doctor                # check setup
```

For development: `pip install -e .[dev]` and `pytest`. Optional extras: `agh[mcp]` for MCP connectors,
`agh[langchain]` for model providers beyond the built-in ones.

## API keys

```
agh key                                   list what is set, masked, and where it came from
agh key set PROVIDER [KEY] [NAME=VALUE]   save a key; prompts (hidden) for anything missing
agh key set azure_openai KEY endpoint=https://... deployment=gpt-5
agh key set ollama url=http://gpu-box:11434/v1
agh key remove PROVIDER
```

Providers: `anthropic`, `openai`, `azure_openai`, `gemini`, `openrouter`, `ollama`, `lmstudio`, `vllm`, plus `tavily`
and `brave` for web search. Keys go to the user-wide `~/.agh/.env` (moved by `AGH_HOME`), shared by every workspace.
`--workspace-only` saves to this workspace's `.env` instead, which wins over the user-wide file. In the UI, `/key`
does the same; the key is masked in the transcript.

On first start (or whenever no working provider and model are selected) AGH opens a setup wizard: pick a provider,
fill in what it needs (API key, and endpoint/deployment for Azure), and it checks the details, sends the chosen model
a test message, saves everything to `~/.agh/.env` and `agh.yaml`, then starts. Run `/setup` to do it again.

Then ask for something new, in the UI:

```
/build Create a GitHub connector from their OpenAPI spec, and a triage agent that uses it, with evals.
```

Or manage one element at a time in the active profile (KIND is agent, skill, tool, connector, hook or eval):

```
/build new agent pentester You are an experienced principal pentesting engineer.
/build edit agent pentester prompt Focus on web applications.
/build delete agent pentester          (then /build delete agent pentester confirm)
/build restore agent reviewer          brings back a default element deleted in this profile
/build list
```

Type `/` for a menu of every command (alphabetical; Up/Down, Enter or Tab to pick, Esc to close); after `/build`
it lists the next word at each step. Tab completes every word too.

Ask the agent to change the look, for example "use a light theme" or "put the chat on the left", or use `/theme NAME`
and `/layout PANE left|right|center|before PANE|after PANE|above PANE|below PANE` (panes: profile, explorer,
editor, chat; by default the explorer sits below the profile, and a column holds at most two panes). Choices are
saved in `state/ui.json`.

Or from the shell: `agh new tool word_count "Count words in a file" --ai`.

## Azure OpenAI / Foundry

Set `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION` and
`AZURE_OPENAI_DEPLOYMENT_NAME_CHAT`, plus optionally `_FAST` and `_REASONING`, in `.env`. Azure is first in auto-detection by default.
In `agent.yaml`, `model: auto` uses CHAT, `model: fast` or `model: reasoning` pick a level, and
`model: azure_openai:<deployment>` names one directly. Compaction and reflection use FAST. For hybrid memory search, set
`memory.embeddings: azure_openai:<embedding deployment>` in `agh.yaml`.

## Selecting a provider and model

Supported: `azure_openai`, `anthropic`, `openai`, `gemini`, `openrouter`, `ollama`, `lmstudio`, `vllm`. The last five
use the OpenAI-compatible API, with URLs from `<NAME>_BASE_URL` in `.env`.

```
/provider            list providers          /provider gemini     select one
/model               list its live models    /model 2             select by name or number
/status              provider, model, level, first-token time, tokens per second
/provider auto       clear the selection
```

The selection is saved in `agh.yaml` under `models.selected`. It applies to every agent, subagent, compaction and
reflection, with no fallback to another provider. `/model` shows nothing until a provider is selected. Evals and
`agh improve` still use each agent's own `model`. Shell equivalents: `agh provider`, `agh model`.

## Choosing models and priorities

Models are tried best first. When one fails after retries, or on an auth error, the next takes over. Entries whose
provider has no credentials are skipped.

```yaml
# agh.yaml
models:
  providers: [anthropic, azure_openai, openai]   # the order `auto` tries them
  default: [anthropic:claude-opus-5-5, ollama:llama3.2, auto]   # a list; `auto` expands in place
  fast: openai:gpt-5-mini
```

In `agent.yaml`, `model` takes the same forms, and `fallbacks` are tried after it. `model_params` apply only to models
from the primary's provider. Compaction and reflection use only the first available `fast` model.

```
agh models                                   show the resolved order
agh models set default MODEL [MODEL...]      save a level's models
agh models priority anthropic openai         save the order for auto
agh run "prompt" --model openai:gpt-5        one run, no fallbacks
```

In the UI, `/models` does the same. These priorities apply when no provider is selected.

## The default agents

| Agent | Job | Reaches the web | Reads workspace files |
|---|---|---|---|
| `assistant` | General work, and delegates to the other two | `web_fetch` with approval | Yes |
| `researcher` | Web research with cited sources | `web_search`, `web_fetch` without approval | No, so give it excerpts in its task |
| `architect` | Creates and changes agents, skills, tools, connectors, hooks and evals, each behind a release test | Through `researcher` | Yes |

`researcher` holds no files and no memory on purpose: it reads untrusted pages unattended, so a malicious page finds
nothing private to steal. `web_search` works without a key through DuckDuckGo's HTML page, which can be rate limited.
For reliable search, set `TAVILY_API_KEY` or `BRAVE_SEARCH_API_KEY` in `.env`.

`architect` asks for the `reasoning` level, which falls back to the default model if you haven't configured one. A
provider chosen with `/provider` overrides this, as it does every agent's `model`.

## Profiles

A profile is a set of agents, skills, tools, connectors, hooks and evals for one kind of work, such as security
or web development, so `default` doesn't fill up with everything you've ever built.

```
/profile                         list profiles and what each adds
/profile save cyber Security work    save what you've added as a new profile and switch to it
/profile default                 back to the starter, exactly as installed
/profile cyber                   switch (a new session starts; the agent stays if the profile has it)
/profile show cyber              what it adds or changes compared with default
/profile delete cyber            moved to .agh-backups/, with its memory
/profile discard                 drop unsaved changes made on default
agh -p cyber run "..."           use a profile for one command without switching
```

- `default` is the shipped starter and can't be changed, saved over or deleted. It's checked on every start and put
  back if anything altered it, so it always matches a fresh install, and a harness upgrade updates it.
- On `default`, anything you or the agents create is kept as **unsaved changes** until `/profile save NAME`. It
  stays on disk if you switch away, and `/profile` shows it.
- A saved profile holds only what it adds or changes; everything else comes from `default`. Changing a default
  agent in a profile (with `update_agent`, `/improve`, `/rollback` or a saved permission rule) copies it into the
  profile first, so `default` and other profiles are unaffected.
- **Memory and learning are per profile.** Each profile has its own memory (core notes and recalled memories), eval
  results and baselines, feedback, improvement records and drafted skills, under `data/<profile>/`. A new profile
  starts with a copy of the memory of the profile it was saved from, then the two grow apart. Your preferences
  carry into a new profile, but what a security agent learns stays out of `default`.
- `/init` looks at the folder you started `agh` in and writes its `AGENTS.md` (improving one that exists; the
  old one is kept as `AGENTS.md.bak`). Agents read that file, or `CLAUDE.md` if there is no `AGENTS.md`.
- Instructions for every profile go in `AGENTS.md` at the workspace root; `profiles/<name>/AGENTS.md` adds
  instructions for one profile. `profiles/<name>/profile.yaml` can set `default_agent` and hide default elements
  with `exclude: {tools: [web_search]}`.
- Sessions remember their profile, and `/resume` switches back to it.
- A workspace from before profiles is converted on first start: anything that differs from the starter becomes the
  profile `custom`, memory moves to `data/`, and the old folders are kept in `.agh-backups/`.

## Workspace layout

```
AGENTS.md                    optional standing instructions for every agent in every profile
agh.yaml                     models, pricing, sandbox settings, and `profile:` (the active one)
.env                         secrets (referenced by name, never stored in element files)
profiles/
  default/                   the shipped starter, kept identical to a fresh install; nothing writes here
  .draft/                    unsaved changes made while on default
  <name>/                    profile.yaml, optional AGENTS.md, and the elements this profile adds or changes:
    agents/<name>/
      agent.yaml             model, fallbacks, tools, skills, connectors, subagents, limits
      prompt.md              system prompt
      permissions.yaml       allow / ask / deny rules
      guardrails.yaml        redaction, blocked patterns, required JSON output schema
      memory/core.md         the core notes a new agent starts with
      tools/, skills/        optional private ones
      versions/              prompt history for rollback
    tools/<name>/            tool.yaml + tool.py (a typed `run` function)
    skills/<name>/           SKILL.md + optional resources, loaded on demand
    connectors/<name>/       connector.yaml for a REST API or MCP server
    hooks/<name>/            hook.yaml + hook.py, lifecycle interception
    evals/<name>/            suite.yaml, optional graders.py
data/<profile>/
  memory/<agent>/            core.md (always in context) and memory.db (searchable memories)
  evals/<suite>/             results/ and baseline.json
  improvements/<agent>/      feedback, improvement records, drafted skills
state/                       sessions, traces, spilled tool output, checkpoints
```

## Features

| Area | What you get |
|---|---|
| **Models** | Any LangChain provider, auto-detection by API key, per-agent model and fallbacks, retries that wait out rate limits (honouring `Retry-After`), streaming, reasoning display, Anthropic prompt caching |
| **Tools** | Typed function = schema. Parallel read-only calls, timeouts, oversized output spills to disk, loop detection |
| **Code work** | `find_files` and `search_files` (ripgrep when installed, skipping git-ignored files), `edit_file` with `replace_all` and all-or-nothing multi-edits that refuse a file changed since it was read, read-only `git`, `shell(background=true)` plus `process` for dev servers, `view_image`, PDF text in `read_file` (with `pypdf`), diffs in approvals, `/undo` |
| **Terminal UI** | Multi-line prompt (Enter sends, Ctrl+J new line), history on Up/Down, `@path` attaches a file (Tab completes it), Tab completes commands and their arguments, messages typed during a run are queued, a bell when a run needs you or a long one ends |
| **Skills** | Progressive disclosure: only descriptions sit in context until `load_skill` |
| **Memory** | Core notes that are always in context (`pin_memory`), plus semantic, episodic and procedural stores recalled by relevance: BM25 + importance + recency, optional embeddings that also find memories sharing no words with the request, reflection |
| **Context** | Clears stale tool output first, then summarizes; originals are archived on disk |
| **Subagents** | Isolated context, parallel delegation, background jobs (`delegate(background=true)`, `delegate_result`), follow-ups that continue a subagent's conversation, shared budget, depth limit, reports fenced as data |
| **Sessions** | Durable, resumable (`agh -c` for the latest), forkable, searchable, renameable, exportable to Markdown, repaired after interruption; JSONL traces with a readable timeline (`agh trace`) |
| **Headless** | `agh run` reads stdin and streams JSON events (`--stream-json`); `agh serve` is a local HTTP API with a bearer token, and runs scheduled `triggers:` from `agh.yaml` |
| **Safety** | allow/ask/deny rules with argument globs, edits, plan and auto modes, approval dialogs, workspace file sandbox, fail-closed hooks, external content fenced as data, secret redaction |
| **Budgets** | Steps, tokens, cost, wall clock, across subagents and release tests. A run stopped by a limit spends one tool-free call reporting what it learned, which can go slightly over budget |
| **Structured output** | JSON schema enforced with automatic retries |
| **Connectors** | REST with bearer, API key, basic, OAuth2 client credentials and OAuth2 sign-in as the user (`agh connector login`, PKCE, auto refresh); rate limits; safe retries; OpenAPI import; MCP servers over stdio or HTTP, kept running per session, with their resources as tools |
| **Evals** | 12 grader types including LLM judge and custom Python, mocks for offline runs, repeats, baselines, regression diffs |
| **Self-improvement** | Feedback capture, prompt rewrites gated by evals (holdout cases hidden from the proposer, no regressions allowed), versioning and rollback, skill mining from sessions |
| **Self-extension** | Agents create agents, skills, tools, connectors, hooks and evals. Generated code is import-checked in a child process and always requires approval |

## Commands

```
agh                              terminal UI (--agent, --resume, -c for the latest session)
agh run "prompt" [--json]        one-shot, for scripts and CI (--yes, --mode edits|plan|auto|full-auto, -c, --stream-json)
echo "prompt" | agh run          the prompt from stdin
agh serve [--port 8787] [--yes]  local HTTP API and scheduled triggers
agh key [set|remove]             provider API keys
agh sessions [search Q|rename ID TITLE|delete ID|export ID]
agh trace [SESSION]              timeline of tool calls, notices and outcomes
agh connector login|logout NAME  OAuth sign-in for a connector
agh new KIND NAME [DESC] [--ai]  agent | skill | tool | connector | hook | eval
agh connector import NAME SPEC   from an OpenAPI URL or file (--include REGEX)
agh eval SUITE [--baseline]      exit code reflects pass/fail
agh improve AGENT SUITE          eval-gated prompt improvement (--dry-run)
agh rollback AGENT               restore the previous prompt
agh feedback AGENT good|bad NOTE
agh learn SESSION / agh promote AGENT NAME
agh memory AGENT [QUERY]         (--add, --forget)
agh models [set|priority]        show or change model selection
agh ls | sessions | doctor
```

In the UI, `/help` lists slash commands. `/reset` previews and `/reset confirm` restores the workspace to its defaults,
keeping `.env` and moving everything replaced to `.agh-backups/`. Keys: Shift+Tab cycles mode (default, edits, plan, auto, full-auto), Ctrl+C cancels a run, Ctrl+B toggles the
sidebar, Ctrl+Q quits. Select a file in the tree to view it. Enter sends, Ctrl+J adds a line, Up/Down recall earlier
messages, and `@path` attaches a file's text to the message.

## Connecting an API

```bash
agh connector import petstore https://petstore3.swagger.io/api/v3/openapi.json --include "pet"
```

Then add `petstore` to `connectors:` in an agent's `agent.yaml`, or ask the architect to with `/build`, and set any
environment variable named in `connector.yaml`. Each operation becomes a `petstore__<operation>` tool. Reads default to allow and writes to ask.
`skills/build-connector/SKILL.md` documents the format, including MCP servers.

MCP connectors need the optional client: `pip install "agh[mcp]"` into the environment `agh` runs from (the
installers leave it out). MCP servers start on first use and keep running for the session, shared with subagents, so servers that hold state
(a browser, a login) keep it between calls. One restarts if its `connector.yaml` changes or it exits, and all of them
stop when you quit, switch agent or start a new session.

For a service that acts as you (GitHub, Google, Slack), use `auth: {type: oauth2_authorization_code, authorize_url,
token_url, client_id_env, client_secret_env, scopes, redirect_port}` and run `agh connector login NAME` once; register
`http://127.0.0.1:<redirect_port>/callback` with the provider. Tokens are kept in `~/.agh/tokens/`, outside the
workspace, and refreshed automatically. MCP servers over HTTP accept the same `auth`.

## Running headless

```bash
echo "Summarise notes/today.md" | agh run --stream-json     # one JSON object per event, then the result
AGH_SERVE_TOKEN=secret agh serve                            # POST /v1/runs, GET /v1/sessions, GET /health
curl -N -H "Authorization: Bearer secret" -d '{"prompt":"hi"}' http://127.0.0.1:8787/v1/runs
```

The server listens on 127.0.0.1 and denies any tool call that would ask, unless started with `--yes`. Triggers run
while it is up, and each run is appended to `state/triggers/<name>.jsonl`:

```yaml
# agh.yaml
triggers:
  - name: morning-digest
    every: 24h
    agent: assistant
    prompt: Summarise yesterday's changes in notes/.
```

## Security notes

- The file sandbox covers the built-in file tools. **`shell` and custom tools run with your user's full rights**, so
  approval is the real control there. The dangerous-command hook is a backstop, not a sandbox. For untrusted work set
  `sandbox: {shell: docker, image: python:3.12-slim}` in `agh.yaml`: shell commands then run in a throwaway container
  that sees only the workspace and, unless `network: true`, has no network.
- `sandbox.allowed_domains: [example.com]` limits every tool call that carries a `url` to those hosts and their
  subdomains.
- "Save rule (p)" in an approval writes the same narrow pattern to the agent's `permissions.yaml`. Allow rules match
  only the main argument (command, path or url), and never a chained or redirected command unless the pattern says
  so. A rule naming arguments beats one naming only the tool, so `allow: [write_file(docs/*)]` works alongside
  `ask: [write_file]`. Deny rules still match any argument.
- "Always this session" in an approval covers only calls like the one approved: a shell command starting with the
  same words (`git status`, never `git push`, and never anything chained with `;`, `&&`, `|` or redirected), a file in
  the same directory or below, or a URL on the same host. The dialog shows exactly what it will allow.
- `AGENTS.md` is trusted prompt text, so writing it asks every time, like the rule files. `researcher` doesn't see it
  (`project_instructions: false`), because it reads the open web unattended.
- A shell command that times out or is cancelled with Ctrl+C is killed along with every process it started.
- Approving a file write shows a diff. Before any tool with a `path` argument writes, the file's old contents are
  saved in `state/checkpoints/`; `/undo` puts back everything the last turn changed (`/undo list` shows the history).
  What shell commands change is not covered.
- The UI starts in `edits` mode: `write_file` and `edit_file` run without asking on files inside the folder you
  started `agh` in (not under `.git`), because `/undo` puts those changes back. Shell commands, network calls and
  everything else still ask. `agh run` and `agh serve` start in `default`, which asks for edits too.
- `auto` mode and `--yes` skip approvals, including for generated code. In `auto` the agent can read your files and
  reach the network unattended, so text it reads could get those files sent somewhere; use it for work you trust.
- Changes to `agh.yaml`, `.env` and an agent's `agent.yaml`, `permissions.yaml` or `guardrails.yaml` ask every time,
  even with a session grant or in `auto` mode, so an agent can't quietly loosen its own rules. Only `--yes` skips this.
- No default agent can both read workspace files and reach the web without approval. A chain can still do it: `assistant`
  can put file contents in a task for `researcher`, which can send them to a URL. The fenced reports make that hard to
  trigger, not impossible, so review delegations when working with sensitive data.
- Evals run real tools. Suites default to `approve: deny`, which blocks anything needing approval.
- Redaction matches values of environment variables with names like KEY, TOKEN or SECRET. It cannot catch secrets it
  doesn't know about.
- Fencing external content lowers prompt-injection risk but does not eliminate it. Keep risky tools on `ask`.

## Tracing

Traces are written to `state/traces/`; `agh trace` or `/trace` shows one as a timeline. Because this is LangChain,
setting `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` also sends runs to LangSmith.

Cost budgets (`max_cost_usd`) only count models listed under `pricing:` in `agh.yaml`; a run says so once when it
uses a model without prices, instead of silently not enforcing the budget.

## Tests

`pytest` runs offline: a scripted model, plus the real OpenAI and Anthropic clients against local servers.
