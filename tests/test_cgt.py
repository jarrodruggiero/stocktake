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


def test_a_capital_loss_does_not_carry_into_the_next_fy(pf):
    loser = make_instrument(pf, "WIDGET", asset_class="share")
    add_trade(pf, loser, "2023-01-01", "buy", 100, "5.00")
    add_trade(pf, loser, "2024-03-01", "sell", 100, "2.00")    # FY2024: 300.00 loss

    winner = make_instrument(pf, "ACME", asset_class="share")
    add_trade(pf, winner, "2022-01-01", "buy", 100, "1.00")
    add_trade(pf, winner, "2024-12-01", "sell", 100, "4.00")   # FY2025: 300.00 gain

    assert fyreport.fy_cgt(pf, 2024).net_capital_loss == Decimal("300.00")

    # Each FY is computed from its own disposals only — carry-forward is
    # deliberately not implemented, so FY2025's gain is discounted, not erased.
    later = fyreport.fy_cgt(pf, 2025)
    assert later.losses == 0
    assert later.gains_discountable == Decimal("300.00")   # 400.00 - 100.00
    assert later.net_capital_gain == Decimal("150.00")     # 300.00 less the 50% discount


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
# The 1 July 2027 regime change. These tests pin the REFUSAL: the alternative
# failure is the worst kind this app has — a confident, plausible number
# computed with repealed rules, on the page somebody prepares a return from.
# decisions.md #26.
# --------------------------------------------------------------------------- #

def test_years_under_the_current_rules_are_still_calculated(pf):
    """The guard must not swallow the years the module does understand — FY2027
    ends 30 June 2027, the day before the change."""
    assert fyreport.regime_warning(2027) is None
    assert fyreport.regime_warning(2020) is None


def test_the_first_year_under_the_new_rules_is_refused(pf):
    """FY2028 starts exactly on 1 July 2027."""
    warning = fyreport.regime_warning(2028)

    assert warning is not None
    assert "FY27/28" in warning
    assert "1 July 2027" in warning
    assert "cost-base indexation" in warning


def test_a_refused_year_returns_no_figures_at_all(pf):
    """Not "the old numbers plus a caveat" — zero, with the reason. A caveat
    beside a wrong total is still a wrong total, and people read totals."""
    alpha = make_instrument(pf, "ALPHA")
    add_trade(pf, alpha, "2020-01-06", "buy", 100, "10.00")
    add_trade(pf, alpha, "2027-09-01", "sell", 100, "30.00")   # FY2028

    summary = fyreport.fy_cgt(pf, 2028)

    assert summary.regime_warning is not None
    assert summary.disposals == []
    assert summary.net_capital_gain == 0
    assert summary.discount == 0


def test_the_same_disposal_a_year_earlier_is_calculated_normally(pf):
    """The mirror, so the guard is proved to be about the DATE and not about
    something else going wrong."""
    alpha = make_instrument(pf, "ALPHA")
    add_trade(pf, alpha, "2020-01-06", "buy", 100, "10.00")
    add_trade(pf, alpha, "2026-09-01", "sell", 100, "30.00")   # FY2027

    summary = fyreport.fy_cgt(pf, 2027)

    assert summary.regime_warning is None
    assert summary.net_capital_gain > 0
    assert summary.discount > 0            # the 50% discount still applies


def test_a_parcel_held_across_the_change_is_flagged_even_in_a_year_we_compute(pf):
    """A parcel bought before and sold after 1 July 2027 needs the transitional
    apportionment — a market valuation at the changeover, whose method is not
    published. Worth saying even where the year itself is calculable."""
    alpha = make_instrument(pf, "ALPHA")
    add_trade(pf, alpha, "2020-01-06", "buy", 100, "10.00")
    disposals = [
        fyreport.Disposal(
            instrument=alpha, date=dt.date(2027, 9, 1), quantity=Decimal(100),
            proceeds=Decimal(3000),
            parcels=[fyreport.ParcelUse(
                acquired=dt.date(2020, 1, 6), quantity=Decimal(100),
                cost_base=Decimal(1000), proceeds=Decimal(3000), discountable=True)],
        )
    ]

    assert fyreport.spans_regime_change(disposals) is True


def test_a_parcel_wholly_before_the_change_is_not_flagged(pf):
    alpha = make_instrument(pf, "ALPHA")
    disposals = [
        fyreport.Disposal(
            instrument=alpha, date=dt.date(2026, 9, 1), quantity=Decimal(100),
            proceeds=Decimal(3000),
            parcels=[fyreport.ParcelUse(
                acquired=dt.date(2020, 1, 6), quantity=Decimal(100),
                cost_base=Decimal(1000), proceeds=Decimal(3000), discountable=True)],
        )
    ]

    assert fyreport.spans_regime_change(disposals) is False


@freeze_time("2027-09-15")
def test_the_fy_page_shows_the_refusal(client, session_factory):
    """It has to reach a human. A guard the page does not render is a guard
    that only protects the test suite.

    Frozen past the changeover on purpose: FY2028 is not even offered until it
    has started, so today the guard is unreachable through the UI. It becomes
    live in July 2027 — which is the point of landing it now rather than
    discovering the problem when somebody opens the page.
    """
    import factories as fac
    from test_routes import bind_to_only_portfolio, make_login

    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        alpha = fac.make_instrument(s, "ALPHA")
        fac.add_trade(s, alpha, "2020-01-06", "buy", 100, "10.00")
        fac.add_trade(s, alpha, "2027-09-01", "sell", 100, "30.00")
        s.commit()

    page = client.get("/", params={"fy": 2028}, headers={"accept": "text/html"}).text

    assert "not calculated for FY27/28" in page
    assert "cost-base indexation" in page
