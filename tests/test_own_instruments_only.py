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

import re

import pytest
from sqlalchemy import select
from test_api import bearer, issue_key
from test_imports_web import staged_id, upload_csv

import factories as fac
from app import queries, tenancy
from app.models import Instrument, Portfolio, Trade
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


def test_holdings_are_built_from_this_portfolios_instruments_only(client, session_factory):
    """`all_holdings` built one for every instrument in the catalogue, three
    queries apiece, and the dashboard read any at all as something recorded
    here: no portfolio after the first was offered the first-run panel."""
    make_login(client, session_factory)
    ids = _theirs(session_factory)

    with session_factory() as s:
        tenancy.bind(s, ids["mine"], None)
        built = sorted((h.instrument.ticker, h.instrument.exchange)
                       for h in queries.all_holdings(s))

    assert built == [("ACME", "ASX"), ("BHP", "ASX")]


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


def _writer(session_factory, ids) -> dict:
    with session_factory() as s:
        owner = s.scalar(select(Portfolio).where(Portfolio.id == ids["mine"])).members[0].user_id
    return bearer(issue_key(session_factory, ids["mine"], created_by=owner,
                            scopes="read,write"))


@pytest.fixture
def new_allowed(monkeypatch):
    """The suite's config has new instruments off, as an install may."""
    from app import main

    monkeypatch.setattr(main.settings.imports, "allow_new_instruments", True)


def _catalogue(session_factory, ticker: str) -> list:
    with session_factory() as s:
        return list(s.scalars(select(Instrument).where(Instrument.ticker == ticker)))


def test_the_api_records_against_this_portfolios_instruments(client, session_factory):
    make_login(client, session_factory)
    ids = _theirs(session_factory)
    resp = client.post("/api/v1/trades", headers=_writer(session_factory, ids), json={
        "ticker": "ACME", "date": "2026-07-01", "type": "buy", "units": "1",
        "unit_price": "1"})
    assert resp.status_code == 201, resp.text
    assert len(_trades_of(session_factory, ids["acme"])) == 2


# OMEGA is in the shared catalogue because another portfolio added it; NOVEL is
# in no portfolio at all. The API must answer the two the same way, or it tells
# this portfolio what another one holds (#139).
@pytest.mark.parametrize("given", [{}, {"exchange": "ASX"}, {"currency": "AUD"}],
                         ids=["neither", "exchange", "currency"])
@pytest.mark.parametrize("ticker", ["OMEGA", "NOVEL"])
def test_the_api_asks_where_an_instrument_new_here_trades(client, session_factory, new_allowed,
                                                          ticker, given):
    make_login(client, session_factory)
    ids = _theirs(session_factory)
    resp = client.post("/api/v1/trades", headers=_writer(session_factory, ids), json={
        "ticker": ticker, "date": "2026-07-01", "type": "buy", "units": "1",
        "unit_price": "1", **given})

    assert resp.status_code == 404
    assert resp.json()["detail"] == (
        f"{ticker} isn't in this portfolio. To add it, give its exchange and currency.")
    assert [t.portfolio_id for t in _trades_of(session_factory, ids["omega"])] == [ids["theirs"]]
    assert len(_catalogue(session_factory, ticker)) == (1 if ticker == "OMEGA" else 0)


@pytest.mark.parametrize("ticker", ["OMEGA", "NOVEL"])
def test_the_api_adds_an_instrument_as_the_add_form_does(client, session_factory, new_allowed, ticker):
    """Given where it trades, the API adds the instrument to this portfolio:
    the catalogue's entry if there is one, a new one if not, and the same
    answer either way."""
    make_login(client, session_factory)
    ids = _theirs(session_factory)
    resp = client.post("/api/v1/trades", headers=_writer(session_factory, ids), json={
        "ticker": ticker.lower(), "exchange": "asx", "currency": "aud", "date": "2026-07-01",
        "type": "buy", "units": "2", "unit_price": "3"})

    assert resp.status_code == 201, resp.text
    assert {k: v for k, v in resp.json().items() if k != "id"} == {
        "ticker": ticker, "exchange": "ASX", "date": "2026-07-01"}
    [inst] = _catalogue(session_factory, ticker)
    assert sorted(t.portfolio_id for t in _trades_of(session_factory, inst.id)) == sorted(
        [ids["mine"]] + ([ids["theirs"]] if ticker == "OMEGA" else []))
    if ticker == "NOVEL":
        assert inst.yahoo_symbol == "NOVEL.AX"
    with session_factory() as s:
        tenancy.bind(s, ids["mine"], None)
        assert inst.id in queries.portfolio_instrument_ids(s)


