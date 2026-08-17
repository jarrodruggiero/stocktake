"""Money renders as money — with its currency, and with the sign in front.

The bug this file exists for: templates wrote a literal `$` in front of
`{{ x | money }}`, so every negative figure came out as **`$-4.00`** and the
symbol sat outside the `<span class="m">` that the idle overlay blurs. Both are
the same mistake — treating the symbol as decoration rather than part of the
value — and both are fixed by one place turning an amount into text.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from app import columns, money

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


# --------------------------------------------------------------------------- #
# The formatting itself
# --------------------------------------------------------------------------- #

def test_the_sign_comes_before_the_symbol():
    """`-$4.00`, never `$-4.00`. The whole reason a literal prefix was wrong."""
    assert money.text(Decimal("-4")) == "-$4.00"


def test_a_positive_amount_has_no_sign():
    assert money.text(Decimal("1234.5")) == "$1,234.50"


def test_thousands_are_grouped_and_cents_always_shown():
    assert money.text(Decimal("1234567")) == "$1,234,567.00"


def test_nothing_renders_as_a_dash_not_a_zero():
    """A missing price and a price of zero are different facts."""
    assert money.text(None) == "—"
    assert money.plain(None) == "—"
    assert money.text(Decimal("0")) == "$0.00"


@pytest.mark.parametrize("code,expected", [
    ("AUD", "$"), ("USD", "$"), ("EUR", "€"), ("GBP", "£"), ("JPY", "¥"),
])
def test_known_currencies_use_their_symbol(code, expected):
    assert money.symbol(code) == expected


def test_an_unknown_currency_renders_as_its_code():
    """Honest beats invented: guessing a symbol for a currency nobody listed
    would be worse than showing the code."""
    assert money.symbol("KRW") == "KRW"
    assert money.text(Decimal("10"), "KRW") == "KRW10.00"


def test_currencies_sharing_a_symbol_are_flagged():
    """T28b shows a second currency beside the first, and `$1,000 ($1,425)`
    says nothing about which is which. These are the ones needing their code."""
    assert money.is_shared("AUD") is True
    assert money.is_shared("USD") is True
    assert money.is_shared("EUR") is False


def test_no_currency_means_the_reporting_one():
    assert money.symbol() == money.symbol(money.REPORTING)


# --------------------------------------------------------------------------- #
# Where the currency is named
# --------------------------------------------------------------------------- #

def test_a_reporting_column_names_the_reporting_currency():
    assert columns.BY_KEY["value_reporting"].heading() == f"Value ({money.REPORTING})"


def test_a_native_column_names_the_portfolio_s_currency_when_there_is_one():
    assert columns.BY_KEY["price"].heading("USD") == "Price (USD)"


def test_a_native_column_says_native_when_the_portfolio_holds_several():
    """It genuinely cannot name one, and saying `(AUD)` would be a lie on
    half the rows."""
    assert columns.BY_KEY["price"].heading(None) == "Price (native)"


def test_a_non_money_column_gets_no_currency():
    assert columns.BY_KEY["units"].heading("AUD") == "Units"
    assert columns.BY_KEY["gain_pct"].heading("AUD") == "Gain %"


def test_the_two_cost_columns_are_told_apart_only_by_their_currency():
    """`cost` and `cost_reporting` share the label "Cost". If `heading` ever stopped
    adding the currency, the chooser would show two identical checkboxes and
    picking one would be a coin toss."""
    native = columns.BY_KEY["cost"].heading("USD")
    reporting = columns.BY_KEY["cost_reporting"].heading("USD")

    assert native != reporting, "the two Cost columns are indistinguishable"
    assert "USD" in native
    assert money.REPORTING in reporting


def test_a_single_currency_table_does_not_repeat_the_currency_per_column():
    """Seven headers each saying "(AUD)" is the same noise as a `$` in every
    cell, moved up one row. The table names it once instead."""
    assert columns.BY_KEY["value_reporting"].heading("AUD", mark=False) == "Value"
    assert columns.BY_KEY["price"].heading("AUD", mark=False) == "Price"


def test_marking_is_off_only_when_everything_really_is_one_currency():
    assert columns.one_currency_everywhere(money.REPORTING) is True
    # A portfolio of US holdings reported in AUD: native and reporting columns
    # show DIFFERENT currencies, so both must be named.
    assert columns.one_currency_everywhere("USD") is False
    # Mixed holdings: there is no single native currency at all.
    assert columns.one_currency_everywhere(None) is False


def test_every_money_column_declares_which_currency_it_is_in():
    """A money column with no `currency` renders a bare number with nothing
    saying what it is — which is the defect this whole task is about."""
    missing = [c.key for c in columns.COLUMNS
               if c.render in ("money", "gain") and c.currency is None]
    assert not missing, (
        f"money columns with no declared currency: {missing}. Set "
        "currency='native' or 'reporting' so the header can name it.")


# --------------------------------------------------------------------------- #
# The habit that caused it
# --------------------------------------------------------------------------- #

def test_no_template_writes_a_currency_symbol_next_to_the_money_filter():
    """The defect, pinned.

    `${{ x | money }}` put the symbol outside the blur span and in front of the
    minus sign. Use `| cash` when a figure needs its symbol — a headline number
    with no column header to carry it — and `| money` in a table cell whose
    header names the currency.
    """
    offenders = []
    for path in sorted(TEMPLATES.glob("*.html")):
        for found in re.finditer(r"[$€£¥]\s*\{\{[^}]*\|\s*(money|cash)", path.read_text()):
            offenders.append(f"{path.name}: {found.group(0)!r}")

    assert not offenders, (
        "a currency symbol is written next to a money filter — the filter puts "
        "it there itself, in the right place relative to the minus sign and "
        "inside the privacy blur:\n  " + "\n  ".join(offenders))
