import html
import os
import re
from urllib.parse import parse_qs, urlparse

import httpx

HEADERS = {"User-Agent": "agh-agent"}


def _tavily(query: str, limit: int, key: str) -> list[tuple[str, str, str]]:
    resp = httpx.post("https://api.tavily.com/search", json={"query": query, "max_results": limit},
                      headers={**HEADERS, "Authorization": f"Bearer {key}"}, timeout=30)
    resp.raise_for_status()
    return [(r.get("title", ""), r.get("url", ""), r.get("content", "")) for r in resp.json().get("results", [])]


def _brave(query: str, limit: int, key: str) -> list[tuple[str, str, str]]:
    resp = httpx.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": limit},
                     headers={**HEADERS, "X-Subscription-Token": key}, timeout=30)
    resp.raise_for_status()
    results = (resp.json().get("web") or {}).get("results", [])
    return [(r.get("title", ""), r.get("url", ""), r.get("description", "")) for r in results]


def _plain(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _duckduckgo(query: str, limit: int) -> list[tuple[str, str, str]]:
    """The no-key fallback reads DuckDuckGo's HTML page, so it can be rate limited or break if the page changes."""
    resp = httpx.post("https://html.duckduckgo.com/html/", data={"q": query}, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; agh-agent)"}, timeout=30)
    resp.raise_for_status()
    links = re.findall(r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', resp.text, re.S)
    snippets = re.findall(r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', resp.text, re.S)
    if not links and "no-results" not in resp.text and "No results" not in resp.text:
        raise RuntimeError("DuckDuckGo returned no readable results, probably rate limiting. "
                           "Set TAVILY_API_KEY or BRAVE_SEARCH_API_KEY in .env for reliable search.")
    results = []
    for index, (href, title) in enumerate(links[:limit]):
        href = html.unescape(href)
        target = parse_qs(urlparse(href).query).get("uddg", [href])[0]  # results link through a redirect
        results.append((_plain(title), target, _plain(snippets[index]) if index < len(snippets) else ""))
    return results


def run(query: str, max_results: int = 5) -> str:
    """Search the web.

    Args:
        query: What to search for.
        max_results: How many results, from 1 to 10.
    """
    limit = max(1, min(max_results, 10))
    if key := os.environ.get("TAVILY_API_KEY"):
        results = _tavily(query, limit, key)
    elif key := os.environ.get("BRAVE_SEARCH_API_KEY"):
        results = _brave(query, limit, key)
    else:
        results = _duckduckgo(query, limit)
    if not results:
        return "no results"
    return "\n\n".join(f"{i}. {title}\n   {url}\n   {snippet}" for i, (title, url, snippet) in enumerate(results[:limit], 1))
