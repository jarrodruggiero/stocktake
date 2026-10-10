"""The month calendar over generated months and generated history.

A mutation run found the edges unguarded: a trade on the first or last day of a
month could be dropped, the grid could end a day short, and the projection's
minimum history and its 20-day floor on a gap could be anything, with every
test green. The calendar's tests used mid-month dates in a mid-year month.
"""

from __future__ import annotations

import datetime as dt
import statistics
from decimal import Decimal

import pytest
from freezegun import freeze_time
from hypothesis import given
from hypothesis import strategies as st

import factories as fac
from app import calendarview, tenancy

TODAY = dt.date(2026, 8, 2)
months = st.tuples(st.integers(2020, 2030), st.integers(1, 12))
days = st.dates(dt.date(2025, 12, 1), dt.date(2026, 12, 31))


@given(when=months)
def test_the_grid_is_whole_weeks_around_the_whole_month(session_factory, portfolio, when):
    year, month = when
    s = session_factory()
    tenancy.bind(s, *portfolio)
    try:
        with freeze_time(TODAY):
            grid = calendarview.month_grid(s, year, month)
        cells = [d for week in grid["weeks"] for d in week]
        assert all(len(week) == 7 for week in grid["weeks"])
        assert cells[0].date.weekday() == 0 and cells[-1].date.weekday() == 6
        assert [b.date - a.date for a, b in zip(cells, cells[1:])] == \
            [dt.timedelta(days=1)] * (len(cells) - 1)
        inside = [c.date for c in cells if c.in_month]
        assert inside[0] == dt.date(year, month, 1)
        assert (inside[-1] + dt.timedelta(days=1)).day == 1, "the month's last day is in"
        assert all(c.date.month == month for c in cells if c.in_month)
        assert cells[0].date > dt.date(year, month, 1) - dt.timedelta(days=7)
        assert cells[-1].date < inside[-1] + dt.timedelta(days=7)
        assert grid["prev"] == ((year, month - 1) if month > 1 else (year - 1, 12))
        assert grid["next"] == ((year, month + 1) if month < 12 else (year + 1, 1))
        assert [c.is_today for c in cells].count(True) == (1 if TODAY in [c.date for c in cells]
                                                           else 0)
    finally:
        s.rollback()
        s.close()


@given(when=months, trades=st.lists(st.tuples(days, st.sampled_from(["buy", "sell"])),
                                    max_size=8),
       paid=st.lists(days, max_size=4))
def test_every_recorded_row_is_on_its_own_day_once(session_factory, portfolio, when, trades,
                                                   paid):
    year, month = when
    s = session_factory()
    tenancy.bind(s, *portfolio)
    try:
        inst = fac.make_instrument(s, "ALPHA")
        # Edges on purpose: the first and last of the month asked for.
        last = dt.date(year, month, 1).replace(day=28) + dt.timedelta(days=4)
        last -= dt.timedelta(days=last.day)
        first = dt.date(year, month, 1)
        grid = (first - dt.timedelta(days=first.weekday()),
                last + dt.timedelta(days=6 - last.weekday()))
        edges = [(first, "buy"), (last, "sell"), (grid[0], "buy"), (grid[1], "buy")]
        paid = paid + list(grid)
        for day, kind in trades + edges:
            fac.add_trade(s, inst, day, kind, 1, "2.00")
        for day in paid:
            fac.add_dividend(s, inst, day, "3.00")
        with freeze_time(TODAY):
            grid = calendarview.month_grid(s, year, month)
        cells = {c.date: c for week in grid["weeks"] for c in week}
        recorded = [(day, "sell" if kind == "sell" else "buy") for day, kind in trades + edges]
        recorded += [(day, "dividend") for day in paid]
        for day, cell in cells.items():
            got = sorted(e.kind for e in cell.events if not e.expected)
            assert got == sorted(k for d, k in recorded if d == day), day
        for e in (e for c in cells.values() for e in c.events if not e.expected):
            assert e.amount == (Decimal(3) if e.kind == "dividend" else Decimal(2))
    finally:
        s.rollback()
        s.close()


@given(gaps=st.lists(st.integers(1, 120), min_size=0, max_size=6),
       last=st.dates(dt.date(2025, 9, 1), dt.date(2026, 9, 1)))
def test_a_payment_is_projected_on_the_median_cycle(session_factory, portfolio, gaps, last):
    """From the holder's own history (no published record here): at least two
    payments, gaps of 20 days or less ignored, the median of the rest stepped
    from the last payment, from today to the end of the grid."""
    s = session_factory()
    tenancy.bind(s, *portfolio)
    try:
        inst = fac.make_instrument(s, "ALPHA")
        fac.add_trade(s, inst, "2024-01-02", "buy", 100, "2.00")
        dates = [last]
        for gap in reversed(gaps):
            dates.insert(0, dates[0] - dt.timedelta(days=gap))
        for day in dates:
            fac.add_dividend(s, inst, day, "5.00")
        until = TODAY + dt.timedelta(days=200)
        with freeze_time(TODAY):
            got = [e.date for e in calendarview.projected_dividends(s, until)]
        counted = [g for g in gaps if g > 20]
        want = []
        if len(dates) >= 2 and counted:
            step = int(statistics.median(counted))
            nxt = dates[-1] + dt.timedelta(days=step)
            while nxt <= until:
                if nxt >= TODAY:
                    want.append(nxt)
                nxt += dt.timedelta(days=step)
        assert got == want
    finally:
        s.rollback()
        s.close()


def test_a_sold_out_holding_projects_nothing(session_factory, portfolio):
    s = session_factory()
    tenancy.bind(s, *portfolio)
    try:
        inst = fac.make_instrument(s, "ALPHA")
        fac.add_trade(s, inst, "2025-01-02", "buy", 10, "2.00")
        for day in ("2025-03-01", "2025-06-01", "2025-09-01"):
            fac.add_dividend(s, inst, day, "5.00")
        with freeze_time(TODAY):
            assert calendarview.projected_dividends(s, TODAY + dt.timedelta(days=200))
            fac.add_trade(s, inst, "2025-10-02", "sell", 10, "2.00")
            s.expire_all()          # as the next request's fresh session would be
            assert calendarview.projected_dividends(s, TODAY + dt.timedelta(days=200)) == []
    finally:
        s.rollback()
        s.close()


@pytest.mark.parametrize("gaps, cycle", [([20, 90], 90), ([21, 90], 55), ([90], 90)])
def test_a_gap_of_twenty_days_or_less_is_not_a_cycle(session_factory, portfolio, gaps, cycle):
    s = session_factory()
    tenancy.bind(s, *portfolio)
    try:
        inst = fac.make_instrument(s, "ALPHA")
        fac.add_trade(s, inst, "2024-01-02", "buy", 100, "2.00")
        day = dt.date(2026, 7, 1)
        dates = [day]
        for gap in reversed(gaps):
            dates.insert(0, dates[0] - dt.timedelta(days=gap))
        for d in dates:
            fac.add_dividend(s, inst, d, "5.00")
        with freeze_time(TODAY):
            got = calendarview.projected_dividends(s, TODAY + dt.timedelta(days=400))
        assert got[0].date == day + dt.timedelta(days=cycle)
        assert f"~{cycle}-day cycle" in got[0].detail
    finally:
        s.rollback()
        s.close()
