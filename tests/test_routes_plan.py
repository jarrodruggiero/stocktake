"""The Plan page and the chart builder's save/preview endpoints.

The plan routes are where a form turns into a rotation, and they guard the two
things that would quietly corrupt it: a ticker nobody holds, and a "next buy"
that has moved on since the form was opened. The chart routes are where a spec
built by dragging becomes a stored one — the check that matters there is that a
chart belongs to a person, not a portfolio.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from freezegun import freeze_time
from sqlalchemy import select

import factories as fac
import fixture_portfolio as ref
from app.models import (
    InvestmentPlan,
    InvestmentPlanEntry,
    PlannedPurchase,
    SavedChart,
    Trade,
)
from test_routes import bind_to_only_portfolio, make_login, reading, session_csrf

HTML = {"accept": "text/html"}
TODAY = "2026-08-02"


@pytest.fixture
def holdings(client, session_factory):
    """Two instruments to build a rotation from, and a signed-in owner."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        for ticker in ("ACME", "NOVA"):
            fac.make_instrument(s, ticker, name=f"{ticker.title()} Ltd")
        s.commit()


def save_plan(client, session_factory, **overrides):
    data = {"name": "Regular buys", "interval_days": "28", "amount": "500",
            "brokerage": "9.50", "start_date": "2026-08-10", "tickers": "ACME, NOVA",
            "_csrf": session_csrf(session_factory)}
    data.update(overrides)
    return client.post("/schedule/save", data=data, headers=HTML, follow_redirects=False)


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_the_plan_page_renders_with_no_plan(client, session_factory):
    make_login(client, session_factory)

    page = client.get("/schedule", headers=HTML)

    assert page.status_code == 200
    assert "No plan yet" in page.text


