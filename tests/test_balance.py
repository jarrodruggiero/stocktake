"""`queries.balance_after` — the guard that stops a sell going negative.

The function exists for one reason, spelled out in its own docstring: a sell
that exceeds the parcels held makes `fyreport.instrument_disposals` raise, which
takes out the whole FY report. Catching that at data entry is far cheaper than
discovering it at tax time, so the last two tests here assert the two actually
agree — a timeline the guard refuses is exactly a timeline the FIFO matcher
cannot process.

The subtle part, and the reason this isn't `sum(buys) - sum(sells)`, is that the
balance is walked in DATE order. A sell must be legal at the moment it happened,
not merely covered by units bought afterwards.

Every unit count below is worked out in the comment beside it. `held_trades()`
is defined locally rather than in factories.py so this file stays self-contained.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from freezegun import freeze_time
from sqlalchemy import select

import fixture_portfolio as ref
from app import fyreport, queries
from app.models import Trade
from factories import add_drp, add_trade, build_trade, make_instrument

ZERO = Decimal(0)


def held_trades(db, inst) -> list[Trade]:
    """The same fetch the trade form and the API do before calling
    `balance_after`. Deliberately unordered — ordering is the function's job,
    not the caller's, and a test that pre-sorted would hide that."""
    return list(db.scalars(select(Trade).where(Trade.instrument_id == inst.id)))


# --------------------------------------------------------------------------- #
# The headline: date order, not the net total
# --------------------------------------------------------------------------- #

def test_backdated_sell_covered_only_by_later_buys_is_refused(pf):
    """The whole point of the function: the timeline must be legal at every
    point, not just at the end."""
    acme = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, acme, "2024-01-10", "buy", 50, "10.00", brokerage="10.00")
    add_trade(pf, acme, "2024-03-10", "buy", 100, "10.00", brokerage="10.00")

    # Dated between the two buys, when only the first parcel existed.
    backdated = build_trade(acme, "2024-02-10", "sell", 100, "12.00", brokerage="10.00")

    # Netted with no regard for order this looks fine: 50 + 100 − 100 = +50.
    # Walked in date order it is not: 50 − 100 = −50 units on 2024-02-10.
    assert queries.balance_after(held_trades(pf, acme), backdated) is None


def test_the_same_sell_dated_after_the_covering_buy_is_allowed(pf):
    """Companion to the test above: only the DATE differs between the two."""
    acme = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, acme, "2024-01-10", "buy", 50, "10.00", brokerage="10.00")
    add_trade(pf, acme, "2024-03-10", "buy", 100, "10.00", brokerage="10.00")

    later = build_trade(acme, "2024-04-10", "sell", 100, "12.00", brokerage="10.00")

    # 50 (Jan) → 150 (Mar) → 50 (Apr). Never negative, ends on 50.
    assert queries.balance_after(held_trades(pf, acme), later) == Decimal("50")


@freeze_time(ref.TODAY)
def test_reference_alpha_drp_units_cannot_cover_an_earlier_sell(pf):
    """The backdated-sell attempt the reference fixture describes but never
    saves — DRP units are still units bought later."""
    r = ref.build_reference(pf)
    held = held_trades(pf, r.alpha)

    # ALPHA is 100 units bought 2021-01-04 plus 5 DRP units on 2021-07-01.
    too_early = build_trade(r.alpha, "2021-02-01", "sell", 105, "10.00")
    # 100 − 105 = −5 on 2021-02-01, even though the whole timeline nets to zero.
    assert queries.balance_after(held, too_early) is None

    after_the_drp = build_trade(r.alpha, "2021-08-01", "sell", 105, "10.00")
    # 100 + 5 − 105 = 0 units, and never negative on the way.
    assert queries.balance_after(held, after_the_drp) == ZERO


# --------------------------------------------------------------------------- #
# The candidate (unsaved) trade
# --------------------------------------------------------------------------- #

def test_the_unsaved_candidate_is_walked_alongside_the_saved_trades(pf):
    """A candidate is what the form has in hand: a row that must be refused
    before it ever reaches the database."""
    acme = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, acme, "2024-01-10", "buy", 50, "10.00")
    held = held_trades(pf, acme)

    assert queries.balance_after(held) == Decimal("50")  # the ledger alone: 50 bought

    oversell = build_trade(acme, "2024-02-10", "sell", 60, "12.00")
    assert queries.balance_after(held, oversell) is None  # 50 − 60 = −10


