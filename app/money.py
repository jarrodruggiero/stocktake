"""Rendering an amount of money so it says which money it is, and reading one
back in: `parse` turns typed or imported text into a figure that fits its column.

`REPORTING` is a constant today and becomes `portfolio.reporting_currency`
later, so nothing outside this module hard-codes "AUD".

Where the symbol goes: headline numbers carry it, table columns carry it in the
HEADER, and a column that can hold more than one currency names the currency
per row (see `columns.py`). Why: decisions.md #80.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

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


def limit(column) -> Decimal:
    """The first magnitude a NUMERIC column cannot hold: 10 ** (precision - scale).

    Read from the column so it cannot drift from the schema. `Numeric(20, 8)`
    holds twelve digits before the point, so 10**12 and up does not fit.
    """
    kind = column.type
    return Decimal(10) ** (kind.precision - kind.scale)


class FigureError(ValueError):
    """A figure that is not a number, or does not fit where it is going.

    A `ValueError`, so a caller that already catches that keeps working; its own
    type so a form can show this message, which says what is wrong with the
    figure, instead of the generic one for a bad date.
    """


# How much of what was typed a message repeats. Forms carry the message in a
# redirect's query string, and a pasted page of text would make that URL too
# long for the proxy, which shows a bare error instead of the message.
_SHOWN = 20


def parse(text, column, name: str | None = None) -> Decimal:
    """Text a person typed or a file carried, as a number that fits `column`.

    A bare `Decimal(text)` accepts `NaN` (whose comparisons then raise),
    `Infinity` (which saves), and `1E+999999` (finite, but SQLite stores a float
    and reads it back as Infinity while Postgres refuses it with an overflow).
    This refuses all three with a message naming the field, `name`, and the
    value. Zero, sign and meaning stay with the caller: it only says whether the
    figure is a figure and fits.

    "Fits" is judged after rounding to the column's scale, because the database
    rounds on the way in: 999999999999.999999999 is under 10**12 as typed and is
    10**12 once Postgres has stored it in a `Numeric(20, 8)`.
    """
    raw = str(text).strip()
    label = f"{name}: " if name else ""
    shown = repr(raw if len(raw) <= _SHOWN else raw[:_SHOWN] + "…")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise FigureError(f"{label}{shown} is not a number") from None
    if not value.is_finite():
        raise FigureError(f"{label}{shown} is not a number")
    ceiling = limit(column)
    # Compared before rounding too: `quantize` itself raises on a value as
    # large as 1E+999999, so the size check has to come first.
    if abs(value) >= ceiling or abs(
            value.quantize(Decimal(1).scaleb(-column.type.scale),
                           rounding=ROUND_HALF_UP)) >= ceiling:
        raise FigureError(
            f"{label}{shown} is too large: it has to be under {ceiling:,}")
    return value
