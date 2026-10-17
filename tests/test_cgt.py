"""The AU capital-gains engine: FIFO parcel matching, the 12-month discount
boundary, loss ordering inside a financial year, and the FY date helpers.

Every expected figure is worked out by hand in the comment beside it. The
scenarios are deliberately tiny — a three-trade instrument whose arithmetic
fits on one line is a far better regression test than a realistic one whose
"expected" value could only ever be obtained by running the code.

The reference portfolio appears here too, but only where its FY2024 figures add
something the small scenarios can't: the loss-ordering counterfactual, and a
disposal whose parcels straddle the discount boundary.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import fixture_portfolio as ref
from app import fyreport
from app.models import Instrument
from factories import add_dividend, add_drp, add_prices, add_trade, make_instrument


def _disposals(session, inst):
    """FIFO disposals for `inst`, with its trades freshly loaded.

    Local to this file on purpose (the shared helpers are off limits): the
    factories attach trades by foreign key and never touch
    `instrument.trades`, so the collection has to be re-read before the engine
    walks it. This is the same selectinload `fy_cgt` does for itself.
    """
    return fyreport.instrument_disposals(
        session.scalars(
            select(Instrument)
            .where(Instrument.id == inst.id)
            .options(selectinload(Instrument.trades))
        ).one()
    )


def _two_parcels(session):
    """100 units at $2.00 (2020) then 100 at $3.00 (2021).

    No brokerage, so every unit cost is a round number and the FIFO split can be
    read straight off the trades.
    """
    inst = make_instrument(session, "ACME", asset_class="share")
    add_trade(session, inst, "2020-01-01", "buy", 100, "2.00")
    add_trade(session, inst, "2021-01-01", "buy", 100, "3.00")
    return inst


# --------------------------------------------------------------------------- #
# FIFO parcel matching
# --------------------------------------------------------------------------- #

def test_fifo_consumes_the_oldest_parcel_first(pf):
    inst = _two_parcels(pf)
    add_trade(pf, inst, "2023-01-01", "sell", 150, "5.00")

    (disposal,) = _disposals(pf, inst)

    # 150 units: all 100 of the 2020 parcel, then 50 of the 2021 one.
    assert [p.acquired for p in disposal.parcels] == [
        dt.date(2020, 1, 1), dt.date(2021, 1, 1),
    ]
    assert [p.quantity for p in disposal.parcels] == [100, 50]
    assert disposal.cost_base == Decimal("350.00")   # 100 x 2.00 + 50 x 3.00
    assert disposal.proceeds == Decimal("750.00")    # 150 x 5.00, no brokerage
    assert disposal.gain == Decimal("400.00")        # 750.00 - 350.00


def test_a_part_used_parcel_leaves_its_remainder_for_a_later_sell(pf):
    inst = _two_parcels(pf)
    add_trade(pf, inst, "2023-01-01", "sell", 150, "5.00")
    add_trade(pf, inst, "2023-06-01", "sell", 50, "6.00")

    first, second = _disposals(pf, inst)

    # The first sell used 50 of the 100-unit 2021 parcel; 50 units at $3.00 are
    # still sitting there for the second.
    assert first.parcels[-1].quantity == 50
    assert [p.acquired for p in second.parcels] == [dt.date(2021, 1, 1)]
    assert second.parcels[0].quantity == 50
    assert second.cost_base == Decimal("150.00")   # 50 x 3.00
    assert second.proceeds == Decimal("300.00")    # 50 x 6.00
    assert second.gain == Decimal("150.00")        # 300.00 - 150.00


def test_buy_brokerage_joins_the_cost_base_and_sell_brokerage_leaves_the_proceeds(pf):
    inst = make_instrument(pf, "WIDGET", asset_class="share")
    add_trade(pf, inst, "2022-01-01", "buy", 100, "5.00", brokerage="10.00")
    add_trade(pf, inst, "2024-01-02", "sell", 100, "8.00", brokerage="20.00")

    (disposal,) = _disposals(pf, inst)

    assert disposal.cost_base == Decimal("510.00")   # 100 x 5.00 + 10.00 = 5.10 a unit
    assert disposal.proceeds == Decimal("780.00")    # 100 x 8.00 - 20.00 = 7.80 a unit
    assert disposal.gain == Decimal("270.00")        # 780.00 - 510.00
    # Without brokerage on both sides the gain would read 300.00 — the $30 of
    # fees is exactly what the two adjustments are worth.
    assert disposal.gain != Decimal("300.00")


def test_a_drp_allocation_is_an_acquisition_parcel_like_any_buy(pf):
    inst = make_instrument(pf, "NOVA", asset_class="etf", drp=True)
    add_trade(pf, inst, "2022-01-01", "buy", 100, "10.00")
    # A $60 distribution taken as 5 units at $12.00.
    add_drp(pf, inst, "2022-07-01", "60.00", 5, "12.00")
    add_trade(pf, inst, "2024-01-02", "sell", 102, "20.00")

    (disposal,) = _disposals(pf, inst)

    bought, reinvested = disposal.parcels
    assert bought.acquired == dt.date(2022, 1, 1)
    assert reinvested.acquired == dt.date(2022, 7, 1)
    assert reinvested.quantity == 2                    # 102 sold - 100 in the buy parcel
    # Reinvested units are free of cash outlay but NOT free for CGT: the $12.00
    # a unit the distribution bought them at is their cost base.
    assert reinvested.cost_base == Decimal("24.00")    # 2 x 12.00
    assert reinvested.proceeds == Decimal("40.00")     # 2 x 20.00
    assert disposal.cost_base == Decimal("1024.00")    # 100 x 10.00 + 24.00


def test_selling_more_than_is_held_raises_naming_the_ticker(pf):
    inst = make_instrument(pf, "QUARK", asset_class="share")
    add_trade(pf, inst, "2023-01-01", "buy", 100, "1.00")
    add_trade(pf, inst, "2024-01-01", "sell", 150, "2.00")

    # Silently matching only what it can find would understate the gain, so the
    # engine refuses rather than guessing at the missing parcel.
    with pytest.raises(ValueError, match="QUARK"):
        _disposals(pf, inst)


def test_a_year_that_depends_on_a_sale_of_more_than_was_held_is_refused(pf):
    """Every way of writing a trade refuses such a sale, but a ledger from
    before they did can hold one. Its gain has no parcels to match, and a
    year's gains without it are not the year's gains, so the year has none:
    the sale is named for somebody to fix (decisions.md #5)."""
    quark = make_instrument(pf, "QUARK", asset_class="share")
    add_trade(pf, quark, "2023-01-01", "buy", 100, "1.00")
    add_trade(pf, quark, "2024-01-01", "sell", 150, "2.00")   # 50 more than held
    acme = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, acme, "2022-01-01", "buy", 100, "10.00")
    add_trade(pf, acme, "2024-03-01", "sell", 100, "15.00")   # a gain of its own

    cgt = fyreport.fy_cgt(pf, 2024)

    (sale,) = cgt.oversold
    assert (sale.instrument.ticker, sale.trade.date, sale.held) == (
        "QUARK", dt.date(2024, 1, 1), Decimal("100"))
    assert cgt.disposals == []
    assert (cgt.net_capital_gain, cgt.gains_discountable, cgt.losses) == (0, 0, 0)


