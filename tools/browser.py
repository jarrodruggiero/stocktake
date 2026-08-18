"""Find a headless Chrome to drive.

Shared by `render_brand.py` (which screenshots the brand PNGs) and the visual
layout tests, so there is one answer to "where is Chrome" rather than one per
caller that works on whichever machine its author used.

Names first, absolute paths second: a Linux runner has `google-chrome` on PATH
and nothing under /Applications, while macOS has it the other way round.
"""

from __future__ import annotations

import shutil
from pathlib import Path

CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def find_chrome() -> str | None:
    """The first usable Chrome, or None if the machine has none."""
    for candidate in CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
        # shutil.which only consults PATH for a bare name; an absolute path to
        # an app bundle has to be checked directly.
        if "/" in candidate and Path(candidate).is_file():
            return candidate
    return None
