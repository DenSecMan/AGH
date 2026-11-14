"""Model construction: the built-in providers, auto-detection, and a scripted offline model.
Other LangChain providers work if `langchain` and their package are installed."""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

# Built-in models for `auto`, as provider: (key variable, default model, fast model).
# Azure has no entry because its models come from AZURE_OPENAI_DEPLOYMENT_NAME_*.
AUTO = {
    "anthropic": ("ANTHROPIC_API_KEY", "anthropic:claude-opus-5-5", "anthropic:claude-haiku-5-5"),
    "openai": ("OPENAI_API_KEY", "openai:gpt-5", "openai:gpt-5-mini"),
}
# `auto` tries these in order. Override with `models.providers` in agh.yaml.
DEFAULT_PROVIDERS = ("azure_openai", "anthropic", "openai")
# Everything /provider can select. `base` is the default URL, overridable by `base_env`. Local servers need no key.
PROVIDERS: dict[str, dict[str, Any]] = {
    "azure_openai": {"key": "AZURE_OPENAI_API_KEY"},
    "anthropic": {"key": "ANTHROPIC_API_KEY"},
    "openai": {"key": "OPENAI_API_KEY"},
    "gemini": {"key": "GEMINI_API_KEY", "base_env": "GEMINI_BASE_URL",
               "base": "https://generativelanguage.googleapis.com/v1beta/openai"},
    "openrouter": {"key": "OPENROUTER_API_KEY", "base_env": "OPENROUTER_BASE_URL",
                   "base": "https://openrouter.ai/api/v1"},
    "ollama": {"key": "OLLAMA_API_KEY", "base_env": "OLLAMA_BASE_URL",
               "base": "http://localhost:11434/v1", "local": True},
    "lmstudio": {"key": "LLMSTUDIO_API_KEY", "base_env": "LLMSTUDIO_BASE_URL",
                 "base": "http://localhost:1234/v1", "local": True},
    "vllm": {"key": "VLLM_API_KEY", "base_env": "VLLM_BASE_URL",
             "base": "http://localhost:8000/v1", "local": True},
}
NOT_CHAT = re.compile(r"embed|whisper|tts|dall-e|moderation|transcribe|image|audio|realtime|rerank", re.I)


class ModelConfigError(RuntimeError):
    pass


LEVELS = ("default", "fast", "reasoning")
# Reasoning effort cycled with Ctrl+E.
EFFORTS = ("high", "medium", "low")
DEFAULT_EFFORT = "low"
AZURE_DEFAULT_API_VERSION = "2025-04-01-preview"


def effort_params(model: str, effort: str | None) -> dict[str, Any]:
    """Constructor params that ask `model` for this reasoning effort; empty when none is given."""
    if not effort:
        return {}
    if effort not in EFFORTS:
        raise ModelConfigError(f"unknown effort {effort!r} (have: {', '.join(EFFORTS)})")
    if provider_of(model) == "anthropic":
        return {"output_config": {"effort": effort}}
    if provider_of(model) in PROVIDERS:  # OpenAI and the OpenAI-compatible providers
        return {"reasoning_effort": effort}
    return {}


def _env_stripped(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _azure_deployment(level: str) -> str | None:
    """Deployment for a level from AZURE_OPENAI_DEPLOYMENT_NAME_*; levels without one use CHAT."""
    if not (_env_stripped("AZURE_OPENAI_ENDPOINT") and _env_stripped("AZURE_OPENAI_API_KEY")):
        return None
    suffix = {"fast": "FAST", "reasoning": "REASONING"}.get(level, "CHAT")
    return (_env_stripped(f"AZURE_OPENAI_DEPLOYMENT_NAME_{suffix}")
            or _env_stripped("AZURE_OPENAI_DEPLOYMENT_NAME_CHAT") or None)


def provider_priority(config: dict[str, Any]) -> list[str]:
    providers = (config.get("models") or {}).get("providers") or list(DEFAULT_PROVIDERS)
    for provider in providers:
        if provider not in DEFAULT_PROVIDERS:
            raise ModelConfigError(
                f"unknown provider {provider!r} in models.providers (have: {', '.join(DEFAULT_PROVIDERS)}). "
                "For any other provider, list its models directly, e.g. models.default: [ollama:llama3.2, auto].")
    return list(providers)


def has_credentials(provider: str) -> bool:
    if provider == "azure_openai":
        return bool(_env_stripped("AZURE_OPENAI_ENDPOINT") and _env_stripped("AZURE_OPENAI_API_KEY"))
    spec = PROVIDERS.get(provider)
    return not spec or spec.get("local", False) or bool(_env_stripped(spec["key"]))


def selection(config: dict[str, Any]) -> tuple[str | None, str | None]:
    """The provider and model chosen with /provider and /model, if any."""
    chosen = (config.get("models") or {}).get("selected") or {}
    return chosen.get("provider"), chosen.get("model")


def _base_url(provider: str) -> str:
    spec = PROVIDERS[provider]
    return (_env_stripped(spec["base_env"]) or spec["base"]).rstrip("/")


def list_models(provider: str) -> list[str]:
    """Chat models this provider offers right now. Raises ModelConfigError if it can't be asked."""
    import httpx

    if provider not in PROVIDERS:
        raise ModelConfigError(f"unknown provider {provider!r} (have: {', '.join(PROVIDERS)})")
    if not has_credentials(provider):
        raise ModelConfigError(f"{provider} has no credentials; set {PROVIDERS[provider]['key']} in .env")
    if provider == "azure_openai":  # deployments can't be listed with an inference key, so .env is the list
        return list(dict.fromkeys(filter(None, (
            _env_stripped(f"AZURE_OPENAI_DEPLOYMENT_NAME_{s}") for s in ("CHAT", "FAST", "REASONING")))))
    key = _env_stripped(PROVIDERS[provider]["key"])
    if provider == "anthropic":
        url, headers = "https://api.anthropic.com/v1/models?limit=1000", {
            "x-api-key": key, "anthropic-version": "2023-06-01"}
    elif provider == "openai":
        url, headers = "https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"}
    else:
        url, headers = f"{_base_url(provider)}/models", {"Authorization": f"Bearer {key}"} if key else {}
    try:
        response = httpx.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        ids = [str(m["id"]).removeprefix("models/") for m in response.json()["data"]]
    except httpx.HTTPStatusError as exc:
        raise ModelConfigError(f"{provider} refused the model list: HTTP {exc.response.status_code}") from None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise ModelConfigError(f"could not list {provider} models from {url.split('?')[0]}: {type(exc).__name__}") from None
    return sorted(i for i in ids if not NOT_CHAT.search(i))


