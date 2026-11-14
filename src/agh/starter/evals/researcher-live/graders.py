import re
from urllib.parse import urlparse


def has_report_sections(text, result, case):
    missing = [s for s in ("Answer", "Evidence", "Unconfirmed") if not re.search(rf"\b{s}\b", text)]
    return not missing, f"missing sections: {missing}"


def _host(url):
    return (urlparse(url).hostname or "").removeprefix("www.")


def cites_a_fetched_page(text, result, case):
    fetched = {_host(c["args"].get("url", "")) for c in result.tool_calls if c["name"] == "web_fetch" and not c["error"]}
    cited = {_host(url) for url in re.findall(r"https?://[^\s)\]>\"']+", text)}
    return bool(fetched & cited), f"fetched {sorted(fetched)}, cited {sorted(cited)}"
