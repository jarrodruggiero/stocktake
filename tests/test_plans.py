"""The investment plan and the calendar.

What comes next is always *computed* — the rotation says what order to buy in,
`planned_purchase` records what actually happened, and next is the slot after
the last recorded one. The subtle part is that recording is keyed on the plan
entry rather than on a count, so editing the rotation afterwards cannot
silently re-point "next" at the wrong holding.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from freezegun import freeze_time
from sqlalchemy import select

import factories as fac
from app import calendarview, plans, tenancy
from app.models import (
    InvestmentPlan,
    InvestmentPlanEntry,
    PlannedPurchase,
)

TODAY = "2026-08-02"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def make_plan(db, tickers, *, interval_days=28, start_date=None, amount="500",
              active=True, name="Regular buys"):
    """A plan plus its rotation, in the given order. A ticker may repeat — that
    is how a 2:1 weighting is expressed."""
    plan = tenancy.owned(db, InvestmentPlan(
        name=name, interval_days=interval_days, active=active,
        amount=Decimal(amount) if amount else None,
        start_date=fac.d(start_date) if start_date else None))
    db.add(plan)
    db.flush()
    for position, ticker in enumerate(tickers):
        inst = db.scalars(
            select(fac.Instrument).where(fac.Instrument.ticker == ticker)
        ).first() or fac.make_instrument(db, ticker)
        db.add(InvestmentPlanEntry(plan_id=plan.id, position=position,
                                   instrument_id=inst.id))
    db.flush()
    db.refresh(plan)
    return plan


def record(db, plan, position, due_date, status="done"):
    """Record a purchase against a specific slot in the rotation."""
    entry = next(e for e in plan.entries if e.position == position)
    row = tenancy.owned(db, PlannedPurchase(
        due_date=fac.d(due_date), instrument_id=entry.instrument_id,
        plan_entry_id=entry.id, status=status))
    db.add(row)
    db.flush()
    return row


# --------------------------------------------------------------------------- #
# Which plan is in play
# --------------------------------------------------------------------------- #

def test_only_the_active_plan_is_returned(pf):
    make_plan(pf, ["ALPHA"], active=False, name="Retired plan")
    live = make_plan(pf, ["BETAX"], name="Current plan")

    assert plans.active_plan(pf).id == live.id


def test_with_no_plan_there_is_nothing_upcoming(pf):
    sched = plans.schedule(pf)

    assert sched["plan"] is None
    assert sched["next"] == []


def test_a_plan_with_an_empty_rotation_has_nothing_upcoming(pf):
    make_plan(pf, [])

    assert plans.schedule(pf)["next"] == []


# --------------------------------------------------------------------------- #
# When the next buy falls
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_the_start_date_is_itself_the_first_buy(pf):
    """The anchor is the first purchase, not one interval after it — stepping
    a cycle forward before the first buy would leave a new plan looking dormant
    for a month."""
    make_plan(pf, ["ALPHA", "BETAX"], interval_days=28, start_date="2026-08-10")

    upcoming = plans.schedule(pf, upcoming=2)["next"]

    assert upcoming[0].due_date == dt.date(2026, 8, 10)
    # …and the one after it is exactly one interval later.
    assert upcoming[1].due_date == dt.date(2026, 9, 7)   # 10 Aug + 28 days


@freeze_time(TODAY)
def test_with_history_the_schedule_steps_from_the_last_recorded_buy(pf):
    plan = make_plan(pf, ["ALPHA", "BETAX"], interval_days=28, start_date="2026-01-05")
    record(pf, plan, 0, "2026-07-06")

    upcoming = plans.schedule(pf, upcoming=1)["next"]

    assert upcoming[0].due_date == dt.date(2026, 8, 3)   # 6 Jul + 28 days


@freeze_time(TODAY)
def test_with_no_start_date_and_nothing_recorded_the_first_buy_is_today(pf):
    make_plan(pf, ["ALPHA"], interval_days=28)

    assert plans.schedule(pf)["next"][0].due_date == dt.date(2026, 8, 2)


@freeze_time(TODAY)
def test_the_schedule_lists_three_buys_unless_asked_for_more(pf):
    make_plan(pf, ["ALPHA"], interval_days=28, start_date="2026-08-03")

    assert len(plans.schedule(pf)["next"]) == 3


@freeze_time(TODAY)
def test_each_upcoming_buy_points_at_its_own_slot(pf):
    """The ticker comes from the projection and the slot from the rotation;
    they have to be the same slot, or a link or a prefill goes to the wrong
    holding."""
    plan = make_plan(pf, ["ALPHA", "BETAX", "GAMMA"], start_date="2026-01-05")
    record(pf, plan, 0, "2026-07-06")
    slots = {e.id: e for e in plan.entries}

    upcoming = plans.schedule(pf, upcoming=3)["next"]

    assert [(b.ticker, slots[b.entry_id].instrument.ticker,
             pf.get(fac.Instrument, b.instrument_id).ticker) for b in upcoming] == [
        ("BETAX", "BETAX", "BETAX"), ("GAMMA", "GAMMA", "GAMMA"), ("ALPHA", "ALPHA", "ALPHA")]


@freeze_time(TODAY)
def test_the_last_buy_is_the_latest_recorded_whatever_order_it_was_written(pf):
    """By due date: the July buy, written first, is the last one, so GAMMA is
    next, from 6 July."""
    plan = make_plan(pf, ["ALPHA", "BETAX", "GAMMA"], start_date="2026-01-05")
    record(pf, plan, 1, "2026-07-06")
    record(pf, plan, 0, "2026-06-08")

    nxt = plans.next_buy(pf)

    assert (nxt.ticker, nxt.due_date) == ("GAMMA", dt.date(2026, 8, 3))


@freeze_time(TODAY)
def test_the_last_done_is_the_latest_bought_not_skipped(pf):
    plan = make_plan(pf, ["ALPHA", "BETAX", "GAMMA"], start_date="2026-01-05")
    assert plans.schedule(pf)["last_done"] is None
    record(pf, plan, 0, "2026-05-11")
    record(pf, plan, 1, "2026-06-08")
    record(pf, plan, 2, "2026-07-06", status="skipped")

    assert plans.schedule(pf)["last_done"].due_date == dt.date(2026, 6, 8)


def test_no_rotation_or_no_interval_projects_nothing():
    """`project` is pure, and both the schedule and the editor's preview go
    through it: an empty draft, or an interval of 0, is no dates rather than a
    crash or a column of the same day."""
    day = dt.date(2026, 8, 3)

    assert plans.project([], 28, position=0, last_due=day, upcoming=3) == []
    assert plans.project(["ALPHA"], 0, position=0, last_due=day, upcoming=3) == []
    assert plans.project(["ALPHA"], 1, position=0, last_due=day, upcoming=2) == [
        (dt.date(2026, 8, 4), "ALPHA"), (dt.date(2026, 8, 5), "ALPHA")]


@freeze_time(TODAY)
def test_with_no_plan_a_draft_starts_at_its_first_slot(pf):
    alpha, betax = (fac.make_instrument(pf, t) for t in ("ALPHA", "BETAX"))

    rows = plans.preview(pf, [(alpha.id, "ALPHA"), (betax.id, "BETAX")], 28,
                         fac.d("2026-08-10"), upcoming=2)

    assert rows == [(dt.date(2026, 8, 10), "ALPHA"), (dt.date(2026, 9, 7), "BETAX")]


@freeze_time(TODAY)
def test_a_plan_whose_rotation_was_emptied_can_still_preview_a_draft(pf):
    """Removing the last holding in a rotation leaves a plan with no slots and
    a buy recorded against one of them."""
    plan = make_plan(pf, ["ALPHA"], start_date="2026-01-05")
    record(pf, plan, 0, "2026-07-06")
    alpha = plan.entries[0].instrument
    assert plans.remove_instrument(pf, alpha.id) is True
    betax = fac.make_instrument(pf, "BETAX")

    rows = plans.preview(pf, [(betax.id, "BETAX")], 28, None, upcoming=1)

    assert rows == [(dt.date(2026, 8, 3), "BETAX")]


@freeze_time(TODAY)
def test_a_draft_with_every_slot_taken_out_previews_nothing(pf):
    """What the editor asks for when the last chip is pulled out, with buys
    already recorded."""
    plan = make_plan(pf, ["ALPHA"], start_date="2026-01-05")
    record(pf, plan, 0, "2026-07-06")

    assert plans.preview(pf, [], 28, None) == []


def test_removing_an_instrument_no_plan_has_says_so(pf):
    make_plan(pf, ["ALPHA"])
    other = fac.make_instrument(pf, "OTHER")

    assert plans.remove_instrument(pf, other.id) is False


# --------------------------------------------------------------------------- #
# Which slot comes next
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_next_is_the_slot_after_the_last_one_recorded(pf):
    plan = make_plan(pf, ["ALPHA", "BETAX", "GAMMA"], start_date="2026-01-05")
    record(pf, plan, 0, "2026-07-06")

    assert plans.next_buy(pf).ticker == "BETAX"


@freeze_time(TODAY)
def test_the_rotation_wraps_at_the_end(pf):
    plan = make_plan(pf, ["ALPHA", "BETAX"], start_date="2026-01-05")
    record(pf, plan, 1, "2026-07-06")

    assert plans.next_buy(pf).ticker == "ALPHA"


@freeze_time(TODAY)
def test_next_follows_the_recorded_slot_not_the_number_of_purchases(pf):
    """The reason `plan_entry_id` exists. One purchase recorded against slot 2
    means slot 0 is next; a count of purchases would say slot 1. If the app
    ever went back to counting, this is the test that catches it."""
    plan = make_plan(pf, ["ALPHA", "BETAX", "GAMMA"], start_date="2026-01-05")
    record(pf, plan, 2, "2026-07-06")

    # 1 purchase recorded, so a naive count would land on BETAX (index 1).
    assert plans.next_buy(pf).ticker == "ALPHA"


@freeze_time(TODAY)
def test_editing_another_slot_leaves_the_sequence_where_it_was(pf):
    """The guarantee `plan_entry_id` buys: as long as the slot you last bought
    still exists, changing a *different* slot cannot move your place in the
    cycle."""
    plan = make_plan(pf, ["ALPHA", "BETAX", "GAMMA"], start_date="2026-01-05")
    record(pf, plan, 0, "2026-07-06")
    assert plans.next_buy(pf).ticker == "BETAX"

    delta = fac.make_instrument(pf, "DELTA")
    ids = [e.instrument_id for e in sorted(plan.entries, key=lambda e: e.position)]
    plans.set_entries(pf, plan, ids[:2] + [delta.id])   # swap GAMMA out for DELTA

    assert plans.next_buy(pf).ticker == "BETAX"


@freeze_time(TODAY)
def test_appending_a_slot_puts_it_next_in_the_cycle(pf):
    """Extending the rotation after buying the last slot does move "next" onto
    the new one — the sequence follows position, so a slot appended after the
    one just bought is genuinely what comes next. Pinned because it looks like
    a surprise until you follow the cycle."""
    plan = make_plan(pf, ["ALPHA", "BETAX", "GAMMA"], start_date="2026-01-05")
    record(pf, plan, 2, "2026-07-06")
    assert plans.next_buy(pf).ticker == "ALPHA"     # wraps while the cycle is 3 long

    delta = fac.make_instrument(pf, "DELTA")
    ids = [e.instrument_id for e in sorted(plan.entries, key=lambda e: e.position)]
    plans.set_entries(pf, plan, ids + [delta.id])

    # Slot 3 now exists and follows the slot just bought, so it is next.
    assert plans.next_buy(pf).ticker == "DELTA"


@freeze_time(TODAY)
def test_a_repeated_instrument_is_honoured_as_a_weighting(pf):
    """Listing the same holding twice is how a 2:1 split is expressed, so the
    cycle must visit it twice rather than collapsing the duplicates."""
    make_plan(pf, ["ALPHA", "BETAX", "ALPHA"], start_date="2026-01-05")

    upcoming = plans.schedule(pf, upcoming=4)["next"]

    assert [b.ticker for b in upcoming] == ["ALPHA", "BETAX", "ALPHA", "ALPHA"]


# --------------------------------------------------------------------------- #
# Editing the rotation
# --------------------------------------------------------------------------- #

def test_an_unchanged_slot_keeps_its_row(pf):
    """History is recorded against the entry id, so a slot that did not change
    must keep it — otherwise editing the plan would orphan what was recorded."""
    plan = make_plan(pf, ["ALPHA", "BETAX", "GAMMA"])
    before = {e.position: e.id for e in plan.entries}

    ids = [e.instrument_id for e in sorted(plan.entries, key=lambda e: e.position)]
    plans.set_entries(pf, plan, ids[:2])   # drop the last slot

    after = {e.position: e.id for e in plan.entries}
    assert after[0] == before[0]
    assert after[1] == before[1]
    assert len(after) == 2


def test_shrinking_the_rotation_renumbers_without_a_collision(pf):
    plan = make_plan(pf, ["ALPHA", "BETAX", "GAMMA"])
    alpha_id = next(e.instrument_id for e in plan.entries if e.position == 0)

    plans.set_entries(pf, plan, [alpha_id])

    assert [e.position for e in plan.entries] == [0]


def test_reversing_the_rotation_is_allowed(pf):
    """Positions are unique per plan, so a straight swap would collide unless
    the rows are parked out of range first."""
    plan = make_plan(pf, ["ALPHA", "BETAX"])
    ids = [e.instrument_id for e in sorted(plan.entries, key=lambda e: e.position)]

    plans.set_entries(pf, plan, list(reversed(ids)))

    ordered = sorted(plan.entries, key=lambda e: e.position)
    assert [e.instrument_id for e in ordered] == list(reversed(ids))
    assert [e.position for e in ordered] == [0, 1]


# --------------------------------------------------------------------------- #
# Projected distributions
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_the_published_cycle_drives_the_projection(pf):
    """A quarterly ETF pays roughly every 91 days; the next date is the last
    one plus the median gap."""
    alpha = fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    fac.add_trade(pf, alpha, "2026-01-05", "buy", 100, "10.00")
    for ex_date in ("2025-09-20", "2025-12-20", "2026-03-20", "2026-06-20"):
        fac.add_market_dividend(pf, alpha, ex_date, "0.50")
    pf.commit()

    projected = calendarview.projected_dividends(pf, fac.d("2026-10-31"))

    # Gaps are 91, 90 and 92 days → median 91; 20 Jun 2026 + 91 = 19 Sep 2026.
    assert [e.date for e in projected] == [dt.date(2026, 9, 19)]
    assert projected[0].kind == "dividend-expected"
    assert projected[0].expected is True


@freeze_time(TODAY)
def test_a_projection_estimates_the_amount_from_the_units_held(pf):
    alpha = fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    fac.add_trade(pf, alpha, "2026-01-05", "buy", 100, "10.00")
    for ex_date in ("2025-12-20", "2026-03-20", "2026-06-20"):
        fac.add_market_dividend(pf, alpha, ex_date, "0.50")
    pf.commit()

    projected = calendarview.projected_dividends(pf, fac.d("2026-10-31"))

    # 100 units x the latest published 0.50 per unit = 50.00
    assert projected[0].amount == Decimal("50.00")


@freeze_time(TODAY)
def test_a_position_that_has_been_sold_projects_nothing(pf):
    """Income for a holding you no longer own would be a real defect, not just
    noise on the calendar."""
    alpha = fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    fac.add_trade(pf, alpha, "2026-01-05", "buy", 100, "10.00")
    fac.add_trade(pf, alpha, "2026-02-05", "sell", 100, "11.00")
    for ex_date in ("2025-12-20", "2026-03-20", "2026-06-20"):
        fac.add_market_dividend(pf, alpha, ex_date, "0.50")
    pf.commit()

    assert calendarview.projected_dividends(pf, fac.d("2026-12-31")) == []


@freeze_time(TODAY)
def test_a_single_payment_is_not_a_cycle(pf):
    alpha = fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    fac.add_trade(pf, alpha, "2026-01-05", "buy", 100, "10.00")
    fac.add_market_dividend(pf, alpha, "2026-06-20", "0.50")
    pf.commit()

    assert calendarview.projected_dividends(pf, fac.d("2026-12-31")) == []


@freeze_time(TODAY)
def test_the_securitys_own_record_is_preferred_over_the_holders(pf):
    """Someone who bought last month has no personal history to find a cycle
    in, but the fund has been paying quarterly for years."""
    alpha = fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    fac.add_trade(pf, alpha, "2026-06-01", "buy", 100, "10.00")
    fac.add_dividend(pf, alpha, "2026-06-20", "50.00")     # their only payment
    for ex_date in ("2025-09-20", "2025-12-20", "2026-03-20", "2026-06-20"):
        fac.add_market_dividend(pf, alpha, ex_date, "0.50")
    pf.commit()

    projected = calendarview.projected_dividends(pf, fac.d("2026-10-31"))

    assert [e.date for e in projected] == [dt.date(2026, 9, 19)]
    assert "published history" in projected[0].detail


@freeze_time(TODAY)
def test_projections_stop_at_the_horizon(pf):
    alpha = fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    fac.add_trade(pf, alpha, "2026-01-05", "buy", 100, "10.00")
    for ex_date in ("2025-12-20", "2026-03-20", "2026-06-20"):
        fac.add_market_dividend(pf, alpha, ex_date, "0.50")
    pf.commit()

    near = calendarview.projected_dividends(pf, fac.d("2026-09-30"))
    far = calendarview.projected_dividends(pf, fac.d("2027-06-30"))

    assert len(near) == 1
    assert len(far) > len(near)
    assert all(e.date <= dt.date(2027, 6, 30) for e in far)


@freeze_time(TODAY)
def test_the_amount_is_this_funds_latest_published_one(pf):
    """Not the earliest published, and not another fund's. With every
    published amount the same 0.50 and one fund publishing, neither was
    tested. Written out of order, so the cycle depends on the dates' order
    too: 20 Dec, 20 Mar and 20 Jun are 90 and 92 days apart."""
    alpha = fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    fac.add_trade(pf, alpha, "2026-01-05", "buy", 100, "10.00")
    for ex_date, amount in (("2025-12-20", "0.48"), ("2026-06-20", "0.55"),
                            ("2026-03-20", "0.50")):
        fac.add_market_dividend(pf, alpha, ex_date, amount)
    other = fac.make_instrument(pf, "OTHER")
    fac.add_market_dividend(pf, other, "2026-07-30", "9.99")    # later, and not ALPHA's
    pf.commit()

    projected = calendarview.projected_dividends(pf, fac.d("2026-10-31"))

    assert [(e.ticker, e.date, e.amount) for e in projected] == [
        ("ALPHA", dt.date(2026, 9, 19), Decimal("55.00"))]


@freeze_time(TODAY)
def test_without_a_published_record_the_amount_comes_from_the_last_payment(pf):
    """Per unit: the last payment over the units held before it, scaled to the
    units held now. A sale before it counts against, a buy after it does not
    count, and an earlier payment of another size is not the last one."""
    alpha = fac.make_instrument(pf, "ALPHA")
    fac.add_trade(pf, alpha, "2025-06-02", "buy", 100, "10.00")
    fac.add_dividend(pf, alpha, "2025-12-20", "30.00")           # not the last
    fac.add_trade(pf, alpha, "2026-02-02", "sell", 20, "10.00")  # 80 held at the last
    fac.add_dividend(pf, alpha, "2026-03-20", "40.00")           # 0.50 a unit
    fac.add_trade(pf, alpha, "2026-04-02", "buy", 40, "10.00")   # 120 held now
    pf.commit()

    [projected] = calendarview.projected_dividends(pf, fac.d("2026-09-30"))

    assert (projected.date, projected.amount) == (dt.date(2026, 9, 16), Decimal("60.00"))
    assert "your history" in projected.detail


@freeze_time(TODAY)
def test_units_a_payment_reinvested_did_not_earn_it(pf):
    """A reinvestment is allotted on the payment date. Counted as held that
    day, its own units shrank the figure per unit, so a reinvesting holding's
    projection never grew: 0.50 on 100 units, with 4 reinvested, is 52.00
    next time, not 50.00."""
    alpha = fac.make_instrument(pf, "ALPHA")
    fac.add_trade(pf, alpha, "2025-06-02", "buy", 100, "10.00")
    fac.add_dividend(pf, alpha, "2025-12-20", "50.00")
    fac.add_drp(pf, alpha, "2026-03-20", "50.00", 4, "12.50")
    pf.commit()

    [projected] = calendarview.projected_dividends(pf, fac.d("2026-09-30"))

    assert projected.amount == Decimal("52.00")


@freeze_time(TODAY)
def test_a_last_payment_with_nothing_held_before_it_gives_no_amount(pf):
    """Sold out before the last payment was recorded, and bought back since:
    there is no figure per unit to scale, so the date comes without one."""
    alpha = fac.make_instrument(pf, "ALPHA")
    fac.add_trade(pf, alpha, "2025-06-02", "buy", 100, "10.00")
    fac.add_dividend(pf, alpha, "2025-12-20", "50.00")
    fac.add_trade(pf, alpha, "2026-03-01", "sell", 100, "10.00")
    fac.add_dividend(pf, alpha, "2026-03-20", "50.00")           # earned before the sale
    fac.add_trade(pf, alpha, "2026-04-02", "buy", 100, "10.00")
    pf.commit()

    [projected] = calendarview.projected_dividends(pf, fac.d("2026-09-30"))

    assert (projected.date, projected.amount) == (dt.date(2026, 9, 16), None)


@freeze_time(TODAY)
def test_one_unit_held_at_the_last_payment_is_enough_to_work_it_out(pf):
    alpha = fac.make_instrument(pf, "ALPHA")
    fac.add_trade(pf, alpha, "2025-06-02", "buy", 1, "10.00")
    fac.add_dividend(pf, alpha, "2025-12-20", "0.50")
    fac.add_dividend(pf, alpha, "2026-03-20", "0.50")
    fac.add_trade(pf, alpha, "2026-04-02", "buy", 9, "10.00")    # ten now
    pf.commit()

    [projected] = calendarview.projected_dividends(pf, fac.d("2026-09-30"))

    assert projected.amount == Decimal("5.00")


@freeze_time(TODAY)
def test_the_published_amount_beats_one_worked_out_from_the_holders_payments(pf):
    alpha = fac.make_instrument(pf, "ALPHA")
    fac.add_trade(pf, alpha, "2026-01-05", "buy", 100, "10.00")
    fac.add_dividend(pf, alpha, "2026-03-20", "40.00")           # 0.40 a unit, theirs
    for ex_date in ("2025-12-20", "2026-03-20", "2026-06-20"):
        fac.add_market_dividend(pf, alpha, ex_date, "0.55")
    pf.commit()

    [projected] = calendarview.projected_dividends(pf, fac.d("2026-10-31"))

    assert projected.amount == Decimal("55.00")


@freeze_time(TODAY)
@pytest.mark.parametrize("published, cycle", [(1, 61), (2, 91)], ids=["one", "two"])
def test_two_published_payments_show_a_cycle_and_one_does_not(pf, published, cycle):
    """With one published payment the holder's own are followed instead:
    theirs are 61 days apart, the fund's 91."""
    alpha = fac.make_instrument(pf, "ALPHA")
    fac.add_trade(pf, alpha, "2025-06-02", "buy", 100, "10.00")
    for paid in ("2026-01-20", "2026-03-22", "2026-05-22"):
        fac.add_dividend(pf, alpha, paid, "50.00")
    for ex_date in ("2026-03-20", "2026-06-19")[2 - published:]:
        fac.add_market_dividend(pf, alpha, ex_date, "0.50")
    pf.commit()

    projected = calendarview.projected_dividends(pf, fac.d("2026-12-31"))

    assert f"~{cycle}-day cycle" in projected[0].detail


