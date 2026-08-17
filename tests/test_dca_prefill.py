"""Recording today's scheduled buy without retyping it.

The trade form fills itself in from the DCA Schedule. The
design note that shaped it: **a form that silently supplies a quantity is
asking to be signed off without being read.** The number can be wrong — a stale
close is the obvious way — so the form shows the arithmetic that produced it.

`prefill()` returns None rather than a partial answer whenever any input is
missing. A half-filled form looks like the app knows something it does not.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import pytest
from freezegun import freeze_time

import factories as fac
from app import plans
from test_routes import bind_to_only_portfolio, make_login

HTML = {"accept": "text/html"}
TODAY = "2026-08-08"


def _planned(session_factory, *, amount="500", brokerage="9.50", price="142.50"):
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "ALPHA")
        if price is not None:
            fac.add_price(s, inst, "2026-08-07", price)
        from app.models import InvestmentPlan

        plan = InvestmentPlan(name="Regular buys", interval_days=28,
                              amount=Decimal(amount), brokerage=Decimal(brokerage),
                              start_date=dt.date(2026, 8, 1), active=True)
        s.add(plan)
        s.flush()
        plans.set_entries(s, plan, [inst.id])
        s.commit()
        return inst.id


@freeze_time(TODAY)
def test_it_buys_whole_units_only(client, session_factory):
    """You cannot buy part of a share. $500 − $9.50 = $490.50, and at $142.50
    that is 3 units — not 3.44."""
    make_login(client, session_factory)
    _planned(session_factory)

    with session_factory() as s:
        bind_to_only_portfolio(s)
        got = plans.prefill(s)

    assert got is not None
    assert got.units == 3


@freeze_time(TODAY)
def test_the_remainder_is_reported_rather_than_hidden(client, session_factory):
    """$490.50 − 3 × $142.50 = $63.00. The same idea as a DRP residual, and
    worded the same way — money that did not get invested."""
    make_login(client, session_factory)
    _planned(session_factory)

    with session_factory() as s:
        bind_to_only_portfolio(s)
        got = plans.prefill(s)

    assert got.leftover == Decimal("63.00")


@freeze_time(TODAY)
def test_brokerage_comes_off_before_the_division(client, session_factory):
    """Buying $500 worth and paying brokerage on top spends more than $500,
    which is not what "about $500 each" means."""
    make_login(client, session_factory)
    _planned(session_factory, amount="500", brokerage="0")

    with session_factory() as s:
        bind_to_only_portfolio(s)
        assert plans.prefill(s).units == 3   # 500 / 142.50 = 3.50


@freeze_time(TODAY)
@pytest.mark.parametrize("kwargs, why", [
    ({"price": None}, "no stored price to divide by"),
    ({"amount": "0"}, "no amount set on the plan"),
    ({"amount": "5"}, "not enough for even one unit"),
    ({"amount": "10", "brokerage": "10"}, "brokerage swallows the whole buy"),
])
def test_it_declines_rather_than_guessing(client, session_factory, kwargs, why):
    """None, not a partial fill. A form half-filled from missing data looks
    like the app knows something it does not."""
    make_login(client, session_factory)
    _planned(session_factory, **kwargs)

    with session_factory() as s:
        bind_to_only_portfolio(s)
        assert plans.prefill(s) is None, f"should have declined: {why}"


@freeze_time(TODAY)
def test_with_no_plan_there_is_nothing_to_prefill(client, session_factory):
    make_login(client, session_factory)

    with session_factory() as s:
        bind_to_only_portfolio(s)
        assert plans.prefill(s) is None


# --------------------------------------------------------------------------- #
# On the form
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_the_form_shows_the_arithmetic_not_just_the_answer(client, session_factory):
    """The whole point. Somebody who can see how 3 was reached will notice a
    stale price; somebody shown only "3" will not."""
    make_login(client, session_factory)
    _planned(session_factory)

    page = client.get("/trade/new", headers=HTML).text

    assert "From your DCA Schedule" in page
    assert "$500.00" in page          # the plan's amount
    assert "$9.50" in page            # brokerage, taken off first
    assert "$142.50" in page          # the close it divided by
    assert "2026-08-07" in page       # …and WHEN that close was
    assert "$427.50" in page          # 3 x $142.50 — what this buy costs


@freeze_time(TODAY)
def test_the_fields_are_filled_in(client, session_factory):
    make_login(client, session_factory)
    _planned(session_factory)

    page = client.get("/trade/new", headers=HTML).text

    assert re.search(r'name="quantity"[^>]*value="3"', page)
    # Normalised: the stored close is Numeric(18,6) and would otherwise fill
    # the box with "142.500000".
    assert re.search(r'name="unit_price"[^>]*value="142\.5"', page)


@freeze_time(TODAY)
def test_the_date_is_today_not_the_due_date(client, session_factory):
    """Back-dating a trade quietly would be worse than not helping — the due
    date is when it was scheduled, not when you bought it."""
    make_login(client, session_factory)
    _planned(session_factory)

    page = client.get("/trade/new", headers=HTML).text

    assert re.search(r'name="trade_date"[^>]*value="2026-08-08"', page)


@freeze_time(TODAY)
def test_nothing_is_prefilled_when_the_schedule_is_switched_off(client, session_factory):
    """An install that turned the feature off should not have its trade form
    quietly filled in from a schedule it does not have."""
    from app.main import settings

    make_login(client, session_factory)
    _planned(session_factory)
    settings.features.dca_schedule = False

    page = client.get("/trade/new", headers=HTML).text

    assert "From your DCA Schedule" not in page