def test_a_sale_of_more_than_was_held_is_named_in_the_years_that_depend_on_it(pf):
    quark = make_instrument(pf, "QUARK", asset_class="share")
    add_trade(pf, quark, "2020-01-01", "buy", 100, "1.00")
    add_trade(pf, quark, "2021-03-01", "sell", 50, "2.00")    # FY2021, matched
    add_trade(pf, quark, "2024-01-01", "sell", 100, "2.00")   # FY2024, only 50 held
    add_trade(pf, quark, "2025-01-01", "buy", 100, "3.00")    # FY2025, nothing sold
    add_trade(pf, quark, "2026-01-01", "sell", 10, "4.00")    # FY2026

    def named(fy):
        return [sale.trade.date for sale in fyreport.fy_cgt(pf, fy).oversold]

    # A year before it is matched as usual. Every year after it depends on
    # it: a later sale's parcels on what the oversold one used up, and any
    # year, one that sold nothing included, on the loss it would have carried.
    assert [d.date for d in fyreport.fy_cgt(pf, 2021).disposals] == [dt.date(2021, 3, 1)]
    assert named(2021) == []
    assert named(2024) == [dt.date(2024, 1, 1)]
    assert named(2025) == [dt.date(2024, 1, 1)]
    assert named(2026) == [dt.date(2024, 1, 1)]


def test_parcel_proceeds_sum_to_the_net_sale_proceeds(pf):
    """What the ATO schedule reports as proceeds has to be what was received."""
    inst = make_instrument(pf, "PIXEL", asset_class="share")
    for day in ("2020-01-01", "2020-01-02", "2020-01-03"):
        add_trade(pf, inst, day, "buy", 1, "1.00")
    # 3 x 5.00 less 1.00 brokerage = 14.00 received, i.e. 4.6666... a unit.
    add_trade(pf, inst, "2024-01-02", "sell", 3, "5.00", brokerage="1.00")

    (disposal,) = _disposals(pf, inst)

    # Per-unit-then-round gave each one-unit parcel 4.67 and reported 14.01
    # against 14.00 of money. Both halves are asserted: the header figure is
    # what was received, and the rows under it add up to it — a schedule whose
    # lines do not sum to its total is exactly what an accountant queries.
    assert disposal.proceeds == Decimal("14.00")
    assert sum(p.proceeds for p in disposal.parcels) == Decimal("14.00")
    # The leftover cent lands on a parcel rather than on nobody.
    assert sorted(p.proceeds for p in disposal.parcels) == [
        Decimal("4.66"), Decimal("4.67"), Decimal("4.67"),
    ]


def test_parcel_cost_bases_sum_to_what_was_paid_for_the_parcel(pf):
    """The same rounding fault, one column across, and just as visible.

    A parcel bought for 10.00 and sold a unit at a time reports a 3.33
    cost base three times — 9.99 against money that was 10.00. A cost base a
    cent light is a gain a cent heavy, which is the direction that costs you.
    """
    inst = make_instrument(pf, "PRISM", asset_class="share")
    # 3 units at 3.00 plus 1.00 brokerage = 10.00 paid, i.e. 3.3333... a unit.
    add_trade(pf, inst, "2020-01-01", "buy", 3, "3.00", brokerage="1.00")
    for day in ("2024-01-02", "2024-01-03", "2024-01-04"):
        add_trade(pf, inst, day, "sell", 1, "5.00")

    disposals = _disposals(pf, inst)

    spent = sum(p.cost_base for d in disposals for p in d.parcels)
    assert spent == Decimal("10.00")


# --------------------------------------------------------------------------- #
# The 12-month discount boundary
# --------------------------------------------------------------------------- #

def test_one_year_after_is_the_same_day_of_the_next_year():
    assert fyreport._one_year_after(dt.date(2023, 6, 15)) == dt.date(2024, 6, 15)


def test_one_year_after_a_leap_day_lands_on_28_february():
    """29 February has no anniversary, so the engine uses 28 February.

    Paired with the strict ">" test that makes 1 March the first discountable
    day — 366 days held, the same count every other acquisition date needs.
    """
    assert fyreport._one_year_after(dt.date(2024, 2, 29)) == dt.date(2025, 2, 28)


