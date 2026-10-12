"""A page is drawn in a fixed number of pixels, whatever size it says it is.

The visual mapper drew each page at 200 dpi and OCR at 300, so the memory a
page took grew with the size the PDF gave it. A page over the budget is
drawn at a lower resolution instead, so a large-format scan still reads.

`to_image` is stood in for here, and what each page would have cost is
recorded: drawing a big page for real takes a lot of memory.
"""

from __future__ import annotations

import io

import pytest

from app import ocr, pagemap


def one_page_pdf(width: float, height: float) -> bytes:
    """A blank one-page PDF whose page is this big, in points."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] >>".encode()]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    out.write(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets))
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
              f"startxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


@pytest.fixture
def drawn(monkeypatch):
    """The pixels each page would have been drawn in."""
    import pdfplumber.page
    from PIL import Image

    pixels: list[float] = []

    class _Drawn:
        original = Image.new("RGB", (10, 10), "white")

    def to_image(page, resolution=None, **_kwargs):
        pixels.append(float(page.width) * float(page.height) * (resolution / 72) ** 2)
        return _Drawn()

    monkeypatch.setattr(pdfplumber.page.Page, "to_image", to_image)
    monkeypatch.setattr(ocr, "available", lambda: False)
    monkeypatch.setattr(ocr, "_read_image", lambda image: "")
    return pixels


def _draw(how: str, data: bytes) -> None:
    if how == "mapper":
        pagemap._render(data, pagemap.RESOLUTION)
    else:
        ocr.read_pdf(data)


@pytest.mark.parametrize("how", ["mapper", "ocr"])
def test_a_page_of_any_size_is_drawn_in_the_budget(drawn, how):
    assert len(one_page_pdf(3600, 3600)) < 500

    _draw(how, one_page_pdf(3600, 3600))

    assert drawn and max(drawn) <= ocr.MAX_PIXELS


@pytest.mark.parametrize("how, dpi", [("mapper", pagemap.RESOLUTION), ("ocr", ocr.RESOLUTION)])
def test_a_statement_sized_page_is_drawn_as_sharp_as_ever(drawn, how, dpi):
    """A4 and US Legal, the biggest page a statement comes on: the budget is
    for pages no statement has."""
    for width, height in ((595, 842), (612, 1008)):
        _draw(how, one_page_pdf(width, height))

    assert drawn == [595 * 842 * (dpi / 72) ** 2, 612 * 1008 * (dpi / 72) ** 2]