@freeze_time(TODAY)
def test_a_payment_due_today_and_one_on_the_last_day_asked_for_are_both_in(pf):
    """1 Feb and 3 May are 91 days apart, so the next is today, 2 Aug, and
    the one after is 1 Nov."""
    alpha = fac.make_instrument(pf, "ALPHA")
    fac.add_trade(pf, alpha, "2026-01-05", "buy", 1, "10.00")    # one unit is a holding
    for ex_date in ("2026-02-01", "2026-05-03"):
        fac.add_market_dividend(pf, alpha, ex_date, "0.50")
    pf.commit()

    projected = calendarview.projected_dividends(pf, fac.d("2026-11-01"))

    assert [e.date for e in projected] == [dt.date(2026, 8, 2), dt.date(2026, 11, 1)]


@freeze_time(TODAY)
def test_payments_expected_on_one_day_are_listed_in_ticker_order(pf):
    for ticker in ("ZETA", "ALPHA"):                              # ids the other way
        inst = fac.make_instrument(pf, ticker)
        fac.add_trade(pf, inst, "2026-01-05", "buy", 10, "10.00")
        for ex_date in ("2026-03-20", "2026-06-20"):
            fac.add_market_dividend(pf, inst, ex_date, "0.50")
    pf.commit()

    upcoming = calendarview.month_grid(pf, 2026, 8)["upcoming"]

    assert [e.ticker for e in upcoming if e.kind == "dividend-expected"][:2] == [
        "ALPHA", "ZETA"]


