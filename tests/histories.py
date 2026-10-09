"""Generated trade histories, for the tests that check the numbers against a
working-out of their own (test_*_properties.py).

The hand-worked tests use round numbers: no brokerage, AUD only, whole units.
A formula that adds brokerage where it should subtract it, or divides by a
rate it should multiply by, gives the same answer on those, and a mutation run
found dozens that did. These histories have brokerage, fractional units, US
holdings with and without a recorded rate, and sells of part and all of a
holding, on dates either side of financial-year ends.

A history is plain data, so a failing case prints as something a person can
read, and the expected figures are worked out from that data alone — never by
asking the app.
"""

from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from hypothesis import strategies as st

import factories as fac

# Seen from the suite's frozen today (conftest.FROZEN_TODAY).
FIRST, TODAY = dt.date(2021, 3, 1), dt.date(2026, 8, 2)
ZERO, ONE, CENTS = Decimal(0), Decimal(1), Decimal("0.01")


@dataclass(frozen=True)
class Trade:
    date: dt.date
    type: str
    quantity: Decimal
    unit_price: Decimal
    brokerage: Decimal
    fx_rate: Decimal | None


@dataclass(frozen=True)
class Dividend:
    date: dt.date
    cash: Decimal
    franking: Decimal | None
    fx_rate: Decimal | None


@dataclass(frozen=True)
class Holding:
    ticker: str
    currency: str
    trades: tuple[Trade, ...]          # in timeline order
    closes: tuple[tuple[dt.date, Decimal], ...]
    dividends: tuple[Dividend, ...]
    active: bool = True                # False: removed from the portfolio's list


@dataclass(frozen=True)
class History:
    holdings: tuple[Holding, ...]
    usd: tuple[tuple[dt.date, Decimal], ...]    # USDAUD


def _amount(low: str, high: str, places: int):
    return st.decimals(min_value=Decimal(low), max_value=Decimal(high), places=places,
                       allow_nan=False, allow_infinity=False)


dates = st.dates(FIRST, TODAY)


@st.composite
def holding(draw, ticker: str, currency: str) -> Holding:
    when = sorted(draw(st.lists(dates, min_size=1, max_size=9)))
    held = ZERO
    trades = []
    for day in when:
        kind = draw(st.sampled_from(["buy", "buy", "drp"] + (["sell"] * 3 if held else [])))
        if kind == "sell":
            quantity = draw(st.one_of(st.just(held), _amount("0.0001", str(held), 4)))
            held -= quantity
        else:
            quantity = draw(_amount("0.0001", "5000", 4))
            held += quantity
        price = draw(_amount("0.01", "500", 4))
        brokerage = ZERO if kind == "drp" else draw(st.one_of(st.just(ZERO),
                                                              _amount("0.01", "40", 2)))
        rate = ONE if currency == "AUD" else draw(st.one_of(st.none(),
                                                            _amount("0.5", "2", 6)))
        trades.append(Trade(day, kind, quantity, price, brokerage, rate))
    closes = draw(st.lists(st.tuples(dates, _amount("0.01", "500", 4)), max_size=6,
                           unique_by=lambda c: c[0]))
    dividends = draw(st.lists(st.builds(
        Dividend, dates, _amount("0.01", "900", 4),
        st.one_of(st.none(), _amount("0", "300", 4)),
        st.just(ONE) if currency == "AUD" else st.one_of(st.none(), _amount("0.5", "2", 6))),
        max_size=3))
    return Holding(ticker, currency, tuple(trades), tuple(sorted(closes)),
                   tuple(sorted(dividends, key=lambda d: d.date)),
                   active=draw(st.sampled_from([True, True, True, False])))


@st.composite
def histories(draw) -> History:
    currencies = draw(st.lists(st.sampled_from(["AUD", "USD"]), min_size=1, max_size=3))
    held = tuple(draw(holding(f"TK{i}", ccy)) for i, ccy in enumerate(currencies))
    usd = draw(st.lists(st.tuples(dates, _amount("0.5", "2", 6)), max_size=5,
                        unique_by=lambda r: r[0]))
    return History(held, tuple(sorted(usd)))


def load(session, history: History) -> dict:
    """Write a history into a bound session (flushed, not committed)."""
    rows = {}
    for h in history.holdings:
        inst = fac.make_instrument(session, h.ticker, currency=h.currency, asset_class="share",
                                   exchange="ASX" if h.currency == "AUD" else "NASDAQ",
                                   active=h.active)
        for t in h.trades:
            fac.add_trade(session, inst, t.date, t.type, t.quantity, t.unit_price,
                          brokerage=t.brokerage, fx_rate=t.fx_rate)
        fac.add_prices(session, inst, h.closes)
        for d in h.dividends:
            fac.add_dividend(session, inst, d.date, d.cash, fx_rate=d.fx_rate,
                             franking_credits=d.franking)
        rows[h.ticker] = inst
    fac.add_fx_series(session, "USDAUD", history.usd)
    return rows


# --------------------------------------------------------------------------- #
# The working-out, from the documented rules alone
# --------------------------------------------------------------------------- #

def stored_rate(history: History, currency: str, on: dt.date) -> Decimal | None:
    """decisions.md #5: the nearest stored rate on or before the date, else the
    earliest stored; none stored is no rate. AUD needs none."""
    if currency == "AUD":
        return ONE
    if not history.usd:
        return None
    days = [d for d, _ in history.usd]
    i = bisect.bisect_right(days, on)
    return history.usd[i - 1][1] if i else history.usd[0][1]


def booked_rate(history: History, currency: str, row) -> Decimal:
    """What the FY report books a trade or dividend at: its own rate, else the
    stored series, else 1 (fyreport._rate's documented fallback)."""
    if row.fx_rate is not None:
        return row.fx_rate
    return stored_rate(history, currency, row.date) or ONE


def close_on_or_before(h: Holding, on: dt.date) -> tuple[Decimal, dt.date] | None:
    usable = [(c, d) for d, c in h.closes if d <= on]
    return max(usable, key=lambda c: c[1]) if usable else None
