"""One portfolio holding one ticker on two exchanges.

BHP on the ASX and BHP on the NYSE are two holdings with one code. Every path
that took a bare ticker took the older of the two: the holding page and every
link and redirect to it, a dividend statement, the API, and the plan's
rotation. Each now names the exchange when the ticker alone is not enough,
and only then, so a ticker held once keeps the address and the form it had.
"""

from __future__ import annotations

import html
import re

from freezegun import freeze_time
from sqlalchemy import select
from test_api import bearer, issue_key
from test_routes_plan import TODAY, save_plan

import factories as fac
from app import statements, tenancy
from app.models import Dividend, Instrument, InvestmentPlanEntry, Portfolio, Trade
from test_routes import make_login, session_csrf

HTML = {"accept": "text/html"}


def _both(session_factory) -> dict:
    """Mine holds BHP on the ASX, then BHP on the NYSE, and ACME once."""
    with session_factory() as s:
        mine = s.scalar(select(Portfolio))
        asx = fac.make_instrument(s, "BHP", name="BHP on the ASX")
        nyse = fac.make_instrument(s, "BHP", exchange="NYSE", currency="USD",
                                   name="BHP on the NYSE")
        acme = fac.make_instrument(s, "ACME", name="Acme")
        fac.add_trade(s, asx, "2026-01-06", "buy", 7, "40.00", portfolio_id=mine.id)
        fac.add_trade(s, nyse, "2026-01-07", "buy", 2, "60.00", fx_rate="1.50",
                      portfolio_id=mine.id)
        fac.add_trade(s, acme, "2026-01-05", "buy", 10, "5.00", portfolio_id=mine.id)
        s.commit()
        return {"asx": asx.id, "nyse": nyse.id, "acme": acme.id, "mine": mine.id,
                "owner": mine.members[0].user_id}


def _unscoped(session_factory, model):
    with session_factory() as s:
        tenancy.allow_unscoped(s)
        return list(s.scalars(select(model)))


def _hrefs(page: str) -> set[str]:
    return {html.unescape(h) for h in re.findall(r'href="(/holding/[^"]*)"', page)}


def test_each_listing_has_its_own_page(client, session_factory):
    make_login(client, session_factory)
    _both(session_factory)

    nyse = client.get("/holding/BHP?exchange=NYSE", headers=HTML).text
    bare = client.get("/holding/BHP", headers=HTML).text

    assert "BHP on the NYSE" in nyse and "BHP on the ASX" not in nyse
    assert "BHP on the ASX" in bare                       # the older, as before,
    assert "/holding/BHP?exchange=NYSE" in _hrefs(bare)   # with the other one click away
    assert client.get("/holding/BHP?exchange=LSE", headers=HTML).status_code == 404


def test_a_link_names_the_exchange_only_when_the_ticker_needs_it(client, session_factory):
    make_login(client, session_factory)
    _both(session_factory)

    links = _hrefs(client.get("/holdings", headers=HTML).text)

    assert {"/holding/BHP?exchange=ASX&return=/holdings",
            "/holding/BHP?exchange=NYSE&return=/holdings",
            "/holding/ACME?return=/holdings"} <= links


