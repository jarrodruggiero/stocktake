"""The Plan page and the chart builder's save/preview endpoints.

The plan routes are where a form turns into a rotation, and they guard the two
things that would quietly corrupt it: a ticker nobody holds, and a "next buy"
that has moved on since the form was opened. The chart routes are where a spec
built by dragging becomes a stored one — the check that matters there is that a
chart belongs to a person, not a portfolio.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import pytest
from formdata import as_sent
from freezegun import freeze_time
from sqlalchemy import select

import factories as fac
import fixture_portfolio as ref
from app import chart_templates, money, plans, tenancy
from app.models import (
    InvestmentPlan,
    InvestmentPlanEntry,
    PlannedPurchase,
    PortfolioMember,
    SavedChart,
    Trade,
    User,
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
            fac.hold(s, fac.make_instrument(s, ticker, name=f"{ticker.title()} Ltd"))
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
@pytest.mark.parametrize("field, value", [
    ("amount", "NaN"), ("amount", "Infinity"), ("amount", "1E+999999"),
    ("amount", "10000000000"), ("brokerage", "NaN"), ("brokerage", "1E+999999"),
])
def test_a_figure_that_is_not_finite_or_too_big_is_refused_on_the_plan(
        client, session_factory, holdings, field, value):
    """The plan route caught only `InvalidOperation`, so a `ValueError` from the
    shared parser would have been a 500 here."""
    resp = save_plan(client, session_factory, **{field: value})

    assert resp.status_code == 400
    with reading(session_factory) as s:
        assert s.scalars(select(InvestmentPlan)).all() == []


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
    assert "Next buy is now" in resp.text


@freeze_time(TODAY)
@pytest.mark.parametrize("field,value", [
    ("quantity", "0"), ("unit_price", "-1"), ("quantity", "-1"),
    ("trade_date", "not-a-date"), ("quantity", "many"),
    ("quantity", "NaN"), ("quantity", "Infinity"), ("quantity", "1E+999999"),
    ("unit_price", "NaN"), ("unit_price", "1E+999999"), ("brokerage", "Infinity"),
])
def test_bad_numbers_on_a_completed_buy_are_refused(client, session_factory, holdings,
                                                    field, value):
    """`unit_price` of 0 was here and has moved to the accepted side — a free
    parcel is a real trade, and this route records an ordinary buy like any
    other. Negative is what is left to refuse."""
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
def test_the_preview_of_a_reordered_rotation_agrees_with_saving_it(client, session_factory,
                                                                   holdings):
    """Saving keeps a slot's row with its instrument, so after ACME's buy,
    moving ACME to the end leaves NOVA next. The preview applied the old slot
    number to the draft instead, and showed ACME."""
    save_plan(client, session_factory, start_date="2026-07-06")      # ACME, NOVA
    client.post("/schedule/skip", data={"ticker": "ACME", "due_date": "2026-07-06",
                                        "_csrf": session_csrf(session_factory)}, headers=HTML)

    preview = client.post("/schedule/preview", headers=HTML, data={
        "interval_days": "28", "start_date": "", "tickers": "NOVA, ACME",
        "_csrf": session_csrf(session_factory)}).json()["rows"]
    save_plan(client, session_factory, start_date="2026-07-06", tickers="NOVA, ACME")
    with reading(session_factory) as s:
        saved = plans.next_buy(s)

    assert preview[0] == {"date": "2026-08-03", "ticker": "NOVA"}
    assert (saved.ticker, saved.due_date.isoformat()) == ("NOVA", "2026-08-03")


@freeze_time(TODAY)
def test_an_overdue_buy_is_on_the_page_to_skip(client, session_factory, holdings):
    """The test above posts the skip straight to the route. From the page it
    could not be done: the overdue buy was left off the list."""
    save_plan(client, session_factory, start_date="2026-07-06")

    page = client.get("/schedule", headers=HTML).text

    assert 'name="due_date" value="2026-07-06"' in page
    assert page.count('<li class="due">') == 1          # marked due, and only it


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

    resp = client.post("/charts/preview", json=SPEC,
                       headers={"x-csrf-token": session_csrf(session_factory)})

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
                             "type": "bar"},
                       headers={"x-csrf-token": session_csrf(session_factory)})

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


# Both chart routes read their JSON by hand, so nothing but the handler stood
# between a body of the wrong shape and a 500 (found by test_hostile_input).
WRONG_SHAPES = [
    pytest.param({"name": 12345}, id="a number for a name"),
    pytest.param({"spec": "NaN"}, id="a string for a spec"),
    pytest.param({"spec": {**SPEC, "x": ["ticker"]}}, id="a list for the x axis"),
    pytest.param({"spec": {**SPEC, "measures": [["pos_value"]]}}, id="a list in measures"),
    pytest.param({"id": "NaN"}, id="a word for an id"),
    pytest.param({"id": 2**63}, id="an id no row can have"),
    pytest.param({"template_key": ["bar"]}, id="a list for a template"),
    pytest.param({"template_key": "x" * 500}, id="a template that does not exist"),
]


@freeze_time(ref.TODAY)
@pytest.mark.parametrize("change", WRONG_SHAPES)
def test_a_chart_of_the_wrong_shape_is_refused_and_not_saved(
        client, session_factory, change):
    make_login(client, session_factory)

    resp = client.post("/charts/save",
                       json={"name": "A chart", "spec": SPEC, **change},
                       headers={"X-CSRF-Token": session_csrf(session_factory)})

    assert resp.status_code in (400, 422)
    with reading(session_factory) as s:
        assert s.scalars(select(SavedChart)).all() == []


@freeze_time(ref.TODAY)
@pytest.mark.parametrize("spec", [
    pytest.param({**SPEC, "x": ["ticker"]}, id="a list for the x axis"),
    pytest.param({**SPEC, "measures": "pos_value"}, id="a string for measures"),
    pytest.param({**SPEC, "split": {"a": 1}}, id="an object for the split"),
    pytest.param(["not", "a", "spec"], id="not an object at all"),
])
def test_a_preview_of_the_wrong_shape_is_refused(client, session_factory, spec):
    make_login(client, session_factory)

    resp = client.post("/charts/preview", json=spec,
                       headers={"x-csrf-token": session_csrf(session_factory)})

    assert resp.status_code == 400


@freeze_time(ref.TODAY)
def test_a_chart_keeps_the_template_it_came_from(client, session_factory):
    """The control for the template check: a real key is kept."""
    make_login(client, session_factory)
    key = chart_templates.DEFAULT_KEYS[0]

    resp = client.post("/charts/save",
                       json={"name": "From a template", "spec": SPEC, "template_key": key},
                       headers={"X-CSRF-Token": session_csrf(session_factory)})

    assert resp.status_code == 200
    with reading(session_factory) as s:
        assert s.scalars(select(SavedChart)).one().template_key == key


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


def _chart_order(session_factory) -> list[int]:
    with reading(session_factory) as s:
        return [c.id for c in s.scalars(select(SavedChart).order_by(SavedChart.position))]


def _move_buttons(page: str, chart_id: int) -> dict[str, bool]:
    """Each of a card's two buttons, and whether it can be pressed."""
    form = re.search(rf'<form[^>]*action="/charts/{chart_id}/move".*?</form>', page, re.S)
    assert form, f"no move buttons on chart {chart_id}"
    tags = re.findall(r"<button([^>]*)>", form.group(0))
    return {re.search(r'value="(earlier|later)"', tag).group(1): "disabled" not in tag
            for tag in tags}


