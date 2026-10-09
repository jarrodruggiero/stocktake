"""Holdings, closed positions and the dashboard totals, against a working-out
of their own over generated histories.

A mutation run found most of these figures unguarded by anything but round
numbers: a holding's cost could subtract its brokerage, the AUD gain could be
`value + cost`, and a closed position's proceeds and profit could take almost
any sign, with every test green.

The rules are the documented ones: cost is buys with their brokerage; the
average price is over buys and DRP allotments without it (decisions.md #50);
an AUD figure uses each row's own rate and is blank when a foreign row has
none (#5, #6); value uses the latest close and the latest rate.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given

import factories as fac
from app import queries, tenancy
from histories import ZERO, History, histories, load

EXACT = Decimal("0.00000001")


@pytest.fixture
def portfolio(session_factory):
    """An owner and a portfolio, committed: each case rolls back only its own rows."""
    with session_factory() as s:
        user = fac.make_user(s, "owner@example.test")
        p = fac.make_portfolio(s, "Generated", owner=user)
        s.commit()
        return p.id, user.id


def _q(v):
    return None if v is None else Decimal(v).quantize(EXACT)


def _in_aud(amount, currency, rate):
    if currency == "AUD":
        return amount
    return None if rate is None else amount * rate


def _sum_aud(parts):
    parts = list(parts)
    return None if any(p is None for p in parts) else sum(parts, ZERO)


def _expected(history: History) -> dict:
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


FIELDS = ("units", "cost", "avg_price", "price", "value", "gain", "gain_pct", "cost_aud",
          "value_aud", "gain_aud", "dividends_cash", "dividends_aud", "avg_price_aud",
          "price_aud", "day_pct", "day_change_aud")


@given(history=histories())
def test_every_holding_agrees_with_the_working_out(session_factory, portfolio, history):
    s = session_factory()
    tenancy.bind(s, *portfolio)
    try:
        load(s, history)
        want = _expected(history)
        holdings = queries.all_holdings(s)
        assert sorted(h.instrument.ticker for h in holdings) == sorted(want)
        for h in holdings:
            w = want[h.instrument.ticker]
            got = {f: _q(getattr(h, f)) for f in FIELDS}
            assert got == {f: _q(w[f]) for f in FIELDS}, h.instrument.ticker

        open_, closed = queries.split_positions(holdings)
        active = {h.ticker for h in history.holdings if h.active}
        assert {h.instrument.ticker for h in open_} == {t for t, w in want.items()
                                                      if w["units"] > 0 and t in active}
        assert {c.instrument.ticker for c in closed} == set(want) - {
            h.instrument.ticker for h in open_}
        for c in closed:
            w = want[c.instrument.ticker]
            assert (c.outlay, _q(c.proceeds)) == (w["cost"], _q(w["proceeds"]))
            assert _q(c.profit) == (None if w["proceeds"] is None else
                                    _q(w["proceeds"] + w["dividends_cash"] - w["cost"]))

        totals = queries.totals(open_)
        held = {t: w for t, w in want.items() if w["units"] > 0 and t in active}
        valued = [w for w in held.values()
                  if w["cost_aud"] is not None and w["value_aud"] is not None]
        cost = sum((w["cost_aud"] for w in valued), ZERO)
        value = sum((w["value_aud"] for w in valued), ZERO)
        assert (_q(totals.cost), _q(totals.value), _q(totals.gain)) == (
            _q(cost), _q(value), _q(value - cost))
        assert _q(totals.gain_pct) == (_q((value - cost) / cost) if cost else None)
        assert sorted(totals.excluded) == sorted(
            t for t, w in held.items() if w["cost_aud"] is None or w["value_aud"] is None)
        assert _q(totals.dividends) == _q(sum((w["dividends_aud"] for w in held.values()
                                               if w["dividends_aud"] is not None), ZERO))
        tickers_with = {h.ticker for h in history.holdings if h.dividends}
        assert sorted(totals.dividends_excluded) == sorted(
            t for t, w in held.items() if w["dividends_aud"] is None and t in tickers_with)
        day = sum((w["day_change_aud"] for w in held.values()
                   if w["day_change_aud"] is not None), ZERO)
        assert _q(totals.day_change) == _q(day)
        yesterday = value - day
        assert _q(totals.day_pct) == (_q(day / yesterday) if yesterday else None)
    finally:
        s.rollback()
        s.close()
