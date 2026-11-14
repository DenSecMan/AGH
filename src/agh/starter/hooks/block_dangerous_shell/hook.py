import re

BLOCKED = [r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\*)", r"\bmkfs\b", r"\bdd\s+.*of=/dev/", r"\bformat\s+[a-z]:",
           r"\bgit\s+push\s+.*--force", r"\bgit\s+reset\s+--hard", r"\bdel\s+/s", r"\bRemove-Item\b.*-Recurse"]


def handle(event: dict) -> dict | None:
    command = event.get("args", {}).get("command", "")
    for pattern in BLOCKED:
        if re.search(pattern, command, re.I):
            return {"deny": f"blocked dangerous command (matched {pattern})"}
    return None