def test_an_instrument_every_portfolio_removed_comes_back(client, session_factory, new_allowed):
    make_login(client, session_factory)
    ids = _theirs(session_factory)
    with session_factory() as s:
        s.get(Instrument, ids["omega"]).active = False
        s.commit()
    resp = client.post("/api/v1/trades", headers=_writer(session_factory, ids), json={
        "ticker": "OMEGA", "exchange": "ASX", "currency": "AUD", "date": "2026-07-01",
        "type": "buy", "units": "1", "unit_price": "1"})

    assert resp.status_code == 201, resp.text
    with session_factory() as s:
        assert s.get(Instrument, ids["omega"]).active is True


def test_the_catalogues_details_win_over_the_apis(client, session_factory, new_allowed):
    """As on the add form: an entry already in the catalogue is reused as it
    is. Refusing a currency that disagrees with it would say it was there."""
    make_login(client, session_factory)
    ids = _theirs(session_factory)
    resp = client.post("/api/v1/trades", headers=_writer(session_factory, ids), json={
        "ticker": "OMEGA", "exchange": "ASX", "currency": "USD", "date": "2026-07-01",
        "type": "buy", "units": "1", "unit_price": "1"})

    assert resp.status_code == 201, resp.text
    [inst] = _catalogue(session_factory, "OMEGA")
    assert inst.currency == "AUD"


def test_a_new_instrument_takes_the_currency_and_class_given(client, session_factory, new_allowed):
    make_login(client, session_factory)
    ids = _theirs(session_factory)
    resp = client.post("/api/v1/trades", headers=_writer(session_factory, ids), json={
        "ticker": "NOVEL", "exchange": "NASDAQ", "currency": "USD", "asset_class": "etf",
        "date": "2026-07-01", "type": "buy", "units": "1", "unit_price": "1",
        "fx_rate": "1.5"})

    assert resp.status_code == 201, resp.text
    [inst] = _catalogue(session_factory, "NOVEL")
    assert (inst.exchange, inst.currency, inst.asset_class, inst.yahoo_symbol) == (
        "NASDAQ", "USD", "etf", "NOVEL")


@pytest.mark.parametrize("ticker", ["OMEGA", "NOVEL"])
def test_with_new_instruments_off_the_api_refuses_both_alike(client, session_factory,
                                                             monkeypatch, ticker):
    from app import main

    monkeypatch.setattr(main.settings.imports, "allow_new_instruments", False)
    make_login(client, session_factory)
    ids = _theirs(session_factory)
    resp = client.post("/api/v1/trades", headers=_writer(session_factory, ids), json={
        "ticker": ticker, "exchange": "ASX", "currency": "AUD", "date": "2026-07-01",
        "type": "buy", "units": "1", "unit_price": "1"})

    assert resp.status_code == 404
    assert resp.json()["detail"] == f"{ticker} isn't in this portfolio. Add it in the app first."
    assert [t.portfolio_id for t in _trades_of(session_factory, ids["omega"])] == [ids["theirs"]]


@pytest.mark.parametrize("field, value", [("exchange", "a b"), ("currency", "dollars"),
                                          ("currency", "a1b"), ("asset_class", "bond")])
def test_a_new_instrument_given_nonsense_is_refused(client, session_factory, new_allowed,
                                                    field, value):
    make_login(client, session_factory)
    ids = _theirs(session_factory)
    body = {"ticker": "NOVEL", "exchange": "ASX", "currency": "AUD", "date": "2026-07-01",
            "type": "buy", "units": "1", "unit_price": "1", field: value}
    resp = client.post("/api/v1/trades", headers=_writer(session_factory, ids), json=body)

    assert resp.status_code in (400, 422)
    assert _catalogue(session_factory, "NOVEL") == []


