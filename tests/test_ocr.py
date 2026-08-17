"""Reading a scanned statement: when OCR runs, and when it must not.

Registries do post statements as scans, and without this they fail in the least
helpful way available — `extract_text` returned nothing, no template matched,
and the app said the statement was not recognised, which sends you to your
templates when the problem is that the PDF has no text at all.

Three rules shape everything here:

**Local only, and not negotiable.** A statement carries a name, an address, a
holder number and an amount. There is no cloud OCR path and there must never be
one, which leaves Tesseract — a subprocess with no network of its own.

**Only when there is no text layer.** OCR on a PDF that has one is slower and
strictly worse: it re-derives from pixels what the file already states exactly.

**Say so afterwards.** OCR misreads digits, and these are money — so a statement
read this way is flagged through to the preview, because whoever checks it needs
to know to check harder.

No real PDFs: rasterising and Tesseract are stubbed. What is tested is the
decision. `test_ocr_endtoend.py` is the half that proves a scan is readable.
"""

from __future__ import annotations

import re

import pytest

from app import ocr, statements

# --------------------------------------------------------------------------- #
# When it runs
# --------------------------------------------------------------------------- #

def test_a_pdf_with_a_text_layer_is_never_ocred(monkeypatch):
    """The expensive, lossy path must not run when the exact answer is
    already in the file."""
    called = []
    real = ("Vanguard Australian Shares Index ETF — distribution advice. "
            "Payment date 15 July 2026. Net amount 104.70.")
    monkeypatch.setattr(statements, "_pdf_text", lambda data: real)
    monkeypatch.setattr(ocr, "read_pdf", lambda data: called.append(1) or "")

    result = statements.extract(b"whatever")

    assert result.text == real
    assert result.used_ocr is False
    assert called == []


def test_a_pdf_with_no_text_layer_is_ocred(monkeypatch):
    monkeypatch.setattr(statements, "_pdf_text", lambda data: "")
    monkeypatch.setattr(ocr, "read_pdf", lambda data: "SCANNED CONTENT")
    monkeypatch.setattr(ocr, "available", lambda: True)

    result = statements.extract(b"x")

    assert result.text == "SCANNED CONTENT"
    assert result.used_ocr is True


def test_a_nearly_empty_text_layer_is_ocred(monkeypatch):
    """A scan is often wrapped in a PDF that carries a few characters of
    producer metadata or a page number. "Not empty" is not the same as "has
    the statement in it"."""
    monkeypatch.setattr(statements, "_pdf_text", lambda data: "Page 1 of 2")
    monkeypatch.setattr(ocr, "read_pdf", lambda data: "SCANNED CONTENT")
    monkeypatch.setattr(ocr, "available", lambda: True)

    result = statements.extract(b"x")

    assert result.used_ocr is True


def test_a_short_but_real_text_layer_is_left_alone(monkeypatch):
    """The threshold has to sit above page furniture and below a real
    statement. A terse one-line advice is still a text layer."""
    text = "ALPHA distribution 104.70 franking 44.87 paid 2026-07-15 holder 12345678"
    monkeypatch.setattr(statements, "_pdf_text", lambda data: text)
    monkeypatch.setattr(ocr, "read_pdf", lambda data: "SHOULD NOT RUN")
    monkeypatch.setattr(ocr, "available", lambda: True)

    assert statements.extract(b"x").used_ocr is False


# --------------------------------------------------------------------------- #
# When it cannot run
# --------------------------------------------------------------------------- #

def test_a_missing_tesseract_is_reported_not_raised(monkeypatch):
    """Someone running an image without the binary, or a source install. The
    upload must fail with a sentence that says what to do, not a traceback and
    not a silent empty result."""
    called = []
    monkeypatch.setattr(statements, "_pdf_text", lambda data: "")
    monkeypatch.setattr(ocr, "available", lambda: False)
    monkeypatch.setattr(ocr, "read_pdf", lambda data: called.append(1) or "")

    result = statements.extract(b"x")

    assert result.used_ocr is False
    assert result.text == ""
    # The SPECIFIC message, not merely "something went wrong". Found by
    # mutation: deleting this branch entirely still passed, because the
    # generic "OCR could not read it" message that followed happened to
    # contain the same words. The value here is the actionable sentence —
    # which program to install — so that is what gets asserted.
    assert "tesseract" in result.problem.lower()
    assert "install" in result.problem.lower()
    # And it must not have tried to run OCR after saying it could not.
    assert called == []


def test_ocr_can_be_turned_off(monkeypatch):
    """It is a subprocess against an uploaded file. Somebody who does not want
    that running on their box must be able to say so, and still be told why
    their import did nothing."""
    monkeypatch.setattr(statements, "_pdf_text", lambda data: "")
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "read_pdf", lambda data: "SHOULD NOT RUN")

    result = statements.extract(b"x", ocr_enabled=False)

    assert result.used_ocr is False
    assert result.text == ""
    assert result.problem


def test_ocr_failing_is_reported_not_raised(monkeypatch):
    """Tesseract exits non-zero on a file it cannot make sense of. That is a
    bad upload, not a broken app."""
    def boom(data):
        raise ocr.OcrFailed("tesseract exited 1")

    monkeypatch.setattr(statements, "_pdf_text", lambda data: "")
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "read_pdf", boom)

    result = statements.extract(b"x")

    assert result.used_ocr is False
    assert result.problem


