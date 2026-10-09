"""Every route that takes an instrument keeps to this portfolio's own.

The catalogue is shared (decisions.md #125) and a portfolio is shown its own
instruments (#139). Manage holdings already kept to that, and so did saving a
holding's details. The holding page, the price lookup, the trade form and the
API did not: each looked the instrument up in the whole catalogue, so one could
show or take an instrument that only another portfolio has.

Looking up by ticker across the catalogue was also wrong for a reason of its
own: a ticker is unique per exchange, not overall, so two portfolios holding
BHP on different exchanges each risked being shown the other's.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from test_api import bearer, issue_key

import factories as fac
from app import tenancy
from app.models import Portfolio, Trade
from test_routes import make_login, session_csrf

HTML = {"accept": "text/html"}


def _theirs(session_factory) -> dict:
    """Mine holds ACME; a stranger's portfolio holds OMEGA, and BHP on the NYSE
    beside mine on the ASX."""
    with session_factory() as s:
        mine = s.scalar(select(Portfolio))
        stranger = fac.make_user(s, "stranger@example.test")
        theirs = fac.make_portfolio(s, "Theirs", owner=stranger)
        acme = fac.make_instrument(s, "ACME", name="Acme")
        omega = fac.make_instrument(s, "OMEGA", name="Omega Holdings")
        bhp_nyse = fac.make_instrument(s, "BHP", exchange="NYSE", currency="USD",
                                       name="BHP on the NYSE")
        bhp_asx = fac.make_instrument(s, "BHP", name="BHP on the ASX")
        fac.add_trade(s, acme, "2026-01-05", "buy", 10, "5.00", portfolio_id=mine.id)
        fac.add_trade(s, bhp_asx, "2026-01-06", "buy", 7, "40.00", portfolio_id=mine.id)
        fac.add_trade(s, omega, "2026-01-05", "buy", 3, "5.00", portfolio_id=theirs.id)
        fac.add_trade(s, bhp_nyse, "2026-01-07", "buy", 2, "60.00", portfolio_id=theirs.id)
        s.commit()
        return {"acme": acme.id, "omega": omega.id, "mine": mine.id, "theirs": theirs.id,
                "stranger": stranger.id}


def _trades_of(session_factory, instrument_id: int) -> list:
    with session_factory() as s:
        tenancy.allow_unscoped(s)
        return list(s.scalars(select(Trade).where(Trade.instrument_id == instrument_id)))


def test_another_portfolios_instrument_has_no_page_here(client, session_factory):
    make_login(client, session_factory)
    _theirs(session_factory)
    assert client.get("/holding/ACME", headers=HTML).status_code == 200
    page = client.get("/holding/OMEGA", headers=HTML)
    assert page.status_code == 404
    assert "Omega Holdings" not in page.text


def test_a_ticker_on_two_exchanges_shows_this_portfolios_one(client, session_factory):
    make_login(client, session_factory)
    _theirs(session_factory)
    page = client.get("/holding/BHP", headers=HTML).text
    assert "BHP on the ASX" in page and "BHP on the NYSE" not in page


def test_the_price_of_another_portfolios_instrument_is_not_asked_for(client, session_factory):
    make_login(client, session_factory)
    ids = _theirs(session_factory)
    assert client.get(f"/holdings/price?instrument={ids['acme']}&date=2026-07-01"
                      ).status_code == 200
    assert client.get(f"/holdings/price?instrument={ids['omega']}&date=2026-07-01"
                      ).status_code == 404


def test_a_trade_cannot_be_recorded_against_another_portfolios_instrument(
        client, session_factory):
    make_login(client, session_factory)
    ids = _theirs(session_factory)
    resp = client.post("/trade/new", headers=HTML, follow_redirects=False, data={
        "_csrf": session_csrf(session_factory), "instrument_id": str(ids["omega"]),
        "type": "buy", "trade_date": "2026-07-01", "quantity": "1", "unit_price": "1"})
    assert resp.status_code == 200 and "Pick an instrument." in resp.text
    assert [t.portfolio_id for t in _trades_of(session_factory, ids["omega"])] == [ids["theirs"]]
    assert "Omega Holdings" not in client.get("/holdings", headers=HTML).text


@pytest.mark.parametrize("ticker, expected", [("ACME", 201), ("OMEGA", 404)])
def test_the_api_takes_only_this_portfolios_instruments(client, session_factory, ticker,
                                                        expected):
    make_login(client, session_factory)
    ids = _theirs(session_factory)
    with session_factory() as s:
        owner = s.scalar(select(Portfolio).where(Portfolio.id == ids["mine"])).members[0].user_id
    raw = issue_key(session_factory, ids["mine"], created_by=owner, scopes="read,write")
    resp = client.post("/api/v1/trades", headers=bearer(raw), json={
        "ticker": ticker, "date": "2026-07-01", "type": "buy", "units": "1",
        "unit_price": "1"})
    assert resp.status_code == expected, resp.text
    portfolios = [t.portfolio_id for t in _trades_of(session_factory, ids["omega"])]
    assert portfolios == [ids["theirs"]]


@pytest.mark.parametrize("return_to", ["", "/holding/ZULU", "/holding/ZULU?sort=date"])
def test_deleting_the_last_row_never_ends_on_a_404(client, session_factory, return_to):
    """Once a portfolio's last row for an instrument has gone, its holding page
    is gone too, so the delete lands on the portfolio instead."""
    make_login(client, session_factory)
    with session_factory() as s:
        mine = s.scalar(select(Portfolio))
        zulu = fac.make_instrument(s, "ZULU")
        trade = fac.add_trade(s, zulu, "2026-01-05", "buy", 10, "5.00", portfolio_id=mine.id)
        s.commit()
        trade_id = trade.id
    resp = client.post(f"/trade/{trade_id}/delete", headers=HTML, follow_redirects=False,
                       data={"_csrf": session_csrf(session_factory), "return_to": return_to})
    assert resp.headers["location"] == "/"


def test_deleting_the_last_dividend_never_ends_on_a_404(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        mine = s.scalar(select(Portfolio))
        zulu = fac.make_instrument(s, "ZULU")
        dividend = fac.add_dividend(s, zulu, "2026-01-05", "12.00", portfolio_id=mine.id)
        s.commit()
        dividend_id = dividend.id
    resp = client.post(f"/dividend/{dividend_id}/delete", headers=HTML, follow_redirects=False,
                       data={"_csrf": session_csrf(session_factory)})
    assert resp.headers["location"] == "/"


def test_a_delete_that_leaves_the_holding_returns_to_it(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        mine = s.scalar(select(Portfolio))
        zulu = fac.make_instrument(s, "ZULU")
        first = fac.add_trade(s, zulu, "2026-01-05", "buy", 10, "5.00", portfolio_id=mine.id)
        fac.add_trade(s, zulu, "2026-01-06", "buy", 5, "5.00", portfolio_id=mine.id)
        s.commit()
        first_id = first.id
    resp = client.post(f"/trade/{first_id}/delete", headers=HTML, follow_redirects=False,
                       data={"_csrf": session_csrf(session_factory),
                             "return_to": "/holding/ZULU?sort=date"})
    assert resp.headers["location"] == "/holding/ZULU?sort=date"