def test_a_sale_exactly_one_year_after_acquisition_is_not_discountable(pf):
    """The discount wants MORE than 12 months, so the anniversary itself misses."""
    inst = make_instrument(pf, "ORBIT", asset_class="share")
    add_trade(pf, inst, "2023-06-15", "buy", 100, "1.00")
    add_trade(pf, inst, "2024-06-15", "sell", 100, "2.00")   # 366 days would be the 16th

    (disposal,) = _disposals(pf, inst)

    assert disposal.parcels[0].discountable is False


def test_a_sale_one_year_and_a_day_after_acquisition_is_discountable(pf):
    inst = make_instrument(pf, "ORBIT", asset_class="share")
    add_trade(pf, inst, "2023-06-15", "buy", 100, "1.00")
    add_trade(pf, inst, "2024-06-16", "sell", 100, "2.00")

    (disposal,) = _disposals(pf, inst)

    assert disposal.parcels[0].discountable is True


def test_a_leap_day_acquisition_becomes_discountable_on_1_march(pf):
    inst = make_instrument(pf, "HELIX", asset_class="share")
    add_trade(pf, inst, "2024-02-29", "buy", 200, "1.00")
    add_trade(pf, inst, "2025-02-28", "sell", 100, "2.00")   # 365 days held
    add_trade(pf, inst, "2025-03-01", "sell", 100, "2.00")   # 366 days held

    on_28_feb, on_1_mar = _disposals(pf, inst)

    assert on_28_feb.parcels[0].discountable is False
    assert on_1_mar.parcels[0].discountable is True


# --------------------------------------------------------------------------- #
# fy_cgt: window, discount, loss ordering
# --------------------------------------------------------------------------- #

def test_only_disposals_inside_the_fy_window_count(pf):
    inst = make_instrument(pf, "NOVA", asset_class="share")
    add_trade(pf, inst, "2020-01-01", "buy", 300, "1.00")
    add_trade(pf, inst, "2023-06-30", "sell", 100, "2.00")   # last day of FY2023
    add_trade(pf, inst, "2023-07-01", "sell", 100, "2.00")   # first day of FY2024
    add_trade(pf, inst, "2024-06-30", "sell", 100, "2.00")   # last day of FY2024

    # Both ends of the window are inclusive, and a FY with no sells is empty
    # even though the instrument still has parcels sitting in it.
    assert [d.date for d in fyreport.fy_cgt(pf, 2023).disposals] == [dt.date(2023, 6, 30)]
    assert [d.date for d in fyreport.fy_cgt(pf, 2024).disposals] == [
        dt.date(2023, 7, 1), dt.date(2024, 6, 30),
    ]
    assert fyreport.fy_cgt(pf, 2025).disposals == []


def test_the_fifty_percent_discount_halves_the_surviving_discountable_gain(pf):
    inst = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, inst, "2022-01-01", "buy", 100, "10.00")
    add_trade(pf, inst, "2024-03-01", "sell", 100, "15.00")

    cgt = fyreport.fy_cgt(pf, 2024)

    assert cgt.gains_discountable == Decimal("500.00")   # 1500.00 - 1000.00, held 2 years
    assert cgt.gains_other == 0
    assert cgt.discount == Decimal("250.00")             # 500.00 / 2
    assert cgt.net_capital_gain == Decimal("250.00")     # 500.00 - 250.00


def test_losses_offset_non_discountable_gains_before_discountable_ones(pf):
    # Two NOVA parcels — one long held, one fresh — sold a day apart so FIFO
    # matches each sell to exactly one of them.
    nova = make_instrument(pf, "NOVA", asset_class="share")
    add_trade(pf, nova, "2022-01-01", "buy", 100, "1.00")
    add_trade(pf, nova, "2024-01-10", "buy", 100, "1.00")
    add_trade(pf, nova, "2024-03-01", "sell", 100, "5.00")   # 500 - 100 = 400 gain, >12m
    add_trade(pf, nova, "2024-03-02", "sell", 100, "2.00")   # 200 - 100 = 100 gain, <12m

    vertex = make_instrument(pf, "VERTEX", asset_class="share")
    add_trade(pf, vertex, "2023-05-01", "buy", 100, "3.00")
    add_trade(pf, vertex, "2024-05-01", "sell", 100, "2.00")  # 200 - 300 = 100 loss

    cgt = fyreport.fy_cgt(pf, 2024)

    assert cgt.gains_discountable == Decimal("400.00")
    assert cgt.gains_other == Decimal("100.00")
    assert cgt.losses == Decimal("100.00")
    # The whole loss lands on the undiscounted gain, where a dollar of loss
    # cancels a dollar of tax instead of only fifty cents of it.
    assert cgt.losses_applied_other == Decimal("100.00")
    assert cgt.losses_applied_discountable == 0
    assert cgt.discount == Decimal("200.00")            # 400.00 / 2, nothing taken off it
    assert cgt.net_capital_gain == Decimal("200.00")    # (100-100) + 400 - 200

    # The other order would spend the loss on the discountable gain: 400 - 100
    # = 300 survives, discount 150, and the 100 non-discountable gain stands —
    # 100 + 300 - 150 = 250.00, i.e. 50.00 more assessable income.
    assert cgt.net_capital_gain != Decimal("250.00")


@freeze_time(ref.TODAY)
def test_reference_fy2024_applies_losses_in_the_taxpayer_favourable_order(pf):
    ref.build_reference(pf)

    cgt = fyreport.fy_cgt(pf, ref.FY2024)

    # ZULU's 220.00 loss: BETAX's undiscounted 91.67 goes first, then the
    # remaining 128.33 reduces the discountable 283.33 to 155.00.
    assert cgt.losses_applied_other == ref.FY2024_GAINS_OTHER
    assert cgt.losses_applied_discountable == Decimal("128.33")   # 220.00 - 91.67
    assert cgt.net_capital_gain == ref.FY2024_NET_CAPITAL_GAIN
    assert cgt.net_capital_gain != ref.FY2024_NET_IF_LOSSES_APPLIED_TO_DISCOUNTABLE_FIRST
    assert cgt.discount != ref.FY2024_DISCOUNT_IF_LOSSES_APPLIED_TO_DISCOUNTABLE_FIRST