def test_selling_exactly_the_units_held_lands_on_zero_rather_than_none(pf):
    """Zero is a legal balance. Callers must test `is None`, not truthiness —
    `Decimal(0)` is falsy, so an `if not balance` check would refuse every
    complete exit."""
    acme = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, acme, "2024-01-10", "buy", 100, "10.00", brokerage="10.00")

    exit_all = build_trade(acme, "2024-06-10", "sell", 100, "12.00", brokerage="10.00")
    balance = queries.balance_after(held_trades(pf, acme), exit_all)

    assert balance is not None
    assert balance == ZERO  # 100 bought − 100 sold = 0 units


def test_selling_one_unit_more_than_is_held_is_refused(pf):
    acme = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, acme, "2024-01-10", "buy", 100, "10.00", brokerage="10.00")

    one_too_many = build_trade(acme, "2024-06-10", "sell", 101, "12.00")
    assert queries.balance_after(held_trades(pf, acme), one_too_many) is None  # 100 − 101 = −1


def test_a_sell_as_the_very_first_trade_is_refused(pf):
    """Nothing held means nothing to sell — no special-casing of the empty
    ledger."""
    acme = make_instrument(pf, "ACME", asset_class="share")

    assert queries.balance_after([]) == ZERO  # no trades = no units
    first_ever = build_trade(acme, "2024-01-10", "sell", 10, "12.00")
    assert queries.balance_after([], first_ever) is None  # 0 − 10 = −10


# --------------------------------------------------------------------------- #
# DRP units are acquisitions
# --------------------------------------------------------------------------- #

def test_drp_units_count_as_acquisitions(pf):
    """A reinvested distribution adds real units, so it raises the ceiling on
    what can be sold — even though no cash was ever paid for them."""
    widget = make_instrument(pf, "WIDGET", asset_class="etf", drp=True)
    add_trade(pf, widget, "2024-01-10", "buy", 100, "10.00", brokerage="10.00")
    add_drp(pf, widget, "2024-07-01", "50.00", 5, "10.00")  # $50 distribution → 5 units
    held = held_trades(pf, widget)

    to_the_unit = build_trade(widget, "2025-01-10", "sell", 105, "11.00")
    assert queries.balance_after(held, to_the_unit) == ZERO  # 100 + 5 − 105 = 0

    one_over = build_trade(widget, "2025-01-10", "sell", 106, "11.00")
    assert queries.balance_after(held, one_over) is None  # 100 + 5 − 106 = −1


# --------------------------------------------------------------------------- #
# Same-date ties
# --------------------------------------------------------------------------- #

def test_trades_on_the_same_date_are_walked_in_id_order(pf):
    """Two saved trades sharing a date: the one recorded first is walked first,
    which is what makes a same-day round trip legal."""
    nova = make_instrument(pf, "NOVA", asset_class="share")
    buy = add_trade(pf, nova, "2024-03-10", "buy", 100, "10.00", brokerage="10.00")
    sell = add_trade(pf, nova, "2024-03-10", "sell", 100, "12.00", brokerage="10.00")

    assert buy.id < sell.id  # the tie-break the ordering leans on

    # Passed worst-order-first on purpose: the function sorts, the caller doesn't.
    assert queries.balance_after([sell, buy]) == ZERO  # +100 then −100 = 0


def test_a_same_day_sell_recorded_before_its_buy_is_refused(pf):
    """The flip side of id ordering. With both rows on one date the insertion
    order is the only evidence of what happened first, so a sell recorded ahead
    of the buy that covers it reads as an oversell — record them in order."""
    nova = make_instrument(pf, "NOVA", asset_class="share")
    sell = add_trade(pf, nova, "2024-03-10", "sell", 100, "12.00")
    buy = add_trade(pf, nova, "2024-03-10", "buy", 100, "10.00")

    assert sell.id < buy.id
    assert queries.balance_after([buy, sell]) is None  # −100 first, then +100


def test_a_same_day_sell_entered_after_a_saved_buy_is_allowed(pf):
    """The realistic same-day round trip: buy recorded in the morning, sell
    recorded in the afternoon, both dated today.

    The case that makes trade times necessary: an unsaved candidate has
    `id None`, so a naive `t.id or 0` sorts it before every saved trade sharing
    its date and the sell is checked as though it preceded its own buy. See
    tests/test_trade_time.py for the ordering rules.
    """
    nova = make_instrument(pf, "NOVA", asset_class="share")
    add_trade(pf, nova, "2024-03-10", "buy", 100, "10.00", brokerage="10.00")

    # The buy is already in the ledger, so this sell can only have come after it.
    sell = build_trade(nova, "2024-03-10", "sell", 100, "12.00", brokerage="10.00")
    assert queries.balance_after(held_trades(pf, nova), sell) == ZERO  # 100 − 100 = 0


