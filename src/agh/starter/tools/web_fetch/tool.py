import html
import re

import ipaddress
import socket
from urllib.parse import urlparse

PAGE_CHARS = 15000  # below the harness's 20,000 character spill limit, so paging replaces spilled files
TEXT_TYPES = ("text/", "json", "xml", "javascript", "yaml")


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


def run(url: str, max_chars: int = PAGE_CHARS, offset: int = 0) -> str:
    """Fetch a public URL and return readable text, one page at a time.

    Args:
        url: http(s) URL.
        max_chars: Most characters to return, up to 15000.
        offset: Characters to skip. A long page says which offset to ask for next.
    """
    for _ in range(5):  # follow redirects by hand so each hop gets checked
        check_public(url)
        resp = httpx.get(url, timeout=30, headers={"User-Agent": "agh-agent"})
        if not resp.is_redirect:
            break
        url = str(resp.next_request.url)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    if content_type and not any(kind in content_type for kind in TEXT_TYPES):
        raise ValueError(f"cannot read {content_type} as text")
    text = resp.text
    if "html" in content_type:
        text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>|<!--.*?-->", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
    start = max(offset, 0)
    page = text[start:start + max(1, min(max_chars, PAGE_CHARS))]
    remaining = len(text) - start - len(page)
    return f"{page}\n\n[{remaining} more characters; call again with offset={start + len(page)}]" if remaining > 0 else page
