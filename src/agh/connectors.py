"""API connectors: connectors/<name>/connector.yaml turns a service into agent tools.

REST operations become `<connector>__<operation>` tools; OpenAPI specs can be imported; MCP servers
attach with type: mcp. Secrets are only ever referenced by environment variable name.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml
from pydantic import create_model
from pydantic.fields import FieldInfo

from agh import release
from agh.tooling import ToolSpec
from agh.workspace import Workspace, WorkspaceError, read_yaml, validate_name, write_yaml

TYPES = {"string": str, "integer": int, "number": float, "boolean": bool, "object": dict, "array": list}
READ_METHODS = {"GET", "HEAD", "OPTIONS"}
_last_call: dict[str, float] = {}
_tokens: dict[str, tuple[str, float]] = {}


class ConnectorError(RuntimeError):
    pass


def _env(name: str | None) -> str:
    value = os.environ.get(name or "")
    if not value:
        raise ConnectorError(f"environment variable {name} is not set (add it to the workspace .env)")
    return value


@dataclass
class Connector:
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    root: Path | None = None  # the workspace, which keys where OAuth tokens are stored

    async def _auth(self, headers: dict[str, str], params: dict[str, Any]) -> httpx.Auth | None:
        auth = self.config.get("auth") or {}
        kind = auth.get("type", "none")
        if kind == "none":
            return None
        if kind == "bearer":
            headers["Authorization"] = f"Bearer {_env(auth.get('token_env'))}"
        elif kind == "api_key":
            target = params if auth.get("in", "header") == "query" else headers
            target[auth.get("name", "X-API-Key")] = _env(auth.get("key_env"))
        elif kind == "basic":
            return httpx.BasicAuth(_env(auth.get("username_env")), _env(auth.get("password_env")))
        elif kind == "oauth2_client_credentials":
            headers["Authorization"] = f"Bearer {await self._oauth_token(auth)}"
        elif kind == "oauth2_authorization_code":
            headers["Authorization"] = f"Bearer {await self._user_token(auth)}"
        else:
            raise ConnectorError(f"unknown auth type {kind}")
        return None

    async def _user_token(self, auth: dict[str, Any]) -> str:
        from agh import oauth

        if self.root is None:
            raise ConnectorError("oauth2_authorization_code needs a workspace")
        try:
            return await oauth.access_token(self.root, self.name, auth)
        except oauth.OAuthError as exc:
            raise ConnectorError(str(exc)) from None

    async def _oauth_token(self, auth: dict[str, Any]) -> str:
        cached = _tokens.get(self.name)
        if cached and cached[1] > time.time() + 30:
            return cached[0]
        data = {"grant_type": "client_credentials", "client_id": _env(auth.get("client_id_env")),
                "client_secret": _env(auth.get("client_secret_env"))}
        if auth.get("scopes"):
            data["scope"] = " ".join(auth["scopes"])
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(auth["token_url"], data=data)
            resp.raise_for_status()
        body = resp.json()
        _tokens[self.name] = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
        return body["access_token"]

    async def _throttle(self) -> None:
        per_minute = (self.config.get("rate_limit") or {}).get("per_minute")
        if not per_minute:
            return
        gap = 60 / per_minute
        wait = _last_call.get(self.name, 0) + gap - time.monotonic()
        _last_call[self.name] = max(time.monotonic(), _last_call.get(self.name, 0) + gap)
        if wait > 0:
            await asyncio.sleep(wait)

    async def call(self, op_name: str, args: dict[str, Any]) -> str:
        op = self.config["operations"][op_name]
        method = op.get("method", "GET").upper()
        path = op["path"]
        headers = dict(self.config.get("headers") or {})
        query: dict[str, Any] = {}
        body = None
        for pname, pdef in (op.get("params") or {}).items():
            if pname not in args or args[pname] is None:
                continue
            where = (pdef or {}).get("in", "query")
            value = args[pname]
            if where == "path":
                path = path.replace("{" + pname + "}", quote(str(value), safe=""))
            elif where == "header":
                headers[pname] = str(value)
            elif where == "body":
                body = value
            else:
                query[pname] = value
        missing = re.findall(r"\{(\w+)\}", path)
        if missing:
            raise ConnectorError(f"missing path parameters: {missing}")
        auth = await self._auth(headers, query)
        attempts = int((self.config.get("retry") or {}).get("attempts", 3))
        url = self.config["base_url"].rstrip("/") + "/" + path.lstrip("/")
        async with httpx.AsyncClient(timeout=self.config.get("timeout", 30), auth=auth,
                                     follow_redirects=False) as client:
            for attempt in range(1, attempts + 1):
                await self._throttle()
                try:
                    resp = await client.request(method, url, params=query, headers=headers, json=body)
                except httpx.TransportError:
                    if attempt == attempts or method not in READ_METHODS:
                        raise
                    await asyncio.sleep(0.2 * 2 ** attempt)
                    continue
                # Only read requests retry on 5xx: a POST may have taken effect. 429 means it didn't.
                retryable = resp.status_code == 429 or (resp.status_code >= 500 and method in READ_METHODS)
                if not retryable or attempt == attempts:
                    break
                after = resp.headers.get("Retry-After", "")
                await asyncio.sleep(min(float(after), 30) if after.isdigit() else 0.2 * 2 ** attempt)
        text = resp.text
        if resp.status_code >= 400:
            raise ConnectorError(f"{method} {path} returned HTTP {resp.status_code}: {text[:1000]}")
        return text

    def tools(self) -> list[ToolSpec]:
        specs = []
        for op_name, op in (self.config.get("operations") or {}).items():
            fields: dict[str, Any] = {}
            for pname, pdef in (op.get("params") or {}).items():
                pdef = pdef or {}
                py = TYPES.get(pdef.get("type", "string"), str)
                required = pdef.get("required") or pdef.get("in") == "path"
                fields[pname] = ((py, FieldInfo(default=..., description=pdef.get("description")))
                                 if required else
                                 (py | None, FieldInfo(default=None, description=pdef.get("description"))))
            tool_name = f"{self.name}__{op_name}"[:64]
            method = op.get("method", "GET").upper()

            def bind(op_key: str):
                async def fn(**kwargs: Any) -> str:
                    return await self.call(op_key, kwargs)
                return fn

            schema = create_model(re.sub(r"\W", "_", tool_name) + "_args", **fields)
            specs.append(ToolSpec(
                name=tool_name, description=op.get("description") or f"{method} {op['path']}",
                args_schema=schema, func=bind(op_name), readonly=method in READ_METHODS,
                permission=op.get("permission") or ("allow" if method in READ_METHODS else "ask"),
                timeout=float(self.config.get("timeout", 30)) * 4, untrusted=True,
                source=f"connector:{self.name}"))
        return specs


def validate_config(cfg: dict[str, Any]) -> None:
    kind = cfg.get("type", "rest")
    if kind == "mcp":
        if not (cfg.get("command") or cfg.get("url")):
            raise ConnectorError("mcp connector needs `command` (stdio) or `url` (http)")
        return
    if kind != "rest":
        raise ConnectorError(f"unknown connector type {kind}")
    if not str(cfg.get("base_url", "")).startswith(("http://", "https://")):
        raise ConnectorError("rest connector needs an http(s) base_url")
    for name, op in (cfg.get("operations") or {}).items():
        if not re.fullmatch(r"\w+", str(name)):
            raise ConnectorError(f"bad operation name {name!r}")
        if "path" not in (op or {}):
            raise ConnectorError(f"operation {name} needs a path")


def load_connector(ws: Workspace, name: str) -> Connector:
    path = ws.element("connectors", validate_name(name)) / "connector.yaml"
    if not path.exists():
        raise WorkspaceError(f"no connector named {name!r}")
    cfg = read_yaml(path)
    validate_config(cfg)
    return Connector(name=name, config=cfg, root=ws.root)


async def connector_tools(ws: Workspace, names: list[str], pool: "McpPool | None" = None) -> list[ToolSpec]:
    """Tools from all named connectors. One unreachable service must not take the agent down.
    With a pool, MCP servers stay running between calls; without one, each is started just to list tools."""
    out: list[ToolSpec] = []
    for name in names:
        try:
            conn = load_connector(ws, name)
            if not release.admit(ws, "connector", ws.element("connectors", name)):
                continue
            if conn.config.get("type") == "mcp":
                out.extend(await pool.tools(conn) if pool else await _mcp_tools(conn))
            else:
                out.extend(conn.tools())
            ws.load_errors.pop(f"connector:{name}", None)
        except Exception as exc:  # network, config and subprocess failures all land here
            ws.load_errors[f"connector:{name}"] = f"{type(exc).__name__}: {exc}"
    return out


def require_mcp() -> None:
    """The MCP client is an optional install, so workspaces without MCP connectors don't carry it."""
    import importlib.util

    if importlib.util.find_spec("langchain_mcp_adapters") is None or importlib.util.find_spec("mcp") is None:
        raise ConnectorError('MCP connectors need the optional MCP client: pip install "agh[mcp]" '
                             "(into the environment agh runs from)")


