"""Permissions and guardrails.

permissions.yaml:
  default: ask            # allow | ask | deny
  allow: [read_file, "github__*", "shell(git status*)"]
  ask: [write_file]
  deny: ["shell(rm *)"]

Order: deny, plan mode, session grants, then ask and allow rules that name arguments, then ask and allow
rules that name only the tool, then the tool's own setting, then default. Where that ends in "ask", auto modes
allow instead, and edits mode does for a file edit inside the project.
"""
from __future__ import annotations

import fnmatch
import json
import posixpath
import re
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

import jsonschema

from agh.sessions import redact
from agh.tooling import ToolSpec

FILE_READERS = ["read_file", "list_dir", "search_files"]  # tools that return the workspace's private contents
# edits = file edits inside the project skip asks, everything else (shell, network) asks as in default;
# plan = read-only tools; auto = skip asks, denies still apply;
# full-auto = auto, plus calls that change agent rules no longer ask every time (deny rules and blocks still apply)
MODES = ("default", "edits", "plan", "auto", "full-auto")
AUTO_MODES = ("auto", "full-auto")
FILE_EDITORS = ("write_file", "edit_file")  # what edits mode lets through: /undo puts their changes back
SHELL_META = re.compile(r"[;&|`<>\n\r]|\$\(")  # chaining, redirection or substitution: no prefix grant covers these


@dataclass(frozen=True)
class Grant:
    """"Always this session" for one kind of call, never a whole tool that takes free-form input.

    kind: cmd   a shell command starting with these words, with no chaining or redirection
          dir   a file path inside this directory
          host  a URL on this scheme and host
          exact this argument value only
          any   every call (only for tools whose arguments are not strings)
    """
    tool: str
    kind: str
    arg: str = ""
    value: Any = ""

    def matches(self, tool: str, args: dict[str, Any]) -> bool:
        if tool != self.tool:
            return False
        if self.kind == "any":
            return True
        given = args.get(self.arg)
        if not isinstance(given, str):
            return False
        if self.kind == "cmd":
            words = given.split()
            return not SHELL_META.search(given) and words[:len(self.value)] == list(self.value)
        if self.kind == "dir":
            parent = posixpath.dirname(_norm_path(given)) or "."
            return not parent.startswith("..") and (self.value == "." or parent == self.value
                                                    or parent.startswith(self.value + "/"))
        if self.kind == "host":
            return _origin(given) == self.value
        return given == self.value

    def describe(self) -> str:
        if self.kind == "cmd":
            return f"{self.tool} commands starting `{' '.join(self.value)}`"
        if self.kind == "dir":
            return f"{self.tool} on files in {'the workspace' if self.value == '.' else self.value + '/'}"
        if self.kind == "host":
            return f"{self.tool} on {self.value}"
        if self.kind == "exact":
            return f"{self.tool} with {self.arg}={self.value!r}"
        return f"every {self.tool} call"