@freeze_time(ref.TODAY)
def test_reference_fy2024_disposals_carry_hand_computed_parcels(pf):
    ref.build_reference(pf)

    disposals = fyreport.fy_cgt(pf, ref.FY2024).disposals
    assert [d.instrument.ticker for d in disposals] == ["BETAX", "ZULU"]   # date order
    betax, zulu = disposals

    # BETAX sold 150 at 8.00 less 10.00 brokerage = 1190.00, i.e. 7.93333... a unit.
    old, new = betax.parcels
    assert old.acquired == dt.date(2023, 1, 10)
    assert old.quantity == 100
    assert old.cost_base == Decimal("510.00")    # 100 x (100x5.00 + 10.00)/100 = 5.10
    assert old.proceeds == Decimal("793.33")     # 100 x 7.93333...
    assert old.discountable is True              # sold 2024-06-10 > 2024-01-10
    assert new.acquired == dt.date(2023, 9, 10)
    assert new.quantity == 50
    assert new.cost_base == Decimal("305.00")    # 50 x (100x6.00 + 10.00)/100 = 6.10
    assert new.proceeds == Decimal("396.67")     # 50 x 7.93333...
    assert new.discountable is False             # sold 2024-06-10 < 2024-09-10

    # One parcel straddling nothing: ZULU sold out at 200 x 9.00 - 10.00 = 1790.00
    # against a 200 x 10.05 = 2010.00 cost base.
    (parcel,) = zulu.parcels
    assert parcel.cost_base == Decimal("2010.00")
    assert zulu.proceeds == Decimal("1790.00")
    assert zulu.gain == Decimal("-220.00")


def test_a_fy_netting_to_a_loss_reports_net_capital_loss_and_no_gain(pf):
    winner = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, winner, "2022-01-01", "buy", 100, "1.00")
    add_trade(pf, winner, "2024-03-01", "sell", 100, "2.00")   # 200 - 100 = 100 gain, >12m

    loser = make_instrument(pf, "WIDGET", asset_class="share")
    add_trade(pf, loser, "2023-01-01", "buy", 100, "5.00")
    add_trade(pf, loser, "2024-03-01", "sell", 100, "2.00")    # 200 - 500 = 300 loss

    cgt = fyreport.fy_cgt(pf, 2024)

    # 300.00 of losses swallows the 100.00 gain whole and 200.00 is left over.
    # A carried-forward loss is never discounted, so the discount is nil rather
    # than half of anything.
    assert cgt.discount == 0
    assert cgt.net_capital_gain == 0
    assert cgt.net_capital_loss == Decimal("200.00")   # 300.00 - 100.00


def _sale(pf, ticker: str, bought: str, sold: str, cost: str, price: str):
    """100 units bought on `bought` at `cost` and sold on `sold` at `price`."""
    inst = make_instrument(pf, ticker, asset_class="share")
    add_trade(pf, inst, bought, "buy", 100, cost)
    add_trade(pf, inst, sold, "sell", 100, price)
    return inst


def test_an_earlier_years_loss_is_applied_before_the_discount(pf):
    """s102-5(1): this year's losses, then earlier years' net capital losses,
    then the discount. A FY2024 loss of 300 left FY2025's discountable gain of
    300 standing at 150; the net capital gain is nil."""
    _sale(pf, "WIDGET", "2023-01-02", "2024-03-01", "5.00", "2.00")    # FY2024: 300 loss
    _sale(pf, "ACME", "2022-01-03", "2024-12-02", "1.00", "4.00")      # FY2025: 300 gain, >12m

    earlier = fyreport.fy_cgt(pf, 2024)
    later = fyreport.fy_cgt(pf, 2025)

    assert (earlier.net_capital_loss, earlier.losses_carried_out) == (300, 300)
    assert (later.losses, later.losses_carried_in, later.losses_carried_applied) == (0, 300, 300)
    assert (later.discount, later.net_capital_gain, later.losses_carried_out) == (0, 0, 0)


def test_what_an_earlier_loss_does_not_use_carries_on(pf):
    _sale(pf, "WIDGET", "2023-01-02", "2024-03-01", "5.00", "4.00")    # FY2024: 100 loss
    _sale(pf, "ACME", "2022-01-03", "2024-12-02", "1.00", "4.00")      # FY2025: 300 gain

    later = fyreport.fy_cgt(pf, 2025)

    # 300 − 100 carried = 200, halved by the discount.
    assert (later.losses_carried_applied, later.discount, later.net_capital_gain) == (
        100, 100, 100)
    assert later.losses_carried_out == 0


def test_an_earlier_loss_goes_to_the_gains_with_no_discount_first(pf):
    """The taxpayer-favourable order, as for this year's own losses: a dollar
    of loss on an undiscounted gain saves a dollar, on a discounted one fifty
    cents."""
    _sale(pf, "WIDGET", "2023-01-02", "2024-03-01", "5.00", "4.00")    # FY2024: 100 loss
    _sale(pf, "ACME", "2022-01-03", "2024-12-02", "1.00", "4.00")      # FY2025: 300, >12m
    _sale(pf, "NOVA", "2024-09-02", "2025-02-03", "1.00", "1.50")      # FY2025: 50, <12m

    later = fyreport.fy_cgt(pf, 2025)

    # 50 of the 100 cancels the 50 with no discount; 50 comes off the 300,
    # and the 250 left is halved: 125.
    assert (later.losses_carried_applied, later.discount, later.net_capital_gain) == (
        100, 125, 125)


