"""Every shipped template, run against its own redacted sample.

**This is the mechanism that makes contributed formats trustworthy**
(decisions.md #64): whoever has the document contributes the template plus a
redacted sample plus what it should produce, and CI checks it forever on
hardware that has never seen the real statement.

The sample is text, not a PDF, on purpose — every character being published can
be read, a reviewer sees the whole thing in a diff, no binaries land in the
repository, and the parser works on extracted text anyway.

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


# --------------------------------------------------------------------------- #
# The same arrangement, for broker CSV formats
# --------------------------------------------------------------------------- #
# A broker format is as unverifiable by the maintainer as a statement template:
# nobody here holds an account at most brokers (decisions.md #64).

BROKERS = fmt.BUILTIN_DIR / "brokers"


def _broker_samples() -> list[Path]:
    return sorted(SAMPLES.glob("*.csv")) if SAMPLES.is_dir() else []


def test_every_shipped_broker_format_has_a_sample():
    formats = {p.stem for p in BROKERS.glob("*.yaml")}
    samples = {p.stem for p in _broker_samples()}
    assert formats - samples == set(), (
        f"broker format(s) with no sample: {sorted(formats - samples)} — a format "
        f"nobody can verify is the situation this design exists to escape"
    )


@pytest.mark.parametrize("sample", _broker_samples(), ids=_ids(_broker_samples()))
def test_the_shipped_broker_format_reads_its_own_sample(sample: Path):
    """Run the real format over the sample and compare every field.

    Not "it parsed" — the actual numbers. A mapping that reads Consideration as
    the unit price parses perfectly and overstates every holding by the number
    of units, so only comparing values catches it.
    """
    from app import brokercsv
    from app.settings import BrokerFormat

    expected = _expected(sample)
    broker = sample.stem
    shipped = fmt.load_broker_formats()[broker]
    result = brokercsv.parse_csv(sample.read_text(), broker,
                                 BrokerFormat.model_validate(shipped))

    assert not result.errors, f"{broker} could not read its own sample: {result.errors}"

    got = [
        {"date": c.date.isoformat(), "ticker": c.ticker, "type": c.type,
         "quantity": str(c.quantity), "unit_price": str(c.unit_price),
         "brokerage": str(c.brokerage)}
        for c in result.candidates
    ]
    assert got == expected["trades"]

    # Rows it must NOT import. A format that silently starts importing DRP
    # allotments at a zero cost base is the failure this pins.
    assert len(result.skipped) == len(expected.get("skipped", [])), (
        f"expected {len(expected.get('skipped', []))} skipped row(s), "
        f"got {len(result.skipped)}: {result.skipped}"
    )

    # A midnight time is padding and must not be stored — it would sort every
    # imported trade ahead of every hand-entered one on the same day.
    times = [c.time.isoformat() for c in result.candidates if c.time]
    assert times == expected.get("times", [])