def _mcp_server(cfg: dict[str, Any]) -> dict[str, Any]:
    if cfg.get("command"):
        server: dict[str, Any] = {"transport": "stdio", "command": cfg["command"], "args": cfg.get("args") or []}
        if cfg.get("env"):  # values name environment variables, keeping secrets out of the yaml
            server["env"] = {**os.environ, **{k: _env(v) for k, v in cfg["env"].items()}}
        if cfg.get("cwd"):
            server["cwd"] = cfg["cwd"]
    else:
        server = {"transport": cfg.get("transport", "streamable_http"), "url": cfg["url"]}
        if cfg.get("headers_env"):
            server["headers"] = {k: _env(v) for k, v in cfg["headers_env"].items()}
    return server


def _mcp_specs(conn: Connector, tools: list[Any]) -> list[ToolSpec]:
    cfg = conn.config
    readonly = set(cfg.get("readonly_tools") or [])
    specs = []
    for tool in tools:
        original = tool.name
        tool.name = f"{conn.name}__{original}"[:64]
        specs.append(ToolSpec(name=tool.name, description=tool.description or original,
                              lc_tool=tool, readonly=original in readonly, untrusted=True,
                              permission="allow" if original in readonly else "ask",
                              timeout=float(cfg.get("timeout", 120)), source=f"mcp:{conn.name}"))
    return specs