def test_a_dividend_adds_its_instrument_too(client, session_factory, new_allowed):
    make_login(client, session_factory)
    ids = _theirs(session_factory)
    resp = client.post("/api/v1/dividends", headers=_writer(session_factory, ids), json={
        "ticker": "OMEGA", "exchange": "ASX", "currency": "AUD", "date": "2026-07-01",
        "cash_amount": "4.50"})

    assert resp.status_code == 201, resp.text
    with session_factory() as s:
        tenancy.bind(s, ids["mine"], None)
        assert ids["omega"] in queries.portfolio_instrument_ids(s)


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


# --------------------------------------------------------------------------- #
# A dividend statement
# --------------------------------------------------------------------------- #
# The statement upload offered every ticker in the catalogue to choose from,
# guessed among every instrument, and recorded against the first instrument
# with the ticker it was given: BHP on the NYSE, made first, for mine on the
# ASX; OMEGA, which only the stranger holds, without a word.

def test_a_statement_offers_and_guesses_only_this_portfolios_instruments(
        client, session_factory, monkeypatch):
    import re

    from app import statements

    make_login(client, session_factory)
    _theirs(session_factory)
    monkeypatch.setattr(statements, "_pdf_text", lambda data: (
        "Omega Holdings distribution advice\nOMEGA\nPayment Date: 15 March 2026\n"
        "Net Amount: $10.00\n"))

    page = client.post("/imports-exports/statement",
                       files={"file": ("advice.pdf", b"%PDF-1.4", "application/pdf")},
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML)

    assert page.status_code == 200
    assert re.findall(r'<option value="([A-Z]+)"', page.text) == ["ACME", "BHP"]
    assert re.search(r'<option value="[A-Z]+" selected', page.text) is None, \
        "a ticker this portfolio does not have was guessed"


def test_a_combined_advice_knows_only_this_portfolios_funds(client, session_factory):
    from app import statements

    make_login(client, session_factory)
    ids = _theirs(session_factory)
    text = ("Distribution and Reinvestment Advice\nPayment Date: 15 March 2026\n\n"
            "Fund Price Held PerSec Tax Amount Brought Allotted Carried\n"
            "ACME 5.00 10 0.50000000 0.00 5.00 0.00 1 0.00\n"
            "OMEGA 5.00 3 0.50000000 0.00 1.50 0.00 0 1.50\n")
    with session_factory() as s:
        tenancy.bind(s, ids["mine"])
        statements._pdf_text, real = (lambda data: text), statements._pdf_text
        try:
            rows = {r.ticker: r for r in statements.parse_statement(b"", s).rows}
        finally:
            statements._pdf_text = real

    assert rows["ACME"].status != "unknown-instrument" and rows["ACME"].db_units == "10"
    assert rows["OMEGA"].status == "unknown-instrument"


@pytest.mark.parametrize(("ticker", "status"), [("BHP", 303), ("OMEGA", 400)])
def test_a_statement_is_recorded_against_this_portfolios_own_holding(
        client, session_factory, ticker, status):
    from app.models import Dividend, Instrument

    make_login(client, session_factory)
    ids = _theirs(session_factory)

    resp = client.post("/imports-exports/statement/commit", data={
        "_csrf": session_csrf(session_factory), "ticker": ticker,
        "payment_date": "2026-03-15", "net_amount": "12.00"},
        headers=HTML, follow_redirects=False)

    assert resp.status_code == status
    with session_factory() as s:
        tenancy.allow_unscoped(s)
        recorded = [(s.get(Instrument, d.instrument_id).name, d.portfolio_id)
                    for d in s.scalars(select(Dividend))]
    assert recorded == ([("BHP on the ASX", ids["mine"])] if ticker == "BHP" else [])


