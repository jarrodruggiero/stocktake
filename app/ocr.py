"""Reading a scanned statement, entirely on this machine.

Some registries post distribution advices as scans, with no text in the PDF.
Without this they failed unhelpfully: no text, no template matched, and the app
said the statement was not recognised — which sends you to look at your
templates when the problem is there was nothing to match against.

Local only, which leaves Tesseract: decisions.md #84. It is optional at
runtime, so `available()` answers once and the caller turns a missing binary
into a sentence rather than a traceback.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from functools import lru_cache

BINARY = "tesseract"

# OCR is seconds per page, single-threaded, on a container that is usually given
# half a CPU. A statement is one or two pages; a hundred-page PDF is not a
# statement, and looping over it is a request that never returns.
MAX_PAGES = 4

# Enough for Tesseract to resolve small print without making the bitmap
# enormous. 300 is the scanning convention and what its models were trained
# near; 150 loses decimal points, and 600 is four times the pixels for no gain.
RESOLUTION = 300

# Per page. Generous for a statement and short enough that a pathological file
# cannot hold a worker thread forever.
TIMEOUT_SECONDS = 60


class OcrFailed(RuntimeError):
    """Tesseract ran and could not produce text."""


@lru_cache(maxsize=1)
def available() -> bool:
    """Whether the Tesseract binary is on PATH.

    Cached: this is asked on every upload and the answer cannot change without
    the process restarting. Tests clear it with `available.cache_clear()`.
    """
    return shutil.which(BINARY) is not None


def _open_pdf(data: bytes):
    # Imported here rather than at module scope, the same as everywhere else
    # that touches pdfplumber: this module is reachable from startup and
    # uploading a scan is a rare, deliberate act.
    import pdfplumber

    return pdfplumber.open(io.BytesIO(data))


def _read_image(image) -> str:
    """One page image through Tesseract.

    An argument list and no shell at all: the bytes going in came from an
    upload, and handing attacker-controlled input to a shell is how a filename
    becomes a command. stdin/stdout rather than temporary files, so there is
    nothing on disk to clean up or to leak — the page of a statement is exactly
    the kind of thing that should not be left in /tmp.
    """
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    try:
        finished = subprocess.run(
            [BINARY, "stdin", "stdout", "--psm", "6"],
            input=buffer.getvalue(),
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrFailed(f"{BINARY} did not finish within {TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise OcrFailed(f"could not run {BINARY}: {exc}") from exc
    if finished.returncode != 0:
        detail = finished.stderr.decode("utf-8", "replace").strip().splitlines()
        raise OcrFailed(detail[-1] if detail else f"{BINARY} exited {finished.returncode}")
    return finished.stdout.decode("utf-8", "replace")


def word_boxes(image) -> list[dict]:
    """Every word Tesseract found, with where it found it.

    `--psm 6` with TSV output: one row per word, with pixel coordinates in the
    image handed in. This is what makes the visual mapper work on a SCAN —
    pdfplumber's `extract_words` reads the text layer, and a scan has none, so
    without this the mapper renders a page with nothing clickable on exactly
    the documents it exists for.

    Returns [] rather than raising: a page with no words is a page with no
    boxes, which the caller can render perfectly well.
    """
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    try:
        finished = subprocess.run(
            [BINARY, "stdin", "stdout", "--psm", "6", "tsv"],
            input=buffer.getvalue(), capture_output=True,
            timeout=TIMEOUT_SECONDS, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if finished.returncode != 0:
        return []

    words: list[dict] = []
    for row in finished.stdout.decode("utf-8", "replace").splitlines()[1:]:
        parts = row.split("\t")
        if len(parts) < 12:
            continue
        text = parts[11].strip()
        if not text:
            continue
        try:
            left, top, width, height = (int(parts[i]) for i in (6, 7, 8, 9))
        except ValueError:
            continue
        words.append({"text": text, "x0": left, "top": top,
                      "x1": left + width, "bottom": top + height})
    return words


def read_pdf(data: bytes) -> str:
    """Every page of a scanned PDF, rasterised and read.

    Raises `OcrFailed` rather than returning empty, so the caller can tell "the
    OCR did not work" from "the OCR worked and the page was blank" — two
    different sentences for the person who uploaded it.
    """
    try:
        pdf = _open_pdf(data)
    except Exception as exc:  # pdfplumber raises several unrelated types
        raise OcrFailed(f"this file could not be opened as a PDF: {exc}") from exc

    out = []
    with pdf:
        for page in list(pdf.pages)[:MAX_PAGES]:
            out.append(_read_image(page.to_image(resolution=RESOLUTION).original))
    return "\n".join(out)