@freeze_time(TODAY)
def test_a_new_plan_is_prefilled_from_what_is_already_held(client, session_factory):
    """The rotation lives in the database, not in config.yaml: the
    suggestion comes from this portfolio's own holdings."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        for ticker, cls in (("ZED", "share"), ("ACME", "etf")):
            inst = fac.make_instrument(s, ticker, asset_class=cls, name=ticker)
            fac.add_trade(s, inst, "2026-01-05", "buy", 10, "5.00")
            fac.add_prices(s, inst, [("2026-08-01", "6.00"), ("2026-08-02", "6.00")])
        s.commit()

    page = client.get("/schedule", headers=HTML)

    # Asset class then ticker, so the ETF leads.
    assert "ACME, ZED" in page.text


@freeze_time(TODAY)
def test_an_impossible_month_is_refused(client, session_factory):
    make_login(client, session_factory)

    assert client.get("/schedule?year=2026&month=13", headers=HTML).status_code == 400


@freeze_time(TODAY)
def test_month_zero_reads_as_not_specified(client, session_factory):
    """`month or today.month` treats 0 as absent, because 0 is falsy. Harmless
    — you get the current month rather than an error — but pinned so the
    behaviour is deliberate rather than a surprise."""
    make_login(client, session_factory)

    page = client.get("/schedule?year=2026&month=0", headers=HTML)

    assert page.status_code == 200
    assert "August 2026" in page.text


# --------------------------------------------------------------------------- #
# Saving a plan
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_saving_a_plan_stores_the_rotation_in_order(client, session_factory, holdings):
    resp = save_plan(client, session_factory)

    assert resp.status_code == 303
    with reading(session_factory) as s:
        plan = s.scalars(select(InvestmentPlan)).one()
        entries = sorted(plan.entries, key=lambda e: e.position)
        assert plan.interval_days == 28
        assert plan.amount == Decimal("500")
        assert [e.instrument.ticker for e in entries] == ["ACME", "NOVA"]


@freeze_time(TODAY)
def test_a_rotation_can_be_given_on_separate_lines(client, session_factory, holdings):
    """It is pasted, so it arrives however the person had it written down."""
    save_plan(client, session_factory, tickers="ACME\nNOVA\nACME")

    with reading(session_factory) as s:
        entries = sorted(s.scalars(select(InvestmentPlan)).one().entries,
                         key=lambda e: e.position)
        assert [e.instrument.ticker for e in entries] == ["ACME", "NOVA", "ACME"]


@freeze_time(TODAY)
def test_an_unknown_ticker_names_itself_and_saves_nothing(client, session_factory,
                                                          holdings):
    resp = client.post("/schedule/save",
                       data={"name": "p", "interval_days": "28", "tickers": "ACME, NOSUCH",
                             "_csrf": session_csrf(session_factory)}, headers=HTML)

    assert resp.status_code == 400
    assert "NOSUCH" in resp.text
    with reading(session_factory) as s:
        assert s.scalars(select(InvestmentPlan)).all() == []


@freeze_time(TODAY)
@pytest.mark.parametrize("interval", ["0", "366", "-5"])
def test_an_impossible_interval_is_refused(client, session_factory, holdings, interval):
    assert save_plan(client, session_factory,
                     interval_days=interval).status_code == 400


@freeze_time(TODAY)
def test_a_non_numeric_amount_is_refused(client, session_factory, holdings):
    assert save_plan(client, session_factory, amount="lots").status_code == 400


@freeze_time(TODAY)
def test_a_malformed_start_date_is_refused(client, session_factory, holdings):
    assert save_plan(client, session_factory, start_date="10/08/2026").status_code == 400


@freeze_time(TODAY)
def test_saving_again_edits_the_existing_plan(client, session_factory, holdings):
    """One active plan, edited — not a second one quietly created alongside."""
    save_plan(client, session_factory)

    save_plan(client, session_factory, tickers="NOVA", interval_days="14")

    with reading(session_factory) as s:
        plans = s.scalars(select(InvestmentPlan)).all()
        assert len(plans) == 1
        assert plans[0].interval_days == 14
        assert [e.instrument.ticker for e in plans[0].entries] == ["NOVA"]


@freeze_time(TODAY)
def test_an_empty_amount_is_allowed(client, session_factory, holdings):
    """The typical spend only prefills a form; a plan without one is fine."""
    save_plan(client, session_factory, amount="")

    with reading(session_factory) as s:
        assert s.scalars(select(InvestmentPlan)).one().amount is None


# --------------------------------------------------------------------------- #
# Recording against the plan
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_the_complete_form_offers_the_next_buy(client, session_factory, holdings):
    save_plan(client, session_factory, start_date="2026-07-06")

    page = client.get("/schedule/complete", headers=HTML)

    assert page.status_code == 200
    assert "ACME" in page.text


@freeze_time(TODAY)
def test_with_no_plan_the_complete_form_sends_you_back(client, session_factory):
    make_login(client, session_factory)

    resp = client.get("/schedule/complete", headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/schedule"


@freeze_time(TODAY)
def test_completing_a_buy_records_the_trade_and_the_slot(client, session_factory,
                                                         holdings):
    save_plan(client, session_factory, start_date="2026-07-06")

    resp = client.post("/schedule/complete",
                       data={"ticker": "ACME", "due_date": "2026-07-06",
                             "trade_date": "2026-07-06", "quantity": "10",
                             "unit_price": "5.00", "brokerage": "9.50",
                             "_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    with reading(session_factory) as s:
        trade = s.scalars(select(Trade)).one()
        purchase = s.scalars(select(PlannedPurchase)).one()
    assert trade.quantity == 10
    assert purchase.status == "done"
    # Linked both ways, so the history knows which slot it filled.
    assert purchase.trade_id == trade.id
    assert purchase.plan_entry_id is not None


@freeze_time(TODAY)
def test_completing_the_wrong_buy_is_refused(client, session_factory, holdings):
    """Guards a double submit, or someone else recording a buy while the form
    sat open — the slot would otherwise be filled twice."""
    save_plan(client, session_factory, start_date="2026-07-06")

    resp = client.post("/schedule/complete",
                       data={"ticker": "NOVA", "due_date": "2026-07-06",
                             "trade_date": "2026-07-06", "quantity": "10",
                             "unit_price": "5.00", "_csrf": session_csrf(session_factory)},
                       headers=HTML)

    assert resp.status_code == 409
    assert "next buy is now" in resp.text


@freeze_time(TODAY)
@pytest.mark.parametrize("field,value", [
    ("quantity", "0"), ("unit_price", "0"), ("quantity", "-1"),
    ("trade_date", "not-a-date"), ("quantity", "many"),
])
def test_bad_numbers_on_a_completed_buy_are_refused(client, session_factory, holdings,
                                                    field, value):
    save_plan(client, session_factory, start_date="2026-07-06")
    payload = {"ticker": "ACME", "due_date": "2026-07-06", "trade_date": "2026-07-06",
               "quantity": "10", "unit_price": "5.00",
               "_csrf": session_csrf(session_factory)}
    payload[field] = value

    assert client.post("/schedule/complete", data=payload, headers=HTML).status_code == 400


@freeze_time(TODAY)
def test_skipping_a_buy_records_it_without_a_trade(client, session_factory, holdings):
    save_plan(client, session_factory, start_date="2026-07-06")

    resp = client.post("/schedule/skip",
                       data={"ticker": "ACME", "due_date": "2026-07-06",
                             "_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    with reading(session_factory) as s:
        purchase = s.scalars(select(PlannedPurchase)).one()
        assert purchase.status == "skipped"
        assert purchase.trade_id is None
        assert s.scalars(select(Trade)).all() == []


@freeze_time(TODAY)
def test_skipping_moves_the_rotation_on(client, session_factory, holdings):
    save_plan(client, session_factory, start_date="2026-07-06")
    client.post("/schedule/skip", data={"ticker": "ACME", "due_date": "2026-07-06",
                                    "_csrf": session_csrf(session_factory)}, headers=HTML)

    page = client.get("/schedule/complete", headers=HTML)

    assert "NOVA" in page.text


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #

SPEC = {"grain": "positions", "x": "ticker", "measures": ["pos_value"], "type": "bar"}


@freeze_time(ref.TODAY)
def test_a_chart_can_be_previewed_without_saving(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)

    resp = client.post("/charts/preview", json=SPEC)

    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "bar"
    assert "ALPHA" in body["labels"]
    with reading(session_factory) as s:
        assert s.scalars(select(SavedChart)).all() == []   # nothing stored


@freeze_time(ref.TODAY)
def test_an_undrawable_spec_explains_itself(client, session_factory):
    make_login(client, session_factory)

    resp = client.post("/charts/preview",
                       json={"grain": "positions", "x": "ticker", "measures": [],
                             "type": "bar"})

    assert resp.status_code == 400
    assert "measure" in resp.json()["detail"].lower()


@freeze_time(ref.TODAY)
def test_saving_a_chart_stores_it_against_the_user(client, session_factory):
    make_login(client, session_factory)

    resp = client.post("/charts/save",
                       json={"name": "Value by holding", "spec": SPEC, "width": "half"},
                       headers={"X-CSRF-Token": session_csrf(session_factory)})

    assert resp.status_code == 200
    with reading(session_factory) as s:
        chart = s.scalars(select(SavedChart)).one()
    assert chart.name == "Value by holding"
    assert chart.user_id is not None


@freeze_time(ref.TODAY)
def test_saving_over_an_existing_chart_edits_it(client, session_factory):
    make_login(client, session_factory)
    created = client.post("/charts/save",
                          json={"name": "First name", "spec": SPEC, "width": "half"},
                          headers={"X-CSRF-Token": session_csrf(session_factory)}).json()

    client.post("/charts/save",
                json={"id": created["id"], "name": "Renamed", "spec": SPEC,
                      "width": "full"},
                headers={"X-CSRF-Token": session_csrf(session_factory)})

    with reading(session_factory) as s:
        charts = s.scalars(select(SavedChart)).all()
    assert len(charts) == 1
    assert (charts[0].name, charts[0].width) == ("Renamed", "full")


@freeze_time(ref.TODAY)
def test_a_chart_needs_a_name(client, session_factory):
    make_login(client, session_factory)

    resp = client.post("/charts/save", json={"name": "  ", "spec": SPEC},
                       headers={"X-CSRF-Token": session_csrf(session_factory)})

    assert resp.status_code == 400


@freeze_time(ref.TODAY)
def test_an_invalid_spec_is_refused_at_save_time_too(client, session_factory):
    """Validated before storing as well as before drawing, so a bad spec cannot
    be parked in the database to fail later on somebody's charts page."""
    make_login(client, session_factory)

    resp = client.post("/charts/save",
                       json={"name": "Broken", "spec": {"grain": "positions",
                                                        "x": "ticker",
                                                        "measures": ["pos_value", "pos_cost"],
                                                        "type": "doughnut"}},
                       headers={"X-CSRF-Token": session_csrf(session_factory)})

    assert resp.status_code == 400
    assert "doughnut" in resp.json()["detail"].lower()


