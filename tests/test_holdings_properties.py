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

from hypothesis import given

from app import queries, tenancy
from histories import ZERO, histories, load, worked_holdings

EXACT = Decimal("0.00000001")


def _q(v):
    return None if v is None else Decimal(v).quantize(EXACT)


FIELDS = ("units", "cost", "avg_price", "price", "value", "gain", "gain_pct", "cost_aud",
          "value_aud", "gain_aud", "dividends_cash", "dividends_aud", "avg_price_aud",
          "price_aud", "day_pct", "day_change_aud")


@given(history=histories())
def test_every_holding_agrees_with_the_working_out(session_factory, portfolio, history):
    s = session_factory()
    tenancy.bind(s, *portfolio)
    try:
        load(s, history)
        want = worked_holdings(history)
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