def test_the_fy_report_matches_a_same_day_buy_then_sell(pf):
    """Context for the test above: the FIFO matcher this guard exists to
    protect has no trouble with a same-day round trip, so refusing one at the
    form was a false alarm rather than a save."""
    nova = make_instrument(pf, "NOVA", asset_class="share")
    add_trade(pf, nova, "2024-03-10", "buy", 100, "10.00", brokerage="10.00")
    add_trade(pf, nova, "2024-03-10", "sell", 100, "12.00", brokerage="10.00")

    disposals = fyreport.instrument_disposals(nova)

    assert [d.quantity for d in disposals] == [Decimal("100")]


# --------------------------------------------------------------------------- #
# The valid case returns a balance
# --------------------------------------------------------------------------- #

def test_returns_the_final_unit_balance_for_a_valid_timeline(pf):
    widget = make_instrument(pf, "WIDGET", asset_class="etf", drp=True)
    add_trade(pf, widget, "2024-01-10", "buy", 100, "10.00", brokerage="10.00")
    add_drp(pf, widget, "2024-07-01", "50.00", 5, "10.00")
    add_trade(pf, widget, "2024-08-01", "sell", 30, "12.00", brokerage="10.00")
    add_trade(pf, widget, "2024-09-01", "buy", 20, "11.00", brokerage="10.00")

    # 100 → 105 → 75 → 95, never negative.
    assert queries.balance_after(held_trades(pf, widget)) == Decimal("95")


def test_a_dip_to_zero_mid_timeline_is_not_a_breach(pf):
    """Fully exiting and buying back in is ordinary behaviour — only a NEGATIVE
    balance is a breach, and zero is not negative."""
    betax = make_instrument(pf, "BETAX", asset_class="share")
    add_trade(pf, betax, "2024-01-10", "buy", 100, "5.00", brokerage="10.00")
    add_trade(pf, betax, "2024-03-10", "sell", 100, "6.00", brokerage="10.00")
    add_trade(pf, betax, "2024-09-10", "buy", 40, "4.00", brokerage="10.00")

    # 100 → 0 → 40.
    assert queries.balance_after(held_trades(pf, betax)) == Decimal("40")


# --------------------------------------------------------------------------- #
# The consequence: the guard and the FIFO matcher agree
# --------------------------------------------------------------------------- #

def test_a_timeline_the_guard_refuses_would_break_the_fy_report(pf):
    """Why the guard exists. The rows are written straight through the factories
    here (which have no guard), producing exactly the database state the form is
    meant to keep out — and the FY report then cannot be built at all."""
    acme = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, acme, "2024-01-10", "buy", 50, "10.00", brokerage="10.00")
    add_trade(pf, acme, "2024-02-10", "sell", 100, "12.00", brokerage="10.00")
    add_trade(pf, acme, "2024-03-10", "buy", 100, "10.00", brokerage="10.00")

    # 50 − 100 = −50 on 2024-02-10, so the guard refuses the timeline...
    assert queries.balance_after(held_trades(pf, acme)) is None

    # ...and FIFO can only find 50 of the 100 units that sell claims to dispose.
    with pytest.raises(ValueError, match="exceeds held parcels"):
        fyreport.instrument_disposals(acme)


def test_a_timeline_the_guard_accepts_keeps_the_fy_report_building(pf):
    """The other half of the agreement: what the guard lets through, the FIFO
    matcher can process."""
    acme = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, acme, "2024-01-10", "buy", 50, "10.00", brokerage="10.00")
    add_trade(pf, acme, "2024-03-10", "buy", 100, "10.00", brokerage="10.00")
    add_trade(pf, acme, "2024-04-10", "sell", 100, "12.00", brokerage="10.00")

    assert queries.balance_after(held_trades(pf, acme)) == Decimal("50")  # 50 + 100 − 100

    disposals = fyreport.instrument_disposals(acme)

    assert len(disposals) == 1
    # FIFO consumes the January parcel whole (50) then 50 of the March one.
    assert [p.quantity for p in disposals[0].parcels] == [Decimal("50"), Decimal("50")]
