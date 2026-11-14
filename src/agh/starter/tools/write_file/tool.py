from agh.language import with_feedback
from agh.sdk import ToolContext, read_text_eol, write_text_eol


async def run(path: str, content: str, ctx: ToolContext) -> str:
    """Create or overwrite a text file, making parent directories. An existing file keeps its line endings;
    a new file is written exactly as given.

    Args:
        path: Path relative to the workspace.
        content: Full new contents.
    """
    target = ctx.resolve(path)
    ctx.check_fresh(target)
    eol = "\n"
    if target.is_file():
        try:
            eol = read_text_eol(target)[1]
        except (UnicodeDecodeError, OSError):
            pass  # not text until now: nothing to preserve
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text_eol(target, content, eol)
    ctx.note_read(target)
    return await with_feedback(f"wrote {len(content)} characters to {path}", target, ctx)
