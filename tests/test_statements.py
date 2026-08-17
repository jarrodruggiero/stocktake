"""Dividend-statement parsing.

No real PDFs: `_pdf_text` is stubbed and the text-level parsing is what gets
tested. That is also where the meaning lives — the risk in this module is not
"did a regex match" but "did we take the right number off the page".

Extraction is best-effort by design. The preview form shows every parsed field
for correction before anything is committed, so a miss must degrade to an empty
form rather than an exception or, worse, a confident wrong figure.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

import factories as fac
from app import statements

# A Vanguard/Computershare combined DRP advice, in the layout described in
# What a registry statement carries: ticker, DRP price, units held, cash per
# security, tax withheld,
# REINVESTMENT AMOUNT, balance brought forward, units allotted, balance carried
# forward.
COMBINED_ADVICE = """
Distribution and Reinvestment Advice
Payment Date: 15 March 2026

Fund Price Held PerSec Tax Amount Brought Allotted Carried
ALPHA 100.00 200 0.50000000 0.00 103.52 28.54 1 23.26
BETAX 50.00 100 0.20000000 0.00 20.00 5.00 0 25.00
"""


@pytest.fixture
def stub_text(monkeypatch):
    def _install(text: str):
        monkeypatch.setattr(statements, "_pdf_text", lambda data: text)
    return _install


# --------------------------------------------------------------------------- #
# The combined DRP advice
# --------------------------------------------------------------------------- #

def test_each_fund_on_a_combined_advice_becomes_a_row(pf, stub_text):
    fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    fac.make_instrument(pf, "BETAX", name="Betax Holdings")
    pf.commit()
    stub_text(COMBINED_ADVICE)

    parsed = statements.parse_statement(b"", pf)

    assert [r.ticker for r in parsed.rows] == ["ALPHA", "BETAX"]
    assert parsed.payment_date == dt.date(2026, 3, 15)


def test_the_distribution_is_the_reinvestment_amount_not_units_times_price(pf, stub_text):
    """The figure that matters is the cash the fund distributed, which is NOT
    units allotted x DRP price — residual balances are carried forward between
    quarters, so the two differ. Learned from a real statement; getting it
    wrong would understate income and the cost base together."""
    fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    pf.commit()
    stub_text(COMBINED_ADVICE)

    row = statements.parse_statement(b"", pf).rows[0]

    assert row.cash_amount == "103.52"          # the Amount column
    assert row.drp_price == "100.00"
    assert row.drp_units == "1"
    # 1 unit x 100.00 = 100.00, which is deliberately NOT what we recorded.
    assert Decimal(row.cash_amount) != Decimal(row.drp_units) * Decimal(row.drp_price)


def test_a_fund_allotted_no_units_still_produces_a_row(pf, stub_text):
    """A quarter can allot zero units with the cash carried forward. Dropping
    the row would lose a distribution that really was paid."""
    fac.make_instrument(pf, "BETAX", name="Betax Holdings")
    pf.commit()
    stub_text(COMBINED_ADVICE)

    betax = [r for r in statements.parse_statement(b"", pf).rows if r.ticker == "BETAX"][0]

    assert betax.drp_units == "0"
    assert betax.cash_amount == "20.00"


def test_the_carry_forward_balances_are_kept_on_the_row(pf, stub_text):
    fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    pf.commit()
    stub_text(COMBINED_ADVICE)

    row = statements.parse_statement(b"", pf).rows[0]

    assert "28.54" in row.carry_note and "23.26" in row.carry_note


def test_a_fund_we_do_not_track_is_flagged(pf, stub_text):
    fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    pf.commit()
    stub_text(COMBINED_ADVICE)

    betax = [r for r in statements.parse_statement(b"", pf).rows if r.ticker == "BETAX"][0]

    assert betax.status == "unknown-instrument"


def test_a_distribution_already_recorded_is_flagged_as_a_duplicate(pf, stub_text):
    alpha = fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    fac.add_dividend(pf, alpha, "2026-03-15", "103.52")
    pf.commit()
    stub_text(COMBINED_ADVICE)

    row = statements.parse_statement(b"", pf).rows[0]

    assert row.status == "duplicate"


def test_the_units_held_figure_is_checked_against_the_ledger(pf, stub_text):
    """The statement says how many units the registry thinks you hold; showing
    ours beside it turns a silent divergence into an obvious one."""
    alpha = fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    fac.add_trade(pf, alpha, "2025-01-06", "buy", 150, "10.00")
    pf.commit()
    stub_text(COMBINED_ADVICE)

    row = statements.parse_statement(b"", pf).rows[0]

    assert row.units_held == "200"    # per the statement
    assert row.db_units == "150"      # per our ledger — a discrepancy to notice


# --------------------------------------------------------------------------- #
# The single-statement path
# --------------------------------------------------------------------------- #

SINGLE = """
Acme Industries Limited
Dividend Statement
Payment Date: 15 March 2026
Net Amount: $1,234.56
Franked Amount: $1,000.00
Franking Credits: $428.57
Units Allotted: 42
Allotment Price: $29.39
"""


def test_a_single_statement_yields_its_fields(pf, stub_text):
    fac.make_instrument(pf, "ACME", name="Acme Industries Limited")
    pf.commit()
    stub_text(SINGLE)

    parsed = statements.parse_statement(b"", pf)

    assert parsed.rows is None            # not a combined advice
    assert parsed.ticker == "ACME"
    assert parsed.payment_date == dt.date(2026, 3, 15)
    assert parsed.net_amount == "1234.56"   # separators stripped
    assert parsed.franked_amount == "1000.00"
    assert parsed.franking_credits == "428.57"
    assert parsed.drp_units == "42"
    assert parsed.drp_price == "29.39"


@pytest.mark.parametrize(
    "line,expected",
    [
        ("Payment Date: 15 March 2026", dt.date(2026, 3, 15)),
        ("Payment date: 15 Mar 2026", dt.date(2026, 3, 15)),
        ("Date Paid: 15/03/2026", dt.date(2026, 3, 15)),
        ("Paid on: 15-03-2026", dt.date(2026, 3, 15)),
    ],
)
def test_the_common_payment_date_wordings_are_understood(pf, stub_text, line, expected):
    fac.make_instrument(pf, "ACME", name="Acme Industries Limited")
    pf.commit()
    stub_text(f"Acme Industries Limited\n{line}\nNet Amount: $10.00\n")

    assert statements.parse_statement(b"", pf).payment_date == expected


def test_an_instrument_is_matched_by_ticker_before_name(pf, stub_text):
    fac.make_instrument(pf, "ACME", name="Acme Industries Limited")
    fac.make_instrument(pf, "NOVA", name="Nova Group")
    pf.commit()
    stub_text("Statement for ACME\nPayment Date: 15 March 2026\nNet Amount: $10.00\n")

    assert statements.parse_statement(b"", pf).ticker == "ACME"


def test_an_instrument_is_matched_by_name_when_the_ticker_is_absent(pf, stub_text):
    fac.make_instrument(pf, "NOVA", name="Nova Group")
    pf.commit()
    stub_text("Nova Group distribution advice\nPayment Date: 15 March 2026\n"
              "Net Amount: $10.00\n")

    assert statements.parse_statement(b"", pf).ticker == "NOVA"


def test_an_unrecognised_statement_degrades_gracefully(pf, stub_text):
    """A layout we have never seen costs a minute of typing, not an error page
    — the preview form is designed to be filled in by hand."""
    stub_text("A letter about something else entirely.\nNothing useful here.\n")

    parsed = statements.parse_statement(b"", pf)

    assert parsed.ticker is None
    assert parsed.payment_date is None
    assert parsed.net_amount is None
    assert parsed.rows is None
    # …but the operator still gets to see what we read.
    assert "A letter about something else" in parsed.text_sample


def test_the_text_sample_is_capped(pf, stub_text):
    stub_text("\n".join(f"line {i}" for i in range(200)))

    parsed = statements.parse_statement(b"", pf)

    assert len(parsed.text_sample.splitlines()) == 25


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def test_duplicate_detection_matches_instrument_date_and_amount(pf):
    alpha = fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    fac.add_dividend(pf, alpha, "2026-03-15", "103.52")
    pf.commit()

    assert statements.duplicate_of(pf, alpha, fac.d("2026-03-15"),
                                   Decimal("103.52")) is not None
    # A different amount on the same day is a different distribution.
    assert statements.duplicate_of(pf, alpha, fac.d("2026-03-15"),
                                   Decimal("103.53")) is None
    assert statements.duplicate_of(pf, alpha, fac.d("2026-06-15"),
                                   Decimal("103.52")) is None


@pytest.mark.parametrize("junk", ["", None, "sometime last year", "31 Febtember 2026"])
def test_an_unparseable_date_returns_none(junk):
    """Date parsing moved to docformats with the rest of the format handling
    (2026-08-03). The behaviour is unchanged: junk is None, not an exception —
    a half-read statement is still worth previewing."""
    from app import docformats

    assert docformats._parse_date(junk) is None
