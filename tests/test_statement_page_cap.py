"""A statement PDF over the page limit is refused, before any page is read.

Reported in #46. OCR was capped at four pages and the visual mapper at six, but
the text-layer path read every page of whatever was uploaded — and 10 MB holds
a great many pages. One upload could keep a worker busy for a long time.

Refused rather than truncated. Reading only the first pages would drop rows
without saying so, which this project treats as worse than refusing; a person
told the file is too long can split it.

The property worth testing is the ORDER. A cap applied after extracting every
page would produce the right message and fix nothing, so
`test_no_page_is_read_when_the_file_is_refused` counts the reads. The PDFs are
real — built with PIL — because stubbing `_pdf_text` would bypass the check.
"""

from __future__ import annotations

import io

import pytest

from app import ocr, statements


def pdf_with_pages(count: int) -> bytes:
    """A real, image-only PDF of `count` blank pages. Tiny pages keep it fast;
    the page count is the only thing under test."""
    from PIL import Image

    pages = [Image.new("RGB", (20, 20), "white") for _ in range(count)]
    buffer = io.BytesIO()
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
    return buffer.getvalue()


def test_a_pdf_over_the_limit_is_refused_with_a_reason():
    result = statements.extract(pdf_with_pages(statements.MAX_PAGES + 1),
                                ocr_enabled=False)

    assert result.text == ""
    assert f"{statements.MAX_PAGES + 1} pages" in result.problem
    assert str(statements.MAX_PAGES) in result.problem


def test_a_pdf_at_the_limit_is_read():
    """The boundary. These pages are blank, so the read ends at "no text
    layer" — which is the point: it got past the page check to say so."""
    result = statements.extract(pdf_with_pages(statements.MAX_PAGES),
                                ocr_enabled=False)

    assert "pages" not in result.problem
    assert "no text layer" in result.problem


def test_no_page_is_read_when_the_file_is_refused(monkeypatch):
    """What #46 is actually about: the work, not the message."""
    import pdfplumber.page

    reads: list[int] = []
    real = pdfplumber.page.Page.extract_text
    monkeypatch.setattr(pdfplumber.page.Page, "extract_text",
                        lambda self, *a, **k: reads.append(1) or real(self, *a, **k))

    statements.extract(pdf_with_pages(statements.MAX_PAGES + 1), ocr_enabled=False)

    assert reads == []


def test_a_long_scan_is_not_sent_to_ocr(monkeypatch):
    """OCR has its own four-page cap, but a refused file should not reach it
    at all — nor pay for a text-layer pass over every page on the way."""
    called: list[int] = []
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "read_pdf", lambda data: called.append(1) or "TEXT")

    result = statements.extract(pdf_with_pages(statements.MAX_PAGES + 1))

    assert called == []
    assert "pages" in result.problem


@pytest.mark.parametrize("pages", [1, 2])
def test_ordinary_statements_are_nowhere_near_it(pages):
    """A dividend statement is a page or two. The limit must not be the
    reason one fails."""
    result = statements.extract(pdf_with_pages(pages), ocr_enabled=False)

    assert "pages" not in result.problem


def test_the_upload_page_says_why(client, session_factory):
    from test_routes import make_login, session_csrf

    make_login(client, session_factory)

    page = client.post(
        "/imports-exports/statement",
        data={"_csrf": session_csrf(session_factory)},
        files={"file": ("long.pdf", pdf_with_pages(statements.MAX_PAGES + 1),
                        "application/pdf")},
        headers={"accept": "text/html"},
    )

    assert f"{statements.MAX_PAGES + 1} pages" in page.text
