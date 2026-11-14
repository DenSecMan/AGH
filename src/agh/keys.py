"""API keys and provider settings, kept in .env files.

Two places, applied in this order so the more specific one wins:
  ~/.agh/.env          user-wide, shared by every workspace (AGH_HOME moves it)
  <workspace>/.env     this workspace only
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


@dataclass(frozen=True)
class Field:
    var: str
    label: str
    secret: bool = False
    required: bool = False
    default: str = ""


# What each provider needs. The first secret field is "the key" for `agh key set PROVIDER KEY`.
FIELDS: dict[str, list[Field]] = {
    "anthropic": [Field("ANTHROPIC_API_KEY", "API key", secret=True, required=True)],
    "openai": [Field("OPENAI_API_KEY", "API key", secret=True, required=True)],
    "azure_openai": [
        Field("AZURE_OPENAI_API_KEY", "API key", secret=True, required=True),
        Field("AZURE_OPENAI_ENDPOINT", "endpoint URL", required=True),
        Field("AZURE_OPENAI_DEPLOYMENT_NAME_CHAT", "chat deployment name", required=True),
        Field("AZURE_OPENAI_API_VERSION", "API version", default="2025-04-01-preview"),
        Field("AZURE_OPENAI_DEPLOYMENT_NAME_FAST", "fast deployment name (optional)"),
        Field("AZURE_OPENAI_DEPLOYMENT_NAME_REASONING", "reasoning deployment name (optional)"),
    ],
    "gemini": [Field("GEMINI_API_KEY", "API key", secret=True, required=True),
               Field("GEMINI_BASE_URL", "base URL (optional)")],
    "openrouter": [Field("OPENROUTER_API_KEY", "API key", secret=True, required=True),
                   Field("OPENROUTER_BASE_URL", "base URL (optional)")],
    "ollama": [Field("OLLAMA_API_KEY", "API key (optional)", secret=True),
               Field("OLLAMA_BASE_URL", "base URL", default="http://localhost:11434/v1")],
    "lmstudio": [Field("LLMSTUDIO_API_KEY", "API key (optional)", secret=True),
                 Field("LLMSTUDIO_BASE_URL", "base URL", default="http://localhost:1234/v1")],
    "vllm": [Field("VLLM_API_KEY", "API key (optional)", secret=True),
             Field("VLLM_BASE_URL", "base URL", default="http://localhost:8000/v1")],
    "tavily": [Field("TAVILY_API_KEY", "API key (web search)", secret=True, required=True)],
    "brave": [Field("BRAVE_SEARCH_API_KEY", "API key (web search)", secret=True, required=True)],
}
LLM_PROVIDERS = [p for p in FIELDS if p not in ("tavily", "brave")]


class KeySetupError(ValueError):
    pass


def agh_home() -> Path:
    return Path(os.environ.get("AGH_HOME") or Path.home() / ".agh").expanduser()


def global_env_path() -> Path:
    return agh_home() / ".env"


def parse_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#"):
            continue
        if m := ENV_LINE.match(line):
            out[m[1]] = m[2].strip().strip("'\"")
    return out


def load_env_file(path: Path) -> None:
    """A non-empty value in the file beats the process environment: it is the more specific setting,
    and a stale machine-wide key must not silently override it."""
    for key, value in parse_env(path).items():
        if value:
            os.environ[key] = value


def _quote(value: str) -> str:
    return f'"{value}"' if re.search(r"\s|#|['\"]", value) else value


def write_values(path: Path, values: dict[str, str | None]) -> None:
    """Set (or with None, remove) variables in place, keeping comments and every other line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    out = []
    for line in lines:
        m = ENV_LINE.match(line)
        if m and not line.strip().startswith("#") and m[1] in pending:
            value = pending.pop(m[1])
            if value is not None:
                out.append(f"{m[1]}={_quote(value)}")
            continue
        out.append(line)
    out += [f"{k}={_quote(v)}" for k, v in pending.items() if v is not None]
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def target_path(workspace_root: Path | None, scope: str) -> Path:
    if scope == "workspace":
        if workspace_root is None:
            raise KeySetupError("no workspace here; use the user-wide store instead")
        return workspace_root / ".env"
    return global_env_path()


