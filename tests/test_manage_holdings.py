"""A portfolio sees its own instruments, removes its own, deletes its own trades.

The instrument catalogue is shared (decisions.md #125), and Manage holdings
listed all of it: a portfolio saw every ticker another one had added, and a
removed instrument stayed on the page. Remove stopped the instrument for every
portfolio, even one still following it.

Now Manage holdings and the trade and plan dropdowns list what this portfolio
has (a holding setting, a trade, a dividend or a place in its plan). Remove
drops it from this portfolio, and only stops its prices once no portfolio uses
it. "Delete all trades" clears this portfolio's trades and distributions in
one go, so it can then be removed.
"""

from __future__ import annotations

import html
import json
import re

import pytest
from sqlalchemy import select
from test_pricefeed import stub_yf

import factories as fac
from app import tenancy
from app.models import Dividend, HoldingPref, Instrument, PlannedPurchase, Trade, User
from test_routes import make_login, session_csrf

HTML = {"accept": "text/html"}


@pytest.fixture
def two(client, session_factory):
    """One person, two portfolios. Mine holds ACME; the other holds ZULU and
    has a setting for ACME too."""
    make_login(client, session_factory)
    with session_factory() as s:
        user = s.scalar(select(User))
        mine = s.scalar(select(fac.Portfolio))
        other = fac.make_portfolio(s, "Other", owner=user)
        acme = fac.make_instrument(s, "ACME")
        zulu = fac.make_instrument(s, "ZULU")
        fac.add_trade(s, acme, "2026-01-05", "buy", 10, "5.00", portfolio_id=mine.id)
        fac.add_trade(s, zulu, "2026-01-05", "buy", 10, "5.00", portfolio_id=other.id)
        s.add(HoldingPref(portfolio_id=other.id, instrument_id=acme.id))
        s.commit()
        return {"mine": mine.id, "other": other.id, "acme": acme.id, "zulu": zulu.id}