@freeze_time(ref.TODAY)
def test_a_chart_moves_a_place_without_being_dragged(client, session_factory):
    """Dragging was the only way, and HTML drag fires for neither a finger
    nor a keyboard (decisions.md #101). Each card has buttons, in a form
    that works with scripting off too."""
    make_login(client, session_factory)
    page = client.get("/charts", headers=HTML).text          # seeds the defaults
    ids = _chart_order(session_factory)
    sent = {**as_sent(page, f"/charts/{ids[1]}/move"), "direction": "earlier"}

    moved = client.post(f"/charts/{ids[1]}/move", data=sent, headers=HTML,
                        follow_redirects=False)

    assert (moved.status_code, moved.headers["location"]) == (303, "/charts")
    assert _chart_order(session_factory) == [ids[1], ids[0], *ids[2:]]
    page = client.get("/charts", headers=HTML).text
    assert _move_buttons(page, ids[1]) == {"earlier": False, "later": True}
    assert _move_buttons(page, ids[-1]) == {"earlier": True, "later": False}


@freeze_time(ref.TODAY)
def test_a_chart_at_an_end_stays_there(client, session_factory):
    make_login(client, session_factory)
    client.get("/charts", headers=HTML)
    ids = _chart_order(session_factory)
    token = session_csrf(session_factory)

    client.post(f"/charts/{ids[0]}/move", data={"_csrf": token, "direction": "earlier"},
                headers=HTML)
    client.post(f"/charts/{ids[-1]}/move", data={"_csrf": token, "direction": "later"},
                headers=HTML)
    refused = client.post(f"/charts/{ids[0]}/move", data={"_csrf": token, "direction": "up"},
                          headers=HTML)

    assert _chart_order(session_factory) == ids
    assert refused.status_code == 400


