from agh.sdk import ToolContext

IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_CHARS = 16_000  # under the harness's tool result limit, so a long file pages cleanly instead of being cut


def run(path: str, ctx: ToolContext, start_line: int = 1, max_lines: int = 2000) -> str:
    """Read a text file, or the text of a PDF. For images use view_image. A long file comes back in parts:
    the result ends with the start_line to continue from.

    Args:
        path: Path relative to the workspace.
        start_line: First line to return, starting at 1.
        max_lines: Most lines to return.
    """
    target = ctx.resolve(path)
    suffix = target.suffix.lower()
    if suffix in IMAGE_TYPES:
        return f"{path} is an image; use view_image to look at it"
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ValueError("reading PDFs needs the pypdf package: pip install pypdf") from None
        pages = PdfReader(str(target)).pages
        text = "\n".join(f"--- page {i} ---\n{p.extract_text() or ''}" for i, p in enumerate(pages, 1))
    else:
        text = target.read_text(encoding="utf-8", errors="replace")
        ctx.note_read(target)
    lines = text.splitlines()
    start = max(start_line, 1)
    chosen, size = [], 0
    for number, line in enumerate(lines[start - 1 : start - 1 + max_lines], start):
        row = f"{number}\t{line}"
        if chosen and size + len(row) + 1 > MAX_CHARS:  # the first line always goes out whole
            break
        chosen.append(row)
        size += len(row) + 1
    following = start + len(chosen)
    remaining = len(lines) - (following - 1)
    more = f"\n[{remaining} more lines; continue with start_line={following}]" if remaining > 0 else ""
    return "\n".join(chosen) + more
