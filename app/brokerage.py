"""What a portfolio's broker charges for a trade.

One shape covers the fees brokers actually quote: a flat fee, plus a
percentage of the trade's value, or the minimum if that is more. Flat only,
flat plus a percentage, a percentage only, "$14.95 or 0.11%, whichever is
greater", and free (zeros) are all this.

Only ever a suggestion for the trade form: the brokerage recorded is whatever
the form sends. `tradeform.js` does the same sum as units and price are typed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from . import money
from .models import Portfolio

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Fee:
    flat: Decimal | None = None
    percent: Decimal | None = None
    minimum: Decimal | None = None

    @property
    def is_set(self) -> bool:
        """Any part given. All blank means "not set", which is not free."""
        return any(v is not None for v in (self.flat, self.percent, self.minimum))

    def on(self, value: Decimal) -> Decimal:
        charged = (self.flat or 0) + (self.percent or 0) * value / 100
        return max(self.minimum or 0, charged).quantize(CENT, rounding=ROUND_HALF_UP)

    def as_json(self) -> dict | None:
        if not self.is_set:
            return None
        return {name: (f"{v.normalize():f}" if v is not None else None)
                for name, v in (("flat", self.flat), ("percent", self.percent),
                                ("minimum", self.minimum))}


def local(portfolio: Portfolio | None) -> Fee:
    """The fee for a trade in the reporting currency."""
    if portfolio is None:
        return Fee()
    return Fee(portfolio.brokerage_flat, portfolio.brokerage_percent,
               portfolio.brokerage_minimum)


def foreign(portfolio: Portfolio | None) -> Fee:
    """The fee for a trade in any other currency."""
    if portfolio is None:
        return Fee()
    return Fee(portfolio.foreign_brokerage_flat, portfolio.foreign_brokerage_percent,
               portfolio.foreign_brokerage_minimum)


def is_foreign(currency: str | None) -> bool:
    return bool(currency) and currency != money.REPORTING
