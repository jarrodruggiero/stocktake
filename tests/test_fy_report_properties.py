"""The FY report against a working-out of its own, over generated histories.

`test_cgt.py` works its figures by hand, on round numbers. A mutation run
showed how much that leaves open: the snapshot's gain could be `value +
invested`, a US sale's proceeds could be divided by the rate, the year's
activity could count trades from other years, and the price could come from
after the year ended, all with every test green.

Each case here is a history from `histories.py`. Every financial year it
reaches is reported by the app and worked out again below from the rules in
fyreport.py's docstring and decisions.md (#5, #7, #93), and the two must agree
to the cent.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from freezegun import freeze_time
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select

import factories as fac
from app import fyreport, tenancy
from app.models import Portfolio, User
from histories import (
    CENTS,
    ONE,
    TODAY,
    ZERO,
    History,
    booked_rate,
    close_on_or_before,
    histories,
    load,
    stored_rate,
)

EXACT = Decimal("0.00000001")


@pytest.fixture
def portfolio(session_factory):
    """An owner and a portfolio, committed: each case rolls back only its own rows."""
    with session_factory() as s:
        user = fac.make_user(s, "owner@example.test")
        p = fac.make_portfolio(s, "Generated", owner=user)
        s.commit()
        return p.id, user.id


def _session(session_factory, portfolio):
    s = session_factory()
    tenancy.bind(s, *portfolio)
    return s


# --------------------------------------------------------------------------- #
# The working-out
# --------------------------------------------------------------------------- #

def _bounds(fy: int) -> tuple[dt.date, dt.date]:
    return dt.date(fy - 1, 7, 1), dt.date(fy, 6, 30)


def _fy_of(day: dt.date) -> int:
    return day.year + (1 if day.month >= 7 else 0)


def _over_a_year(acquired: dt.date, sold: dt.date) -> bool:
    """Held more than twelve months: sold after the anniversary, which for a
    29 February purchase is 28 February."""
    try:
        anniversary = acquired.replace(year=acquired.year + 1)
    except ValueError:
        anniversary = dt.date(acquired.year + 1, 2, 28)
    return sold > anniversary


def _disposals(history: History) -> list[dict]:
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
                             "discountable": _over_a_year(p[0], t.date)})
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


def _cgt(history: History, fy: int) -> dict:
    start, end = _bounds(fy)
    disposals = sorted((d for d in _disposals(history) if start <= d["date"] <= end),
                       key=lambda d: d["date"])
    gains_disc = gains_other = losses = ZERO
    for d in disposals:
        for p in d["parcels"]:
            gain = p["proceeds"] - p["cost_base"]
            if gain < 0:
                losses -= gain
            elif p["discountable"]:
                gains_disc += gain
            else:
                gains_other += gain
    # Losses come off the gains that get no discount first, then the discount
    # applies to what is left of the rest.
    off_other = min(losses, gains_other)
    off_disc = min(losses - off_other, gains_disc)
    unapplied = losses - off_other - off_disc
    discounted = gains_disc - off_disc
    net_gain = ZERO if unapplied > 0 else (gains_other - off_other) + discounted / 2
    return {"disposals": disposals, "gains_discountable": gains_disc,
            "gains_other": gains_other, "losses": losses, "discount": discounted / 2,
            "net_capital_gain": net_gain, "net_capital_loss": max(unapplied, ZERO)}


def _report(history: History, fy: int) -> dict:
    start, end = _bounds(fy)
    asof = min(end, TODAY)
    snapshot, activity = {}, dict(invested=ZERO, buys=0, sells=0, brokerage=ZERO,
                                  proceeds=ZERO)
    for h in history.holdings:
        units = invested = ZERO
        for t in h.trades:
            if t.date > asof:
                continue
            rate = booked_rate(history, h.currency, t)
            units += -t.quantity if t.type == "sell" else t.quantity
            if t.type == "buy":
                invested += (t.quantity * t.unit_price + t.brokerage) * rate
            if not start <= t.date <= end or t.type == "drp":
                continue
            activity["brokerage"] += t.brokerage * rate
            if t.type == "buy":
                activity["buys"] += 1
                activity["invested"] += (t.quantity * t.unit_price + t.brokerage) * rate
            else:
                activity["sells"] += 1
                activity["proceeds"] += (t.quantity * t.unit_price - t.brokerage) * rate
        if units > 0:
            close = close_on_or_before(h, asof)
            rate = stored_rate(history, h.currency, asof)
            # No rate stored for the currency is no AUD value: decisions.md #5.
            value = units * close[0] * rate if close and rate is not None else None
            snapshot[h.ticker] = {
                "units": units, "invested": invested,
                "price": close[0] if close else None, "price_date": close[1] if close else None,
                "value": value, "gain": None if value is None else value - invested}
    income = {}
    for h in history.holdings:
        paid = [d for d in h.dividends if start <= d.date <= end]
        if paid:
            income[h.ticker] = {
                "cash": sum((d.cash * booked_rate(history, h.currency, d) for d in paid), ZERO),
                "franking": sum((d.franking or ZERO for d in paid), ZERO),
                "missing": sum(1 for d in paid if d.franking is None)}
    return {"snapshot": snapshot, "activity": activity, "income": income,
            "cgt": _cgt(history, fy)}


# --------------------------------------------------------------------------- #
# What the app says, in the same shape
# --------------------------------------------------------------------------- #

def _round(v):
    return None if v is None else Decimal(v).quantize(EXACT)


def _app(report: dict) -> dict:
    a = report["activity"]
    cgt = report["cgt"]
    return {
        "snapshot": {r.instrument.ticker: {
            "units": _round(r.units), "invested": _round(r.invested_cum), "price": r.price,
            "price_date": r.price_date, "value": _round(r.value_aud), "gain": _round(r.gain_aud)}
            for r in report["snapshot"]},
        "activity": {"invested": _round(a.invested), "buys": a.buys, "sells": a.sells,
                     "brokerage": _round(a.brokerage), "proceeds": _round(a.proceeds)},
        "income": {r.instrument.ticker: {"cash": _round(r.cash), "franking": _round(r.franking),
                                         "missing": r.franking_missing}
                   for r in report["income"]},
        "cgt": {"disposals": [{"ticker": d.instrument.ticker, "date": d.date,
                               "quantity": _round(d.quantity), "proceeds": d.proceeds,
                               "parcels": [{"acquired": p.acquired, "quantity": _round(p.quantity),
                                            "cost_base": p.cost_base, "discountable": p.discountable,
                                            "proceeds": p.proceeds} for p in d.parcels]}
                              for d in cgt.disposals],
                "gains_discountable": cgt.gains_discountable, "gains_other": cgt.gains_other,
                "losses": cgt.losses, "discount": cgt.discount,
                "net_capital_gain": cgt.net_capital_gain,
                "net_capital_loss": cgt.net_capital_loss},
    }


def _rounded(expected: dict) -> dict:
    """The working-out in the comparison's shape (quantities and money to 1e-8)."""
    snap = {t: {k: (_round(v) if k in ("units", "invested", "value", "gain") else v)
                for k, v in row.items()} for t, row in expected["snapshot"].items()}
    act = {k: (_round(v) if isinstance(v, Decimal) else v)
           for k, v in expected["activity"].items()}
    inc = {t: {"cash": _round(r["cash"]), "franking": _round(r["franking"]),
               "missing": r["missing"]} for t, r in expected["income"].items()}
    cgt = dict(expected["cgt"])
    cgt["disposals"] = [{**d, "quantity": _round(d["quantity"]),
                         "parcels": [{**p, "quantity": _round(p["quantity"])}
                                     for p in d["parcels"]]} for d in cgt["disposals"]]
    return {"snapshot": snap, "activity": act, "income": inc, "cgt": cgt}