# --------------------------------------------------------------------------- #
# Recorded events and the grid
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_recorded_events_cover_trades_and_distributions_in_the_window(pf):
    alpha = fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    fac.add_trade(pf, alpha, "2026-08-05", "buy", 10, "10.00")
    fac.add_trade(pf, alpha, "2026-08-06", "sell", 5, "11.00")
    fac.add_dividend(pf, alpha, "2026-08-07", "12.00")
    fac.add_trade(pf, alpha, "2026-09-01", "buy", 1, "10.00")   # outside the window
    pf.commit()

    events = calendarview.recorded_events(pf, fac.d("2026-08-01"), fac.d("2026-08-31"))

    assert sorted((e.date, e.kind) for e in events) == [
        (dt.date(2026, 8, 5), "buy"),
        (dt.date(2026, 8, 6), "sell"),
        (dt.date(2026, 8, 7), "dividend"),
    ]


@freeze_time(TODAY)
def test_the_month_grid_is_monday_first_and_covers_the_overhang(pf):
    grid = calendarview.month_grid(pf, 2026, 8)

    days = [day for week in grid["weeks"] for day in week]
    assert all(len(week) == 7 for week in grid["weeks"])
    # 1 Aug 2026 is a Saturday, so the grid opens on Monday 27 July.
    assert days[0].date == dt.date(2026, 7, 27)
    assert days[0].in_month is False
    assert grid["month_label"] == "August 2026"
    assert grid["prev"] == (2026, 7)
    assert grid["next"] == (2026, 9)


