"""Rendering an amount of money so it says which money it is.

The app had no currency formatting: templates wrote a literal `$` in front of
`{{ x | money }}`. That produced `$-4.00` — the sign belongs to the number and
the symbol in front of the whole thing — and it put the symbol *outside* the
privacy blur, since `money` wraps its output in `<span class="m">` and a `$`
written outside stayed sharp next to a smudge. Both are the same mistake:
treating the symbol as decoration rather than part of the value.

`REPORTING` is a constant today and becomes `portfolio.reporting_currency` in
T28b. Nothing outside this module should hard-code `"AUD"`, so that the later
change is a change *here*.

Where the symbol goes:

  * **Headline numbers** (tiles, hero figures) always carry it — there is no
    column header to carry it for them.
  * **Table columns carry their currency in the HEADER.** A `$` repeated down
    two hundred right-aligned cells is noise and fights the alignment that
    makes a numeric column scannable.
  * **A column that can hold more than one currency** names the currency per
    row instead — see `columns.py`. An AUD-only portfolio never sees this.
"""

from __future__ import annotations

from decimal import Decimal

# The currency every total is computed in. A constant until T28b makes it a
# property of the portfolio — see the module docstring.
REPORTING = "AUD"

# Only the symbol. Deliberately not a full locale table: the app formats
# thousands and decimals one way (1,234.56) and inventing per-locale grouping
# here would be a half-implementation of something T28b has to do properly.
SYMBOLS = {
    "AUD": "$", "USD": "$", "NZD": "$", "CAD": "$", "SGD": "$", "HKD": "$",
    "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥", "CHF": "CHF", "INR": "₹",
}

# Currencies whose symbol is shared with another. When two amounts in different
# currencies sit together, these need their code as well or the pair is a lie:
# "$1,000.20 ($1,425.40)" says nothing about which is which.
_SHARED = {code for code, symbol in SYMBOLS.items()
           if sum(1 for s in SYMBOLS.values() if s == symbol) > 1}


def symbol(currency: str | None = None) -> str:
    """The symbol for a currency, falling back to the code itself.

    An unknown code renders as the code — `KRW 1,000.00` — which is honest.
    Guessing a symbol for a currency nobody listed would be worse than plain.
    """
    code = (currency or REPORTING).upper()
    return SYMBOLS.get(code, code)


def is_shared(currency: str | None = None) -> bool:
    """Whether this currency's symbol is used by another currency too."""
    return (currency or REPORTING).upper() in _SHARED


def plain(value) -> str:
    """The digits alone: grouped thousands, two decimals, an em dash for None.

    Used inside HTML attributes (placeholders, titles) where markup cannot go.
    """
    return f"{value:,.2f}" if value is not None else "—"


def text(value, currency: str | None = None) -> str:
    """An amount with its symbol, as plain text.

    The sign comes FIRST — `-$4.00`, not `$-4.00`. That is the whole reason a
    literal `$` in a template could not be right.
    """
    if value is None:
        return "—"
    number = Decimal(value)
    sign = "-" if number < 0 else ""
    return f"{sign}{symbol(currency)}{abs(number):,.2f}"
