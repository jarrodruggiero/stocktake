"""The daily series behind every chart, against a working-out of its own.

A mutation run left the series' arithmetic almost entirely unguarded: a US
sale's proceeds or a dividend could be divided by its rate, and the holding
left out of a day it cannot be valued on could keep its proceeds, with every
test green. Worked out again here, day by day, from the rules in
`portfolio_series`'s docstring and decisions.md #5 and #54:

- invested is the cumulative cost of buys with brokerage, at each trade's rate;
- value is units at the latest close on or before the day, at that day's rate;
- a holding with units but no close yet leaves the day whole — its cost, its
  proceeds and its income — rather than charting as a total loss;
- a currency with no stored rate at all is left out of everything, by name.

A chart split by ticker or class follows the same rules per line, so its lines
add up to the total. It did not: a holding not yet priced charted as a loss of
its whole cost in the split, the cliff the total was fixed for.
"""

from __future__ import annotations

import datetime as dt

import pytest
from hypothesis import given

from app import queries, tenancy
from histories import ZERO, History, fx_of, histories, load, stored_rate


def _worked(history: History, group=None) -> dict:
    """The series, by day, as {label: {"value": [...], ...}} plus the dates."""
    excluded = sorted(h.ticker for h in history.holdings
                      if h.currency != "AUD" and not history.usd)
    priced = [h for h in history.holdings if h.ticker not in excluded]
    events = sorted([(t.date, "t", h, t) for h in priced for t in h.trades]
                    + [(d.date, "d", h, d) for h in priced for d in h.dividends],
                    key=lambda e: e[0])
    if not events:
        return {"dates": [], "lines": {}, "excluded": excluded}
    # Every stored close makes a day, an excluded holding's included.
    dates = sorted({d for h in history.holdings for d, _c in h.closes if d >= events[0][0]})
    label = (lambda h: "all") if group is None else group
    lines = {label(h): {k: [] for k in ("value", "invested", "gain", "gain_pct", "flow_in",
                                        "cash_div")} for h in priced}
    units, spent, taken, earned = {}, {}, {}, {}
    applied = 0
    for day in dates:
        flow_in = {k: ZERO for k in lines}
        income = {k: ZERO for k in lines}
        while applied < len(events) and events[applied][0] <= day:
            _when, kind, h, row = events[applied]
            applied += 1
            rate = fx_of(history, h.currency, row)
            if kind == "d":
                cash = row.cash * rate
                earned[h.ticker] = earned.get(h.ticker, ZERO) + cash
                income[label(h)] += cash
            elif row.type == "sell":
                units[h.ticker] = units.get(h.ticker, ZERO) - row.quantity
                got = (row.quantity * row.unit_price - row.brokerage) * rate
                taken[h.ticker] = taken.get(h.ticker, ZERO) + got
            else:
                units[h.ticker] = units.get(h.ticker, ZERO) + row.quantity
                if row.type == "buy":
                    cost = (row.quantity * row.unit_price + row.brokerage) * rate
                    spent[h.ticker] = spent.get(h.ticker, ZERO) + cost
                    flow_in[label(h)] += cost
        value = {k: ZERO for k in lines}
        kept = {k: [ZERO, ZERO, ZERO] for k in lines}      # invested, proceeds, income
        for h in priced:
            u = units.get(h.ticker, ZERO)
            usable = [c for d, c in h.closes if d <= day]
            if u and not usable:
                continue                    # not valued today, so not counted at all
            part = kept[label(h)]
            part[0] += spent.get(h.ticker, ZERO)
            part[1] += taken.get(h.ticker, ZERO)
            part[2] += earned.get(h.ticker, ZERO)
            if u:
                value[label(h)] += u * usable[-1] * stored_rate(history, h.currency, day)
        for k, line in lines.items():
            invested, proceeds, cash = kept[k]
            gain = value[k] + proceeds + cash - invested
            line["value"].append(round(float(value[k]), 2))
            line["invested"].append(round(float(invested), 2))
            line["gain"].append(round(float(gain), 2))
            line["gain_pct"].append(round(float(gain / invested), 4) if invested else 0.0)
            line["flow_in"].append(round(float(flow_in[k]), 2))
            line["cash_div"].append(round(float(income[k]), 2))
    return {"dates": [d.isoformat() for d in dates], "lines": lines, "excluded": excluded}


@given(history=histories())
def test_the_portfolio_series_agrees_with_the_working_out(session_factory, portfolio, history):
    s = session_factory()
    tenancy.bind(s, *portfolio)
    try:
        load(s, history)
        got = queries.portfolio_series(s)
        want = _worked(history)
        assert got["dates"] == want["dates"]
        assert got["excluded"] == want["excluded"]
        line = want["lines"].get("all")
        for key in ("value", "invested", "gain", "gain_pct", "flow_in", "cash_div"):
            assert got[key] == (line[key] if line else []), key
    finally:
        s.rollback()
        s.close()


@pytest.mark.parametrize("by", ["ticker", "currency"])
@given(history=histories())
def test_a_split_chart_follows_the_same_rules_and_adds_up(session_factory, portfolio, history,
                                                          by):
    s = session_factory()
    tenancy.bind(s, *portfolio)
    try:
        load(s, history)
        got = queries.grouped_series(s, by)
        want = _worked(history, group=lambda h: getattr(h, by))
        assert got["dates"] == want["dates"]
        assert got["excluded"] == want["excluded"]
        live = {k: v for k, v in want["lines"].items()
                if any(any(series) for series in v.values())}
        assert got["groups"] == live
        total = queries.portfolio_series(s)
        for key in ("value", "invested", "gain"):
            for i in range(len(got["dates"])):
                split = sum(line[key][i] for line in got["groups"].values())
                assert abs(split - total[key][i]) <= 0.005 * max(len(got["groups"]), 1) + 1e-9
    finally:
        s.rollback()
        s.close()


def test_a_holding_not_yet_priced_is_left_out_of_its_line_too(session_factory, portfolio):
    """The case the total was fixed for, in a split: ZULU bought on the 2nd and
    first priced on the 4th is not a loss of its whole cost on the 3rd."""
    import factories as fac

    s = session_factory()
    tenancy.bind(s, *portfolio)
    try:
        alpha, zulu = fac.make_instrument(s, "ALPHA"), fac.make_instrument(s, "ZULU")
        fac.add_trade(s, alpha, "2026-03-02", "buy", 10, "1.00")
        fac.add_trade(s, zulu, "2026-03-02", "buy", 10, "5.00")
        fac.add_prices(s, alpha, [(dt.date(2026, 3, d), "1.00") for d in (2, 3, 4)])
        fac.add_prices(s, zulu, [(dt.date(2026, 3, 4), "5.50")])
        lines = queries.grouped_series(s, "ticker")["groups"]
        assert lines["ZULU"]["gain"] == [0.0, 0.0, 5.0]
        assert lines["ZULU"]["invested"] == [0.0, 0.0, 50.0]
        assert queries.portfolio_series(s)["gain"] == [0.0, 0.0, 5.0]
    finally:
        s.rollback()
        s.close()