def _norm_path(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else ""


def suggest_grant(tool: str, args: dict[str, Any]) -> Grant:
    """The narrowest useful session grant for a call the user just approved."""
    strings = {k: v for k, v in args.items() if isinstance(v, str)}
    if "command" in strings:
        command = strings["command"]
        words = command.split()
        if SHELL_META.search(command) or not words:
            return Grant(tool, "exact", "command", command)
        prefix = words[:2] if len(words) > 1 and re.fullmatch(r"[\w.:-]+", words[1]) else words[:1]
        return Grant(tool, "cmd", "command", tuple(prefix))
    if "path" in strings:
        return Grant(tool, "dir", "path", posixpath.dirname(_norm_path(strings["path"])) or ".")
    if "url" in strings and (origin := _origin(strings["url"])):
        return Grant(tool, "host", "url", origin)
    if strings:
        arg = sorted(strings)[0]
        return Grant(tool, "exact", arg, strings[arg])
    return Grant(tool, "any")


PRIMARY_ARGS = ("command", "path", "url")


def _matches(rule: str, tool: str, args: dict[str, Any], strict: bool = False) -> bool:
    """Does a permissions.yaml rule cover this call?

    Deny and ask rules match a pattern against any string argument, which errs toward stopping a call.
    Allow rules are strict, since they let a call through unasked: the pattern is matched against the main
    argument only (command, path or url), paths are normalised first so `notes/../x` is not inside notes/, and a
    shell command that chains, redirects or substitutes only matches a pattern containing those characters.
    """
    match = re.fullmatch(r"([^()]+)\((.*)\)", rule, re.S)
    if not match:
        return fnmatch.fnmatchcase(tool, rule)
    if not fnmatch.fnmatchcase(tool, match[1]):
        return False
    pattern = match[2]
    if not strict:
        values = [v for v in args.values() if isinstance(v, str)] or [json.dumps(args, default=str)]
        return any(fnmatch.fnmatchcase(v, pattern) for v in values)
    key = next((k for k in PRIMARY_ARGS if isinstance(args.get(k), str)), None)
    if key is None:
        strings = [v for v in args.values() if isinstance(v, str)]
        return len(strings) == 1 and fnmatch.fnmatchcase(strings[0], pattern)
    value = args[key]
    if key == "command" and SHELL_META.search(value) and not SHELL_META.search(pattern):
        return False
    if key == "path":
        value = _norm_path(value)
        if value.startswith(".."):
            return False
    return fnmatch.fnmatchcase(value, pattern)


def _glob_escape(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)


def grant_rule(grant: Grant) -> str | None:
    """The permissions.yaml allow rule that keeps a session grant across sessions."""
    if grant.kind == "any":
        return grant.tool
    if grant.kind == "cmd":
        words = " ".join(_glob_escape(w) for w in grant.value)
        return f"{grant.tool}({words}*)"
    if grant.kind == "dir":
        return f"{grant.tool}(*)" if grant.value == "." else f"{grant.tool}({_glob_escape(grant.value)}/*)"
    if grant.kind == "host":
        return f"{grant.tool}({_glob_escape(grant.value)}/*)"
    if grant.kind == "exact":
        return f"{grant.tool}({_glob_escape(str(grant.value))})"
    return None


def save_allow_rule(path: Any, rule: str) -> bool:
    """Add a rule to the allow list in permissions.yaml, keeping comments. False if it was already there."""
    import yaml

    text = path.read_text(encoding="utf-8") if path.exists() else ""
    data = yaml.safe_load(text) or {}
    allow = list(data.get("allow") or [])
    if rule in allow:
        return False
    quoted = json.dumps(rule)  # a JSON string is valid YAML, whatever the rule contains
    flow = re.search(r"^allow:\s*\[(.*)\]\s*(#.*)?$", text, re.M)
    if flow:
        inner = flow[1].strip()
        line = f"allow: [{inner + ', ' if inner else ''}{quoted}]" + (f"  {flow[2]}" if flow[2] else "")
        text = text[:flow.start()] + line + text[flow.end():]
    else:
        data["allow"] = allow + [rule]
        text = yaml.safe_dump(data, sort_keys=False)
    if (yaml.safe_load(text) or {}).get("allow", [])[-1:] != [rule]:  # never write a file that parses wrong
        data["allow"] = allow + [rule]
        text = yaml.safe_dump(data, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return True


def domain_block(args: dict[str, Any], config: dict[str, Any]) -> str | None:
    """With `sandbox.allowed_domains` in agh.yaml, a call carrying a url may only reach those hosts and their
    subdomains, whatever tool makes it."""
    allowed = [d.lower().lstrip(".") for d in (config.get("sandbox") or {}).get("allowed_domains") or []]
    url = args.get("url")
    if not allowed or not isinstance(url, str):
        return None
    host = (urlsplit(url).hostname or "").lower()
    if any(host == d or host.endswith("." + d) for d in allowed):
        return None
    return f"{host or url} is not in sandbox.allowed_domains"


def decide(spec: ToolSpec, args: dict[str, Any], rules: dict[str, Any], mode: str,
           grants: set[Grant], project_edit: bool = False) -> tuple[str, str]:
    """Return (allow|ask|deny, reason). `project_edit` says the call is a file edit inside the project, which is
    what edits mode lets through without asking (see Engine._is_project_edit)."""
    unattended = mode in AUTO_MODES or (mode == "edits" and project_edit)
    for rule in rules.get("deny") or []:
        if _matches(rule, spec.name, args):
            return "deny", f"denied by rule {rule}"
    if spec.permission == "deny":
        return "deny", "tool is disabled"
    if mode == "plan" and not spec.readonly:
        return "deny", "plan mode allows read-only tools only"
    for grant in grants:
        if grant.matches(spec.name, args):
            return "allow", f"granted for this session: {grant.describe()}"
    # A rule naming arguments is more specific than one naming only the tool, so it is checked first:
    # `allow: [write_file(docs/*)]` beats `ask: [write_file]`, while `ask: [shell(git push*)]` beats both.
    for specific in (True, False):
        for rule in rules.get("ask") or []:
            if ("(" in rule) == specific and _matches(rule, spec.name, args):
                return ("allow", f"{mode} mode") if unattended else ("ask", f"rule {rule}")
        for rule in rules.get("allow") or []:
            if ("(" in rule) == specific and _matches(rule, spec.name, args, strict=True):
                return "allow", f"rule {rule}"
    verdict = spec.permission or rules.get("default", "ask")
    if verdict == "ask" and unattended:
        return "allow", f"{mode} mode"
    return verdict, "tool default"


def guard_leaks(tools: dict[str, ToolSpec], rules: dict[str, Any]) -> dict[str, ToolSpec]:
    """An agent that reads files unattended must not also reach outside unattended, or a malicious page could send
    those files anywhere. A tool that allows itself doesn't know which agent it joined (with "*" in agent.yaml it may
    be one built after the agent), so its own `allow` becomes `ask` here. Only the agent's permissions.yaml can allow
    such a tool, which is a deliberate choice for that agent. System tools are left alone: delegate's report is
    fenced as outside text, but what the subagent may reach is settled by the subagent's own permissions."""
    def verdict(spec: ToolSpec) -> str:
        return decide(spec, {}, rules, "default", set())[0]

    if not any(n in tools and verdict(tools[n]) == "allow" for n in FILE_READERS):
        return tools
    out = dict(tools)
    for name, spec in tools.items():
        if spec.untrusted and not spec.source.startswith("system:") and verdict(spec) == "allow":
            asking = replace(spec, permission="ask")
            if verdict(asking) != "allow":  # no rule in permissions.yaml allows it by name
                out[name] = asking
    return out


def apply_output_guardrails(text: str, rails: dict[str, Any]) -> tuple[str, list[str]]:
    """Redact secrets and configured patterns. Returns the clean text and what was triggered."""
    triggered = []
    clean = redact(text)
    if clean != text:
        triggered.append("secret_redacted")
    for pattern in rails.get("redact_patterns") or []:
        clean, count = re.subn(pattern, "[REDACTED]", clean)
        if count:
            triggered.append(f"redacted:{pattern}")
    for pattern in rails.get("block_patterns") or []:
        if re.search(pattern, clean):
            triggered.append(f"blocked:{pattern}")
            clean = "[response withheld by guardrail]"
            break
    return clean, triggered


def schema_error(text: str, schema: dict[str, Any]) -> str | None:
    """Validate a final answer against a required JSON schema. Tolerates a markdown code fence."""
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        jsonschema.validate(json.loads(stripped), schema)
    except json.JSONDecodeError as exc:
        return f"not valid JSON: {exc}"
    except jsonschema.ValidationError as exc:
        return f"schema violation: {exc.message}"
    return None


def escape_closing_tag(text: str, tag: str) -> str:
    """Stop fenced text from closing its own fence."""
    return re.sub(rf"</\s*{tag}\s*>", f"<\\\\/{tag}>", text, flags=re.I)


def fence_untrusted(source: str, text: str) -> str:
    """Mark external content as data so instructions inside it carry no authority."""
    safe = escape_closing_tag(text, "untrusted_data")
    return f'<untrusted_data source="{source}">\n{safe}\n</untrusted_data>'
