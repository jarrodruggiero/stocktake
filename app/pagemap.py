"""Rendering a statement so it can be mapped by pointing at the page.

The text designer works on extracted text: you pick a field, then click the word
you want in a wall of words lifted out of its layout. For a PDF that came
through OCR that is a real problem — OCR output reads as a jumble, and two
amounts of similar size are indistinguishable in a text dump and obvious on the
page.

So this renders each page to an image and puts the word boxes back where they
belong. Clicking a box sends the same token index the text designer sends, into
the same `docformats.infer_field`: the inference is not duplicated, only the way
you point at it.

**Nothing is held between requests.** Page images go to disk and are served from
there, so a pod that has run the mapper once is not carrying bitmaps around
until its next restart.

**A session belongs to one person.** The token is random and the directory
records who created it; another signed-in user presenting the token gets
nothing. These are pictures of somebody's dividend statement.

**Thirty minutes of idleness ends it**, and ending it deletes the files rather
than refusing to serve them — swept lazily on the next upload, and by the daily
maintenance job for the install where nobody uses the feature again.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# How long a session survives without being touched. Long enough to map a
# statement carefully; short enough that a forgotten tab is not a directory of
# page images sitting on the volume overnight.
IDLE_MINUTES = 30

# One render serves both jobs, so the resolution is a compromise: high enough
# for Tesseract to read small print off a scan (it wants 150 at the very least
# and prefers 300), low enough that the PNG is not enormous — these are written
# to disk and sent over the wire.
RESOLUTION = 200

# A statement is a page or two. The cap is the same reasoning as the OCR one:
# a hundred-page PDF is not a statement, and rendering it is a request that
# never returns and a directory that fills the volume.
MAX_PAGES = 6

_TOKEN = re.compile(r"^[0-9a-f-]{36}$")


@dataclass
class RenderedPage:
    png: bytes
    width: int
    height: int
    # Word boxes in the RENDERED IMAGE's pixels, whichever source they came
    # from — `_render` converts pdfplumber's PDF points before returning, so
    # nothing downstream has to know which of the two paths ran.
    words: list[dict]
    # Whether these came from OCR rather than a text layer. Shown to the person
    # mapping, who should treat the words with more suspicion.
    from_ocr: bool = False


@dataclass(frozen=True)
class Box:
    index: int          # the token index `docformats.infer_field` expects
    text: str
    # PERCENTAGES of the page, not pixels. The image is shown at whatever width
    # the screen allows, and a box positioned in pixels drifts off its word the
    # moment the page is scaled — which is every screen that is not exactly as
    # wide as the render.
    left: float
    top: float
    width: float
    height: float


@dataclass
class Session:
    token: str
    user_id: int
    text: str
    pages: list[dict]   # {"width", "height", "boxes": [Box, ...]}


def text_from(words: list[dict]) -> str:
    """The document text, built from the word boxes.

    **Not** `page.extract_text()`, and this is the load-bearing decision in the
    module. The two orderings differ — `extract_text` reflows and merges,
    `extract_words` does not — so a click on box 4 that infers from token 4 of
    a different sequence produces a template reading the wrong number. Building
    the text out of the same list the boxes come from makes them the same
    sequence by construction rather than by hope.

    Words are grouped into lines by their vertical position, because
    `docformats.tokenize` records a line number and the label inference uses it
    to avoid taking a label from the row above.
    """
    lines: dict[int, list[dict]] = {}
    for word in words:
        # Round to the nearest few points: characters on one line differ
        # slightly in `top`, and an exact key would make every word its own
        # line.
        key = int(round(float(word["top"]) / 4))
        lines.setdefault(key, []).append(word)
    out = []
    for key in sorted(lines):
        row = sorted(lines[key], key=lambda w: float(w["x0"]))
        out.append(" ".join(str(w["text"]) for w in row))
    return "\n".join(out)


def boxes_for(words: list[dict], *, page_width: float, page_height: float,
              first_index: int) -> list[Box]:
    """The clickable boxes for one page, in the same order `text_from` emits.

    `first_index` continues the numbering across pages: page two's first word is
    not token zero, and restarting would make every click on page two map to a
    word on page one.

    Coordinates come out as percentages of the page, so the image can be shown
    at any width and the boxes stay on their words.
    """
    lines: dict[int, list[dict]] = {}
    for word in words:
        lines.setdefault(int(round(float(word["top"]) / 4)), []).append(word)

    boxes: list[Box] = []
    index = first_index
    wide = float(page_width) or 1.0
    high = float(page_height) or 1.0
    for key in sorted(lines):
        for word in sorted(lines[key], key=lambda w: float(w["x0"])):
            x0, x1 = float(word["x0"]), float(word["x1"])
            top, bottom = float(word["top"]), float(word["bottom"])
            boxes.append(Box(index=index, text=str(word["text"]),
                             left=100 * x0 / wide, top=100 * top / high,
                             width=100 * (x1 - x0) / wide,
                             height=100 * (bottom - top) / high))
            index += 1
    return boxes


def _render(data: bytes, resolution: int) -> list[RenderedPage]:
    """Rasterise and read the word boxes. Stubbed in tests — rasterising a PDF
    is pdfplumber's job and is tested by pdfplumber."""
    import io

    import pdfplumber

    from . import ocr

    scale = resolution / 72
    pages: list[RenderedPage] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in list(pdf.pages)[:MAX_PAGES]:
            image = page.to_image(resolution=resolution)
            buffer = io.BytesIO()
            image.original.save(buffer, format="PNG")

            # A text layer if there is one, OCR if there is not. Without the
            # second path the mapper renders a page with nothing clickable on
            # exactly the documents it exists for — a scan has no words to
            # extract, which is why it needed OCR in the first place. This was
            # found by running a real scanned PDF through it: zero boxes.
            words = page.extract_words() or []
            from_ocr = False
            if words:
                # pdfplumber reports PDF points; everything downstream works in
                # the rendered image's pixels, so convert here and once.
                words = [{"text": w["text"],
                          "x0": float(w["x0"]) * scale, "x1": float(w["x1"]) * scale,
                          "top": float(w["top"]) * scale,
                          "bottom": float(w["bottom"]) * scale}
                         for w in words]
            elif ocr.available():
                words = ocr.word_boxes(image.original)
                from_ocr = True

            pages.append(RenderedPage(
                png=buffer.getvalue(), width=image.original.width,
                height=image.original.height, words=words, from_ocr=from_ocr))
    return pages


