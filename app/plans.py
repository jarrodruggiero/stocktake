"""The investment plan: a repeating buy rotation, edited in the app.

The rotation lives in the database and is edited on the Plan page. It used to
be a list in config.yaml, which meant a redeploy to change it and one
person's holdings as every deployment's default; `suggested_rotation` now
prefills a new plan from what the portfolio actually holds.

What's next is still *computed*, never stored: the rotation says what order to
buy in, `planned_purchase` records what actually happened, and the next buy is
the slot after the last recorded one. Recording against `plan_entry_id` means
editing the rotation later doesn't shift the sequence — with only a count to go
on, inserting a slot would silently re-point "next" at the wrong instrument.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import clock
from .models import (
    Instrument,
    InvestmentPlan,
    InvestmentPlanEntry,
    PlannedPurchase,
    Price,
)


@dataclass
class UpcomingBuy:
    ticker: str
    due_date: dt.date
    entry_id: int
    instrument_id: int
    amount: Decimal | None = None
    exchange: str | None = None


def active_plan(session: Session) -> InvestmentPlan | None:
    return session.scalars(
        select(InvestmentPlan)
        .where(InvestmentPlan.active.is_(True))
        .order_by(InvestmentPlan.id)
        .options(
            selectinload(InvestmentPlan.entries).selectinload(
                InvestmentPlanEntry.instrument
            )
        )
    ).first()


def suggested_rotation(session: Session) -> list[str]:
    """A starting rotation for someone who has no plan yet: the things they
    already hold, grouped by asset class then ticker.

    Only a suggestion, prefilled into the form for editing. It replaced a
    rotation baked into config.yaml, which meant every deployment inherited one
    person's holdings as its default — and could not be changed without a
    redeploy.
    """
    from .queries import all_holdings, ambiguous_tickers, listing_ref, split_positions

    open_positions, _closed = split_positions(all_holdings(session))
    ordered = sorted(
        open_positions, key=lambda h: (h.instrument.asset_class, h.instrument.ticker)
    )
    twice = ambiguous_tickers(session)
    return [listing_ref(h.instrument.ticker, h.instrument.exchange, twice) for h in ordered]


def history(session: Session) -> list[PlannedPurchase]:
    return list(
        session.scalars(
            select(PlannedPurchase)
            .order_by(PlannedPurchase.due_date, PlannedPurchase.id)
            .options(selectinload(PlannedPurchase.instrument))
        ).all()
    )


def _next_position(plan: InvestmentPlan | None, past: list[PlannedPurchase],
                   rotation: list[int] | None = None) -> int:
    """Index of the slot to buy next, in the plan as it stands or in
    `rotation`, a draft of instrument ids as it would be saved.

    Preferred signal is the last entry actually recorded (its slot + 1). Rows
    imported before `plan_entry_id` existed have none, so fall back to
    counting them — the imported history started at slot 0.
    """
    entries = sorted(plan.entries, key=lambda e: e.position) if plan is not None else []
    if rotation is None:
        rotation = [e.instrument_id for e in entries]
    size = len(rotation)
    if not size:
        return 0
    # Where each slot's row sits in `rotation`, placed the way `set_entries`
    # places it: a row stays with its instrument wherever that moves to, so a
    # reordered draft's next buy is still the one after the last recorded.
    unused: dict[int, list[int]] = {}
    for entry in entries:
        unused.setdefault(entry.instrument_id, []).append(entry.id)
    positions: dict[int, int] = {}
    for position, instrument_id in enumerate(rotation):
        rows = unused.get(instrument_id)
        if rows:
            positions[rows.pop(0)] = position
    for row in reversed(past):
        if row.plan_entry_id in positions:
            return (positions[row.plan_entry_id] + 1) % size
    return len(past) % size


def anchor(plan: InvestmentPlan | None, past: list[PlannedPurchase],
           interval_days: int, start_date: dt.date | None,
           today: dt.date, rotation: list[int] | None = None) -> tuple[int, dt.date]:
    """Where the rotation stands: (slot to buy next, date it follows).

    Split out of `schedule` so the live preview stands on exactly the same two
    numbers. A draft `rotation` is an input because saving it moves slots with
    their instruments: carrying the old slot number over to a reordered draft
    named a different next buy from the one saving produced.
    """
    position = _next_position(plan, past, rotation)
    if past:
        last_due = past[-1].due_date
    elif start_date:
        # First run: the anchor date IS the first buy, so step back one interval
        # and let `project` add it again.
        last_due = start_date - dt.timedelta(days=interval_days)
    else:
        last_due = today - dt.timedelta(days=interval_days)
    return position, last_due


def project(tickers: list[str], interval_days: int, *, position: int,
            last_due: dt.date, upcoming: int) -> list[tuple[dt.date, str]]:
    """The dates a rotation produces, as (date, ticker).

    Pure: no session, no plan object, no saving. This is the ONE piece of
    arithmetic that says when the next buys fall, and both the saved schedule
    and the unsaved preview go through it — otherwise the preview would be a
    second implementation quietly disagreeing with the real one.
    """
    size = len(tickers)
    if not size or interval_days < 1:
        return []
    return [
        (last_due + dt.timedelta(days=interval_days * (i + 1)),
         tickers[(position + i) % size])
        for i in range(upcoming)
    ]


def preview(session: Session, rotation: list[tuple[int, str]], interval_days: int,
            start_date: dt.date | None, upcoming: int = 24) -> list[tuple[dt.date, str]]:
    """What a DRAFT rotation would produce, without saving anything. The draft
    is (instrument id, how it is shown) per slot.

    Reads the existing plan and history only to find where the rotation stands;
    nothing is written, so opening the editor and pulling chips around cannot
    change the schedule people are following.
    """
    position, last_due = anchor(
        active_plan(session), history(session), interval_days, start_date,
        clock.today(), rotation=[instrument_id for instrument_id, _ in rotation])
    return project([shown for _, shown in rotation], interval_days,
                   position=position, last_due=last_due, upcoming=upcoming)


def schedule(session: Session, upcoming: int = 3) -> dict:
    """Everything the Plan/dashboard views need: what's next, and what's done."""
    plan = active_plan(session)
    today = clock.today()
    past = history(session)
    done = [p for p in past if p.status == "done"]
    base = {
        "plan": plan,
        "next": [],
        "last_done": done[-1] if done else None,
        "history": list(reversed(past)),
        "today": today,
    }
    if plan is None or not plan.entries:
        return base

    size = len(plan.entries)
    position, last_due = anchor(
        plan, past, plan.interval_days, plan.start_date, today)
    dates = project([e.instrument.ticker for e in plan.entries], plan.interval_days,
                    position=position, last_due=last_due, upcoming=upcoming)

    base["next"] = [
        UpcomingBuy(
            ticker=ticker,
            due_date=due,
            entry_id=plan.entries[(position + i) % size].id,
            instrument_id=plan.entries[(position + i) % size].instrument_id,
            amount=plan.amount,
            exchange=plan.entries[(position + i) % size].instrument.exchange,
        )
        for i, (due, ticker) in enumerate(dates)
    ]
    return base


def next_buy(session: Session) -> UpcomingBuy | None:
    sched = schedule(session, upcoming=1)
    return sched["next"][0] if sched["next"] else None


# --------------------------------------------------------------------------- #
# Editing
# --------------------------------------------------------------------------- #

def set_entries(session: Session, plan: InvestmentPlan, instrument_ids: list[int]) -> None:
    """Replace the rotation with this exact ordered list.

    A slot keeps its row (and therefore its id) while its instrument stays in
    the rotation, wherever it moves to, so history recorded against it still
    resolves and the next buy is still the one after the last. An instrument in
    the rotation twice keeps its rows in order. The rest are created/deleted.
    """
    # (plan_id, position) is unique and checked per-statement, so park every row
    # beyond the new range before renumbering into it.
    offset = 1000 + max((e.position for e in plan.entries), default=0)
    unused: dict[int, list[InvestmentPlanEntry]] = {}
    for entry in sorted(plan.entries, key=lambda e: e.position):
        entry.position += offset
        unused.setdefault(entry.instrument_id, []).append(entry)
    session.flush()

    for position, instrument_id in enumerate(instrument_ids):
        rows = unused.get(instrument_id)
        if rows:
            rows.pop(0).position = position
        else:
            session.add(
                InvestmentPlanEntry(
                    plan_id=plan.id, position=position, instrument_id=instrument_id
                )
            )
    for rows in unused.values():
        for entry in rows:
            session.delete(entry)
    session.flush()
    session.expire(plan, ["entries"])


def remove_instrument(session: Session, instrument_id: int) -> bool:
    """Take an instrument out of every plan this portfolio has, closing up the
    rotation behind it. True if any plan had it.

    Not `set_entries`, which keeps a slot's row only where the same instrument
    stays at the same position: every slot after the gap would be a new row,
    the buys recorded against them would resolve to nothing, and the rotation
    would lose its place. Here the slots left keep their rows, so it carries on
    from the last buy still in it.
    """
    removed = False
    for plan in session.scalars(select(InvestmentPlan)).all():
        left = [e for e in plan.entries if e.instrument_id != instrument_id]
        if len(left) == len(plan.entries):
            continue
        removed = True
        offset = 1000 + max(e.position for e in plan.entries)
        for entry in plan.entries:
            if entry.instrument_id == instrument_id:
                session.delete(entry)
            else:
                entry.position += offset    # parked: positions are unique
        session.flush()
        for position, entry in enumerate(left):
            entry.position = position
        session.flush()
        session.expire(plan, ["entries"])
    return removed


# --------------------------------------------------------------------------- #
# Prefilling the trade form from the schedule
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Prefill:
    """A scheduled buy, worked out into the numbers the trade form wants.

    **And the arithmetic that produced them**, because a form that silently
    fills in a quantity is asking to be signed off without being read. The
    number can be wrong — a stale close is the obvious way — and somebody who
    can see how it was reached will notice; somebody shown only `3` will not.
    """

    instrument_id: int
    ticker: str
    units: int
    unit_price: Decimal
    price_date: dt.date
    brokerage: Decimal
    amount: Decimal
    spend: Decimal          # units x price — what this buy actually costs
    leftover: Decimal
    currency: str


def prefill(session: Session) -> Prefill | None:
    """What to put in the trade form for today's scheduled buy, or None.

    None whenever any part is missing — no plan, no amount set, no stored price
    — rather than a partial guess. A half-filled form is worse than an empty
    one: it looks like the app knows something it does not.

    **Whole units only.** You cannot buy part of a share, so the division is
    floored and the remainder is reported rather than hidden. That remainder is
    the same idea as a DRP residual, and is worded the same way.
    """
    upcoming = next_buy(session)
    if upcoming is None or not upcoming.amount:
        return None

    brokerage = active_plan(session).brokerage      # the plan `upcoming` came from

    row = session.execute(
        select(Price.close, Price.date)
        .where(Price.instrument_id == upcoming.instrument_id)
        .order_by(Price.date.desc())
        .limit(1)
    ).first()
    if row is None or not row.close:
        return None

    spend = upcoming.amount - brokerage
    units = int(spend // row.close)
    if units <= 0:
        return None     # not enough for one share, or brokerage swallows the buy

    instrument = session.get(Instrument, upcoming.instrument_id)
    return Prefill(
        instrument_id=upcoming.instrument_id,
        ticker=upcoming.ticker,
        units=units,
        unit_price=row.close,
        price_date=row.date,
        brokerage=brokerage,
        amount=upcoming.amount,
        spend=units * row.close,
        # Kept even though the form now shows the total instead: the schedule's
        # amount rarely divides evenly into whole units, and the difference is
        # a fact worth having available rather than a problem to report.
        leftover=spend - (units * row.close),
        currency=instrument.currency,
    )
