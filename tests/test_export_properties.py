"""Every downloadable report, row by row, against the working-out in
histories.py, over generated histories.

These are the files that go to an accountant, and a mutation run found most of
their columns unchecked: a closed position's discount could be doubled, its
gain after discount could add the discount, the holdings export's gain % could
divide by 100, and the realised schedule's financial year could be anything,
with every test green.

The rules each report states are the ones checked: the realised schedule
discounts each parcel on its own (decisions.md #77); the unrealised one prices
what is left at the latest close and rate, and leaves a cost blank where no
rate is known (#5); a closed position's figures come from the same FIFO
engine as the schedule.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from freezegun import freeze_time
from hypothesis import given

from app import exports, fyreport, tenancy
from histories import (
    TODAY,
    ZERO,
    History,
    fx_of,
    histories,
    load,
    over_a_year,
    worked_disposals,
    worked_holdings,
)


def _d(value, places="0.01"):
    return "" if value is None else Decimal(value).quantize(Decimal(places))


def _label(day: dt.date) -> str:
    return fyreport.fy_label(day.year + (1 if day.month >= 7 else 0))


def _realised(history: History, names: dict) -> list:
    rows = []
    for d in worked_disposals(history):
        for p in d["parcels"]:
            # Either side with no rate is no gain at all: a blank, not 1:1 (#5).
            gain = (None if p["proceeds"] is None or p["cost_base"] is None
                    else p["proceeds"] - p["cost_base"])
            discount = (None if gain is None else
                        gain / 2 if p["discountable"] and gain > 0 else ZERO)
            rows.append([d["date"].isoformat(), d["ticker"], names[d["ticker"]],
                         _d(p["quantity"], "0.00000001"), p["acquired"].isoformat(),
                         (d["date"] - p["acquired"]).days, _d(p["cost_base"]),
                         _d(p["proceeds"]), _d(gain), "yes" if p["discountable"] else "no",
                         _d(discount), _d(None if gain is None else gain - discount),
                         _label(d["date"])])
    return sorted(rows, key=lambda r: (r[0], r[1]))


def _unrealised(history: History, names: dict) -> list:
    rate_now = history.usd[-1][1] if history.usd else None
    rows = []
    for h in history.holdings:
        if not h.closes:
            continue
        price = h.closes[-1][1]
        fx_now = Decimal(1) if h.currency == "AUD" else rate_now
        left = []                       # [acquired, units, unit cost or None]
        for t in h.trades:
            if t.type != "sell":
                fx = fx_of(history, h.currency, t)
                unit = None if fx is None else (
                    (t.quantity * t.unit_price + t.brokerage) * fx / t.quantity)
                left.append([t.date, t.quantity, unit])
                continue
            sold = t.quantity
            while sold > 0:
                take = min(left[0][1], sold)
                sold -= take
                left[0][1] -= take
                if left[0][1] == 0:
                    left.pop(0)
        for acquired, units, unit in left:
            cost = None if unit is None else units * unit
            value = None if fx_now is None else units * price * fx_now
            gain = None if cost is None or value is None else value - cost
            eligible = over_a_year(acquired, TODAY)
            discount = gain / 2 if gain is not None and eligible and gain > 0 else ZERO
            rows.append([h.ticker, names[h.ticker], acquired.isoformat(),
                         _d(units, "0.00000001"), (TODAY - acquired).days, _d(cost),
                         _d(value), _d(gain), "yes" if eligible else "no",
                         "" if gain is None else _d(gain - discount)])
    return sorted(rows, key=lambda r: (r[0], r[2]))


def _closed(history: History, names: dict) -> list:
    held = worked_holdings(history)
    by_ticker: dict = {}
    for d in worked_disposals(history):
        by_ticker.setdefault(d["ticker"], []).append(d)
    rows = []
    for h in history.holdings:
        sales = by_ticker.get(h.ticker)
        if not sales or (held[h.ticker]["units"] > 0 and h.active):
            continue
        parcels = [p for d in sales for p in d["parcels"]]
        cost = _all(p["cost_base"] for p in parcels)
        proceeds = _all(d["proceeds"] for d in sales)
        gross = None if cost is None or proceeds is None else proceeds - cost
        discount = None if gross is None else sum(
            (((p["proceeds"] - p["cost_base"]) / 2) for p in parcels
             if p["discountable"] and p["proceeds"] > p["cost_base"]), ZERO)
        rates = [fx_of(history, h.currency, d) for d in h.dividends]
        dividends = _all(None if r is None else d.cash * r for d, r in zip(h.dividends, rates))
        rows.append([h.ticker, names[h.ticker],
                     _d(sum((d["quantity"] for d in sales), ZERO), "0.00000001"),
                     min(t.date for t in h.trades if t.type != "sell").isoformat(),
                     max(d["date"] for d in sales).isoformat(), _d(cost), _d(proceeds),
                     _d(dividends), _d(gross), _d(discount),
                     _d(None if gross is None else gross - discount),
                     _d(None if gross is None or dividends is None else gross + dividends), ""])
    return sorted(rows, key=lambda r: r[0])


def _holdings(history: History, names: dict) -> list:
    held = worked_holdings(history)
    rows = []
    for h in history.holdings:
        w = held[h.ticker]
        if not (w["units"] > 0 and h.active):
            continue
        price_date = h.closes[-1][0].isoformat() if h.closes else ""
        rows.append([h.ticker, names[h.ticker], "share", h.currency,
                     _d(w["units"], "0.00000001"), _d(w["avg_price"], "0.000001"),
                     _d(w["price"], "0.000001"), price_date, _d(w["cost_aud"]),
                     _d(w["value_aud"]), _d(w["gain_aud"]),
                     "" if w["gain_pct"] is None else _d(w["gain_pct"] * 100),
                     _d(w["dividends_cash"]), "no", ""])
    return sorted(rows, key=lambda r: ("share", r[0]))


def _transactions(history: History, names: dict) -> list:
    rows = []
    for h in history.holdings:
        for t in h.trades:
            gross = t.quantity * t.unit_price
            signed = gross - t.brokerage if t.type == "sell" else gross + t.brokerage
            fx = fx_of(history, h.currency, t)
            rows.append([t.date.isoformat(), h.ticker, names[h.ticker],
                         "ASX" if h.currency == "AUD" else "NASDAQ", t.type,
                         _d(t.quantity, "0.00000001"), _d(t.unit_price, "0.000001"),
                         _d(t.brokerage), h.currency, _d(t.fx_rate, "0.000001"),
                         _d(None if fx is None else signed * fx)])
    return sorted(rows, key=lambda r: r[0])


def _dividends(history: History, names: dict) -> list:
    rows = []
    for h in history.holdings:
        for d in h.dividends:
            fx = fx_of(history, h.currency, d)
            cash = None if fx is None else d.cash * fx
            rows.append([d.date.isoformat(), h.ticker, names[h.ticker], _d(d.cash, "0.0001"),
                         h.currency, _d(d.fx_rate, "0.000001"), _d(cash), _d(d.franking),
                         _d(None if cash is None or d.franking is None else cash + d.franking),
                         "no", _label(d.date)])
    return sorted(rows, key=lambda r: r[0])


def _all(parts):
    """A sum, or None when any part is unknown."""
    parts = list(parts)
    return None if any(p is None for p in parts) else sum(parts, ZERO)


def _unordered(rows) -> list:
    return sorted((list(r) for r in rows), key=lambda r: [str(x) for x in r])


@given(history=histories())
def test_every_report_agrees_with_the_working_out(session_factory, portfolio, history):
    s = session_factory()
    tenancy.bind(s, *portfolio)
    try:
        load(s, history)
        names = {h.ticker: "" for h in history.holdings}
        with freeze_time(TODAY):
            sheets = exports.build(s, list(exports.TITLES))
        got = {key: sheets[title][1] for key, title in exports.TITLES.items()}
        assert got["realised_cgt"] == _realised(history, names)
        assert got["unrealised_cgt"] == _unrealised(history, names)
        assert got["closed_positions"] == _closed(history, names)
        assert got["holdings"] == _holdings(history, names)
        # Same-day rows come in the order they were recorded; the user and the
        # note columns are left out (no user, no note here).
        assert _unordered(r[:11] for r in got["transactions"]) == _unordered(
            _transactions(history, names))
        assert _unordered(r[:11] for r in got["dividends"]) == _unordered(
            _dividends(history, names))
        for fy in {fyreport.current_fy(d["date"]) for d in worked_disposals(history)}:
            with freeze_time(TODAY):
                year = exports.build(s, ["realised_cgt"], fy=fy)[exports.TITLES["realised_cgt"]]
            assert year[1] == [r for r in _realised(history, names)
                               if r[12] == fyreport.fy_label(fy)]
    finally:
        s.rollback()
        s.close()


def test_a_parcel_held_exactly_a_year_is_not_yet_eligible(session_factory, portfolio):
    """Eligible once held MORE than twelve months: on the anniversary itself,
    not yet; the day after, yes. Both schedules, and the 29 February case."""
    import factories as fac

    s = session_factory()
    tenancy.bind(s, *portfolio)
    try:
        made = {}
        for ticker, bought in (("ALPHA", "2025-08-02"), ("BETAX", "2025-08-01"),
                               ("GAMMA", "2024-02-29")):
            made[ticker] = inst = fac.make_instrument(s, ticker)
            fac.add_trade(s, inst, bought, "buy", 10, "1.00")
            fac.add_prices(s, inst, [(TODAY, "2.00")])
        gamma = made["GAMMA"]
        fac.add_trade(s, gamma, "2025-02-28", "sell", 5, "2.00")
        fac.add_trade(s, gamma, "2025-03-01", "sell", 5, "2.00")
        with freeze_time(TODAY):
            sheets = exports.build(s, ["unrealised_cgt", "realised_cgt"])
        unrealised = {r[0]: r[8] for r in sheets[exports.TITLES["unrealised_cgt"]][1]}
        assert unrealised == {"ALPHA": "no", "BETAX": "yes"}
        realised = [(r[0], r[9]) for r in sheets[exports.TITLES["realised_cgt"]][1]]
        assert realised == [("2025-02-28", "no"), ("2025-03-01", "yes")]
    finally:
        s.rollback()
        s.close()