def test_this_years_losses_come_off_before_an_earlier_years(pf):
    _sale(pf, "WIDGET", "2023-01-02", "2024-03-01", "5.00", "4.00")    # FY2024: 100 loss
    _sale(pf, "ACME", "2022-01-03", "2024-12-02", "1.00", "4.00")      # FY2025: 300 gain
    _sale(pf, "GAMMA", "2024-08-01", "2025-01-06", "3.00", "2.00")     # FY2025: 100 loss

    later = fyreport.fy_cgt(pf, 2025)

    assert (later.losses_applied_discountable, later.losses_carried_applied) == (100, 100)
    assert later.net_capital_gain == 50     # (300 − 100 − 100) / 2


def test_a_years_own_net_loss_is_what_its_losses_leave_after_its_gains(pf):
    """This year's losses meet its gains before an earlier year's do, so the
    year's own net capital loss is what is left of them, and the earlier loss
    carries on untouched beside it."""
    _sale(pf, "WIDGET", "2023-01-02", "2024-03-01", "5.00", "4.50")    # FY2024:  50 loss
    _sale(pf, "ACME", "2022-01-03", "2024-12-02", "1.00", "2.00")      # FY2025: 100 gain
    _sale(pf, "GAMMA", "2024-08-01", "2025-01-06", "3.00", "1.50")     # FY2025: 150 loss

    later = fyreport.fy_cgt(pf, 2025)

    assert (later.net_capital_loss, later.losses_carried_applied,
            later.losses_carried_out) == (50, 0, 100)


def test_a_year_that_nets_to_a_loss_carries_it_with_the_earlier_ones(pf):
    """Nothing was left to set an earlier loss against, so all of it carries
    on, with this year's own."""
    _sale(pf, "WIDGET", "2023-01-02", "2024-03-01", "5.00", "4.00")    # FY2024: 100 loss
    _sale(pf, "GAMMA", "2024-08-01", "2025-01-06", "3.00", "2.50")     # FY2025:  50 loss
    _sale(pf, "ACME", "2022-01-03", "2025-12-01", "1.00", "3.00")      # FY2026: 200 gain

    middle = fyreport.fy_cgt(pf, 2025)
    last = fyreport.fy_cgt(pf, 2026)

    assert (middle.net_capital_loss, middle.losses_carried_in, middle.losses_carried_out) == (
        50, 100, 150)
    assert (last.losses_carried_in, last.net_capital_gain, last.losses_carried_out) == (
        150, 25, 0)


def test_a_year_with_no_sales_passes_an_earlier_loss_on(pf):
    _sale(pf, "WIDGET", "2023-01-02", "2024-03-01", "5.00", "4.00")    # FY2024: 100 loss
    _sale(pf, "ACME", "2022-01-03", "2025-12-01", "1.00", "3.00")      # FY2026: 200 gain

    quiet = fyreport.fy_cgt(pf, 2025)

    assert (quiet.losses_carried_in, quiet.losses_carried_out) == (100, 100)
    assert fyreport.fy_cgt(pf, 2026).net_capital_gain == 50


def test_a_year_that_cannot_be_worked_out_leaves_the_years_after_it_unknown(pf):
    """Whether it made a loss to carry is not known, so neither is any later
    year's net gain: it names the same sale."""
    oversold = make_instrument(pf, "OVER", asset_class="share")
    add_trade(pf, oversold, "2023-01-02", "buy", 100, "5.00")
    add_trade(pf, oversold, "2024-03-01", "sell", 150, "4.00")         # FY2024: refused
    _sale(pf, "ACME", "2022-01-03", "2024-12-02", "1.00", "4.00")      # FY2025: 300 gain

    later = fyreport.fy_cgt(pf, 2025)

    assert [sale.trade.date for sale in later.oversold] == [dt.date(2024, 3, 1)]
    assert (later.disposals, later.net_capital_gain) == ([], 0)


@freeze_time("2026-08-03")
def test_the_fy_pages_and_the_api_show_a_loss_carried_forward(client, session_factory):
    """A loss year read as a gain of nothing, under a green arrow."""
    from test_api import bearer, issue_key

    from test_routes import bind_to_only_portfolio, make_login

    make_login(client, session_factory)
    with session_factory() as s:
        portfolio = bind_to_only_portfolio(s)
        _sale(s, "WIDGET", "2023-01-02", "2024-03-01", "5.00", "2.00")    # FY2024: 300 loss
        _sale(s, "ACME", "2022-01-03", "2024-12-02", "1.00", "4.00")      # FY2025: 300 gain
        s.commit()
        user_id, portfolio_id = portfolio.members[0].user_id, portfolio.id
    html = {"accept": "text/html"}

    loss_year = client.get("/", params={"fy": 2024}, headers=html).text
    gain_year = client.get("/", params={"fy": 2025}, headers=html).text
    api = client.get("/api/v1/fy/2025",
                     headers=bearer(issue_key(session_factory, portfolio_id,
                                              created_by=user_id))).json()["cgt"]

    tile = re.search(r'<div class="tile[^"]*">\s*<span class="label">Net capital loss</span>'
                     r'.*?</div>', loss_year, re.S).group(0)
    assert 'class="tile down"' in tile and "300.00" in tile
    assert "carried forward: 300.00" in tile
    assert re.search(r"<td>Losses from earlier years</td><td class=\"num\">−"
                     r"<span class=\"m\">300\.00</span>", gain_year)
    assert (api["losses_carried_in"], api["losses_carried_applied"],
            api["losses_carried_forward"], api["net_capital_gain"]) == (300, 300, 0, 0)