def fields_for(provider: str) -> list[Field]:
    if provider not in FIELDS:
        raise KeySetupError(f"unknown provider {provider!r} (have: {', '.join(FIELDS)})")
    return FIELDS[provider]


def build_values(provider: str, key: str | None, extras: dict[str, str]) -> dict[str, str]:
    """Map `KEY` and `NAME=value` extras onto the provider's variables. Extras may use the full variable
    name or a short one: endpoint, deployment, url, version."""
    fields = fields_for(provider)
    values: dict[str, str] = {}
    secret = next((f for f in fields if f.secret), None)
    if key:
        if not secret:
            raise KeySetupError(f"{provider} takes no key")
        values[secret.var] = key
    short = {"key": secret.var if secret else None}
    for f in fields:
        v = f.var
        if v.endswith("_BASE_URL") or v.endswith("_ENDPOINT"):
            short.setdefault("url", v)
            short.setdefault("endpoint", v)
        if v.endswith("_DEPLOYMENT_NAME_CHAT"):
            short["deployment"] = v
        if v.endswith("_DEPLOYMENT_NAME_FAST"):
            short["fast"] = v
        if v.endswith("_DEPLOYMENT_NAME_REASONING"):
            short["reasoning"] = v
        if v.endswith("_API_VERSION"):
            short["version"] = v
    known = {f.var for f in fields}
    for name, value in extras.items():
        var = name.upper() if name.upper() in known else short.get(name.lower())
        if not var:
            raise KeySetupError(f"{provider} has no setting {name!r} (have: {', '.join(sorted(known))})")
        values[var] = value
    return values


def set_values(workspace_root: Path | None, values: dict[str, str], scope: str = "user") -> Path:
    path = target_path(workspace_root, scope)
    write_values(path, values)
    reload(workspace_root)
    return path


def remove_provider(workspace_root: Path | None, provider: str, scope: str = "user") -> tuple[Path, list[str]]:
    path = target_path(workspace_root, scope)
    names = [f.var for f in fields_for(provider) if f.var in parse_env(path)]
    if names:
        current = parse_env(path)
        for name in names:  # drop what this file put into the process, then re-apply what remains
            if os.environ.get(name) == current.get(name):
                os.environ.pop(name, None)
        write_values(path, {n: None for n in names})
        reload(workspace_root)
    return path, names


def reload(workspace_root: Path | None) -> None:
    load_env_file(global_env_path())
    if workspace_root is not None:
        load_env_file(workspace_root / ".env")


def mask(value: str) -> str:
    if not value:
        return ""
    return f"{value[:4]}…{value[-4:]}" if len(value) >= 16 else "•" * 6


def status(workspace_root: Path | None) -> list[dict[str, str]]:
    """One row per variable that is set anywhere, plus required ones that are missing."""
    reload(workspace_root)
    user = parse_env(global_env_path())
    local = parse_env(workspace_root / ".env") if workspace_root else {}
    rows = []
    for provider, fields in FIELDS.items():
        for f in fields:
            value = os.environ.get(f.var, "")
            if local.get(f.var):
                source = "workspace .env"
            elif user.get(f.var):
                source = "user ~/.agh/.env"
            elif value:
                source = "environment"
            else:
                source = ""
            if not value and not (f.required and f is fields[0]):
                continue
            rows.append({"provider": provider, "var": f.var,
                         "value": (mask(value) if f.secret else value) or "not set", "source": source})
    return rows


def configured(provider: str) -> bool:
    return all(os.environ.get(f.var) for f in fields_for(provider) if f.required)


def any_cloud_key() -> bool:
    """True if some hosted LLM provider is fully configured. Local servers need no key, so they don't count."""
    return any(configured(p) for p in LLM_PROVIDERS if p not in ("ollama", "lmstudio", "vllm"))
