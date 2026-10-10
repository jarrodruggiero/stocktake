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


def fy_bounds(fy: int) -> tuple[dt.date, dt.date]:
    return dt.date(fy - 1, 7, 1), dt.date(fy, 6, 30)


def fy_of(day: dt.date) -> int:
    return day.year + (1 if day.month >= 7 else 0)


def over_a_year(acquired: dt.date, sold: dt.date) -> bool:
    """Held more than twelve months: sold after the anniversary, which for a
    29 February purchase is 28 February."""
    try:
        anniversary = acquired.replace(year=acquired.year + 1)
    except ValueError:
        anniversary = dt.date(acquired.year + 1, 2, 28)
    return sold > anniversary


def worked_disposals(history: History) -> list[dict]:
    """FIFO, with each parcel's cost and each sale's proceeds billed by running
    total so the parts add to the whole (decisions.md #7, #93)."""
    out = []
    for h in history.holdings:
        parcels = []        # [acquired, remaining, quantity, cost, billed]
        for t in h.trades:
            rate = booked_rate(history, h.currency, t)
            if t.type != "sell":
                cost = (t.quantity * t.unit_price + t.brokerage) * rate
                parcels.append([t.date, t.quantity, t.quantity, cost, ZERO])
                continue
            proceeds = ((t.quantity * t.unit_price - t.brokerage) * rate).quantize(CENTS)
            left, uses = t.quantity, []
            while left > 0:
                p = parcels[0]
                take = min(p[1], left)
                p[1] -= take
                owed = (p[3] * (p[2] - p[1]) / p[2]).quantize(CENTS)
                uses.append({"acquired": p[0], "quantity": take, "cost_base": owed - p[4],
                             "discountable": over_a_year(p[0], t.date)})
                p[4] = owed
                left -= take
                if p[1] == 0:
                    parcels.pop(0)
            sold = billed = ZERO
            for use in uses:
                sold += use["quantity"]
                owed = (proceeds * sold / t.quantity).quantize(CENTS)
                use["proceeds"], billed = owed - billed, owed
            out.append({"ticker": h.ticker, "date": t.date, "quantity": t.quantity,
                        "proceeds": proceeds, "parcels": uses})
    return out


def fx_of(history: History, currency: str, row) -> Decimal | None:
    """queries.FxBook.of: the row's own rate, else the stored series, else none."""
    return row.fx_rate if row.fx_rate is not None else stored_rate(history, currency, row.date)


def _in_aud(amount, currency, rate):
    if currency == "AUD":
        return amount
    return None if rate is None else amount * rate


def _sum_aud(parts):
    parts = list(parts)
    return None if any(p is None for p in parts) else sum(parts, ZERO)


def worked_holdings(history: History) -> dict:
    """Each holding's figures by ticker, as queries.build_holding should give them."""
    latest_rate = history.usd[-1][1] if history.usd else None
    out = {}
    for h in history.holdings:
        rate_now = Decimal(1) if h.currency == "AUD" else latest_rate
        buys = [t for t in h.trades if t.type == "buy"]
        acquired = [t for t in h.trades if t.type != "sell"]
        sells = [t for t in h.trades if t.type == "sell"]
        units = sum((t.quantity for t in acquired), ZERO) - sum((t.quantity for t in sells), ZERO)
        cost = sum((t.quantity * t.unit_price + t.brokerage for t in buys), ZERO)
        acquired_units = sum((t.quantity for t in acquired), ZERO)
        price = h.closes[-1][1] if h.closes else None
        value = units * price if price is not None and units else None
        value_aud = None if value is None or rate_now is None else value * rate_now
        cost_aud = _sum_aud(_in_aud(t.quantity * t.unit_price + t.brokerage, h.currency,
                                    t.fx_rate) for t in buys)
        paid_aud = _sum_aud(_in_aud(t.quantity * t.unit_price, h.currency, t.fx_rate)
                            for t in acquired)
        closes = [c for _d, c in reversed(h.closes)][:2]
        day_pct = day_change = None
        if units and len(closes) == 2 and closes[1]:
            day_pct = (closes[0] - closes[1]) / closes[1]
            if rate_now is not None:
                day_change = (closes[0] - closes[1]) * units * rate_now
        dividends = sum((d.cash for d in h.dividends), ZERO)
        out[h.ticker] = {
            "units": units, "cost": cost,
            "avg_price": (sum((t.quantity * t.unit_price for t in acquired), ZERO)
                          / acquired_units) if acquired_units else None,
            "price": price, "value": value,
            "gain": None if value is None else value - cost,
            "gain_pct": None if value is None or not cost else (value - cost) / cost,
            "cost_aud": cost_aud, "value_aud": value_aud,
            "gain_aud": (value_aud - cost_aud
                         if value_aud is not None and cost_aud is not None else None),
            "dividends_cash": dividends,
            "dividends_aud": _sum_aud(_in_aud(d.cash, h.currency, d.fx_rate)
                                      for d in h.dividends),
            "avg_price_aud": (paid_aud / acquired_units
                              if paid_aud is not None and acquired_units else None),
            "price_aud": None if price is None or rate_now is None else price * rate_now,
            "day_pct": day_pct, "day_change_aud": day_change,
            # A sold-out holding is a closed position: what came back for it.
            "proceeds": (sum((t.quantity * t.unit_price - t.brokerage for t in sells), ZERO)
                         if sells else None),
        }
    return out
