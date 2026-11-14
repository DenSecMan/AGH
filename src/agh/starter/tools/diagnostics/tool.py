from agh.language import checker_for, feedback, missing_programs
from agh.sdk import ToolContext


async def run(paths: list[str], ctx: ToolContext, format: bool = False) -> str:
    """Check files with the workspace's configured linters and type checkers (agh.yaml `language:`).
    edit_file and write_file already do this after each change; use this to re-check files or ones you did
    not write.

    Args:
        paths: Files to check, relative to the workspace.
        format: Also run the formatter on each file first.
    """
    reports = []
    for path in paths:
        target = ctx.resolve(path)
        if not target.is_file():
            reports.append(f"{path}: not a file")
        elif not checker_for(target, ctx.config):
            reports.append(f"{path}: no checker configured for {target.suffix or 'this file type'}")
        else:
            missing = missing_programs(target, ctx, fmt=format)
            unchecked = f"not checked: {', '.join(missing)} is not installed" if missing else ""
            report = "\n\n".join(filter(None, [await feedback(target, ctx, fmt=format), unchecked]))
            reports.append(f"{path}:\n{report or 'clean'}")
    return "\n\n".join(reports)
