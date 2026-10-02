"""No tracked text file carries a carriage return.

`.gitattributes` normalises line endings at commit, for every git client that
honours it. This catches whatever gets past that — a tool that ignores
attributes, a file uploaded through a web form — and says how to fix it.

It exists because #44 arrived with 325 files rewritten from LF to CRLF and
every check green. Python reads CRLF happily, so nothing failed; the only
symptom was a one-line fix that nobody could read in review.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Walked only when there is no git metadata — inside the test image, which
# copies the source but not `.git`. Dot-directories are caches and virtualenvs,
# except these two, which hold files the repository ships.
_KEEP_DOTDIRS = {".github", ".claude"}


def _tracked_files() -> list[Path]:
    """Every file git tracks, or every file under ROOT if git is not there."""
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                             capture_output=True).stdout
        return [ROOT / name for name in out.decode().split("\0") if name]
    except (OSError, subprocess.CalledProcessError):
        found = []
        for path in ROOT.rglob("*"):
            parts = path.relative_to(ROOT).parts
            if any(p.startswith(".") and p not in _KEEP_DOTDIRS for p in parts):
                continue
            if "__pycache__" in parts or not path.is_file():
                continue
            found.append(path)
        return found


def _has_carriage_return(path: Path) -> bool:
    """True for a TEXT file containing a CR. A NUL byte in the first 8 KB is
    git's own test for binary, and binary files are not this guard's business."""
    data = path.read_bytes()
    if b"\0" in data[:8000]:
        return False
    return b"\r" in data


def test_there_are_files_to_check():
    """An empty sweep proves nothing — the guard's own smoke test."""
    names = {p.relative_to(ROOT).as_posix() for p in _tracked_files()}

    assert "app/main.py" in names
    assert len(names) > 100


def test_no_tracked_text_file_has_a_carriage_return():
    offenders = sorted(p.relative_to(ROOT).as_posix()
                       for p in _tracked_files() if p.is_file()
                       and _has_carriage_return(p))

    assert not offenders, (
        f"{len(offenders)} file(s) have CRLF line endings: {offenders[:10]}. "
        "Run `git add --renormalize .` and amend the commit — .gitattributes "
        "will store them as LF."
    )


def test_the_guard_notices_crlf(tmp_path):
    """Planted violation, so the check cannot pass by finding nothing to flag."""
    crlf = tmp_path / "windows.py"
    crlf.write_bytes(b"x = 1\r\ny = 2\r\n")
    lf = tmp_path / "unix.py"
    lf.write_bytes(b"x = 1\ny = 2\n")
    binary = tmp_path / "image.png"
    binary.write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR")

    assert _has_carriage_return(crlf) is True
    assert _has_carriage_return(lf) is False
    assert _has_carriage_return(binary) is False   # a PNG header has CRLF in it
