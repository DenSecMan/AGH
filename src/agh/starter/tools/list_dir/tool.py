from agh.sdk import ToolContext


def run(ctx: ToolContext, path: str = ".") -> str:
    """List a directory.

    Args:
        path: Path relative to the workspace.
    """
    entries = sorted(ctx.resolve(path).iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    return "\n".join(p.name + ("/" if p.is_dir() else "") for p in entries[:500]) or "(empty)"