@given(history=histories())
def test_every_financial_year_agrees_with_the_working_out(session_factory, portfolio, history):
    s = _session(session_factory, portfolio)
    try:
        load(s, history)
        with freeze_time(TODAY):
            years = fyreport.available_fys(s)
            first = min(t.date for h in history.holdings for t in h.trades)
            assert years == list(range(_fy_of(TODAY), _fy_of(first) - 1, -1))
            for fy in years:
                got = _app(fyreport.fy_report(s, fy))
                want = _rounded(_report(history, fy))
                # Sales on one day in different holdings come in no set order.
                for side in (got, want):
                    side["cgt"]["disposals"].sort(key=lambda d: (d["date"], d["ticker"]))
                for part in ("snapshot", "activity", "income", "cgt"):
                    assert got[part] == want[part], f"FY{fy} {part}"
    finally:
        s.rollback()
        s.close()


@given(history=histories())
def test_the_parts_of_every_disposal_add_up(session_factory, portfolio, history):
    """Independent of the working-out: each sale's parcels add to its proceeds,
    and a parcel used up has billed exactly its cost, to the cent."""
    s = _session(session_factory, portfolio)
    try:
        rows = load(s, history)
        for h in history.holdings:
            inst = s.get(type(rows[h.ticker]), rows[h.ticker].id)
            s.refresh(inst, ["trades"])
            disposals = fyreport.instrument_disposals(inst, _book(s))
            for d in disposals:
                assert sum((p.proceeds for p in d.parcels), ZERO) == d.proceeds
                assert sum((p.quantity for p in d.parcels), ZERO) == d.quantity
            billed: dict[dt.date, Decimal] = {}
            for d in disposals:
                for p in d.parcels:
                    billed[p.acquired] = billed.get(p.acquired, ZERO) + p.cost_base
            sold = sum((t.quantity for t in h.trades if t.type == "sell"), ZERO)
            used = ZERO
            for t in h.trades:
                if t.type == "sell":
                    continue
                if used + t.quantity > sold:
                    break                   # FIFO: nothing after this is used up
                used += t.quantity
                cost = ((t.quantity * t.unit_price + t.brokerage)
                        * booked_rate(history, h.currency, t)).quantize(CENTS)
                same_day = [x for x in h.trades if x.type != "sell" and x.date == t.date]
                if len(same_day) == 1:      # parcels from one day share a key here
                    assert billed.get(t.date, ZERO) == cost, t
    finally:
        s.rollback()
        s.close()