def test_a_loss_carried_in_names_a_holding_an_earlier_year_left_out(pf):
    """An earlier sale with no rate was left out of that year's figures, and
    so out of the loss it carried."""
    nova = make_instrument(pf, "NOVA", exchange="NASDAQ", asset_class="share",
                           currency="USD")
    add_trade(pf, nova, "2023-01-02", "buy", 100, "5.00", fx_rate=None)
    add_trade(pf, nova, "2024-03-01", "sell", 100, "4.00", fx_rate=None)
    _sale(pf, "ACME", "2022-01-03", "2024-12-02", "1.00", "4.00")

    assert fyreport.fy_cgt(pf, 2025).withheld == ["NOVA"]




def test_a_fy_with_no_disposals_is_all_zeros(pf):
    inst = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, inst, "2023-08-01", "buy", 100, "1.00")

    cgt = fyreport.fy_cgt(pf, 2024)

    assert cgt.disposals == []
    assert (cgt.gains_discountable, cgt.gains_other, cgt.losses) == (0, 0, 0)
    assert cgt.discount == 0
    assert cgt.net_capital_gain == 0
    assert cgt.net_capital_loss == 0


# --------------------------------------------------------------------------- #
# FY date helpers
# --------------------------------------------------------------------------- #

def test_fy_bounds_run_from_1_july_to_30_june():
    assert fyreport.fy_bounds(2026) == (dt.date(2025, 7, 1), dt.date(2026, 6, 30))


def test_fy_label_names_both_calendar_years():
    assert fyreport.fy_label(2026) == "FY25/26"
    assert fyreport.fy_label(2000) == "FY99/00"   # the century rollover keeps two digits


@freeze_time("2026-08-02")
def test_available_fys_is_empty_with_no_trades(pf):
    """Nothing bought yet means no year has anything to report."""
    assert fyreport.available_fys(pf) == []


@freeze_time("2026-08-02")
def test_available_fys_descends_from_the_current_fy_to_the_first_trade_fy(pf):
    inst = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, inst, "2021-01-04", "buy", 100, "1.00")
    add_trade(pf, inst, "2024-05-05", "buy", 100, "1.00")

    # January 2021 falls in FY2021; August 2026 is already in FY2027. Newest
    # first, because that is the one the FY page opens on.
    assert fyreport.available_fys(pf) == [2027, 2026, 2025, 2024, 2023, 2022, 2021]


@freeze_time("2022-03-01")
def test_available_fys_starts_at_the_fy_the_first_trade_falls_in(pf):
    inst = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, inst, "2021-07-01", "buy", 100, "1.00")

    # 1 July 2021 opens FY2022, and March 2022 is still inside it — one year.
    assert fyreport.available_fys(pf) == [2022]


# --------------------------------------------------------------------------- #
# fy_report totals and fy_income
# --------------------------------------------------------------------------- #

@freeze_time("2026-08-02")
def test_fy_report_totals_cover_open_positions_and_in_year_activity(pf):
    acme = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, acme, "2023-08-01", "buy", 100, "10.00", brokerage="10.00")
    add_prices(pf, acme, [("2024-06-28", "12.00")])

    widget = make_instrument(pf, "WIDGET", asset_class="share")
    add_trade(pf, widget, "2023-09-01", "buy", 50, "4.00", brokerage="5.00")
    add_trade(pf, widget, "2024-05-01", "sell", 50, "6.00", brokerage="5.00")

    report = fyreport.fy_report(pf, 2024)

    assert report["label"] == "FY23/24"
    assert (report["start"], report["end"]) == (dt.date(2023, 7, 1), dt.date(2024, 6, 30))
    assert report["asof"] == dt.date(2024, 6, 30)   # a finished FY is valued at its close

    # WIDGET was sold out during the year, so the snapshot is ACME alone.
    assert [r.instrument.ticker for r in report["snapshot"]] == ["ACME"]
    assert report["total_invested"] == Decimal("1010.00")   # 100 x 10.00 + 10.00
    assert report["total_value"] == Decimal("1200.00")      # 100 x 12.00 (28 Jun close)

    activity = report["activity"]
    assert (activity.buys, activity.sells) == (2, 1)
    assert activity.invested == Decimal("1215.00")   # 1010.00 + (50 x 4.00 + 5.00)
    assert activity.proceeds == Decimal("295.00")    # 50 x 6.00 - 5.00
    assert activity.brokerage == Decimal("20.00")    # 10.00 + 5.00 buy + 5.00 sell

    # WIDGET's disposal is the only CGT event: 295.00 - 205.00, held under a year.
    assert report["cgt"].gains_other == Decimal("90.00")
    assert report["cgt"].net_capital_gain == Decimal("90.00")


@freeze_time("2026-08-02")
def test_fy_report_marks_only_the_fy_containing_today_as_current(pf):
    inst = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, inst, "2026-07-05", "buy", 10, "1.00")

    current = fyreport.fy_report(pf, 2027)
    finished = fyreport.fy_report(pf, 2026)

    assert current["is_current"] is True
    assert current["asof"] == dt.date(2026, 8, 2)   # today, not 30 June 2027
    assert finished["is_current"] is False
    assert finished["asof"] == dt.date(2026, 6, 30)


@freeze_time("2026-08-02")
def test_fy_report_rolls_up_dividend_income_and_the_franking_gap(pf):
    inst = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, inst, "2023-08-01", "buy", 100, "10.00")
    add_dividend(pf, inst, "2023-09-01", "40.00", franking_credits="17.14")
    add_dividend(pf, inst, "2024-03-01", "40.00")   # statement not parsed yet

    report = fyreport.fy_report(pf, 2024)

    assert report["income_cash"] == Decimal("80.00")       # 40.00 + 40.00
    assert report["income_franking"] == Decimal("17.14")
    assert report["franking_missing"] == 1


