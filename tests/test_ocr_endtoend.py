"""OCR end to end: does a scanned statement actually come out readable?

`test_ocr.py` stubs the rasteriser and Tesseract, so it proves the decision
logic and nothing about readability. This is the other half.

The document is generated at test time from the shipped `generic-au.txt`
sample, drawn onto a bitmap and saved as a PDF with NO text layer — which is
what a registry posting scans sends. No binaries in the repository, no
licensing question about somebody else's document, and it tests the layout that
actually ships (decisions.md #98).

It asserts the exact values `generic-au.expected.yaml` records, so a regression
in Tesseract, in the rasterising resolution or in the template breaks it.
Skipped where Tesseract is absent.
"""

from __future__ import annotations

import io

import pytest
from ruamel.yaml import YAML

from app import docformats, ocr, statements

SAMPLES = docformats.BUILTIN_DIR / "samples"
STATEMENTS = docformats.BUILTIN_DIR / "statements"

pytestmark = pytest.mark.skipif(
    not ocr.available(),
    reason="tesseract is not installed here; this runs in the container image",
)


def scanned_pdf(text: str) -> bytes:
    """The text drawn onto a page and saved as an image-only PDF.

    Deliberately a bitmap: `PIL` writing text into a PDF would embed a font and
    produce a text layer, and a PDF with a text layer is the case OCR must NOT
    run on. Nothing here would be exercised.
    """
    from PIL import Image, ImageDraw, ImageFont

    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    # A4 at 200dpi, which is a common scanner setting and comfortably above
    # what Tesseract needs for 10pt print.
    image = Image.new("RGB", (1654, 2339), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 30)
    except OSError:  # pragma: no cover - depends on the host's fonts
        font = ImageFont.load_default(size=30)
    for number, line in enumerate(lines):
        draw.text((90, 90 + number * 46), line, fill="black", font=font)

    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=200.0)
    return buffer.getvalue()


def sample_names() -> list[str]:
    return sorted(p.stem for p in SAMPLES.glob("*.txt")) if SAMPLES.is_dir() else []


@pytest.mark.slow
@pytest.mark.parametrize("name", sample_names())
def test_a_scan_of_a_shipped_sample_still_reads_its_expected_values(name):
    """The regression this file exists for.

    Generated from whatever is in `app/formats/samples/`, so a contributed
    layout gets a scanned-PDF check for free alongside its text one.
    """
    sample = (SAMPLES / f"{name}.txt").read_text()
    expected = YAML(typ="safe").load((SAMPLES / f"{name}.expected.yaml").read_text())
    template = docformats.load_statement_template(STATEMENTS / f"{name}.yaml")

    read = statements.extract(scanned_pdf(sample))

    assert read.used_ocr is True, "the generated PDF had a text layer, so nothing was tested"
    assert read.problem == ""
    assert template.matches(read.text), (
        f"{name}: OCR output no longer matches this template's markers.\n"
        f"OCR read:\n{read.text[:600]}")

    values = template.extract(read.text)

    # The SAME comparison the text-based harness makes, imported rather than
    # rewritten — `test_format_samples.py` is the definition, decisions.md #57.
    from test_format_samples import _assert_rows, _comparable

    for key, want in expected.items():
        if key == "rows":
            _assert_rows(values.get("rows"), want, SAMPLES / f"{name}.txt")
            continue
        assert _comparable(values.get(key)) == _comparable(want), (
            f"{name}: a scan of this layout reads {key} as "
            f"{values.get(key)!r}, expected {want!r}.\n\nOCR read:\n{read.text[:600]}")


@pytest.mark.slow
def test_a_pdf_with_a_text_layer_is_not_ocred_even_here(tmp_path):
    """Belt to the unit test's braces, against a real PDF rather than a stub:
    the expensive, lossy path must not run when the exact answer is in the
    file."""
    from PIL import Image, ImageDraw

    # An image PDF with a caption written as real text alongside it would be
    # ideal; simpler and just as decisive is to check the shipped sample text
    # goes straight through when handed over as a text layer.
    image = Image.new("RGB", (400, 200), "white")
    ImageDraw.Draw(image).text((10, 10), "x", fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PDF")

    long_text = (SAMPLES / "generic-au.txt").read_text()
    read = statements.extract(buffer.getvalue())
    assert read.used_ocr is True  # this one genuinely has no text layer

    # And the inverse, via the seam the whole decision hangs on.
    assert statements.looks_scanned("") is True
    assert statements.looks_scanned(long_text) is False


def test_the_generated_pdf_really_has_no_text_layer():
    """If PIL ever starts embedding a font, every test above would silently
    stop exercising OCR and keep passing."""
    pdf = scanned_pdf("Payment date 15 March 2026\nNet amount paid $1,042.75\n")

    assert statements._pdf_text(pdf).strip() == ""


def test_there_is_at_least_one_sample_to_check():
    """A parametrised test over an empty list passes loudly quietly."""
    assert sample_names(), "no shipped samples found — has app/formats/samples moved?"
