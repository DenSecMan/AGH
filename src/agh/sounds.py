"""Short notification sounds, so the user can look away while AGH works.

done()      a pleasant rising chime: the task is finished.
attention() one brief soft beep: AGH is waiting for the user, e.g. to approve a tool call.

Sounds play on a background thread and never raise. Set AGH_SOUND=0 to silence them; they are also off under pytest.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading

# (frequency Hz, milliseconds): a major arpeggio C5 E5 G5 C6 for done, one soft A5 for attention.
DONE_TONES = ((523, 110), (659, 110), (784, 110), (1047, 260))
ATTENTION_TONES = ((880, 120),)


def enabled() -> bool:
    return os.environ.get("AGH_SOUND", "1").lower() not in ("0", "off", "false", "no") \
        and "PYTEST_CURRENT_TEST" not in os.environ


def done() -> None:
    _play(DONE_TONES, "Glass", "complete")


def attention() -> None:
    _play(ATTENTION_TONES, "Tink", "message")


def _play(tones: tuple[tuple[int, int], ...], mac_sound: str, freedesktop_sound: str) -> None:
    if enabled():
        threading.Thread(target=_play_now, args=(tones, mac_sound, freedesktop_sound), daemon=True).start()


def _play_now(tones: tuple[tuple[int, int], ...], mac_sound: str, freedesktop_sound: str) -> None:
    try:
        if sys.platform == "win32":
            import winsound
            for freq, ms in tones:
                winsound.Beep(freq, ms)
        elif sys.platform == "darwin":
            subprocess.run(["afplay", f"/System/Library/Sounds/{mac_sound}.aiff"], timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("canberra-gtk-play"):
            subprocess.run(["canberra-gtk-play", "-i", freedesktop_sound], timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()
    except Exception:
        pass  # a missing sound device must never disturb a run
