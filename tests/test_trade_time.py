"""Intra-day trade ordering.

A trade carries an OPTIONAL time. Left blank it is treated as **10:00, market
open**, so nothing about a trade recorded before times existed changes meaning,
and the field only appears when someone ticks the box to set one. Ordering
within a day is (date, time, id), with an unsaved trade — the one being entered
right now — sorting after every saved trade sharing its date and time.

That last rule is the whole point. Before it, an unsaved candidate had `id
None`, `t.id or 0` sorted it before everything, and selling on the day you
bought was refused because the sell was checked as though it preceded its own
buy.

The tests below are split deliberately:

  * the first group is the behaviour this bought (written as `xfail(strict)`
    before it existed, in the session that agreed the design — 2026-08-02);
  * the second is the invariants that held before and must still hold. They are
    the guard rail: it would be easy to "fix" same-day selling by dropping the
    balance check altogether, and these would catch it.
"""

from __future__ import annotations

import datetime as dt

import pytest

import factories as fac
from app import fyreport, queries

MARKET_OPEN = dt.time(10, 0)


@pytest.fixture
def acme(pf):
    return fac.make_instrument(pf, "ACME", name="Acme Industries")


# --------------------------------------------------------------------------- #
# What times bought
# --------------------------------------------------------------------------- #

def test_selling_later_the_same_day_is_allowed(pf, acme):
    """Buying at open and selling that afternoon is an ordinary thing to do,
    and the ledger has no reason to refuse it."""
    buy = fac.add_trade(pf, acme, "2026-03-02", "buy", 100, "5.00")
    # The sell happens after the buy on the same day; once trades carry a time
    # this orders correctly and the balance lands on zero.
    sell = fac.build_trade(acme, "2026-03-02", "sell", 100, "6.00")

    assert queries.balance_after([buy], sell) == 0


def test_a_trade_left_without_a_time_is_treated_as_market_open(pf, acme):
    """Blank means 10:00 rather than midnight, so an unspecified sell still
    lands after an unspecified buy's parcel rather than tying with it."""
    trade = fac.add_trade(pf, acme, "2026-03-02", "buy", 100, "5.00")

    assert trade.time == MARKET_OPEN


def test_explicit_times_order_the_day_regardless_of_entry_order(pf, acme):
    """Someone entering a day's trades out of order must still get the right
    running balance — the time is the truth, not the order they typed them."""
    # Recorded second but happened first.
    sell = fac.add_trade(pf, acme, "2026-03-02", "sell", 60, "6.00",
                         time=dt.time(15, 0))
    buy = fac.add_trade(pf, acme, "2026-03-02", "buy", 100, "5.00",
                        time=dt.time(11, 0))

    assert queries.balance_after([sell, buy]) == 40


def test_fifo_consumes_the_earlier_parcel_of_the_same_day_first(pf, acme):
    """Two parcels bought the same day at different prices must still be
    consumed oldest-first, or the cost base of a later sale is wrong."""
    fac.add_trade(pf, acme, "2026-03-02", "buy", 100, "5.00", time=dt.time(10, 0))
    fac.add_trade(pf, acme, "2026-03-02", "buy", 100, "8.00", time=dt.time(14, 0))
    fac.add_trade(pf, acme, "2027-06-01", "sell", 100, "9.00")

    disposal = fyreport.instrument_disposals(acme)[0]

    # The 10:00 parcel at 5.00 is the one consumed: 100 x 5.00 = 500.00 cost base.
    assert disposal.parcels[0].cost_base == 500


# --------------------------------------------------------------------------- #
# What must stay true afterwards
# --------------------------------------------------------------------------- #

def test_selling_before_buying_on_the_same_day_is_still_refused(pf, acme):
    """The obvious wrong way to fix same-day selling is to stop checking. A
    sell that genuinely precedes its buy must still be refused, times or not."""
    buy = fac.add_trade(pf, acme, "2026-03-02", "buy", 100, "5.00")
    # No units held when this sell happens, whichever way the day is ordered.
    early_sell = fac.build_trade(acme, "2026-03-01", "sell", 100, "6.00")

    assert queries.balance_after([buy], early_sell) is None


def test_selling_more_than_was_ever_held_is_still_refused(pf, acme):
    buy = fac.add_trade(pf, acme, "2026-03-02", "buy", 100, "5.00")
    too_big = fac.build_trade(acme, "2026-03-03", "sell", 101, "6.00")

    assert queries.balance_after([buy], too_big) is None


def test_a_backdated_sell_is_still_refused(pf, acme):
    """The running balance is walked in date order precisely so a sell that
    only balances thanks to units bought later is caught."""
    early = fac.add_trade(pf, acme, "2026-01-05", "buy", 10, "5.00")
    later = fac.add_trade(pf, acme, "2026-06-05", "buy", 100, "5.00")
    backdated = fac.build_trade(acme, "2026-02-01", "sell", 50, "6.00")

    assert queries.balance_after([early, later], backdated) is None


def test_ordinary_multi_day_trading_is_unaffected(pf, acme):
    buy = fac.add_trade(pf, acme, "2026-03-02", "buy", 100, "5.00")
    sell = fac.build_trade(acme, "2026-03-03", "sell", 40, "6.00")

    assert queries.balance_after([buy], sell) == 60


def test_trades_sharing_a_day_and_a_time_keep_their_recorded_order(pf, acme):
    """With no time set, both sit at market open, so the tie has to break on
    the id — the order they were recorded in."""
    first = fac.add_trade(pf, acme, "2026-03-02", "buy", 100, "5.00")
    second = fac.add_trade(pf, acme, "2026-03-02", "buy", 50, "6.00")

    assert queries.balance_after([second, first]) == 150