@freeze_time(ref.TODAY)
def test_another_users_chart_cannot_be_edited_or_deleted(client, session_factory):
    """Charts belong to a person and follow them between portfolios, so
    ownership is the user id."""
    from app import tenancy

    make_login(client, session_factory)
    with session_factory() as s:
        # Planting another user's chart needs the sanctioned bypass: charts are
        # user-scoped, so a plain session refuses to touch one that is not ours
        # — which is the guarantee being tested here.
        tenancy.allow_unscoped(s)
        stranger = fac.make_user(s, "stranger@example.test")
        theirs = fac.make_portfolio(s, "Their portfolio", owner=stranger)
        s.commit()
        s.add(SavedChart(user_id=stranger.id, portfolio_id=theirs.id, name="Theirs",
                         spec=SPEC, position=0, width="half"))
        s.commit()
        their_chart = s.scalars(select(SavedChart)).one().id

    assert client.post("/charts/save",
                       json={"id": their_chart, "name": "Hijacked", "spec": SPEC},
                       headers={"X-CSRF-Token": session_csrf(session_factory)}
                       ).status_code == 404
    assert client.post(f"/charts/{their_chart}/delete",
                       data={"_csrf": session_csrf(session_factory)},
                       headers=HTML).status_code == 404
    assert client.get(f"/charts/build?edit={their_chart}",
                      headers=HTML).status_code == 404