def _auto_models(config: dict[str, Any], level: str) -> list[str]:
    found = []
    for provider in provider_priority(config):
        if provider == "azure_openai":
            if deployment := _azure_deployment(level):
                found.append(f"azure_openai:{deployment}")
        elif has_credentials(provider):
            found.append(AUTO[provider][2 if level == "fast" else 1])
    return found


def candidates(name: Any, config: dict[str, Any], level: str = "default") -> list[str]:
    """The models to try, best first. `name` is a model, a level, `auto`, a list of those, or None.

    A list may name providers with no credentials; those are skipped. A single named model is
    kept as is, so a missing key surfaces as that provider's own error.
    """
    if isinstance(name, str) and name in LEVELS:  # `model: reasoning` in agent.yaml picks a level rather than a model
        name, level = None, name
    if not name or name == "auto":
        name = (config.get("models") or {}).get(level, "auto")
    if isinstance(name, str) and name != "auto":
        return [name]
    chain: list[str] = []
    for entry in [name] if isinstance(name, str) else name:
        for model in _auto_models(config, level) if entry == "auto" else [entry]:
            if model not in chain and has_credentials(provider_of(model)):
                chain.append(model)
    if not chain:
        raise ModelConfigError(
            "No model configured. Set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY and "
            "AZURE_OPENAI_DEPLOYMENT_NAME_CHAT, or ANTHROPIC_API_KEY or OPENAI_API_KEY (environment or workspace .env), "
            "or set models.default in agh.yaml, e.g. ollama:llama3.2."
        )
    return chain


def model_chain(name: Any, fallbacks: list[str], config: dict[str, Any]) -> list[str]:
    """An agent's primary candidates, then its usable fallbacks."""
    chain = candidates(name, config)
    for fallback in fallbacks:
        try:
            options = candidates(fallback, config)
        except ModelConfigError:
            continue
        chain.extend(m for m in options if m not in chain and has_credentials(provider_of(m)))
    return chain


def resolve_name(name: Any, config: dict[str, Any], level: str = "default") -> str:
    return candidates(name, config, level)[0]


def make_model(name: Any, config: dict[str, Any], level: str = "default", **params: Any) -> BaseChatModel:
    resolved = resolve_name(name, config, level)
    provider, model = provider_of(resolved), resolved.split(":", 1)[-1]
    settings = {k: v for k, v in params.items() if v is not None}
    if provider == "azure_openai":
        return _make_azure(model, params)
    if "base_env" in PROVIDERS.get(provider, {}):
        return _make_compatible(resolved, params)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model, **settings)
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, **settings)
    # Any other LangChain provider works once its packages are installed; they are not AGH dependencies.
    try:
        from langchain.chat_models import init_chat_model
    except ImportError:
        raise ModelConfigError(
            f"model {resolved!r} is not from a built-in provider ({', '.join(PROVIDERS)}); write it as "
            f"provider:model. For another LangChain provider: pip install langchain and that provider's "
            f"package.") from None
    return init_chat_model(resolved, **settings)


