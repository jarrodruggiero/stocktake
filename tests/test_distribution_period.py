"""A trust's distribution belongs to the year its period ends.

A unit trust's income is assessed for the trust's year to 30 June (ITAA36
s97), so a June-quarter distribution paid in July is income of the year just
ended, and on that year's AMMA statement. Filed by payment date it landed a
year late. Each distribution can now say when its period ended, and the FY
report and the dividends export file it there; with none, the payment date
decides, as it does for a company's dividend (decisions.md #144).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from formdata import as_sent
from freezegun import freeze_time
from sqlalchemy import select
from test_api import bearer, issue_key

import factories as fac
from app import exports, fyreport
from app.models import Dividend
from test_routes import bind_to_only_portfolio, make_login, reading

HTML = {"accept": "text/html"}
SAVE = "/imports-exports/statement/commit"
TODAY = "2026-08-03"


@pytest.fixture
def fund(pf):
    return fac.make_instrument(pf, "TRUSTY", asset_class="etf", name="Trusty Index Fund")


@freeze_time(TODAY)
def test_a_distribution_counts_in_the_year_its_period_ends(pf, fund):
    dividend = fac.add_dividend(pf, fund, "2026-07-16", "150.00")
    dividend.period_end = dt.date(2026, 6, 30)

    ended, paid = fyreport.fy_income(pf, 2026), fyreport.fy_income(pf, 2027)

    assert [(r.instrument.ticker, r.cash) for r in ended] == [("TRUSTY", Decimal("150.00"))]
    assert paid == []


@freeze_time(TODAY)
def test_with_no_period_end_the_payment_date_decides(pf, fund):
    fac.add_dividend(pf, fund, "2026-07-16", "150.00")

    assert fyreport.fy_income(pf, 2026) == []
    assert [r.cash for r in fyreport.fy_income(pf, 2027)] == [Decimal("150.00")]


@freeze_time(TODAY)
def test_the_dividends_export_files_it_by_its_period(pf, fund):
    dividend = fac.add_dividend(pf, fund, "2026-07-16", "150.00")
    dividend.period_end = dt.date(2026, 6, 30)

    headers, rows = exports.dividends(pf, fy=2026)
    row = dict(zip(headers, rows[0]))

    assert (row["Payment date"], row["Period ended"], row["Financial year"]) == (
        "2026-07-16", "2026-06-30", "FY25/26")
    assert exports.dividends(pf, fy=2027)[1] == []


def _form(client, session_factory) -> dict:
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fund = fac.make_instrument(s, "TRUSTY", asset_class="etf")
        fac.add_trade(s, fund, "2026-01-05", "buy", 100, "5.00")
        s.commit()
    page = client.get("/dividend/new?ticker=TRUSTY", headers=HTML).text
    return {**as_sent(page, SAVE), "payment_date": "2026-07-16", "net_amount": "150.00"}


@freeze_time(TODAY)
def test_the_distribution_form_takes_a_period_end(client, session_factory):
    sent = _form(client, session_factory)
    assert "period_end" in sent

    resp = client.post(SAVE, data={**sent, "period_end": "2026-06-30"}, headers=HTML,
                       follow_redirects=False)

    assert resp.status_code == 303
    with reading(session_factory) as s:
        assert s.scalars(select(Dividend.period_end)).one() == dt.date(2026, 6, 30)
    fy = client.get("/?fy=2026", headers=HTML).text
    assert fy.split("<h2>Dividend income</h2>", 1)[1].split("</table>", 1)[0].count(
        ">TRUSTY</a>") == 1


@freeze_time(TODAY)
@pytest.mark.parametrize("period_end", ["2026-07-17", "2025-07-15"],
                         ids=["after-it-was-paid", "over-a-year-before"])
def test_a_period_end_that_cannot_be_is_refused(client, session_factory, period_end):
    sent = _form(client, session_factory)

    resp = client.post(SAVE, data={**sent, "period_end": period_end}, headers=HTML)

    assert resp.status_code == 400
    assert ("A distribution&#39;s period ends on or before its payment date, and within a "
            "year of it.") in resp.text
    with reading(session_factory) as s:
        assert s.scalars(select(Dividend)).all() == []


@freeze_time(TODAY)
def test_the_edit_page_sets_and_clears_it(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fund = fac.make_instrument(s, "TRUSTY", asset_class="etf")
        dividend_id = fac.add_dividend(s, fund, "2026-07-16", "150.00").id
        s.commit()
    page = client.get(f"/dividend/{dividend_id}/edit", headers=HTML).text
    sent = as_sent(page, f"/dividend/{dividend_id}/edit")

    def stored():
        with reading(session_factory) as s:
            return s.get(Dividend, dividend_id).period_end

    client.post(f"/dividend/{dividend_id}/edit", data={**sent, "period_end": "2026-06-30"},
                headers=HTML)
    assert stored() == dt.date(2026, 6, 30)
    assert 'value="2026-06-30"' in client.get(f"/dividend/{dividend_id}/edit", headers=HTML).text
    refused = client.post(f"/dividend/{dividend_id}/edit",
                          data={**sent, "period_end": "2026-08-01"}, headers=HTML,
                          follow_redirects=True)
    assert "period ends on or before its payment date" in refused.text
    assert stored() == dt.date(2026, 6, 30)
    client.post(f"/dividend/{dividend_id}/edit", data={**sent, "period_end": ""},
                headers=HTML)
    assert stored() is None


@freeze_time(TODAY)
def test_the_api_takes_and_gives_it(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        portfolio = bind_to_only_portfolio(s)
        fund = fac.make_instrument(s, "TRUSTY", asset_class="etf")
        fac.add_trade(s, fund, "2026-01-05", "buy", 100, "5.00")
        s.commit()
        key = bearer(issue_key(session_factory, portfolio.id,
                               created_by=portfolio.members[0].user_id, scopes="read,write"))
    body = {"ticker": "TRUSTY", "date": "2026-07-16", "cash_amount": "150.00"}

    refused = client.post("/api/v1/dividends", headers=key,
                          json={**body, "period_end": "2026-07-20"})
    made = client.post("/api/v1/dividends", headers=key,
                       json={**body, "period_end": "2026-06-30"})
    listed = client.get("/api/v1/dividends", headers=key).json()["dividends"]

    assert refused.status_code == 400 and made.status_code == 201
    assert [d["period_end"] for d in listed] == ["2026-06-30"]