@freeze_time(ref.TODAY)
def test_the_dashboard_chart_says_what_it_shows(client, session_factory):
    """A canvas is nothing to a screen reader. The label says what is drawn
    and where it ends: total return, which counts sales and income as well."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)

    page = client.get("/", headers=HTML).text

    canvas = re.search(r'<canvas id="summarychart"([^>]*)>', page)
    assert canvas and 'role="img"' in canvas.group(1)
    assert re.search(r'aria-label="Total return: -?[0-9]+\.[0-9]{2}% on 20[0-9]{2}-[0-9]{2}-[0-9]{2}"',
                     canvas.group(1)), canvas.group(1)


def test_a_viewer_builds_deletes_and_arranges_their_own_charts(client, session_factory):
    """Charts are the person's, not the portfolio's, so a viewer keeping a page
    of their own changes nothing of the portfolio. Another person's chart is
    still not theirs to touch."""
    make_login(client, session_factory)
    with session_factory() as s:
        member = s.scalar(select(PortfolioMember))
        member.role = "viewer"
        s.get(User, member.user_id).is_admin = False
        other = fac.make_user(s, "other@example.test")
        theirs = SavedChart(user_id=other.id, portfolio_id=member.portfolio_id, name="Theirs",
                            spec={"grain": "positions", "measures": ["pos_value"],
                                  "type": "table"}, position=0, width="half")
        s.add(theirs)
        s.commit()
        theirs_id = theirs.id
    csrf = {"X-CSRF-Token": session_csrf(session_factory)}
    chart = {"name": "Mine", "width": "half",
             "spec": {"grain": "positions", "x": "ticker", "measures": ["pos_value"],
                      "type": "table"}}

    saved = client.post("/charts/save", json=chart, headers=csrf)
    touched = client.post("/charts/save", json={**chart, "id": theirs_id}, headers=csrf)
    removed = client.post(f"/charts/{saved.json()['id']}/delete", headers={**csrf, **HTML},
                          follow_redirects=False)

    assert saved.status_code == 200 and touched.status_code == 404
    assert removed.status_code == 303
    with session_factory() as s:
        tenancy.allow_unscoped(s)          # both people's charts, not only mine
        assert [c.name for c in s.scalars(select(SavedChart))] == ["Theirs"]


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
def test_the_fy_tab_names_what_its_totals_leave_out(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)
        nova = fac.make_instrument(s, "NOVA", exchange="LSE", currency="GBP",
                                   asset_class="share")
        fac.add_trade(s, nova, "2023-08-01", "buy", 100, "10.00")
        fac.add_trade(s, nova, "2023-09-01", "sell", 100, "12.00")
        s.commit()

    page = client.get(f"/?fy={ref.FY2024}", headers=HTML).text

    assert "Totals exclude NOVA — no exchange rate stored" in page


@freeze_time(ref.TODAY)
def test_the_fy_tab_names_a_sale_of_more_than_was_held(client, session_factory):
    """A ledger from before the forms and the importer refused such a sale.
    The whole tab was a 500; now the year's capital gains are refused, and
    the sale is named with what to do."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)
        quark = fac.make_instrument(s, "QUARK", asset_class="share")
        fac.add_trade(s, quark, "2023-08-01", "buy", 100, "1.00")
        fac.add_trade(s, quark, "2023-09-01", "sell", 150, "2.00")
        s.commit()

    page = client.get(f"/?fy={ref.FY2024}", headers=HTML)

    assert page.status_code == 200
    text = re.sub(r"<[^>]+>", "", page.text)
    assert ("Capital gains for FY23/24 can't be worked out: QUARK's sell of 150 units "
            "on 2023-09-01 is more than the 100 units held then. Correct it or record "
            "the missing buy.") in " ".join(text.split())
    # No figure stands in for the year's gains, not even the other holdings'.
    assert money.plain(ref.FY2024_NET_CAPITAL_GAIN) not in text


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


