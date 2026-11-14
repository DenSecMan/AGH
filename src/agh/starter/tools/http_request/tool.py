import ipaddress
import socket
from urllib.parse import urlparse


def check_public(url: str) -> None:
    """Refuse loopback and private addresses, so fetched content can't steer the agent at internal services."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("only http(s) URLs are allowed")
    for info in socket.getaddrinfo(parsed.hostname, parsed.port or 443):
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ValueError(f"{parsed.hostname} resolves to a non-public address")


import httpx


def run(method: str, url: str, headers: dict[str, str] | None = None, json_body: dict | list | None = None) -> str:
    """Make an HTTP request. For services used repeatedly, create a connector instead.

    Args:
        method: GET, POST, PUT, PATCH or DELETE.
        url: Public http(s) URL.
        headers: Optional headers.
        json_body: Optional JSON body.
    """
    check_public(url)
    resp = httpx.request(method.upper(), url, headers=headers, json=json_body, timeout=30)
    return f"HTTP {resp.status_code}\n{resp.text}"
