"""The plan's rotation under generated sequences of buys, skips and edits.

What is next is computed from what was last recorded: the slot after it
(plans.py's module docstring). Recording against the slot rather than a count
is what lets the rotation be edited without "next" jumping, and the tests had
only ever edited it in ways that kept every slot in place. Reordered, or with
a ticker put in at the front, the slot rows were all replaced, the history no
longer pointed anywhere, and "next" fell back to a count: the ticker just
bought could come up again.

So each case here is a starting rotation and a sequence of steps — complete
the next buy, skip it, set a new rotation, take a ticker out — and after every
step the next three buys must be the ones a person following the plan would
expect: the slot after the last buy whose ticker is still in the rotation, then
round in order, an interval apart.
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

import factories as fac
from app import plans, tenancy
from app.models import InvestmentPlan, PlannedPurchase

POOL = ["ALPHA", "BETAX", "GAMMA", "OMEGA", "ZULU"]
START = dt.date(2026, 1, 5)
INTERVAL = 14

rotations = st.lists(st.sampled_from(POOL), min_size=1, max_size=len(POOL), unique=True)
steps = st.lists(st.one_of(
    st.just(("complete",)), st.just(("skip",)),
    st.tuples(st.just("rotate"), rotations),
    st.tuples(st.just("remove"), st.sampled_from(POOL))), max_size=12)


def _expected(rotation: list, history: list, upcoming: int) -> list:
    """(ticker, due date) for the next buys, from the rotation and what was
    recorded: [ticker, due date, still in the slot it was recorded against]."""
    if not rotation:
        return []
    for ticker, _due, linked in reversed(history):
        if linked and ticker in rotation:
            position = (rotation.index(ticker) + 1) % len(rotation)
            break
    else:
        position = len(history) % len(rotation)
    last = history[-1][1] if history else START - dt.timedelta(days=INTERVAL)
    return [(rotation[(position + i) % len(rotation)],
             last + dt.timedelta(days=INTERVAL * (i + 1))) for i in range(upcoming)]


@given(first=rotations, sequence=steps)
def test_next_is_always_the_slot_after_the_last_buy(session_factory, portfolio, first, sequence):
    s = session_factory()
    tenancy.bind(s, *portfolio)
    try:
        ids = {t: fac.make_instrument(s, t).id for t in POOL}
        plan = tenancy.owned(s, InvestmentPlan(name="Plan", interval_days=INTERVAL,
                                               start_date=START))
        s.add(plan)
        s.flush()
        plans.set_entries(s, plan, [ids[t] for t in first])
        rotation, history = list(first), []

        for step in sequence:
            if step[0] in ("complete", "skip"):
                nxt = plans.next_buy(s)
                if nxt is None:
                    continue
                s.add(tenancy.owned(s, PlannedPurchase(
                    due_date=nxt.due_date, instrument_id=nxt.instrument_id,
                    plan_entry_id=nxt.entry_id,
                    status="done" if step[0] == "complete" else "skipped")))
                s.flush()
                history.append([nxt.ticker, nxt.due_date, True])
            elif step[0] == "rotate":
                plans.set_entries(s, plan, [ids[t] for t in step[1]])
                rotation = list(step[1])
            else:
                plans.remove_instrument(s, ids[step[1]])
                rotation = [t for t in rotation if t != step[1]]
            # A slot whose ticker left the rotation is gone, and so is the link
            # every buy recorded against it had.
            for item in history:
                if item[0] not in rotation:
                    item[2] = False
            got = [(b.ticker, b.due_date) for b in plans.schedule(s, upcoming=3)["next"]]
            assert got == _expected(rotation, history, 3), (first, sequence, step)
    finally:
        s.rollback()
        s.close()


def test_a_reordered_rotation_carries_on_after_the_last_buy(session_factory, portfolio):
    """The case that showed it: four buys round A, B, C leave B next; reordered
    to C, A, B, it is still B, not A again."""
    s = session_factory()
    tenancy.bind(s, *portfolio)
    try:
        a, b, c = (fac.make_instrument(s, t).id for t in ("ALPHA", "BETAX", "GAMMA"))
        plan = tenancy.owned(s, InvestmentPlan(name="Plan", interval_days=INTERVAL,
                                               start_date=START))
        s.add(plan)
        s.flush()
        plans.set_entries(s, plan, [a, b, c])
        for _ in range(4):
            nxt = plans.next_buy(s)
            s.add(tenancy.owned(s, PlannedPurchase(
                due_date=nxt.due_date, instrument_id=nxt.instrument_id,
                plan_entry_id=nxt.entry_id, status="done")))
            s.flush()
        assert plans.next_buy(s).ticker == "BETAX"
        plans.set_entries(s, plan, [c, a, b])
        assert plans.next_buy(s).ticker == "BETAX"
        plans.set_entries(s, plan, [b, c, a])
        assert plans.next_buy(s).ticker == "BETAX"
    finally:
        s.rollback()
        s.close()