@freeze_time(ref.TODAY)
@pytest.mark.parametrize("ticker, labelled", [("OMEGA", " (USD)"), ("BETAX", "")])
def test_a_holdings_tiles_say_what_currency_they_are_in(client, session_factory, ticker,
                                                        labelled):
    """OMEGA's tiles read "Value 1,200.00" in US dollars, a page away from
    the dashboard's 1,920.00 in Australian (decisions.md #80). The symbol is
    part of the value, and "$" is both currencies, so a foreign holding's
    tiles name theirs."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)

    page = client.get(f"/holding/{ticker}", headers=HTML).text
    tiles = re.sub(r"<[^>]+>", "", page[page.index('<section class="tiles">'):
                                        page.index("</section>")])

    for label in ("Avg price", "Cost base", "Value", "Dividends"):
        assert re.search(rf"{re.escape(label + labelled)}\s*\$[0-9]", tiles), label


def test_an_unknown_instrument_page_is_a_404(client, session_factory):
    make_login(client, session_factory)

    assert client.get("/holding/NOSUCH", headers=HTML).status_code == 404


@freeze_time(ref.TODAY)
def test_the_instrument_lookup_endpoint_answers(client, session_factory, monkeypatch,
                                                feed_on):
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


# --------------------------------------------------------------------------- #
# What the plan's routes left unchecked
# --------------------------------------------------------------------------- #
# A mutation run removed the lines saving an edited plan's name and brokerage,
# the due-date half of "is this still the next buy", and the AUD/foreign
# choice of a planned buy's rate, and nothing failed.

@freeze_time(TODAY)
def test_saving_again_changes_the_name_and_brokerage_too(client, session_factory, holdings):
    save_plan(client, session_factory)

    save_plan(client, session_factory, name="Monthly", brokerage="4.95")

    with reading(session_factory) as s:
        plan = s.scalars(select(InvestmentPlan)).one()
        assert (plan.name, plan.brokerage) == ("Monthly", Decimal("4.95"))


@freeze_time(TODAY)
@pytest.mark.parametrize("path", ["/schedule/complete", "/schedule/skip"])
def test_a_buy_whose_date_has_moved_on_is_refused(client, session_factory, holdings, path):
    """The right ticker with a stale due date is a form left open while the
    slot was filled; recording it would fill the next slot by mistake."""
    save_plan(client, session_factory, start_date="2026-07-06")

    resp = client.post(path, headers=HTML, data={
        "ticker": "ACME", "due_date": "2026-06-08", "trade_date": "2026-07-06",
        "quantity": "10", "unit_price": "5.00", "_csrf": session_csrf(session_factory)})

    assert resp.status_code == 409
    with reading(session_factory) as s:
        assert s.scalars(select(PlannedPurchase)).all() == []


@freeze_time(TODAY)
@pytest.mark.parametrize("currency, rate", [("USD", None), ("AUD", Decimal(1))])
def test_a_planned_buys_rate_is_1_in_aud_and_left_for_the_feed_otherwise(
        client, session_factory, currency, rate):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fac.hold(s, fac.make_instrument(s, "GAMMA", currency=currency,
                                        exchange="ASX" if currency == "AUD" else "NYSE"))
        s.commit()
    save_plan(client, session_factory, tickers="GAMMA", start_date="2026-07-06")

    client.post("/schedule/complete", headers=HTML, follow_redirects=False, data={
        "ticker": "GAMMA", "due_date": "2026-07-06", "trade_date": "2026-07-06",
        "quantity": "10", "unit_price": "5.00", "_csrf": session_csrf(session_factory)})

    with reading(session_factory) as s:
        assert s.scalars(select(Trade)).one().fx_rate == rate