@freeze_time(TODAY)
def test_today_is_flagged_on_the_grid(pf):
    grid = calendarview.month_grid(pf, 2026, 8)

    flagged = [d.date for week in grid["weeks"] for d in week if d.is_today]
    assert flagged == [dt.date(2026, 8, 2)]


@freeze_time(TODAY)
def test_events_land_on_their_own_day(pf):
    alpha = fac.make_instrument(pf, "ALPHA", name="Alpha Index Fund")
    fac.add_trade(pf, alpha, "2026-08-05", "buy", 10, "10.00")
    pf.commit()

    grid = calendarview.month_grid(pf, 2026, 8)

    day = next(d for week in grid["weeks"] for d in week if d.date == dt.date(2026, 8, 5))
    assert [e.kind for e in day.events] == ["buy"]


def test_a_planned_buy_counts_as_expected():
    planned = calendarview.CalEvent(date=dt.date(2026, 8, 3), kind="buy-planned", ticker="X")
    bought = calendarview.CalEvent(date=dt.date(2026, 8, 3), kind="buy", ticker="X")

    assert (planned.expected, bought.expected) == (True, False)


@freeze_time(TODAY)
def test_a_recorded_distribution_says_whether_it_was_reinvested(pf):
    alpha = fac.make_instrument(pf, "ALPHA")
    fac.add_trade(pf, alpha, "2026-01-05", "buy", 10, "10.00")
    fac.add_dividend(pf, alpha, "2026-08-05", "12.00")
    fac.add_drp(pf, alpha, "2026-08-06", "10.00", 1, "10.00")
    pf.commit()

    events = calendarview.recorded_events(pf, fac.d("2026-08-01"), fac.d("2026-08-31"))

    assert sorted((e.date.day, e.detail) for e in events if e.kind == "dividend") == [
        (5, "paid"), (6, "reinvested")]


