from agh.language import with_feedback
from agh.sdk import ToolContext, read_text_eol, write_text_eol


async def run(path: str, ctx: ToolContext, old: str = "", new: str = "", replace_all: bool = False,
        edits: list[dict[str, str]] | None = None) -> str:
    """Replace exact text in a file: one old/new pair, or several in `edits`, applied in order, all or nothing.
    The file keeps its line endings.

    Args:
        path: Path relative to the workspace.
        old: Text to find. Must appear exactly once unless replace_all is true.
        new: Replacement text.
        replace_all: Replace every occurrence of old.
        edits: Several changes as [{"old": "...", "new": "..."}]; each old must appear exactly once when applied.
    """
    target = ctx.resolve(path)
    ctx.check_fresh(target)
    text, eol = read_text_eol(target)  # matched with "\n" endings, written back with the file's own
    changes = edits or [{"old": old, "new": new}]
    total = 0
    for i, change in enumerate(changes, 1):
        find = change.get("old", "").replace("\r\n", "\n")
        repl = change.get("new", "").replace("\r\n", "\n")
        if not find:
            raise ValueError(f"edit {i}: old must not be empty")
        count = text.count(find)
        if count == 0 or (count > 1 and (edits or not replace_all)):
            where = f"edit {i}: " if edits else ""
            hint = "; pass replace_all or include more surrounding text" if count > 1 else ""
            raise ValueError(f"{where}expected exactly one match, found {count}{hint}")
        text = text.replace(find, repl)
        total += count
    write_text_eol(target, text, eol)
    ctx.note_read(target)
    return await with_feedback(f"edited {path} ({total} replacement{'s' if total != 1 else ''})", target, ctx)