class _McpServer:
    """One running MCP server. A single owner task opens the session and later closes it: the client uses
    anyio cancel scopes, which must be exited by the task that entered them. Tool calls from any task go
    through the open session, so a server that keeps state (a browser, a login) keeps it between calls."""

    def __init__(self, conn: Connector, server: dict[str, Any], key: str):
        self.conn, self.server, self.key = conn, server, key
        self.specs: list[ToolSpec] = []
        self.session: Any = None
        self.listed = 0.0
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        require_mcp()
        ready: asyncio.Future[list[Any]] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._own(ready), name=f"mcp:{self.conn.name}")
        timeout = float(self.conn.config.get("start_timeout", 60))
        try:
            tools = await asyncio.wait_for(asyncio.shield(ready), timeout)
        except BaseException:
            await self.stop()
            raise
        self.specs = _mcp_specs(self.conn, tools) + await self._resource_specs()
        self.listed = time.monotonic()

    async def relist(self) -> None:
        from langchain_mcp_adapters.tools import load_mcp_tools

        try:
            tools = await asyncio.wait_for(load_mcp_tools(self.session, server_name=self.conn.name), 30)
        except Exception:  # keep the last good list; a dead server is restarted on the next refresh
            self.listed = time.monotonic()
            return
        self.specs = _mcp_specs(self.conn, tools) + await self._resource_specs()
        self.listed = time.monotonic()

    async def _resource_specs(self) -> list[ToolSpec]:
        """Servers that publish resources (files, records, docs) get two read-only tools to browse them."""
        session, name = self.session, self.conn.name
        try:
            await asyncio.wait_for(session.list_resources(), 15)
        except Exception:  # the server has no resources capability
            return []

        async def list_resources() -> str:
            """List the resources this MCP server publishes."""
            result = await session.list_resources()
            rows = [f"{r.uri}  {r.name or ''}  {r.description or ''}".rstrip() for r in result.resources]
            return "\n".join(rows) or "no resources"

        async def read_resource(uri: str) -> str:
            """Read one resource from this MCP server.

            Args:
                uri: A URI from list_resources.
            """
            result = await session.read_resource(uri)
            return "\n".join(getattr(c, "text", None) or f"[binary {getattr(c, 'mimeType', '')}]"
                             for c in result.contents)

        from agh.tooling import spec_from_function

        timeout = float(self.conn.config.get("timeout", 120))
        return [spec_from_function(fn, name=f"{name}__{fn.__name__}"[:64], readonly=True, permission="allow",
                                   untrusted=True, timeout=timeout, source=f"mcp:{name}",
                                   description=f"{fn.__doc__.splitlines()[0]} ({name})")
                for fn in (list_resources, read_resource)]

    async def _own(self, ready: asyncio.Future[list[Any]]) -> None:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain_mcp_adapters.tools import load_mcp_tools

        try:
            async with MultiServerMCPClient({self.conn.name: self.server}).session(self.conn.name) as session:
                tools = await load_mcp_tools(session, server_name=self.conn.name)
                self.session = session
                if not ready.done():
                    ready.set_result(tools)
                await self._stop.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except BaseException as exc:  # anyio wraps startup failures in exception groups
            if not ready.done():
                ready.set_exception(_unwrap(exc))

    def request_stop(self) -> None:
        self._stop.set()

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), 10)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)


