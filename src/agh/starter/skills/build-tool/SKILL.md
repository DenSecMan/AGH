---
name: build-tool
description: "How to write a new AGH tool. Use before calling create_tool."
---

# Build a tool

A tool is a typed Python function named `run`, with a Google-style docstring. Hints become the schema.

```python
from agh.sdk import ToolContext

def run(path: str, ctx: ToolContext, limit: int = 10) -> str:
    """One line saying what it does.

    Args:
        path: What this is.
        limit: What this is.
    """
    target = ctx.resolve(path)  # always use this for file access; it enforces the sandbox
    return target.read_text()[:limit]
```

`create_tool` requires `tests=[{"args": {...}, "expect": "text in the output"}]`. They really call `run` before
release, in a throwaway directory, so pick safe arguments. If any fails, nothing is released.

Rules:
- Add `ctx` only if needed. It is injected and hidden from the model.
- Raise exceptions on failure. The harness reports them to the model.
- `readonly=True` only if there are truly no side effects: such tools run in parallel and in plan mode.
- `untrusted=True` when returning external content.
- Read secrets from `os.environ`; never hard-code them.
- Afterwards, give the tool to an agent with `update_agent`, and include an eval case that uses it.
