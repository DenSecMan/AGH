"""First-run provider setup: pick a provider, enter its details, prove they work, then save them.

Nothing is written until the details pass: values are tried in the process environment first and put back if
the check fails.
"""
from __future__ import annotations

import os
from typing import Any

from agh import keys, llm
from agh.workspace import Workspace

HELP = {
    "anthropic": "Claude models. Create a key at https://console.anthropic.com/settings/keys",
    "openai": "GPT models. Create a key at https://platform.openai.com/api-keys",
    "azure_openai": "Azure OpenAI / Foundry. Key and endpoint are under Keys and Endpoint on your resource; "
                    "the deployment name is the one you gave the model in Foundry.",
    "gemini": "Google Gemini. Create a key at https://aistudio.google.com/apikey",
    "openrouter": "Many vendors through one key. Create one at https://openrouter.ai/keys",
    "ollama": "Local models with Ollama (https://ollama.com). Start it, and `ollama pull llama3.2` a model first.",
    "lmstudio": "Local models with LM Studio. Load a model and start its local server first.",
    "vllm": "A vLLM server (yours or your team's) speaking the OpenAI API.",
}
TEST_PROMPT = "Reply with the single word OK."


def needs_setup(ws: Workspace) -> bool:
    """True unless a provider and model are selected and the provider has its credentials."""
    provider, model = llm.selection(ws.config)
    return not (provider and model and llm.has_credentials(provider))


def providers() -> list[tuple[str, str]]:
    """(provider, status) for the picker, hosted ones first as listed in keys.FIELDS."""
    out = []
    for name in keys.LLM_PROVIDERS:
        local = llm.PROVIDERS.get(name, {}).get("local")
        status = "local server" if local else "details found" if llm.has_credentials(name) else "needs an API key"
        out.append((name, status))
    return out


def current_values(provider: str) -> dict[str, str]:
    """What is already set for each field (environment or .env files), else the field's default."""
    return {f.var: os.environ.get(f.var, "").strip() or f.default for f in keys.FIELDS[provider]}


def missing(provider: str, values: dict[str, str]) -> list[str]:
    return [f.label for f in keys.FIELDS[provider] if f.required and not values.get(f.var, "").strip()]


class _Trial:
    """Put values in os.environ for a check; undo unless kept."""

    def __init__(self, values: dict[str, str]):
        self.values = {k: v.strip() for k, v in values.items()}
        self.saved: dict[str, str | None] = {}

    def __enter__(self) -> "_Trial":
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        return self

    def restore(self) -> None:
        for k, old in self.saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old

    def __exit__(self, *exc: Any) -> None:
        if exc[0] is not None:
            self.restore()


def check_connection(provider: str, values: dict[str, str]) -> list[str]:
    """Blocking. Ask the provider for its models with these values; returns them. Raises ValueError with a
    readable reason. The values stay in the environment only if this succeeds."""
    gaps = missing(provider, values)
    if gaps:
        raise ValueError(f"please fill in: {', '.join(gaps)}")
    with _Trial(values) as trial:
        try:
            models = llm.list_models(provider)
        except llm.ModelConfigError as exc:
            trial.restore()
            raise ValueError(str(exc)) from None
        if not models:
            trial.restore()
            raise ValueError(f"{provider} answered but offers no chat models"
                             + (" (is a model loaded?)" if llm.PROVIDERS[provider].get("local") else ""))
    return models


def check_model(provider: str, model: str, config: dict[str, Any]) -> str:
    """Blocking. Send one tiny prompt; returns the reply. Raises ValueError if the model can't answer."""
    try:
        chat = llm.make_model(f"{provider}:{model}", config)
        reply = chat.invoke(TEST_PROMPT)
    except Exception as exc:  # any provider/SDK error means "not working", reported to the user
        raise ValueError(f"{provider} / {model} did not answer: {type(exc).__name__}: {str(exc)[:300]}") from None
    text = reply.content if isinstance(reply.content, str) else " ".join(
        str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in reply.content)
    return text.strip()[:200] or "(empty reply)"


SEARCH_PROVIDERS = {
    "tavily": ("TAVILY_API_KEY", "Tavily. Free tier available; create a key at https://app.tavily.com"),
    "brave": ("BRAVE_SEARCH_API_KEY", "Brave Search API. Create a key at https://api-dashboard.search.brave.com"),
}
SEARCH_ASKED = "search_setup_asked"  # marker in ws.state: the user was asked once, don't nag again


def search_configured() -> str | None:
    """The search service with a key in the environment, if any."""
    return next((name for name, (var, _) in SEARCH_PROVIDERS.items() if os.environ.get(var, "").strip()), None)


def needs_search_setup(ws: Workspace) -> bool:
    return search_configured() is None and not (ws.state / SEARCH_ASKED).exists()


def mark_search_asked(ws: Workspace) -> None:
    ws.state.mkdir(parents=True, exist_ok=True)
    (ws.state / SEARCH_ASKED).write_text("1", encoding="utf-8")


def check_search(service: str, key: str) -> int:
    """Blocking. Run one test query; returns the result count. Raises ValueError with a readable reason."""
    import httpx

    key = key.strip()
    if not key:
        raise ValueError("please enter the API key")
    try:
        if service == "tavily":
            resp = httpx.post("https://api.tavily.com/search", json={"query": "python", "max_results": 1},
                              headers={"Authorization": f"Bearer {key}"}, timeout=20)
            resp.raise_for_status()
            return len(resp.json().get("results", []))
        if service == "brave":
            resp = httpx.get("https://api.search.brave.com/res/v1/web/search", params={"q": "python", "count": 1},
                             headers={"X-Subscription-Token": key}, timeout=20)
            resp.raise_for_status()
            return len((resp.json().get("web") or {}).get("results", []))
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        raise ValueError("the key was rejected" if code in (401, 403) else f"{service} answered HTTP {code}") from None
    except httpx.HTTPError as exc:
        raise ValueError(f"could not reach {service}: {exc}") from None
    raise ValueError(f"unknown search service {service}")


def save_search(ws: Workspace, service: str, key: str) -> str:
    var = SEARCH_PROVIDERS[service][0]
    path = keys.set_values(ws.root, {var: key.strip()}, "user")
    os.environ[var] = key.strip()
    mark_search_asked(ws)
    return f"Saved {var} to {path}"


def save(ws: Workspace, provider: str, model: str, values: dict[str, str]) -> str:
    """Keep the values in the user-wide .env and select provider and model in agh.yaml."""
    from agh.commands import _save_models

    kept = {k: v.strip() for k, v in values.items() if v.strip()}
    path = keys.set_values(ws.root, kept, "user") if kept else None
    _save_models(ws, "selected", {"provider": provider, "model": model})
    return f"Saved to {path}" if path else "Nothing to save for this provider"
