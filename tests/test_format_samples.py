"""Every shipped template, run against its own redacted sample.

**This is the mechanism that makes contributed formats trustworthy.** Nobody
maintaining this project holds accounts at most brokers and registries, so a
format cannot be verified by the person merging it. The arrangement that does
work: whoever has the document contributes the template *plus* a redacted
sample of the extracted text *plus* what it should produce — and from then on
CI checks it forever, on hardware that has never seen the real statement.

The sample is **text, not a PDF**, on purpose:

  * text is trivially redactable — you can read every character you are
    publishing, which is not true of a PDF's metadata, embedded fonts and
    revision history;
  * a reviewer can see the whole thing in a diff;
  * no binaries in the repository;
  * the parser works on extracted text anyway, so nothing is lost.

The tests below are generated from whatever is in `app/formats/samples/`, so a
contributor adds two files and gets CI coverage without touching this module.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from app import docformats as fmt

SAMPLES = fmt.BUILTIN_DIR / "samples"
STATEMENTS = fmt.BUILTIN_DIR / "statements"


def _samples() -> list[Path]:
    return sorted(SAMPLES.glob("*.txt")) if SAMPLES.is_dir() else []


def _expected(sample: Path) -> dict:
    return YAML(typ="safe").load(
        sample.with_suffix("").with_suffix(".expected.yaml").read_text()
    )


def _ids(paths: list[Path]) -> list[str]:
    return [p.stem for p in paths]


# --------------------------------------------------------------------------- #
# Every template must have a sample, and every sample a template
# --------------------------------------------------------------------------- #

def test_every_shipped_statement_template_has_a_sample():
    """A template with no sample is a format nobody can verify — exactly the
    situation this whole design exists to escape. Ship one or don't ship the
    template."""
    templates = {p.stem for p in STATEMENTS.glob("*.yaml")}
    samples = {p.stem for p in _samples()}

    assert templates - samples == set(), (
        f"template(s) with no sample: {sorted(templates - samples)}"
    )


def test_every_sample_has_a_template_and_an_expectation():
    templates = {p.stem for p in STATEMENTS.glob("*.yaml")}
    for sample in _samples():
        assert sample.stem in templates, f"{sample.name} matches no template"
        expectation = sample.with_suffix("").with_suffix(".expected.yaml")
        assert expectation.exists(), f"{sample.name} has no .expected.yaml"


# --------------------------------------------------------------------------- #
# The samples must be safe to publish
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sample", _samples(), ids=_ids(_samples()))
def test_a_sample_says_it_is_redacted(sample: Path):
    """A contributor is publishing a financial document's layout to a public
    repository. The header is where they confirm, in writing, that they
    understood that — and it is what a reviewer looks for first."""
    head = sample.read_text()[:600].upper()

    assert "REDACTED" in head, (
        f"{sample.name} must carry a header stating the values are invented"
    )


# A TFN as it is actually written: nine digits, optionally grouped in threes.
# Anchored away from decimal points and longer runs, because a looser
# this stripped all whitespace and then read "0.50000000 0.00" as a nine-digit
# identifier — a check that cries wolf gets deleted, which is worse than none.
# The lookarounds also exclude a nine-digit run that is part of a longer
# grouped number — an ABN written "12 345 678 901" is public information and
# flagging it would just teach contributors to ignore this test.
TFN_SHAPED = re.compile(
    r"(?<![\d.])(?<![\d][ -])\d{3}[ -]?\d{3}[ -]?\d{3}(?![\d.])(?![ -]\d)"
)


@pytest.mark.parametrize("sample", _samples(), ids=_ids(_samples()))
def test_a_sample_carries_no_australian_tax_file_number(sample: Path):
    """A TFN published to a repository is the worst thing this could leak, and
    registries do print them on statements."""
    for found in TFN_SHAPED.findall(sample.read_text()):
        digits = re.sub(r"[ -]", "", found)
        # Obvious placeholders are fine; a real identifier is not all zeroes.
        assert set(digits) == {"0"}, (
            f"{sample.name} contains {found!r}, which is shaped like a TFN. "
            "Replace it with zeroes."
        )


# --------------------------------------------------------------------------- #
# The template reads its sample correctly
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sample", _samples(), ids=_ids(_samples()))
def test_the_template_claims_its_own_sample(sample: Path):
    """If `pick` chooses a different template for this document, the format's
    match rules are wrong — and the failure would be invisible in production,
    because a wrong template still returns *something*."""
    text = sample.read_text()

    chosen = fmt.pick(fmt.load_statement_templates(), text)

    assert chosen is not None and chosen.key == sample.stem


@pytest.mark.parametrize("sample", _samples(), ids=_ids(_samples()))
def test_the_template_extracts_what_the_sample_expects(sample: Path):
    """The contract. Whatever the contributor said this layout produces, it
    still produces — on every commit, forever, without the original document."""
    text = sample.read_text()
    expected = _expected(sample)
    template = fmt.load_statement_template(STATEMENTS / f"{sample.stem}.yaml")

    got = template.extract(text)

    for key, want in expected.items():
        if key == "rows":
            _assert_rows(got.get("rows"), want, sample)
            continue
        assert _comparable(got.get(key)) == _comparable(want), (
            f"{sample.name}: {key} — expected {want!r}, got {got.get(key)!r}"
        )


def _assert_rows(got, want, sample):
    assert got is not None, f"{sample.name}: expected rows, got none"
    assert len(got) == len(want), (
        f"{sample.name}: expected {len(want)} rows, got {len(got)}"
    )
    for index, (actual, expected_row) in enumerate(zip(got, want)):
        for key, value in expected_row.items():
            assert _comparable(actual.get(key)) == _comparable(value), (
                f"{sample.name}: row {index} {key} — "
                f"expected {value!r}, got {actual.get(key)!r}"
            )


def _comparable(value):
    """Compare on value, not on type.

    The expectation file writes money as a quoted string so trailing zeros
    survive YAML, while the template returns a Decimal — "1042.75" and
    Decimal("1042.75") are the same answer and the test should say so.
    """
    if isinstance(value, dt.date):
        return value
    return str(value) if value is not None else None
