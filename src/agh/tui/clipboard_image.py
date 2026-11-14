"""Read an image off the system clipboard and save it as a PNG, with no extra dependencies.

Windows uses PowerShell, macOS uses osascript, Linux uses wl-paste or xclip. Returns False when the clipboard holds
no image or the platform tool is missing."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
TIMEOUT_S = 10

_WINDOWS_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
    "$i = [System.Windows.Forms.Clipboard]::GetImage(); if ($null -eq $i) { exit 1 }; "
    "$i.Save($env:AGH_CLIP_OUT, [System.Drawing.Imaging.ImageFormat]::Png)"
)
_MAC_SCRIPT = (
    'set f to open for access POSIX file (system attribute "AGH_CLIP_OUT") with write permission\n'
    "set eof f to 0\n"
    "write (the clipboard as «class PNGf») to f\n"
    "close access f"
)


def _run(cmd: list[str], dest: Path, capture: bool = False) -> bool:
    env = {**os.environ, "AGH_CLIP_OUT": str(dest)}
    try:
        done = subprocess.run(cmd, env=env, capture_output=True, timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    if done.returncode != 0:
        return False
    if capture:
        dest.write_bytes(done.stdout)
    return True


def save_clipboard_image(dest: Path) -> bool:
    """Save the clipboard's image to `dest` (a .png path). True only if a real PNG was written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    if sys.platform == "win32":
        shell = shutil.which("powershell") or shutil.which("pwsh")
        ok = bool(shell) and _run([shell, "-NoProfile", "-NonInteractive", "-STA", "-Command", _WINDOWS_SCRIPT], dest)
    elif sys.platform == "darwin":
        ok = _run(["osascript", "-e", _MAC_SCRIPT], dest)
    elif shutil.which("wl-paste"):
        ok = _run(["wl-paste", "--type", "image/png"], dest, capture=True)
    elif shutil.which("xclip"):
        ok = _run(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"], dest, capture=True)
    else:
        ok = False
    try:
        valid = ok and dest.read_bytes()[:8] == PNG_MAGIC
    except OSError:
        valid = False
    if not valid:
        dest.unlink(missing_ok=True)
    return valid