def test_fy_income_totals_cash_franking_and_the_missing_franking_count(pf):
    acme = make_instrument(pf, "ACME", asset_class="share")
    add_dividend(pf, acme, "2023-08-01", "50.00", franking_credits="21.43")
    add_dividend(pf, acme, "2024-02-01", "50.00")   # franking unknown
    add_dividend(pf, acme, "2024-07-01", "99.00")   # next FY, must not count

    widget = make_instrument(pf, "WIDGET", asset_class="share")
    add_dividend(pf, widget, "2024-03-01", "10.00", franking_credits="0")

    rows = fyreport.fy_income(pf, 2024)

    assert [r.instrument.ticker for r in rows] == ["ACME", "WIDGET"]   # sorted by ticker
    acme_row, widget_row = rows
    assert acme_row.cash == Decimal("100.00")         # 50.00 + 50.00; July's is FY2025
    assert acme_row.franking == Decimal("21.43")
    assert acme_row.grossed_up == Decimal("121.43")   # 100.00 + 21.43
    assert acme_row.franking_missing == 1             # only the February one is blank
    # An explicit zero is an answer, not a gap — an unfranked distribution is
    # not waiting on anything.
    assert widget_row.franking_missing == 0


def test_fy_income_counts_a_reinvested_distribution_as_income(pf):
    inst = make_instrument(pf, "NOVA", asset_class="etf", drp=True)
    add_trade(pf, inst, "2023-08-01", "buy", 100, "10.00")
    # Taking a distribution as units does not make it tax free: the $60 is
    # assessable in the year it was paid. (The double-count guard belongs to the
    # performance series, where the units are already in the value — not here.)
    add_drp(pf, inst, "2024-01-15", "60.00", 5, "12.00")

    (row,) = fyreport.fy_income(pf, 2024)

    assert row.cash == Decimal("60.00")


def test_fy_income_converts_foreign_cash_at_the_dividend_fx_rate(pf):
    inst = make_instrument(pf, "OMEGA", exchange="NASDAQ", asset_class="share",
                           currency="USD")
    add_dividend(pf, inst, "2024-03-01", "100.00", fx_rate="1.50")

    (row,) = fyreport.fy_income(pf, 2024)

    assert row.cash == Decimal("150.00")   # 100.00 USD x 1.50 AUD per USD


# --------------------------------------------------------------------------- #
# Before 1 July 2027 the discount rules hold; from it, test_cgt_2027.py.
# --------------------------------------------------------------------------- #

def test_a_sale_before_1_july_2027_is_discounted_and_not_indexed(pf):
    """The mirror of the 2027 tests: the old rules are untouched by them."""
    alpha = make_instrument(pf, "ALPHA")
    add_trade(pf, alpha, "2020-01-06", "buy", 100, "10.00")
    add_trade(pf, alpha, "2026-09-01", "sell", 100, "30.00")   # FY2027

    summary = fyreport.fy_cgt(pf, 2027)
    [use] = summary.disposals[0].parcels

    assert (use.new_rules, use.factor, use.deferred) == (False, None, None)
    assert (summary.gains_from_july_2027, summary.discount, summary.net_capital_gain) == (
        0, Decimal("1000.00"), Decimal("1000.00"))


# --------------------------------------------------------------------------- #
# A foreign row with no exchange rate anywhere: withheld and named, not 1:1
# --------------------------------------------------------------------------- #

def _usd(pf, ticker="NOVA"):
    return make_instrument(pf, ticker, asset_class="share", exchange="NASDAQ", currency="USD")


def test_a_sale_with_no_rate_anywhere_is_listed_but_left_out_of_the_totals(pf):
    """Booked at 1:1, this US$200 gain went into the net capital gain as A$200.
    With no rate it is a question, not a number (decisions.md #5)."""
    inst = _usd(pf)
    add_trade(pf, inst, "2025-08-01", "buy", 100, "10.00")
    add_trade(pf, inst, "2025-09-01", "sell", 100, "12.00")

    out = fyreport.fy_cgt(pf, 2026)

    [sale] = out.disposals
    assert (sale.proceeds, sale.cost_base, sale.gain, sale.known) == (None, None, None, False)
    assert (out.gains_other, out.net_capital_gain, out.withheld) == (0, 0, ["NOVA"])


def test_only_the_sale_that_used_a_rateless_parcel_is_withheld(pf):
    inst = _usd(pf)
    add_trade(pf, inst, "2025-08-01", "buy", 10, "10.00", fx_rate="1.50")
    add_trade(pf, inst, "2025-08-02", "buy", 10, "10.00")                  # no rate anywhere
    add_trade(pf, inst, "2025-09-01", "sell", 10, "12.00", fx_rate="1.60")  # FIFO: the first
    add_trade(pf, inst, "2025-10-01", "sell", 10, "12.00", fx_rate="1.60")  # the second

    first, second = fyreport.fy_cgt(pf, 2026).disposals

    assert (first.known, first.cost_base, first.proceeds) == (True, Decimal("150.00"),
                                                              Decimal("192.00"))
    assert (second.known, second.cost_base, second.proceeds) == (False, None, Decimal("192.00"))
    assert fyreport.fy_cgt(pf, 2026).gains_other == Decimal("42.00")


@freeze_time("2026-07-15")
def test_a_dividend_with_no_rate_leaves_its_holding_out_of_income(pf):
    nova = _usd(pf)
    add_trade(pf, nova, "2025-08-01", "buy", 100, "10.00", fx_rate="1.50")
    add_dividend(pf, nova, "2025-12-01", "50.00")
    acme = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, acme, "2025-08-01", "buy", 100, "10.00")
    add_dividend(pf, acme, "2025-12-01", "30.00")           # AUD needs no rate (#6)

    report = fyreport.fy_report(pf, 2026)

    assert {r.instrument.ticker: r.cash for r in report["income"]} == {
        "ACME": Decimal("30.00"), "NOVA": None}
    assert (report["income_cash"], report["withheld"]) == (Decimal("30.00"), ["NOVA"])
    assert [r.grossed_up for r in report["income"] if r.cash is None] == [None]