def test_a_delete_on_one_listing_returns_to_that_listing(client, session_factory):
    make_login(client, session_factory)
    ids = _both(session_factory)
    with session_factory() as s:
        nyse = s.get(Instrument, ids["nyse"])
        extra = fac.add_trade(s, nyse, "2026-01-08", "buy", 1, "61.00", fx_rate="1.50",
                              portfolio_id=ids["mine"])
        s.commit()
        extra_id = extra.id

    resp = client.post(f"/trade/{extra_id}/delete", data={"_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/holding/BHP?exchange=NYSE"


def test_a_statement_is_recorded_against_the_listing_chosen(client, session_factory,
                                                             monkeypatch):
    make_login(client, session_factory)
    ids = _both(session_factory)
    monkeypatch.setattr(statements, "_pdf_text", lambda data: (
        "BHP Group dividend advice\nBHP\nPayment Date: 15 March 2026\nNet Amount: $10.00\n"))

    page = client.post("/imports-exports/statement",
                       files={"file": ("advice.pdf", b"%PDF-1.4", "application/pdf")},
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML).text
    select = re.search(r'<select name="ticker".*?</select>', page, re.S).group(0)
    offered = re.findall(r'<option value="([^"]+)"( selected)?>([^<]+)</option>', select)
    payload = {"payment_date": "2026-03-15", "net_amount": "10.00",
               "_csrf": session_csrf(session_factory)}
    refused = client.post("/imports-exports/statement/commit", headers=HTML,
                          data={**payload, "ticker": "BHP"})
    saved = client.post("/imports-exports/statement/commit", headers=HTML,
                        data={**payload, "ticker": "BHP:NYSE"}, follow_redirects=False)

    assert offered == [("ACME", "", "ACME"), ("BHP:ASX", "", "BHP (ASX)"),
                       ("BHP:NYSE", "", "BHP (NYSE)")]      # and neither BHP guessed
    assert refused.status_code == 400 and "BHP is held on ASX and NYSE" in refused.text
    assert saved.headers["location"] == "/holding/BHP?exchange=NYSE"
    assert [d.instrument_id for d in _unscoped(session_factory, Dividend)] == [ids["nyse"]]


def test_a_combined_advice_asks_which_listing_an_ambiguous_fund_is(client, session_factory):
    """Which of the two a fund's row is for, the advice does not say, so the
    row offers both and claims no units for either."""
    make_login(client, session_factory)
    ids = _both(session_factory)
    text = ("Distribution and Reinvestment Advice\nPayment Date: 15 March 2026\n\n"
            "Fund Price Held PerSec Tax Amount Brought Allotted Carried\n"
            "BHP 5.00 7 0.50000000 0.00 3.50 0.00 0 3.50\n"
            "ACME 5.00 10 0.50000000 0.00 5.00 0.00 1 0.00\n")
    with session_factory() as s:
        tenancy.bind(s, ids["mine"])
        statements._pdf_text, real = (lambda data: text), statements._pdf_text
        try:
            rows = {r.ticker: r for r in statements.parse_statement(b"", s).rows}
        finally:
            statements._pdf_text = real

    assert rows["BHP"].choices == [("BHP:ASX", "BHP (ASX)"), ("BHP:NYSE", "BHP (NYSE)")]
    assert (rows["BHP"].status, rows["BHP"].db_units) == ("new", None)
    assert (rows["ACME"].choices, rows["ACME"].db_units) == ([], "10")


def test_the_api_asks_which_listing_a_ticker_means(client, session_factory):
    make_login(client, session_factory)
    ids = _both(session_factory)
    raw = issue_key(session_factory, ids["mine"], created_by=ids["owner"], scopes="read,write")
    body = {"ticker": "BHP", "type": "buy", "date": "2026-02-02", "units": "1",
            "unit_price": "61.00", "fx_rate": "1.50"}

    refused = client.post("/api/v1/trades", json=body, headers=bearer(raw))
    taken = client.post("/api/v1/trades", json={**body, "exchange": "NYSE"}, headers=bearer(raw))
    listed = client.get("/api/v1/trades?ticker=BHP&exchange=NYSE",
                        headers=bearer(raw)).json()["trades"]

    assert refused.status_code == 409 and "BHP is held on ASX and NYSE" in refused.json()["detail"]
    assert taken.status_code == 201, taken.text
    assert [(t["exchange"], t["units"]) for t in listed] == [("NYSE", 2), ("NYSE", 1)]
    assert sorted(t.instrument_id for t in _unscoped(session_factory, Trade)
                  if t.date.isoformat() == "2026-02-02") == [ids["nyse"]]


@freeze_time(TODAY)
def test_the_rotation_names_the_listing_and_shows_it_back(client, session_factory):
    make_login(client, session_factory)
    ids = _both(session_factory)

    refused = save_plan(client, session_factory, tickers="BHP, ACME")
    saved = save_plan(client, session_factory, tickers="bhp:nyse, ACME")
    page = client.get("/schedule", headers=HTML).text

    assert refused.status_code == 400 and "BHP is held on ASX and NYSE" in refused.text
    assert saved.status_code == 303
    assert [e.instrument_id for e in sorted(_unscoped(session_factory, InvestmentPlanEntry),
                                            key=lambda e: e.position)] == [ids["nyse"], ids["acme"]]
    assert "BHP:NYSE, ACME</textarea>" in page
    assert 'data-ticker="BHP:ASX"' in page and 'data-ticker="BHP:NYSE"' in page
    assert "/holding/BHP?exchange=NYSE&return=/schedule" in _hrefs(page)   # coming up


def test_recording_a_trade_from_a_listings_page_picks_that_listing(client, session_factory):
    make_login(client, session_factory)
    ids = _both(session_factory)

    page = client.get("/holding/BHP?exchange=NYSE", headers=HTML).text
    record = html.unescape(re.search(r'href="(/trade/new[^"]*)"', page).group(1))
    form = client.get(record, headers=HTML).text
    select = re.search(r'<select name="instrument_id".*?</select>', form, re.S).group(0)
    picked = re.findall(r'<option value="(\d+)"[^>]*\bselected\b', select)

    assert record == "/trade/new?ticker=BHP&exchange=NYSE"
    assert picked == [str(ids["nyse"])]


def test_the_apis_exchange_filter_takes_the_exchange_however_it_is_typed(client, session_factory):
    make_login(client, session_factory)
    ids = _both(session_factory)
    raw = issue_key(session_factory, ids["mine"], created_by=ids["owner"])

    listed = client.get("/api/v1/trades", params={"ticker": "BHP", "exchange": " nyse "},
                        headers=bearer(raw)).json()["trades"]

    assert [t["exchange"] for t in listed] == ["NYSE"]


@freeze_time(TODAY)
def test_each_upcoming_buy_carries_its_own_entrys_exchange(client, session_factory):
    """Three in the rotation, so a slot read from the wrong entry shows."""
    from app import plans

    make_login(client, session_factory)
    ids = _both(session_factory)
    assert save_plan(client, session_factory, tickers="BHP:ASX, ACME, BHP:NYSE").status_code == 303

    with session_factory() as s:
        tenancy.bind(s, ids["mine"], ids["owner"])
        coming = plans.schedule(s, upcoming=3)["next"]

    assert [(b.ticker, b.exchange) for b in coming] == [("BHP", "ASX"), ("ACME", "ASX"),
                                                        ("BHP", "NYSE")]
