"""`agh serve`: a small local HTTP API and a scheduler for triggers.

  POST /v1/runs        {"prompt": "...", "agent"?: "...", "session"?: "...", "mode"?: "default|edits|plan|auto|full-auto"}
                       streams newline-delimited JSON: one object per event, then {"kind": "result", ...}
  GET  /v1/sessions    recent sessions
  GET  /health         {"ok": true}

Every request needs `Authorization: Bearer <token>` (AGH_SERVE_TOKEN, or one printed at start). It listens on
127.0.0.1 unless told otherwise. Tool calls that would ask are denied, unless the server runs with --yes.

Triggers in agh.yaml run on a timer while the server is up; each run is appended to state/triggers/<name>.jsonl:
  triggers:
    - name: morning-digest
      every: 24h          # s, m, h or d
      agent: assistant
      prompt: Summarise yesterday's changes in notes/.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import time
from typing import Any, Callable

from agh.engine import Engine
from agh.sessions import Session, list_sessions
from agh.workspace import Workspace, WorkspaceError

MAX_BODY = 1_000_000
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_every(text: str) -> float:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd])\s*", str(text))
    if not match:
        raise WorkspaceError(f"bad trigger interval {text!r}: use a number and s, m, h or d, like 30m")
    return float(match[1]) * UNITS[match[2]]


def event_json(event: Any) -> dict[str, Any]:
    return {"kind": event.kind, "agent": event.agent, **event.data}


def result_json(engine: Engine) -> dict[str, Any]:
    r = engine.result
    return {"kind": "result", "text": r.text, "stop_reason": r.stop_reason, "error": r.error, "steps": r.steps,
            "session": engine.session.id, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
            "cost_usd": r.cost_usd}


async def _deny(_: dict[str, Any]) -> str:
    return "deny"


async def _allow(_: dict[str, Any]) -> str:
    return "once"


async def run_once(ws: Workspace, prompt: str, agent: str | None = None, session_id: str | None = None,
                   mode: str = "default", yes: bool = False, model: Any = None,
                   on_event: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
    session = Session.load(ws, session_id) if session_id else None
    name = (session.agent if session else None) or agent or ws.default_agent
    engine = Engine(ws, name, session=session, approver=_allow if yes else _deny, mode=mode, model=model)
    try:
        async for event in engine.stream(prompt):
            if on_event and event.kind != "usage":
                await on_event(event_json(event))
    finally:
        await engine.mcp.aclose()
        engine.close()
    return result_json(engine)


class Server:
    def __init__(self, ws: Workspace, token: str, yes: bool = False, model: Any = None,
                 log: Callable[[str], Any] = print):
        self.ws, self.token, self.yes, self.model, self.log = ws, token, yes, model, log
        self._server: asyncio.base_events.Server | None = None
        self._triggers: list[asyncio.Task[None]] = []

    async def start(self, host: str = "127.0.0.1", port: int = 8787) -> int:
        self._server = await asyncio.start_server(self._handle, host, port)
        for trigger in self.ws.config.get("triggers") or []:
            self._triggers.append(asyncio.create_task(self._run_trigger(trigger)))
        return self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        for task in self._triggers:
            task.cancel()
        await asyncio.gather(*self._triggers, return_exceptions=True)
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def serve_forever(self) -> None:
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    # ---- triggers ----------------------------------------------------------------------------

    async def _run_trigger(self, trigger: dict[str, Any]) -> None:
        name = str(trigger.get("name") or "trigger")
        every = parse_every(trigger.get("every", "1h"))
        out = self.ws.state / "triggers" / f"{re.sub(r'[^\w-]', '_', name)}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        while True:
            await asyncio.sleep(every)
            started = time.time()
            try:
                result = await run_once(self.ws, str(trigger["prompt"]), trigger.get("agent"),
                                        mode=trigger.get("mode", "default"), yes=self.yes, model=self.model)
            except Exception as exc:  # one bad run must not stop the schedule
                result = {"kind": "result", "error": f"{type(exc).__name__}: {exc}"}
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": started, "trigger": name, **result}, default=str) + "\n")
            self.log(f"trigger {name}: {result.get('stop_reason') or 'error'}")

    # ---- http --------------------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(self._read(reader), 30)
            if request is None:
                return
            method, path, headers, body = request
            given = headers.get("authorization", "").removeprefix("Bearer ").strip()
            if path != "/health" and not hmac.compare_digest(given, self.token):
                return await self._reply(writer, 401, {"error": "missing or wrong bearer token"})
            if method == "GET" and path == "/health":
                return await self._reply(writer, 200, {"ok": True})
            if method == "GET" and path == "/v1/sessions":
                return await self._reply(writer, 200, {"sessions": list_sessions(self.ws)})
            if method == "POST" and path == "/v1/runs":
                return await self._run(writer, body)
            await self._reply(writer, 404, {"error": f"no route {method} {path}"})
        except (asyncio.TimeoutError, ConnectionError, ValueError) as exc:
            try:
                await self._reply(writer, 400, {"error": str(exc) or type(exc).__name__})
            except ConnectionError:
                pass
        finally:
            writer.close()

    async def _read(self, reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes] | None:
        line = (await reader.readline()).decode("latin-1").strip()
        if not line:
            return None
        method, target, _ = (line.split(" ") + ["", ""])[:3]
        headers: dict[str, str] = {}
        while (raw := await reader.readline()) not in (b"\r\n", b"\n", b""):
            key, _, value = raw.decode("latin-1").partition(":")
            headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0") or 0)
        if length > MAX_BODY:
            raise ValueError("request body too large")
        body = await reader.readexactly(length) if length else b""
        return method.upper(), target.split("?")[0], headers, body

    async def _reply(self, writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=str).encode()
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found"}.get(status, "Error")
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: "
                     f"{len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()

    async def _run(self, writer: asyncio.StreamWriter, body: bytes) -> None:
        data = json.loads(body or b"{}")
        if not isinstance(data, dict) or not str(data.get("prompt", "")).strip():
            raise ValueError("body must be JSON with a prompt")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\nTransfer-Encoding: chunked\r\n"
                     b"Connection: close\r\n\r\n")

        async def send(obj: dict[str, Any]) -> None:
            line = (json.dumps(obj, default=str) + "\n").encode()
            writer.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
            await writer.drain()

        try:
            result = await run_once(self.ws, str(data["prompt"]), data.get("agent"), data.get("session"),
                                    data.get("mode", "default"), self.yes, self.model, on_event=send)
        except WorkspaceError as exc:
            result = {"kind": "result", "error": str(exc), "stop_reason": "error"}
        await send(result)
        writer.write(b"0\r\n\r\n")
        await writer.drain()


def new_token() -> str:
    return secrets.token_urlsafe(24)