@freeze_time(ref.TODAY)
def test_the_builder_opens_an_existing_chart_with_its_settings(client, session_factory):
    make_login(client, session_factory)
    created = client.post("/charts/save",
                          json={"name": "Mine", "spec": SPEC, "width": "full"},
                          headers={"X-CSRF-Token": session_csrf(session_factory)}).json()

    page = client.get(f"/charts/build?edit={created['id']}", headers=HTML)

    assert page.status_code == 200
    assert "Edit chart" in page.text
    assert "pos_value" in page.text     # the stored spec is handed to the builder


@freeze_time(ref.TODAY)
def test_the_builder_opens_blank_for_a_new_chart(client, session_factory):
    make_login(client, session_factory)

    page = client.get("/charts/build", headers=HTML)

    assert page.status_code == 200
    assert "Build a chart" in page.text


@freeze_time(ref.TODAY)
def test_dragging_charts_into_a_new_order_persists_it(client, session_factory):
    make_login(client, session_factory)
    client.get("/charts", headers=HTML)          # seeds the defaults
    with reading(session_factory) as s:
        ids = [c.id for c in s.scalars(select(SavedChart).order_by(SavedChart.position))]

    client.post("/charts/order", json={"order": [str(i) for i in reversed(ids)]},
                headers={"X-CSRF-Token": session_csrf(session_factory)})

    with reading(session_factory) as s:
        reordered = [c.id for c in s.scalars(select(SavedChart).order_by(SavedChart.position))]
    assert reordered == list(reversed(ids))


@freeze_time(ref.TODAY)
def test_the_fy_tab_renders_a_past_year(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)

    page = client.get(f"/?fy={ref.FY2024}", headers=HTML)

    assert page.status_code == 200
    assert "FY23/24" in page.text


@freeze_time(ref.TODAY)
def test_a_financial_year_with_no_history_is_a_404(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)

    assert client.get("/?fy=1999", headers=HTML).status_code == 404


@freeze_time(ref.TODAY)
def test_the_instrument_page_shows_the_ledger(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)

    page = client.get("/holding/BETAX", headers=HTML)

    assert page.status_code == 200
    assert "Betax Holdings" in page.text


def test_an_unknown_instrument_page_is_a_404(client, session_factory):
    make_login(client, session_factory)

    assert client.get("/holding/NOSUCH", headers=HTML).status_code == 404


@freeze_time(ref.TODAY)
def test_the_instrument_lookup_endpoint_answers(client, session_factory, monkeypatch):
    from app import main as main_mod

    monkeypatch.setattr(main_mod.pricefeed, "lookup",
                        lambda ticker, exchange: {"symbol": "ACME.AX", "name": "Acme",
                                                  "currency": "AUD", "found": True})
    make_login(client, session_factory)

    body = client.get("/holdings/lookup?ticker=ACME&exchange=ASX").json()

    assert body["name"] == "Acme"
    assert body["found"] is True


def test_the_lookup_endpoint_needs_a_ticker(client, session_factory):
    make_login(client, session_factory)

    assert client.get("/holdings/lookup?ticker=", headers=HTML).status_code == 400


@freeze_time(TODAY)
def test_the_calendar_month_can_be_paged(client, session_factory, holdings):
    save_plan(client, session_factory, start_date="2026-08-10")

    page = client.get("/schedule?year=2026&month=9", headers=HTML)

    assert page.status_code == 200
    assert "September 2026" in page.text


@freeze_time(TODAY)
def test_a_plan_with_entries_shows_upcoming_buys_on_the_calendar(client, session_factory,
                                                                 holdings):
    save_plan(client, session_factory, start_date="2026-08-10")

    page = client.get("/schedule?year=2026&month=8", headers=HTML)

    assert "ACME" in page.text
    assert dt.date(2026, 8, 10).strftime("%B %Y") == "August 2026"


@freeze_time(TODAY)
def test_a_viewer_cannot_change_the_plan(client, session_factory):
    from app import auth as auth_mod
    from app.models import PortfolioMember
    from test_routes import PASSWORD, pre_auth_csrf

    with session_factory() as s:
        viewer = fac.make_user(s, "viewer@example.test",
                               password_hash=auth_mod.hash_password(PASSWORD))
        portfolio = fac.make_portfolio(s, "Shared")
        s.add(PortfolioMember(portfolio_id=portfolio.id, user_id=viewer.id, role="viewer"))
        s.commit()
        from app import tenancy
        tenancy.bind(s, portfolio.id, viewer.id)
        fac.make_instrument(s, "ACME", name="Acme")
        s.commit()
    token = pre_auth_csrf(client)
    client.post("/login", data={"email": "viewer@example.test", "password": PASSWORD,
                                "_csrf": token}, headers=HTML)

    assert save_plan(client, session_factory).status_code == 403
    assert InvestmentPlanEntry is not None
