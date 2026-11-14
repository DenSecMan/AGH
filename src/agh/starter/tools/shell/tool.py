from agh.sdk import ToolContext, run_process, shell_argv, start_background


async def run(command: str, ctx: ToolContext, timeout: int = 120, background: bool = False) -> str:
    """Run a shell command with the workspace as working directory. On Windows the command runs in cmd.exe, not
    PowerShell: PowerShell syntax ($vars, here-strings) fails unless wrapped in powershell -Command. For multi-line
    code, write a script file or use python -c instead of nested quoting.

    Args:
        command: Command line.
        timeout: Seconds before it and everything it started are killed. Ignored with background.
        background: Start a long-running command (a dev server, a watcher) and return at once. Read its
            output and stop it with the process tool.
    """
    argv = shell_argv(command, ctx.config, ctx.root)
    if background:
        bg = await start_background(argv, ctx.root, ctx.processes)
        return f"started {bg.id} in the background; use process(action='output', id='{bg.id}') to read its output"
    result = await run_process(argv, ctx.root, timeout)
    if result.timed_out:
        return f"killed after {timeout}s (the command and its child processes were stopped)"
    return f"exit code {result.returncode}\n{result.output}"
