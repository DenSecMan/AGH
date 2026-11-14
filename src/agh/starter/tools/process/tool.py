import asyncio
import time

from agh.sdk import ToolContext, kill_tree


async def run(action: str, ctx: ToolContext, id: str = "", tail: int = 100) -> str:
    """Manage background commands.

    Args:
        action: list, output or stop.
        id: The process id from shell, such as p1. Needed for output and stop.
        tail: How many of the most recent output lines to return.
    """
    store = ctx.processes
    if action == "list":
        rows = []
        for p in store.values():
            state = "running" if p.running else f"exited {p.proc.returncode}"
            rows.append(f"{p.id}: {state} {int(time.time() - p.started)}s  {p.command}")
        return "\n".join(rows) or "no background processes"
    bg = store.get(id)
    if not bg:
        return f"no process {id!r}; have: {sorted(store) or 'none'}"
    if action == "output":
        state = "running" if bg.running else f"exited with code {bg.proc.returncode}"
        return f"{bg.id} {state}\n" + "\n".join(bg.lines[-tail:])
    if action == "stop":
        if bg.running:
            kill_tree(bg.proc.pid)
            try:
                await asyncio.wait_for(bg.proc.wait(), 5)
            except asyncio.TimeoutError:
                pass
        return f"stopped {bg.id}\n" + "\n".join(bg.lines[-20:])
    raise ValueError("action must be list, output or stop")