@freeze_time("2026-07-15")
def test_a_sale_whose_parcel_had_no_rate_still_names_its_holding(pf):
    """Bought the year before with no rate, sold this year with one: the sale
    is the only figure the gap reaches, and the page still names it."""
    inst = _usd(pf)
    add_trade(pf, inst, "2025-05-01", "buy", 10, "10.00")
    add_trade(pf, inst, "2025-09-01", "sell", 10, "12.00", fx_rate="1.60")

    assert fyreport.fy_report(pf, 2026)["withheld"] == ["NOVA"]


@freeze_time("2026-07-15")
def test_a_purchase_with_no_rate_leaves_the_amount_invested_unknown(pf):
    """Each AUD figure the purchase feeds is blank or short of it, and the
    holding is named. The year's activity still counts the trade."""
    inst = _usd(pf)
    add_trade(pf, inst, "2025-08-01", "buy", 10, "10.00", fx_rate="1.50")
    add_trade(pf, inst, "2025-08-02", "buy", 10, "10.00")
    add_prices(pf, inst, [("2025-12-31", "11.00")])

    report = fyreport.fy_report(pf, 2026)

    [row] = report["snapshot"]
    assert (row.invested_cum, row.gain_aud, row.gain_pct, row.percentage_applies) == (
        None, None, None, True)                      # unknown, not "nothing invested"
    assert (report["activity"].buys, report["activity"].invested) == (2, Decimal("150.00"))
    assert (report["total_invested"], report["withheld"]) == (0, ["NOVA"])


@freeze_time("2028-07-15")
@pytest.mark.parametrize("bought, sold, invested, proceeds", [
    (None, "1.60", Decimal("0"), Decimal("192.00")),
    ("1.50", None, Decimal("150.00"), Decimal("0")),
], ids=["rateless-purchase", "rateless-sale"])
def test_a_year_without_a_cgt_schedule_still_names_a_rateless_trade(pf, bought, sold,
                                                                     invested, proceeds):
    """Bought and sold inside FY2028, the holding has no snapshot row: the
    year's activity and its gains schedule are where the gap shows."""
    inst = _usd(pf)
    add_trade(pf, inst, "2027-08-01", "buy", 10, "10.00", fx_rate=bought)
    add_trade(pf, inst, "2027-09-01", "sell", 10, "12.00", fx_rate=sold)

    report = fyreport.fy_report(pf, 2028)

    assert report["snapshot"] == [], "the premise"
    assert (report["activity"].invested, report["activity"].proceeds) == (invested, proceeds)
    assert report["withheld"] == ["NOVA"]


def test_without_an_fx_book_aud_needs_no_rate_and_a_foreign_row_is_withheld(pf):
    """The direct call, as a test makes it: only the rates on the rows are
    known, and an AUD row needs none (decisions.md #6)."""
    aud = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, aud, "2025-08-01", "buy", 10, "10.00", fx_rate=None)
    add_trade(pf, aud, "2025-09-01", "sell", 10, "12.00", fx_rate=None)
    usd = _usd(pf)
    add_trade(pf, usd, "2025-08-01", "buy", 10, "10.00")
    add_trade(pf, usd, "2025-09-01", "sell", 10, "12.00")

    [aud_sale] = fyreport.instrument_disposals(aud)
    [usd_sale] = fyreport.instrument_disposals(usd)

    assert (aud_sale.known, aud_sale.gain) == (True, Decimal("20.00"))
    assert (usd_sale.known, usd_sale.proceeds) == (False, None)


def test_a_sale_spanning_a_rated_parcel_and_a_rateless_one_is_withheld(pf):
    inst = _usd(pf)
    add_trade(pf, inst, "2025-08-01", "buy", 10, "10.00", fx_rate="1.50")
    add_trade(pf, inst, "2025-08-02", "buy", 10, "10.00")
    add_trade(pf, inst, "2025-09-01", "sell", 20, "12.00", fx_rate="1.60")

    [sale] = fyreport.fy_cgt(pf, 2026).disposals

    assert (sale.known, sale.cost_base, sale.gain) == (False, None, None)
    assert [p.cost_base for p in sale.parcels] == [Decimal("150.00"), None]


def test_each_holding_withheld_is_named_once_in_ticker_order(pf):
    for ticker in ("ZETA", "NOVA"):
        inst = _usd(pf, ticker)
        add_trade(pf, inst, "2025-08-01", "buy", 20, "10.00")
        add_trade(pf, inst, "2025-09-01", "sell", 10, "12.00")
        add_trade(pf, inst, "2025-10-01", "sell", 10, "12.00")

    assert fyreport.fy_cgt(pf, 2026).withheld == ["NOVA", "ZETA"]


@freeze_time("2026-07-15")
def test_income_reads_in_ticker_order_whatever_order_the_holdings_came_in(pf):
    for ticker in ("ZETA", "ALFA"):
        inst = make_instrument(pf, ticker, asset_class="share")
        add_trade(pf, inst, "2025-08-01", "buy", 10, "10.00")
        add_dividend(pf, inst, "2025-12-01", "5.00")

    assert [r.instrument.ticker for r in fyreport.fy_report(pf, 2026)["income"]] == [
        "ALFA", "ZETA"]


@freeze_time("2026-07-15")
def test_a_priced_holding_with_no_rate_to_value_it_is_named(pf):
    """Its purchases carried their rates; today's value needs one too."""
    inst = _usd(pf)
    add_trade(pf, inst, "2025-08-01", "buy", 10, "10.00", fx_rate="1.50")
    add_prices(pf, inst, [("2025-12-31", "11.00")])

    report = fyreport.fy_report(pf, 2026)

    assert (report["snapshot"][0].value_aud, report["withheld"]) == (None, ["NOVA"])