def _unwrap(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc if isinstance(exc, Exception) else RuntimeError(str(exc))


MCP_RELIST_S = 60  # how often a running server is asked for its current tools


class McpPool:
    """MCP servers kept running for the life of a session, shared with its subagents. A server restarts only
    if its connector.yaml changes or it exits."""

    def __init__(self) -> None:
        self._servers: dict[str, _McpServer] = {}

    async def tools(self, conn: Connector) -> list[ToolSpec]:
        server = _mcp_server(conn.config)
        auth = conn.config.get("auth") or {}
        if auth.get("type") == "oauth2_authorization_code" and server.get("url"):
            server.setdefault("headers", {})["Authorization"] = f"Bearer {await conn._user_token(auth)}"
        key = json.dumps([server, conn.config.get("readonly_tools"), conn.config.get("timeout")],
                         sort_keys=True, default=str)
        current = self._servers.get(conn.name)
        if current and current.key == key and current.alive:
            if time.monotonic() - current.listed > MCP_RELIST_S:
                await current.relist()  # servers may add or drop tools while running
            return current.specs
        if current:
            await current.stop()
            del self._servers[conn.name]
        fresh = _McpServer(conn, server, key)
        await fresh.start()
        self._servers[conn.name] = fresh
        return fresh.specs

    def running(self) -> list[str]:
        return [name for name, s in self._servers.items() if s.alive]

    def close_nowait(self) -> None:
        """Ask every server to shut down; their owner tasks close the sessions on the event loop."""
        for server in self._servers.values():
            server.request_stop()
        self._servers.clear()

    async def aclose(self) -> None:
        servers = list(self._servers.values())
        self._servers.clear()
        await asyncio.gather(*(s.stop() for s in servers), return_exceptions=True)


async def _mcp_tools(conn: Connector) -> list[ToolSpec]:
    """Start the server, list its tools and stop it. Used by release checks; tools returned this way
    can't be called afterwards."""
    pool = McpPool()
    try:
        return await pool.tools(conn)
    finally:
        await pool.aclose()


def from_openapi(name: str, spec: dict[str, Any], include: str | None = None,
                 max_operations: int = 60) -> dict[str, Any]:
    """Translate an OpenAPI 3 document to connector.yaml content. Auth is noted, never filled with values."""
    env_prefix = re.sub(r"\W", "_", name).upper()

    def deref(obj: Any) -> Any:
        if isinstance(obj, dict) and str(obj.get("$ref", "")).startswith("#/"):
            node: Any = spec
            for part in obj["$ref"][2:].split("/"):
                node = node[part]
            return node
        return obj

    operations: dict[str, Any] = {}
    for path, item in (spec.get("paths") or {}).items():
        for method, op in item.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            op_id = re.sub(r"\W+", "_", op.get("operationId") or f"{method}_{path}").strip("_").lower()
            if include and not re.search(include, op_id):
                continue
            if len(operations) >= max_operations:
                break
            params: dict[str, Any] = {}
            for raw in [*(item.get("parameters") or []), *(op.get("parameters") or [])]:
                p = deref(raw)
                if p.get("in") not in {"path", "query", "header"}:
                    continue
                params[p["name"]] = {"in": p["in"], "type": deref(p.get("schema") or {}).get("type", "string"),
                                     "required": bool(p.get("required")),
                                     "description": (p.get("description") or "")[:200]}
            if op.get("requestBody"):
                params["body"] = {"in": "body", "type": "object",
                                  "required": bool(deref(op["requestBody"]).get("required")),
                                  "description": "JSON request body"}
            operations[op_id] = {"method": method.upper(), "path": path,
                                 "description": (op.get("summary") or op.get("description") or op_id)[:300],
                                 "params": params}
    auth: dict[str, Any] = {"type": "none"}
    for scheme in ((spec.get("components") or {}).get("securitySchemes") or {}).values():
        scheme = deref(scheme)
        if scheme.get("type") == "http" and scheme.get("scheme") == "bearer":
            auth = {"type": "bearer", "token_env": f"{env_prefix}_TOKEN"}
        elif scheme.get("type") == "apiKey":
            auth = {"type": "api_key", "in": scheme.get("in", "header"), "name": scheme.get("name"),
                    "key_env": f"{env_prefix}_API_KEY"}
        elif scheme.get("type") == "http" and scheme.get("scheme") == "basic":
            auth = {"type": "basic", "username_env": f"{env_prefix}_USER", "password_env": f"{env_prefix}_PASSWORD"}
        else:
            continue
        break
    servers = spec.get("servers") or [{}]
    return {"name": name, "type": "rest", "description": (spec.get("info") or {}).get("title", name),
            "base_url": servers[0].get("url", ""), "auth": auth, "timeout": 30,
            "retry": {"attempts": 3}, "rate_limit": {"per_minute": 60}, "operations": operations}


def load_openapi_source(source: str) -> dict[str, Any]:
    """Read a spec from a URL or local file; both JSON and YAML parse as YAML."""
    if source.startswith(("http://", "https://")):
        resp = httpx.get(source, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        text = resp.text
    else:
        text = Path(source).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or "paths" not in data:
        raise ConnectorError("not an OpenAPI document: no `paths`")
    return data


def write_connector(ws: Workspace, name: str, cfg: dict[str, Any], overwrite: bool = False) -> Path:
    validate_name(name)
    validate_config(cfg)
    directory = ws.dir("connectors") / name
    target = directory / "connector.yaml"
    if (ws.element("connectors", name) / "connector.yaml").exists() and not overwrite:
        raise WorkspaceError(f"connector {name} already exists")
    write_yaml(target, cfg)
    return target


async def probe(ws: Workspace, name: str) -> str:
    """Load a connector and report what it exposes (no API calls for REST)."""
    tools = await connector_tools(ws, [name])
    if f"connector:{name}" in ws.load_errors:
        return f"FAILED: {ws.load_errors[f'connector:{name}']}"
    return json.dumps({"connector": name, "tools": [t.name for t in tools]}, indent=2)
