import base64
import mimetypes

from agh.sdk import ToolContext

MAX_BYTES = 5_000_000


def run(path: str, ctx: ToolContext) -> list:
    """Show an image to the model. Needs a model that accepts images.

    Args:
        path: Path relative to the workspace.
    """
    target = ctx.resolve(path)
    mime = mimetypes.guess_type(target.name)[0] or ""
    if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        raise ValueError(f"{path} is not a png, jpg, gif or webp image")
    data = target.read_bytes()
    if len(data) > MAX_BYTES:
        raise ValueError(f"{path} is {len(data) // 1_000_000} MB; the limit is 5 MB")
    return [{"type": "text", "text": f"Image {path} ({len(data)} bytes):"},
            {"type": "image", "source_type": "base64", "mime_type": mime,
             "data": base64.b64encode(data).decode("ascii")}]