def _book(s):
    from app import queries
    return queries.FxBook(s)


# --------------------------------------------------------------------------- #
# The boundaries the mutation run found nothing guarding
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("day, fy", [
    ("2026-06-30", 2026), ("2026-07-01", 2027), ("2026-01-01", 2026),
    ("2025-12-31", 2026), ("2024-02-29", 2024), ("2026-05-31", 2026),
])
def test_the_financial_year_a_day_falls_in(day, fy):
    assert fyreport.current_fy(dt.date.fromisoformat(day)) == fy


@given(st.dates(dt.date(2000, 1, 1), dt.date(2099, 12, 31)))
def test_every_day_falls_inside_its_own_financial_year(day):
    start, end = fyreport.fy_bounds(fyreport.current_fy(day))
    assert start <= day <= end
    assert (end - start).days in (364, 365)


def test_a_first_trade_in_june_starts_the_list_at_that_june(session_factory, portfolio):
    s = _session(session_factory, portfolio)
    try:
        inst = fac.make_instrument(s, "ALPHA")
        fac.add_trade(s, inst, "2024-06-30", "buy", 1, "1.00")
        fac.add_trade(s, inst, "2023-06-30", "buy", 1, "1.00")    # earliest, added last
        with freeze_time(TODAY):
            assert fyreport.available_fys(s) == [2027, 2026, 2025, 2024, 2023]
    finally:
        s.rollback()
        s.close()


def test_selling_a_fraction_more_than_is_held_is_refused():
    inst = SimpleInstrument("BTC", [("2026-01-02", "buy", "1.5"), ("2026-02-02", "sell", "1.75")])
    with pytest.raises(ValueError, match="exceeds held parcels"):
        fyreport.instrument_disposals(inst)


def test_a_net_loss_of_cents_is_still_a_loss(session_factory, portfolio):
    s = _session(session_factory, portfolio)
    try:
        inst = fac.make_instrument(s, "ALPHA")
        fac.add_trade(s, inst, "2025-08-01", "buy", 10, "1.00")
        fac.add_trade(s, inst, "2025-09-01", "sell", 10, "0.95")   # 50 cents down
        out = fyreport.fy_cgt(s, 2026)
        assert (out.net_capital_loss, out.net_capital_gain) == (Decimal("0.50"), ZERO)
    finally:
        s.rollback()
        s.close()


class SimpleInstrument:
    """Enough of an Instrument for `instrument_disposals`, with no database."""

    def __init__(self, ticker, trades):
        from types import SimpleNamespace as NS

        self.ticker, self.currency = ticker, "AUD"
        self.trades = [NS(id=i + 1, date=dt.date.fromisoformat(d), time=None, type=k,
                          quantity=Decimal(q), unit_price=ONE, brokerage=ZERO, fx_rate=ONE)
                       for i, (d, k, q) in enumerate(trades)]


def test_the_owner_fixture_is_what_the_cases_are_bound_to(session_factory, portfolio):
    with session_factory() as s:
        assert s.get(Portfolio, portfolio[0]) is not None
        assert s.scalar(select(User.email)) == "owner@example.test"
