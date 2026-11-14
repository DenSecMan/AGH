"""OAuth sign-in for connectors, and MCP resources."""
import asyncio
import json
import sys
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from agh import connectors, oauth


async def test_mcp_connector_without_the_optional_client_says_how_to_install_it(monkeypatch, tmp_path):
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None)
    conn = connectors.Connector(name="srv", config={"type": "mcp", "command": "x"}, root=tmp_path)
    started = time.monotonic()
    with pytest.raises(connectors.ConnectorError, match=r"agh\[mcp\]"):
        await connectors._McpServer(conn, {}, "k").start()
    assert time.monotonic() - started < 5  # refused up front, not after the start timeout


@pytest.fixture
async def provider(unused_tcp_port_factory=None):
    """A tiny OAuth provider: /token swaps code (checking PKCE) or a refresh token for access tokens."""
    import base64
    import hashlib

    seen = {"challenge": None, "issued": 0}

    async def handle(reader, writer):
        head = (await reader.readline()).decode()
        length = 0
        while (line := await reader.readline()) not in (b"\r\n", b""):
            if line.lower().startswith(b"content-length"):
                length = int(line.split(b":")[1])
        form = {k: v[0] for k, v in parse_qs((await reader.readexactly(length)).decode()).items()}
        if form.get("grant_type") == "authorization_code":
            digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
            ok = form["code"] == "good-code" and digest.decode() == seen["challenge"]
        else:
            ok = form.get("refresh_token") == "r1"
        seen["issued"] += ok
        body = json.dumps({"access_token": f"a{seen['issued']}", "refresh_token": "r1", "expires_in": 3600}
                          if ok else {"error": "invalid_grant"})
        status = "200 OK" if ok else "400 Bad Request"
        writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                     f"Connection: close\r\n\r\n{body}".encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", seen
    server.close()


async def test_login_stores_token_outside_workspace_and_refreshes(ws, provider, monkeypatch):
    base, seen = provider
    monkeypatch.setenv("DEMO_CLIENT_ID", "cid")
    auth = {"type": "oauth2_authorization_code", "authorize_url": f"{base}/authorize",
            "token_url": f"{base}/token", "client_id_env": "DEMO_CLIENT_ID", "scopes": ["read"], "redirect_port": 0}
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        auth["redirect_port"] = s.getsockname()[1]

    def browser(url):  # what the provider would do after the user signs in
        q = parse_qs(urlsplit(url).query)
        seen["challenge"] = q["code_challenge"][0]
        assert q["scope"] == ["read"] and q["code_challenge_method"] == ["S256"]

        async def redirect():
            async with httpx.AsyncClient() as c:
                await c.get(q["redirect_uri"][0], params={"code": "good-code", "state": q["state"][0]})
        asyncio.get_running_loop().create_task(redirect())

    path = await oauth.login(ws.root, "demo", auth, open_url=browser, say=lambda _: None)
    assert not path.is_relative_to(ws.root)
    assert await oauth.access_token(ws.root, "demo", auth) == "a1"
    token = json.loads(path.read_text())
    token["expires_at"] = time.time() - 1
    path.write_text(json.dumps(token))
    assert await oauth.access_token(ws.root, "demo", auth) == "a2"  # refreshed
    assert oauth.logout(ws.root, "demo")
    with pytest.raises(oauth.OAuthError, match="connector login demo"):
        await oauth.access_token(ws.root, "demo", auth)


RESOURCE_SERVER = """
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("r")

@mcp.resource("notes://today")
def today() -> str:
    "Today's notes."
    return "buy milk"

@mcp.tool()
def ping() -> str:
    "Ping."
    return "pong"

mcp.run()
"""


async def test_mcp_resources_become_read_only_tools(ws, tmp_path):
    script = tmp_path / "res.py"
    script.write_text(RESOURCE_SERVER)
    connectors.write_connector(ws, "r", {"name": "r", "type": "mcp", "command": sys.executable, "args": [str(script)]})
    pool = connectors.McpPool()
    try:
        tools = {t.name: t for t in await pool.tools(connectors.load_connector(ws, "r"))}
        assert {"r__ping", "r__list_resources", "r__read_resource"} <= set(tools)
        assert tools["r__list_resources"].readonly and tools["r__read_resource"].untrusted
        listing = await tools["r__list_resources"].invoke({}, None)
        assert "notes://today" in listing
        assert await tools["r__read_resource"].invoke({"uri": "notes://today"}, None) == "buy milk"
    finally:
        await pool.aclose()