@freeze_time(TODAY)
def test_planned_buys_run_up_to_and_including_the_day_asked_for(pf):
    make_plan(pf, ["ALPHA"], interval_days=7, start_date="2026-08-03")
    pf.commit()

    buys = calendarview.planned_buys(pf, fac.d("2026-08-17"))

    assert [e.date for e in buys] == [dt.date(2026, 8, 3), dt.date(2026, 8, 10),
                                      dt.date(2026, 8, 17)]


@freeze_time("2026-08-10")
def test_a_day_lists_what_happened_then_what_is_expected(pf):
    """Each by kind, then ticker. BETA's payments, 9 Feb and 11 May, put its
    next one today."""
    alpha, beta, zeta = (fac.make_instrument(pf, t) for t in ("ALPHA", "BETA", "ZETA"))
    fac.add_trade(pf, beta, "2026-01-05", "buy", 10, "10.00")
    fac.add_trade(pf, zeta, "2026-01-05", "buy", 10, "10.00")
    for ex_date in ("2026-02-09", "2026-05-11"):
        fac.add_market_dividend(pf, beta, ex_date, "0.50")
    fac.add_trade(pf, zeta, "2026-08-10", "sell", 5, "11.00")    # written first
    fac.add_trade(pf, alpha, "2026-08-10", "buy", 5, "10.00")
    make_plan(pf, ["ALPHA"], start_date="2026-08-10")
    pf.commit()

    grid = calendarview.month_grid(pf, 2026, 8)

    day = next(d for week in grid["weeks"] for d in week if d.date == dt.date(2026, 8, 10))
    assert [(e.kind, e.ticker) for e in day.events] == [
        ("buy", "ALPHA"), ("sell", "ZETA"), ("buy-planned", "ALPHA"),
        ("dividend-expected", "BETA")]


