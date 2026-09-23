"""Deleting a DCA plan, which was previously a one-way door.

A plan could be created and edited and never removed, so a rotation somebody
tried once sat on the Plan page offering buys for good. The only way out was to
edit it into something else.

What must survive it is the history. `PlannedPurchase` records what was actually
bought or skipped, and those rows are not part of the plan — the purchase
happened whatever became of the schedule that suggested it. The foreign key is
already `ON DELETE SET NULL` for exactly this, so the rows stay and lose only
the slot they came from; the rotation entries themselves are `delete-orphan` and
go with the plan.
"""

from __future__ import annotations

import datetime as dt

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from test_routes_plan import save_plan

import factories as fac
from app import tenancy
from app.models import (
    InvestmentPlan,
    InvestmentPlanEntry,
    PlannedPurchase,
    PortfolioMember,
)
from test_routes import bind_to_only_portfolio, make_login, session_csrf


@pytest.fixture
def holdings(client, session_factory):
    """Two instruments to build a rotation from, and a signed-in owner.

    Declared here rather than imported from `test_routes_plan`: importing a
    fixture shadows the parameter of the same name, which ruff reports as a
    redefinition and pytest resolves by luck.
    """
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        for ticker in ("ACME", "NOVA"):
            fac.make_instrument(s, ticker, name=f"{ticker.title()} Ltd")
        s.commit()

HTML = {"accept": "text/html"}
TODAY = "2026-09-23"


@freeze_time(TODAY)
def test_the_plan_page_offers_a_way_to_delete_the_plan(client, session_factory,
                                                       holdings):
    save_plan(client, session_factory, start_date="2026-09-25")

    page = client.get("/schedule", headers=HTML).text

    assert 'action="/schedule/delete"' in page


@freeze_time(TODAY)
def test_with_no_plan_there_is_nothing_to_delete(client, session_factory, holdings):
    """No control for an action that cannot apply."""
    page = client.get("/schedule", headers=HTML).text

    assert 'action="/schedule/delete"' not in page


@freeze_time(TODAY)
def test_deleting_the_plan_removes_it_and_its_rotation(client, session_factory,
                                                       holdings):
    save_plan(client, session_factory, start_date="2026-09-25")

    resp = client.post("/schedule/delete",
                       data={"_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    with session_factory() as s:
        bind_to_only_portfolio(s)
        assert s.scalars(select(InvestmentPlan)).all() == []
        assert s.scalars(select(InvestmentPlanEntry)).all() == []


@freeze_time(TODAY)
def test_what_was_already_bought_survives_the_delete(client, session_factory,
                                                     holdings):
    """The point of the `SET NULL`: a recorded purchase is not part of the plan.
    Losing it would rewrite the ledger's history to tidy up a schedule."""
    save_plan(client, session_factory, start_date="2026-09-25")
    with session_factory() as s:
        bind_to_only_portfolio(s)
        entry = s.scalars(select(InvestmentPlanEntry)).first()
        step = PlannedPurchase(due_date=dt.date(2026, 9, 25),
                               instrument_id=entry.instrument_id,
                               plan_entry_id=entry.id, status="done")
        tenancy.owned(s, step)
        s.add(step)
        s.commit()
        step_id = step.id

    client.post("/schedule/delete", data={"_csrf": session_csrf(session_factory)},
                headers=HTML, follow_redirects=False)

    with session_factory() as s:
        bind_to_only_portfolio(s)
        kept = s.get(PlannedPurchase, step_id)
        assert kept is not None
        assert kept.status == "done"
        assert kept.plan_entry_id is None      # the slot is gone, the buy is not


@freeze_time(TODAY)
def test_deleting_needs_the_csrf_token(client, session_factory, holdings):
    save_plan(client, session_factory, start_date="2026-09-25")

    resp = client.post("/schedule/delete", data={}, headers=HTML,
                       follow_redirects=False)

    assert resp.status_code == 403
    with session_factory() as s:
        bind_to_only_portfolio(s)
        assert len(s.scalars(select(InvestmentPlan)).all()) == 1


@freeze_time(TODAY)
def test_a_viewer_cannot_delete_the_plan(client, session_factory, holdings):
    save_plan(client, session_factory, start_date="2026-09-25")
    with session_factory() as s:
        s.scalars(select(PortfolioMember)).first().role = "viewer"
        s.commit()

    resp = client.post("/schedule/delete",
                       data={"_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 403


@freeze_time(TODAY)
def test_deleting_when_there_is_no_plan_is_a_404(client, session_factory, holdings):
    resp = client.post("/schedule/delete",
                       data={"_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 404