def _root(settings) -> Path:
    return Path(settings.imports.visual_dir)


def _directory(settings, token: str) -> Path | None:
    """The session's directory, or None if the token is not one we issued.

    The token arrives in a URL path and becomes a directory name, so this is
    the containment check. A UUID shape is the whole allow-list; nothing that
    is not one of ours can name a path at all.
    """
    if not _TOKEN.match(token or ""):
        return None
    return _root(settings) / token


def create(settings, *, user_id: int, data: bytes) -> str:
    """Render a PDF and keep it for this person, for a while."""
    sweep(settings)

    pages = _render(data, RESOLUTION)
    token = str(uuid.uuid4())
    directory = _root(settings) / token
    directory.mkdir(mode=0o700, parents=True)

    text_parts: list[str] = []
    index = 0
    meta: list[dict] = []
    for number, page in enumerate(pages):
        (directory / f"{number}.png").write_bytes(page.png)
        boxes = boxes_for(page.words, page_width=page.width,
                          page_height=page.height, first_index=index)
        index += len(boxes)
        text_parts.append(text_from(page.words))
        meta.append({
            "width": page.width,
            "height": page.height,
            "from_ocr": page.from_ocr,
            "boxes": [vars(b) for b in boxes],
        })

    (directory / "session.json").write_text(json.dumps({
        "user_id": user_id,
        "touched": time.time(),
        "text": "\n".join(text_parts),
        "pages": meta,
    }))
    return token


def _read(settings, token: str) -> tuple[Path, dict] | None:
    directory = _directory(settings, token)
    if directory is None or not (directory / "session.json").is_file():
        return None
    try:
        data = json.loads((directory / "session.json").read_text())
    except (OSError, ValueError):
        return None
    return directory, data


def load(settings, token: str, *, user_id: int) -> Session | None:
    """The session, if it exists, is this person's, and has not gone cold.

    Touching it pushes the expiry out: thirty minutes of *idleness*, not thirty
    minutes of existence — mapping a long statement should not be interrupted
    halfway through.
    """
    found = _read(settings, token)
    if found is None:
        return None
    directory, data = found

    if time.time() - data.get("touched", 0) > IDLE_MINUTES * 60:
        # Expiring means deleting. Refusing to serve it while the images sit on
        # the volume is not cleanup.
        shutil.rmtree(directory, ignore_errors=True)
        return None
    # The owner check is the control; the random token only stops guessing.
    if data.get("user_id") != user_id:
        return None

    data["touched"] = time.time()
    (directory / "session.json").write_text(json.dumps(data))
    return Session(token=token, user_id=user_id, text=data.get("text", ""),
                   pages=data.get("pages", []))


def page_png(settings, token: str, *, user_id: int, index: int) -> bytes | None:
    # The bounds check below is defence in depth rather than the control: the
    # index is typed `int` by the router, so it cannot carry a separator, and a
    # page that is not there fails the read anyway. Kept because "serve page N
    # of a document with fewer than N pages" should be a decision, not an
    # accident of which error happens to be raised.
    session = load(settings, token, user_id=user_id)
    if session is None or not 0 <= index < len(session.pages):
        return None
    path = _directory(settings, token) / f"{index}.png"
    try:
        return path.read_bytes()
    except OSError:
        return None


def sweep(settings) -> int:
    """Delete every session that has gone cold. Returns how many."""
    root = _root(settings)
    if not root.is_dir():
        return 0
    cutoff = time.time() - IDLE_MINUTES * 60
    removed = 0
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        try:
            touched = json.loads((directory / "session.json").read_text()).get("touched", 0)
        except (OSError, ValueError):
            touched = 0  # unreadable: it is rubbish, and rubbish is swept
        if touched < cutoff:
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
    return removed