def _make_compatible(resolved: str, params: dict[str, Any]) -> BaseChatModel:
    """These providers all speak the OpenAI API, so one client covers them."""
    from langchain_openai import ChatOpenAI

    provider, model = resolved.split(":", 1)
    settings = {k: v for k, v in params.items() if v is not None}
    settings.setdefault("base_url", _base_url(provider))
    settings.setdefault("api_key", _env_stripped(PROVIDERS[provider]["key"]) or "not-needed")
    settings.setdefault("stream_usage", True)  # token budgets depend on usage arriving in the stream
    return ChatOpenAI(model=model, **settings)


def _azure_settings(params: dict[str, Any]) -> dict[str, Any]:
    """Built here because LangChain reads the version from OPENAI_API_VERSION, not AZURE_OPENAI_API_VERSION."""
    settings = {k: v for k, v in params.items() if v is not None}
    settings.setdefault("azure_endpoint", _env_stripped("AZURE_OPENAI_ENDPOINT"))
    settings.setdefault("api_key", _env_stripped("AZURE_OPENAI_API_KEY"))
    settings.setdefault("api_version", _env_stripped("AZURE_OPENAI_API_VERSION") or AZURE_DEFAULT_API_VERSION)
    if not settings["azure_endpoint"] or not settings["api_key"]:
        raise ModelConfigError("azure_openai needs AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY")
    return settings


def _make_azure(deployment: str, params: dict[str, Any]) -> BaseChatModel:
    from langchain_openai import AzureChatOpenAI

    settings = _azure_settings(params)
    settings.setdefault("stream_usage", True)  # token budgets depend on usage arriving in the stream
    # Current reasoning models refuse function tools over Chat Completions. Older deployments
    # can opt out with `model_params: {use_responses_api: false}` in agent.yaml.
    settings.setdefault("use_responses_api", True)
    return AzureChatOpenAI(azure_deployment=deployment, **settings)


def make_azure_embeddings(deployment: str) -> Any:
    from langchain_openai import AzureOpenAIEmbeddings

    return AzureOpenAIEmbeddings(azure_deployment=deployment, **_azure_settings({}))


def provider_of(name: str) -> str:
    return name.split(":", 1)[0] if ":" in name else ""


# Context a model takes, by name prefix, longest prefix first. Anything else gets the cautious default;
# set `limits.context_tokens` in agent.yaml for a model that takes more or less.
CONTEXT_WINDOWS = {"openai:gpt-5": 272_000, "anthropic:": 200_000}
DEFAULT_CONTEXT_WINDOW = 120_000


def context_window(model: str) -> int:
    for prefix in sorted(CONTEXT_WINDOWS, key=len, reverse=True):
        if model.startswith(prefix):
            return CONTEXT_WINDOWS[prefix]
    return DEFAULT_CONTEXT_WINDOW


def has_price(model: str, config: dict[str, Any]) -> bool:
    return bool((config.get("pricing") or {}).get(model))


def cost_of(model: str, usage: dict[str, int], config: dict[str, Any]) -> float:
    """USD cost from `pricing:` in agh.yaml ({model: {input, output}} per million tokens). 0 if unpriced."""
    price = (config.get("pricing") or {}).get(model)
    if not price:
        return 0.0
    return (usage.get("input_tokens", 0) * price.get("input", 0)
            + usage.get("output_tokens", 0) * price.get("output", 0)) / 1_000_000


class ScriptedChatModel(BaseChatModel):
    """Plays back canned replies: for offline tests, eval mocks and demos.

    Each response is text, {"text": ..., "tool_calls": [{"name": ..., "args": {...}}]},
    an Exception to raise, or a callable taking the messages and returning any of these.
    """

    responses: list[Any] = []
    _index: int = PrivateAttr(default=0)
    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChatModel":
        return self

    def _next(self, messages: list[BaseMessage]) -> tuple[str, list[dict[str, Any]]]:
        self._seen.append(list(messages))
        item: Any = self.responses[self._index] if self._index < len(self.responses) else "(script exhausted)"
        self._index += 1
        if callable(item):
            item = item(messages)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return item, []
        calls = [{"name": c["name"], "args": c.get("args", {}), "id": c.get("id") or uuid.uuid4().hex[:12]}
                 for c in item.get("tool_calls", [])]
        return item.get("text", ""), calls

    @staticmethod
    def _usage(messages: list[BaseMessage], text: str) -> dict[str, int]:
        i = sum(len(str(m.content)) for m in messages) // 4
        o = len(text) // 4
        return {"input_tokens": i, "output_tokens": o, "total_tokens": i + o}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        text, calls = self._next(messages)
        msg = AIMessage(content=text, tool_calls=calls, usage_metadata=self._usage(messages, text))
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        text, calls = self._next(messages)
        size = max(1, len(text) // 8)
        for start in range(0, len(text), size):
            yield ChatGenerationChunk(message=AIMessageChunk(content=text[start:start + size]))
        yield ChatGenerationChunk(message=AIMessageChunk(
            content="",
            tool_call_chunks=[{"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"],
                               "index": i, "type": "tool_call_chunk"} for i, c in enumerate(calls)],
            usage_metadata=self._usage(messages, text),
        ))