@freeze_time("2026-07-27")
def test_a_buy_on_the_grids_first_day_is_on_it(pf):
    make_plan(pf, ["ALPHA"], start_date="2026-07-27")
    pf.commit()

    first = calendarview.month_grid(pf, 2026, 8)["weeks"][0][0]

    assert (first.date, [e.kind for e in first.events]) == (
        dt.date(2026, 7, 27), ["buy-planned"])


@freeze_time("2026-07-10")
def test_an_overdue_buy_leads_the_coming_up_list(pf):
    """The 6 July buy, not yet recorded, is the plan's next. The list began at
    3 August, so the buy actually owed never showed, and Skip on the first row
    that did was refused for not being the next buy."""
    make_plan(pf, ["ALPHA"], start_date="2026-07-06")
    pf.commit()

    upcoming = calendarview.month_grid(pf, 2026, 7)["upcoming"]

    assert [(e.kind, e.date) for e in upcoming][:2] == [
        ("buy-planned", dt.date(2026, 7, 6)), ("buy-planned", dt.date(2026, 8, 3))]


@freeze_time(TODAY)
def test_coming_up_lists_twelve(pf):
    make_plan(pf, ["ALPHA"], interval_days=7, start_date="2026-08-03")
    pf.commit()

    assert len(calendarview.month_grid(pf, 2026, 8)["upcoming"]) == 12