def test_the_charts_keep_to_this_portfolios_instruments(client, session_factory):
    """The daily series read the whole catalogue, so another portfolio's
    holdings were named and dated on this portfolio's charts."""
    make_login(client, session_factory)
    ids = _theirs(session_factory)
    with session_factory() as s:
        tenancy.allow_unscoped(s)
        for inst in s.scalars(select(Instrument)):
            mine = inst.ticker in ("ACME", "BHP") and inst.exchange == "ASX"
            fac.add_price(s, inst, "2026-01-08" if mine else "2026-01-09", "10.00")
        s.commit()

    with session_factory() as s:
        tenancy.bind(s, ids["mine"], None)
        series = queries.portfolio_series(s)
        grouped = queries.grouped_series(s, "ticker")

    assert series["excluded"] == [] and grouped["excluded"] == []
    assert "2026-01-09" not in [str(d) for d in [*series["dates"], *grouped["dates"]]]
    assert sorted(grouped["groups"]) == ["ACME", "BHP"]


def _row(page: str, ticker: str) -> str:
    row = next(r for r in re.findall(r"<tr\b.*?</tr>", page, re.S) if f">{ticker}<" in r)
    return " ".join(re.sub(r"<[^>]+>", " ", row).replace(ticker, "X").split())


def test_a_csv_row_for_a_strangers_ticker_is_new_here_like_any_other(client, session_factory):
    """The preview looked each row's instrument up in the whole catalogue, so a
    row for a ticker only another portfolio holds came back plain "new", and
    one nobody holds came back "not in this portfolio". The add form is careful
    that the two never read differently (#139)."""
    make_login(client, session_factory)
    _theirs(session_factory)

    page = upload_csv(client, session_factory, b"Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
                                               b"06/01/2025,Buy,OMEGA,1,5.00,0\n"
                                               b"06/01/2025,Buy,NOBODY,1,5.00,0\n").text

    assert _row(page, "OMEGA") == _row(page, "NOBODY")


def test_a_strangers_instrument_is_the_one_a_csv_trade_goes_on(client, session_factory,
                                                               app_module, monkeypatch):
    """New here, and committed onto the catalogue's entry, as the add form
    uses it (#125): not a second OMEGA beside the first."""
    monkeypatch.setattr(app_module.settings.imports, "allow_new_instruments", True)
    make_login(client, session_factory)
    _theirs(session_factory)

    page = upload_csv(client, session_factory, b"Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
                                               b"06/01/2025,Buy,OMEGA,1,5.00,0\n")
    client.post(f"/imports-exports/csv/{staged_id(page)}/commit", headers=HTML,
                data={"_csrf": session_csrf(session_factory)}, follow_redirects=False)

    assert "will be created" in _row(page.text, "OMEGA")
    with session_factory() as s:
        tenancy.allow_unscoped(s)
        omega = s.scalars(select(Instrument).where(Instrument.ticker == "OMEGA")).all()
        assert len(omega) == 1
        assert len(s.scalars(select(Trade).where(Trade.instrument_id == omega[0].id)).all()) == 2


def test_one_ticker_that_cannot_be_converted_leaves_its_namesake_in(client, session_factory):
    """Leaving out went by ticker, so this portfolio's NYSE BHP, in a currency
    with no stored rate, took its ASX BHP out of every chart too."""
    make_login(client, session_factory)
    with session_factory() as s:
        mine = s.scalar(select(Portfolio))
        for exchange, currency in (("ASX", "AUD"), ("NYSE", "USD")):
            bhp = fac.make_instrument(s, "BHP", exchange=exchange, currency=currency)
            fac.add_trade(s, bhp, "2026-01-05", "buy", 10, "40.00", portfolio_id=mine.id)
            fac.add_price(s, bhp, "2026-01-08", "41.00")
        s.commit()

    with session_factory() as s:
        tenancy.bind(s, mine.id, None)
        series = queries.portfolio_series(s)
        grouped = queries.grouped_series(s, "exchange")

    assert series["excluded"] == ["BHP"]
    assert series["value"] and float(series["value"][-1]) == 410.0     # the ASX one, priced
    assert sorted(grouped["groups"]) == ["ASX"]