def test_ocr_that_finds_nothing_says_so(monkeypatch):
    """A blank page, or a photograph of a desk. Reporting success with an
    empty string sends the reader to their templates."""
    monkeypatch.setattr(statements, "_pdf_text", lambda data: "")
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "read_pdf", lambda data: "   \n  \n")

    result = statements.extract(b"x")

    assert result.problem


# --------------------------------------------------------------------------- #
# What the reader is told
# --------------------------------------------------------------------------- #

def test_the_parsed_statement_carries_the_ocr_flag(monkeypatch, db):
    monkeypatch.setattr(statements, "_pdf_text", lambda data: "")
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "read_pdf", lambda data: "ALPHA paid 104.70 on 15 July 2026")

    parsed = statements.parse_statement(b"x", db)

    assert parsed.used_ocr is True


def test_a_normal_statement_does_not_carry_the_flag(monkeypatch, db):
    monkeypatch.setattr(statements, "_pdf_text",
                        lambda data: "ALPHA distribution advice, paid 15 July 2026, "
                                     "net 104.70, franking credits 44.87")

    parsed = statements.parse_statement(b"x", db)

    assert parsed.used_ocr is False


def test_the_upload_page_warns_when_ocr_was_used(client, session_factory, monkeypatch):
    """The whole point of tracking it. "Check the figures" is the only honest
    thing to say about numbers derived from pixels."""
    from test_routes import make_login, session_csrf

    monkeypatch.setattr(statements, "_pdf_text", lambda data: "")
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "read_pdf", lambda data: "ALPHA paid 104.70 on 15 July 2026")
    make_login(client, session_factory)

    page = client.post(
        "/imports-exports/statement",
        data={"_csrf": session_csrf(session_factory)},
        files={"file": ("scan.pdf", b"%PDF-1.4 fake", "application/pdf")},
        headers={"accept": "text/html"},
    )

    assert page.status_code == 200
    # The claim: the page says the text came from OCR, and tells you to check
    # the numbers. Pinning one phrasing of the second half would mean
    # shortening the sentence fails a test about whether the warning exists.
    warning = re.search(r'<p class="panel warn">(.*?)</p>', page.text, re.S)
    assert warning, "no OCR warning rendered"
    # Collapse the source's line wrapping first — the browser does, so a phrase
    # broken across two lines in the template is still one phrase on the page.
    text = " ".join(warning.group(1).split())
    assert "OCR" in text
    assert re.search(r"check (every figure|the figures)", text, re.I)


def test_the_upload_page_explains_a_pdf_it_could_not_read(client, session_factory,
                                                          monkeypatch):
    """The failure this exists to prevent: saying the statement was
    not recognised, which is true and useless."""
    from test_routes import make_login, session_csrf

    monkeypatch.setattr(statements, "_pdf_text", lambda data: "")
    monkeypatch.setattr(ocr, "available", lambda: False)
    make_login(client, session_factory)

    page = client.post(
        "/imports-exports/statement",
        data={"_csrf": session_csrf(session_factory)},
        files={"file": ("scan.pdf", b"%PDF-1.4 fake", "application/pdf")},
        headers={"accept": "text/html"},
    )

    assert "no text" in page.text.lower()


# --------------------------------------------------------------------------- #
# The module itself
# --------------------------------------------------------------------------- #

def test_available_is_false_when_the_binary_is_absent(monkeypatch):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    ocr.available.cache_clear()

    assert ocr.available() is False
    ocr.available.cache_clear()


def test_available_is_true_when_the_binary_is_present(monkeypatch):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/usr/bin/tesseract")
    ocr.available.cache_clear()

    assert ocr.available() is True
    ocr.available.cache_clear()


def test_the_page_limit_is_enforced(monkeypatch):
    """OCR is seconds per page. An unbounded loop over a 200-page PDF is a
    request that never returns, on a container with one CPU."""
    pages = []

    class FakePage:
        def to_image(self, resolution):
            pages.append(1)
            return FakeImage()

    class FakeImage:
        original = "IMG"

    class FakePdf:
        pages = [FakePage() for _ in range(50)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ocr, "_open_pdf", lambda data: FakePdf())
    monkeypatch.setattr(ocr, "_read_image", lambda image: "text")

    ocr.read_pdf(b"x")

    assert len(pages) == ocr.MAX_PAGES


def test_tesseract_is_run_with_no_shell(monkeypatch):
    """The input is an uploaded file. `shell=True` anywhere near that is how a
    filename becomes a command."""
    import inspect

    source = inspect.getsource(ocr)

    assert "shell=True" not in source


def test_tesseract_is_given_a_timeout(monkeypatch):
    """A subprocess with no timeout is a worker thread that never comes back."""
    import inspect

    source = inspect.getsource(ocr._read_image)

    assert "timeout" in source


@pytest.mark.parametrize("text,expected", [
    ("", True),
    ("   \n \t ", True),
    ("Page 1 of 2", True),
    ("a" * 40, True),
    ("ALPHA distribution 104.70 franking 44.87 paid 2026-07-15 holder 12345678", False),
])
def test_the_no_text_layer_test(text, expected):
    assert statements.looks_scanned(text) is expected
