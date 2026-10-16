"""Recording a split or a capital change, from the holding's page.

The forms refuse what the timeline cannot hold, as the trade forms do: an
action on a day nothing was held, a consolidation or a deleted split that
leaves a later sale short, a ratio that changes nothing (decisions.md #146).
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import pytest
from formdata import as_sent
from freezegun import freeze_time
from sqlalchemy import select
from test_api import bearer, issue_key

import factories as fac
from app.models import CorporateAction
from test_routes import bind_to_only_portfolio, make_login, reading

HTML = {"accept": "text/html"}
TODAY = "2026-08-03"
NEW = "/action/new"


def _holding(client, session_factory, *, sold=None) -> dict:
    make_login(client, session_factory)
    with session_factory() as s:
        portfolio = bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME", asset_class="share")
        fac.add_trade(s, acme, "2025-01-06", "buy", 100, "10.00")
        if sold:
            fac.add_trade(s, acme, "2025-06-02", "sell", sold, "12.00")
        fac.add_prices(s, acme, [("2026-08-03", "6.00")])
        s.commit()
        return {"acme": acme.id, "portfolio": portfolio.id,
                "user": portfolio.members[0].user_id}


def _form(client, ticker="ACME") -> dict:
    page = client.get(f"{NEW}?ticker={ticker}", headers=HTML)
    assert page.status_code == 200
    return as_sent(page.text, NEW)


def _stored(session_factory) -> list:
    with reading(session_factory) as s:
        return [(a.kind, a.date, a.ratio_new, a.ratio_old, a.amount)
                for a in s.scalars(select(CorporateAction).order_by(CorporateAction.id))]


@freeze_time(TODAY)
def test_a_split_recorded_doubles_the_holding_on_its_page(client, session_factory):
    _holding(client, session_factory)
    sent = {**_form(client), "kind": "split", "action_date": "2025-03-03",
            "ratio_new": "2", "ratio_old": "1"}

    resp = client.post(NEW, data=sent, headers=HTML, follow_redirects=False)

    assert (resp.status_code, resp.headers["location"]) == (303, "/holding/ACME")
    assert _stored(session_factory) == [("split", dt.date(2025, 3, 3), 2, 1, None)]
    page = client.get("/holding/ACME", headers=HTML).text
    assert re.search(r"Units held</span><span class=\"big\">200<", page)
    assert ">split 2 for 1<" in page


@freeze_time(TODAY)
@pytest.mark.parametrize("kind, amount, direction, stored", [
    ("capital_return", "25.00", "", Decimal("25.00")),
    ("cost_base", "30.00", "increase", Decimal("30.00")),
    ("cost_base", "30.00", "decrease", Decimal("-30.00")),
])
def test_a_capital_change_is_stored_with_its_sign(client, session_factory, kind, amount,
                                                  direction, stored):
    _holding(client, session_factory)
    sent = {**_form(client), "kind": kind, "action_date": "2025-06-30", "amount": amount,
            "direction": direction}

    resp = client.post(NEW, data=sent, headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    assert [row[4] for row in _stored(session_factory)] == [stored]


@freeze_time(TODAY)
@pytest.mark.parametrize("change, message", [
    ({"kind": "split", "action_date": "2025-01-03", "ratio_new": "2", "ratio_old": "1"},
     "Nothing was held on 2025-01-03."),
    ({"kind": "split", "action_date": "2025-03-03", "ratio_new": "2", "ratio_old": "2"},
     "A split changes the number of units: new and old can&#39;t be the same."),
    ({"kind": "split", "action_date": "2025-03-03", "ratio_new": "0", "ratio_old": "1"},
     "New and old units are whole numbers from 1 to 1000000."),
    ({"kind": "capital_return", "action_date": "2025-03-03", "amount": "0"},
     "An amount has to be more than zero."),
    ({"kind": "split", "action_date": "2099-01-02", "ratio_new": "2", "ratio_old": "1"},
     "That date is in the future."),
    ({"kind": "split", "action_date": "2025-04-01", "ratio_new": "1", "ratio_old": "10"},
     "the sell of 50 units on 2025-06-02 would then be more than the 10 units held."),
])
def test_what_the_timeline_cannot_hold_is_refused(client, session_factory, change, message):
    _holding(client, session_factory, sold=50)

    resp = client.post(NEW, data={**_form(client), **change}, headers=HTML)

    assert resp.status_code == 400
    assert message in resp.text
    assert _stored(session_factory) == []


@freeze_time(TODAY)
def test_an_action_is_edited_and_deleted_from_the_ledger(client, session_factory):
    ids = _holding(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = s.get(fac.Instrument, ids["acme"])
        action_id = fac.add_action(s, acme, "2025-03-03", "split", ratio=(2, 1)).id
        s.commit()
    page = client.get("/holding/ACME", headers=HTML).text
    assert f'href="/action/{action_id}/edit"' in page
    edit = client.get(f"/action/{action_id}/edit", headers=HTML).text
    sent = as_sent(edit, f"/action/{action_id}/edit")

    client.post(f"/action/{action_id}/edit", data={**sent, "ratio_new": "3"}, headers=HTML)
    assert _stored(session_factory) == [("split", dt.date(2025, 3, 3), 3, 1, None)]
    deleted = client.post(f"/action/{action_id}/delete",
                          data=as_sent(edit, f"/action/{action_id}/delete"),
                          headers=HTML, follow_redirects=False)
    assert (deleted.status_code, _stored(session_factory)) == (303, [])


@freeze_time(TODAY)
def test_deleting_a_split_a_later_sale_depends_on_is_refused(client, session_factory):
    ids = _holding(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = s.get(fac.Instrument, ids["acme"])
        action_id = fac.add_action(s, acme, "2025-03-03", "split", ratio=(2, 1)).id
        fac.add_trade(s, acme, "2025-06-02", "sell", 150, "6.00")
        s.commit()
    edit = client.get(f"/action/{action_id}/edit", headers=HTML).text

    resp = client.post(f"/action/{action_id}/delete",
                       data=as_sent(edit, f"/action/{action_id}/delete"),
                       headers=HTML, follow_redirects=True)

    assert "the sell of 150 units on 2025-06-02 would then be more than the 100" in resp.text
    assert len(_stored(session_factory)) == 1


@freeze_time(TODAY)
def test_another_portfolios_action_is_not_there_to_edit(client, session_factory):
    _holding(client, session_factory)
    with session_factory() as s:
        acme = s.scalar(select(fac.Instrument).where(fac.Instrument.ticker == "ACME"))
        other = fac.make_portfolio(s, "Theirs", owner=fac.make_user(s, "them@example.test"))
        action_id = fac.add_action(s, acme, "2025-03-03", "split", ratio=(2, 1),
                                   portfolio_id=other.id).id
        s.commit()

    assert client.get(f"/action/{action_id}/edit", headers=HTML).status_code == 404


@freeze_time(TODAY)
def test_the_api_lists_them(client, session_factory):
    ids = _holding(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = s.get(fac.Instrument, ids["acme"])
        fac.add_action(s, acme, "2025-03-03", "split", ratio=(2, 1))
        fac.add_action(s, acme, "2025-06-30", "cost_base", amount="-12.50")
        s.commit()
    key = bearer(issue_key(session_factory, ids["portfolio"], created_by=ids["user"]))

    listed = client.get("/api/v1/actions", headers=key).json()["actions"]

    assert [(a["date"], a["ticker"], a["kind"], a["ratio_new"], a["ratio_old"], a["amount"])
            for a in listed] == [("2025-03-03", "ACME", "split", 2, 1, None),
                                 ("2025-06-30", "ACME", "cost_base", None, None, -12.5)]
    holdings = client.get("/api/v1/holdings", headers=key).json()["holdings"]
    assert holdings[0]["units"] == 200


@freeze_time(TODAY)
def test_deleting_every_trade_takes_the_actions_with_them(client, session_factory):
    ids = _holding(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = s.get(fac.Instrument, ids["acme"])
        fac.add_action(s, acme, "2025-03-03", "split", ratio=(2, 1))
        s.commit()
    page = client.get("/holdings", headers=HTML).text

    client.post(f"/holdings/{ids['acme']}/delete-trades",
                data=as_sent(page, f"/holdings/{ids['acme']}/delete-trades"), headers=HTML)

    assert _stored(session_factory) == []
    assert ("Delete every trade and distribution of ACME in this portfolio? "
            "Its splits and capital changes go with it.") in page


@freeze_time(TODAY)
def test_the_delete_confirm_names_no_changes_a_holding_has_none_of(client, session_factory):
    """Another portfolio's split on the same instrument is theirs: naming it
    would say what they hold."""
    ids = _holding(client, session_factory)
    with session_factory() as s:
        acme = s.get(fac.Instrument, ids["acme"])
        other = fac.make_portfolio(s, "Theirs", owner=fac.make_user(s, "them@example.test"))
        fac.add_action(s, acme, "2025-03-03", "split", ratio=(2, 1), portfolio_id=other.id)
        s.commit()

    page = client.get("/holdings", headers=HTML).text

    assert 'data-confirm="Delete every trade and distribution of ACME in this portfolio?"' in page
