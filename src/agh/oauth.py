"""OAuth 2 authorization code with PKCE, for connectors that act as the user (GitHub, Google, Slack...).

connector.yaml:
  auth:
    type: oauth2_authorization_code
    authorize_url: https://example.com/oauth/authorize
    token_url: https://example.com/oauth/token
    client_id_env: EXAMPLE_CLIENT_ID
    client_secret_env: EXAMPLE_CLIENT_SECRET   # optional; public clients use PKCE alone
    scopes: [read, write]
    redirect_port: 8765                        # register http://127.0.0.1:8765/callback with the provider

`agh connector login NAME` opens the browser once. Tokens live in ~/.agh/tokens/, outside the workspace, so
agents' file tools can't read them, and are refreshed automatically when they expire.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from agh.keys import agh_home

LOGIN_TIMEOUT_S = 300


class OAuthError(RuntimeError):
    pass


def _env(name: str | None, required: bool = True) -> str:
    value = os.environ.get(name or "", "")
    if required and not value:
        raise OAuthError(f"environment variable {name} is not set (agh key set or the workspace .env)")
    return value


def token_path(workspace_root: Path, name: str) -> Path:
    tag = hashlib.sha256(str(workspace_root.resolve()).encode()).hexdigest()[:12]
    return agh_home() / "tokens" / f"{tag}-{name}.json"


def _save(path: Path, token: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def _store(body: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    if "access_token" not in body:
        raise OAuthError(f"token endpoint answered without an access_token: {body.get('error', body)}")
    return {"access_token": body["access_token"],
            "refresh_token": body.get("refresh_token") or (previous or {}).get("refresh_token"),
            "expires_at": time.time() + float(body.get("expires_in", 3600)), "scope": body.get("scope", "")}


def _client_fields(auth: dict[str, Any]) -> dict[str, str]:
    fields = {"client_id": _env(auth.get("client_id_env"))}
    if secret := _env(auth.get("client_secret_env"), required=False):
        fields["client_secret"] = secret
    return fields


async def access_token(workspace_root: Path, name: str, auth: dict[str, Any]) -> str:
    """A valid access token, refreshed if needed. Raises with the login command if there is none."""
    path = token_path(workspace_root, name)
    if not path.exists():
        raise OAuthError(f"connector {name} is not logged in; run `agh connector login {name}` (or /connector login)")
    token = json.loads(path.read_text(encoding="utf-8"))
    if token.get("expires_at", 0) > time.time() + 60:
        return token["access_token"]
    if not token.get("refresh_token"):
        raise OAuthError(f"the {name} login expired; run `agh connector login {name}` again")
    data = {"grant_type": "refresh_token", "refresh_token": token["refresh_token"], **_client_fields(auth)}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(auth["token_url"], data=data, headers={"Accept": "application/json"})
    if resp.status_code >= 400:
        raise OAuthError(f"refreshing the {name} login failed (HTTP {resp.status_code}); "
                         f"run `agh connector login {name}` again")
    token = _store(resp.json(), token)
    _save(path, token)
    return token["access_token"]


async def login(workspace_root: Path, name: str, auth: dict[str, Any],
                open_url: Callable[[str], Any] = webbrowser.open, say: Callable[[str], Any] = print) -> Path:
    """Run the browser flow: a one-shot server on 127.0.0.1 receives the code, which is swapped for tokens."""
    port = int(auth.get("redirect_port", 8765))
    redirect = f"http://127.0.0.1:{port}/callback"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    query = {"response_type": "code", "client_id": _env(auth.get("client_id_env")), "redirect_uri": redirect,
             "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
             **({"scope": " ".join(auth["scopes"])} if auth.get("scopes") else {}),
             **(auth.get("extra_params") or {})}
    url = f"{auth['authorize_url']}{'&' if '?' in auth['authorize_url'] else '?'}{urlencode(query)}"
    got: asyncio.Future[dict[str, list[str]]] = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = (await reader.readline()).decode("latin-1")
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass
        target = line.split(" ")[1] if " " in line else "/"
        params = parse_qs(urlsplit(target).query)
        ok = urlsplit(target).path == "/callback" and params.get("state") == [state]
        body = ("Signed in. You can close this tab and go back to AGH." if ok and "code" in params
                else "Sign-in failed or was cancelled. You can close this tab.")
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: "
                     f"{len(body.encode())}\r\nConnection: close\r\n\r\n{body}".encode())
        await writer.drain()
        writer.close()
        if ok and not got.done():
            got.set_result(params)

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    try:
        say(f"Opening the browser to sign in to {name}. If it doesn't open, visit:\n{url}")
        open_url(url)
        params = await asyncio.wait_for(got, LOGIN_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise OAuthError(f"no sign-in within {LOGIN_TIMEOUT_S}s") from None
    finally:
        server.close()
        await server.wait_closed()
    if "error" in params or "code" not in params:
        raise OAuthError(f"sign-in refused: {params.get('error_description', params.get('error', ['unknown']))[0]}")
    data = {"grant_type": "authorization_code", "code": params["code"][0], "redirect_uri": redirect,
            "code_verifier": verifier, **_client_fields(auth)}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(auth["token_url"], data=data, headers={"Accept": "application/json"})
    if resp.status_code >= 400:
        raise OAuthError(f"token exchange failed: HTTP {resp.status_code} {resp.text[:200]}")
    path = token_path(workspace_root, name)
    _save(path, _store(resp.json()))
    return path


def logout(workspace_root: Path, name: str) -> bool:
    path = token_path(workspace_root, name)
    if path.exists():
        path.unlink()
        return True
    return False