def _switch(client, session_factory, portfolio_id):
    resp = client.post("/portfolio/switch", data={
        "portfolio_id": str(portfolio_id), "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False)
    assert resp.status_code == 303, resp.text


def _listed(page: str) -> set[str]:
    return set(re.findall(r'<td class="tick"><a href="/holding/([A-Z]+)', page))


def _picker(page: str) -> set[str]:
    select_ = re.search(r'<select name="instrument_id".*?</select>', page, re.S).group(0)
    return set(re.findall(r'data-ticker="([A-Z]+)"', select_))


def test_manage_holdings_lists_this_portfolios_own(client, session_factory, two):
    assert _listed(client.get("/holdings", headers=HTML).text) == {"ACME"}
    _switch(client, session_factory, two["other"])
    assert _listed(client.get("/holdings", headers=HTML).text) == {"ACME", "ZULU"}


@pytest.mark.parametrize("path", ["/trade/new", "/"])
def test_the_trade_dropdowns_list_this_portfolios_own(client, session_factory, two, path):
    assert _picker(client.get(path, headers=HTML).text) == {"ACME"}


def test_the_plan_dropdown_lists_this_portfolios_own(client, session_factory, two):
    page = client.get("/schedule", headers=HTML).text
    assert "ZULU" not in page and "ACME" in page


def test_removing_drops_it_from_this_portfolio_only(client, session_factory, two):
    """The other portfolio still has a setting for ACME — so ACME keeps its
    prices; it just leaves this portfolio once its own trade is gone."""
    _switch(client, session_factory, two["other"])
    resp = client.post(f"/holdings/{two['acme']}/remove",
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML,
                       follow_redirects=False)
    assert resp.status_code == 303, resp.text

    assert _listed(client.get("/holdings", headers=HTML).text) == {"ZULU"}
    with session_factory() as s:
        tenancy.allow_unscoped(s)
        assert s.get(Instrument, two["acme"]).active is True
        assert s.scalar(select(HoldingPref).where(
            HoldingPref.portfolio_id == two["other"],
            HoldingPref.instrument_id == two["acme"])) is None


def test_the_last_portfolio_to_remove_it_stops_its_prices(client, session_factory, two):
    with session_factory() as s:
        s.add(HoldingPref(portfolio_id=two["mine"], instrument_id=two["zulu"]))
        s.commit()
    with session_factory() as s:
        tenancy.allow_unscoped(s)
        for trade in s.scalars(select(Trade).where(Trade.instrument_id == two["zulu"])):
            s.delete(trade)
        s.commit()
    for portfolio in (two["mine"], two["other"]):
        _switch(client, session_factory, portfolio)
        client.post(f"/holdings/{two['zulu']}/remove",
                    data={"_csrf": session_csrf(session_factory)}, headers=HTML)
    with session_factory() as s:
        assert s.get(Instrument, two["zulu"]).active is False


def test_adding_one_removed_everywhere_brings_it_back(client, session_factory, two):
    """Deactivated, not deleted: adding it again lists it and prices it again."""
    with session_factory() as s:
        s.add(HoldingPref(portfolio_id=two["mine"], instrument_id=two["zulu"]))
        s.commit()
    with session_factory() as s:
        tenancy.allow_unscoped(s)
        for trade in s.scalars(select(Trade).where(Trade.instrument_id == two["zulu"])):
            s.delete(trade)
        s.commit()
    for portfolio in (two["other"], two["mine"]):
        _switch(client, session_factory, portfolio)
        client.post(f"/holdings/{two['zulu']}/remove",
                    data={"_csrf": session_csrf(session_factory)}, headers=HTML)
    assert "ZULU" not in _listed(client.get("/holdings", headers=HTML).text)

    client.post("/holdings/add", data={
        "_csrf": session_csrf(session_factory), "ticker": "ZULU", "exchange": "ASX",
        "name": "Zulu", "currency": "AUD", "asset_class": "share"},
        headers=HTML, follow_redirects=False)
    assert "ZULU" in _listed(client.get("/holdings", headers=HTML).text)
    with session_factory() as s:
        assert s.get(Instrument, two["zulu"]).active is True


def test_removing_with_trades_is_still_refused(client, session_factory, two):
    resp = client.post(f"/holdings/{two['acme']}/remove",
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML)
    assert resp.status_code == 409


@pytest.mark.parametrize("action", ["remove", "delete-trades"])
def test_another_portfolios_instrument_is_not_found_here(client, session_factory, two, action):
    resp = client.post(f"/holdings/{two['zulu']}/{action}",
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML,
                       follow_redirects=False)
    assert resp.status_code == 404
    assert "ZULU" not in _listed(client.get("/holdings", headers=HTML).text)
    with session_factory() as s:
        assert s.get(Instrument, two["zulu"]).active


def test_deleting_all_trades_clears_this_portfolio_and_no_other(client, session_factory, two):
    with session_factory() as s:
        tenancy.allow_unscoped(s)          # writing into both portfolios
        acme = s.get(Instrument, two["acme"])
        mine, other = two["mine"], two["other"]
        fac.add_trade(s, acme, "2026-02-05", "sell", 4, "6.00", portfolio_id=mine)
        drp = fac.add_trade(s, acme, "2026-03-05", "drp", 1, "5.50", portfolio_id=mine)
        s.add_all([
            Dividend(instrument_id=acme.id, date=fac.d("2026-03-05"), cash_amount=5,
                     reinvest_trade_id=drp.id, portfolio_id=mine),
            Dividend(instrument_id=acme.id, date=fac.d("2026-04-05"), cash_amount=3,
                     portfolio_id=mine),
        ])
        theirs = fac.add_trade(s, acme, "2026-01-07", "buy", 2, "5.00", portfolio_id=other)
        planned = PlannedPurchase(due_date=fac.d("2026-01-05"), instrument_id=acme.id,
                                  status="done", portfolio_id=mine,
                                  trade_id=s.scalar(select(Trade.id).where(
                                      Trade.portfolio_id == mine,
                                      Trade.date == fac.d("2026-01-05"))))
        s.add(planned)
        s.commit()
        theirs_id, planned_id = theirs.id, planned.id

    resp = client.post(f"/holdings/{two['acme']}/delete-trades",
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML,
                       follow_redirects=False)
    assert resp.status_code == 303, resp.text

    with session_factory() as s:
        tenancy.allow_unscoped(s)
        left = s.scalars(select(Trade).where(Trade.instrument_id == two["acme"])).all()
        assert [t.id for t in left] == [theirs_id]
        assert s.scalars(select(Dividend).where(
            Dividend.portfolio_id == two["mine"])).all() == []
        kept = s.get(PlannedPurchase, planned_id)
        assert kept is not None and kept.trade_id is None
    # Still listed, now with nothing recorded, and it can be removed.
    assert "ACME" in _listed(client.get("/holdings", headers=HTML).text)
    resp = client.post(f"/holdings/{two['acme']}/remove",
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML,
                       follow_redirects=False)
    assert resp.status_code == 303


def test_a_ticker_another_portfolio_added_is_reused_not_refused(
        client, session_factory, two):
    """Not listed here, so "Something not listed" is how this portfolio adds it:
    the shared row is reused, with its price history."""
    resp = client.post("/trade/new", data={
        "_csrf": session_csrf(session_factory), "instrument_id": "new",
        "type": "buy", "trade_date": "2026-03-02", "quantity": "10", "unit_price": "5.00",
        "brokerage": "0", "new_ticker": "ZULU", "new_exchange": "ASX",
        "new_asset_class": "etf", "new_currency": "AUD"}, headers=HTML,
        follow_redirects=False)
    assert resp.status_code == 303, resp.text
    with session_factory() as s:
        tenancy.bind(s, two["mine"], s.scalar(select(User)).id)
        assert [t.instrument_id for t in s.scalars(
            select(Trade).where(Trade.instrument_id == two["zulu"]))] == [two["zulu"]]
    assert "ZULU" in _picker(client.get("/trade/new", headers=HTML).text)


def test_the_add_form_takes_its_currency_from_the_lookup(client, session_factory, two,
                                                         monkeypatch):
    """The field started at AUD, so the lookup's currency never landed and a
    USD stock was added as AUD."""
    page = client.get("/holdings", headers=HTML).text
    assert re.search(r'<input name="currency" value=""', page)

    stub_yf(monkeypatch, info={"longName": "Microsoft", "currency": "USD",
                               "regularMarketPrice": 400.0})
    client.post("/holdings/add", data={
        "_csrf": session_csrf(session_factory), "ticker": "MSFT", "exchange": "NASDAQ",
        "asset_class": "share", "currency": "", "name": "", "yahoo_symbol": ""},
        headers=HTML)
    with session_factory() as s:
        assert s.scalar(select(Instrument.currency).where(Instrument.ticker == "MSFT")) == "USD"


def _plan(session_factory, two, tickers, history=()):
    """A plan over `tickers` in this portfolio, with `history` recorded as
    skipped buys of those slots, by position."""
    from app import plans
    from app.models import InvestmentPlan, PlannedPurchase

    with session_factory() as s:
        tenancy.bind(s, two["mine"], s.scalar(select(User)).id)
        ids = {i.ticker: i.id for i in s.scalars(select(Instrument))}
        for ticker in tickers:
            if ticker not in ids:
                ids[ticker] = fac.make_instrument(s, ticker).id
        plan = tenancy.owned(s, InvestmentPlan(name="Regular", interval_days=28))
        s.add(plan)
        s.flush()
        plans.set_entries(s, plan, [ids[t] for t in tickers])
        entries = {e.position: e for e in s.get(InvestmentPlan, plan.id).entries}
        for day, position in enumerate(history, start=1):
            s.add(tenancy.owned(s, PlannedPurchase(
                due_date=fac.d(f"2026-0{day}-05"), instrument_id=entries[position].instrument_id,
                plan_entry_id=entries[position].id, status="skipped")))
        s.commit()
        return plan.id


def _rotation(session_factory, plan_id):
    from app.models import InvestmentPlan

    with session_factory() as s:
        tenancy.allow_unscoped(s)
        return [(e.position, e.instrument.ticker)
                for e in s.get(InvestmentPlan, plan_id).entries]


def test_removing_one_in_the_plan_takes_it_out_of_the_plan(client, session_factory, two):
    plan_id = _plan(session_factory, two, ["BETA", "ZULU", "GAMA", "ZULU"])
    resp = client.post(f"/holdings/{two['zulu']}/remove",
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML)
    assert resp.status_code == 200
    assert "ZULU removed, and taken out of the DCA plan." in html.unescape(
        re.sub(r"<[^>]+>", "", resp.text))
    assert "ZULU" not in _listed(resp.text)
    assert _rotation(session_factory, plan_id) == [(0, "BETA"), (1, "GAMA")]
    with session_factory() as s:
        assert s.get(Instrument, two["zulu"]).active, "the other portfolio still holds it"


def test_the_plan_carries_on_from_its_last_buy_still_in_it(client, session_factory, two):
    """Bought BETA, then ZULU, then GAMA, so BETA was next. Without ZULU it
    still is: the slots left keep the buys recorded against them."""
    from app import plans

    _plan(session_factory, two, ["BETA", "ZULU", "GAMA"], history=[0, 1, 2])
    client.post(f"/holdings/{two['zulu']}/remove",
                data={"_csrf": session_csrf(session_factory)}, headers=HTML)
    with session_factory() as s:
        tenancy.bind(s, two["mine"], s.scalar(select(User)).id)
        assert plans.next_buy(s).ticker == "BETA"


def test_a_plan_left_empty_still_opens(client, session_factory, two):
    plan_id = _plan(session_factory, two, ["ZULU"])
    client.post(f"/holdings/{two['zulu']}/remove",
                data={"_csrf": session_csrf(session_factory)}, headers=HTML)
    assert _rotation(session_factory, plan_id) == []
    assert client.get("/schedule", headers=HTML).status_code == 200


def test_after_deleting_all_trades_each_portfolios_chart_is_its_own(
        client, session_factory, two):
    """Both charts come from a series cached per portfolio behind a fingerprint
    of its trades: the delete has to redraw this one and leave the other's."""
    with session_factory() as s:
        tenancy.allow_unscoped(s)
        for key in ("acme", "zulu"):
            fac.add_prices(s, s.get(Instrument, two[key]),
                           [("2026-01-05", "5.00"), ("2026-08-07", "7.50")])
        s.commit()

    def chart():
        page = client.get("/", headers=HTML).text
        blob = re.search(r'id="summarydata">(.*?)</script>', page, re.S)
        return json.loads(blob.group(1))["gain"] if blob else None

    assert chart(), "this portfolio's chart, cached"
    _switch(client, session_factory, two["other"])
    theirs = chart()
    assert theirs, "the other portfolio's chart, cached"
    _switch(client, session_factory, two["mine"])

    client.post(f"/holdings/{two['acme']}/delete-trades",
                data={"_csrf": session_csrf(session_factory)}, headers=HTML,
                follow_redirects=False)
    assert chart() is None, "nothing left to plot here"
    _switch(client, session_factory, two["other"])
    assert chart() == theirs


# --------------------------------------------------------------------------- #
# Removing one holding, beside another
# --------------------------------------------------------------------------- #
# A mutation run let removing one holding delete every holding setting in the
# portfolio, and let a trade and a distribution cancel out in the count that
# blocks removal; nothing failed.

def test_removing_one_holding_keeps_the_others_settings(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        portfolio = s.scalar(select(fac.Portfolio))
        alpha, omega = fac.make_instrument(s, "ALPHA"), fac.make_instrument(s, "OMEGA")
        s.add(HoldingPref(portfolio_id=portfolio.id, instrument_id=alpha.id))
        s.add(HoldingPref(portfolio_id=portfolio.id, instrument_id=omega.id,
                          note="keep this", drp=True))
        s.commit()
        alpha_id, omega_id = alpha.id, omega.id

    resp = client.post(f"/holdings/{alpha_id}/remove", headers=HTML, follow_redirects=False,
                       data={"_csrf": session_csrf(session_factory)})

    assert resp.status_code == 303
    with session_factory() as s:
        tenancy.allow_unscoped(s)
        left = [(p.instrument_id, p.note, p.drp) for p in s.scalars(select(HoldingPref))]
    assert left == [(omega_id, "keep this", True)]


def test_a_trade_and_a_distribution_both_stand_in_the_way(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        portfolio = s.scalar(select(fac.Portfolio))
        alpha = fac.make_instrument(s, "ALPHA")
        fac.add_trade(s, alpha, "2026-01-05", "buy", 10, "5.00", portfolio_id=portfolio.id)
        fac.add_dividend(s, alpha, "2026-02-05", "3.00", portfolio_id=portfolio.id)
        s.commit()
        alpha_id = alpha.id

    resp = client.post(f"/holdings/{alpha_id}/remove", headers=HTML, follow_redirects=False,
                       data={"_csrf": session_csrf(session_factory)})

    assert resp.status_code == 409
    assert "2 trade(s) or dividend(s)" in resp.text


def test_a_removal_mentions_the_plan_only_when_the_plan_lost_it(client, session_factory):
    make_login(client, session_factory)

    plain = client.get("/holdings?removed=ACME", headers={"accept": "text/html"}).text
    planned = client.get("/holdings?removed=ACME&unplanned=1",
                         headers={"accept": "text/html"}).text

    assert '<p class="panel ok"><strong>ACME</strong> removed.</p>' in plain
    assert ('<p class="panel warn"><strong>ACME</strong> removed, and taken out of the '
            'DCA plan.</p>') in planned
