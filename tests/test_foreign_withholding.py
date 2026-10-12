"""Foreign tax withheld from a distribution.

A US dividend arrives net of the tax the US kept. What is assessable is the
whole of it, and the tax kept is a foreign income tax offset (ITAA97
Div 770); recording only the cash understated the income and left out the
offset. A distribution can now say what was withheld, and the FY report, the
dividends export and the API carry it (decisions.md #145).
"""

from __future__ import annotations

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
def usd(pf):
    return fac.make_instrument(pf, "NOVA", exchange="NASDAQ", asset_class="share",
                               currency="USD")


@freeze_time(TODAY)
def test_the_fy_report_counts_the_tax_withheld(pf, usd):
    """US$85 received after US$15 withheld, at 1.50: A$127.50 cash, A$22.50
    of foreign tax, A$150 of income."""
    dividend = fac.add_dividend(pf, usd, "2026-03-02", "85.00", fx_rate="1.50")
    dividend.withholding_tax = Decimal("15.00")

    [row] = fyreport.fy_income(pf, 2026)
    report = fyreport.fy_report(pf, 2026)

    assert (row.cash, row.withholding, row.grossed_up) == (
        Decimal("127.50"), Decimal("22.50"), Decimal("150.00"))
    assert report["income_withholding"] == Decimal("22.50")


@freeze_time(TODAY)
def test_tax_withheld_with_no_rate_is_unknown_not_one_to_one(pf, usd):
    """decisions.md #5: no rate for the currency, no AUD figure."""
    dividend = fac.add_dividend(pf, usd, "2026-03-02", "85.00", fx_rate=None)
    dividend.withholding_tax = Decimal("15.00")

    [row] = fyreport.fy_income(pf, 2026)

    assert (row.cash, row.withholding, row.grossed_up) == (None, None, None)


@freeze_time(TODAY)
def test_the_dividends_export_has_it(pf, usd):
    dividend = fac.add_dividend(pf, usd, "2026-03-02", "85.00", fx_rate="1.50",
                                franking_credits="0")
    dividend.withholding_tax = Decimal("15.00")

    headers, rows = exports.dividends(pf)
    row = dict(zip(headers, rows[0]))

    assert row["Foreign tax withheld (native)"] == Decimal("15.0000")
    assert row["Grossed up (AUD)"] == Decimal("150.00")   # 127.50 + 22.50 + 0 franking


@freeze_time(TODAY)
def test_the_form_and_the_fy_page_take_and_show_it(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        nova = fac.make_instrument(s, "NOVA", exchange="NASDAQ", currency="USD")
        fac.add_trade(s, nova, "2026-01-05", "buy", 100, "5.00", fx_rate="1.50")
        fac.add_fx(s, "USDAUD", "2026-03-02", "1.50")
        s.commit()
    page = client.get("/dividend/new?ticker=NOVA", headers=HTML).text
    sent = {**as_sent(page, SAVE), "payment_date": "2026-03-02", "net_amount": "85.00",
            "withholding_tax": "15.00"}

    resp = client.post(SAVE, data=sent, headers=HTML, follow_redirects=False)
    refused = client.post(SAVE, data={**sent, "net_amount": "86.00",
                                       "withholding_tax": "-1"}, headers=HTML)

    assert resp.status_code == 303
    assert refused.status_code == 400 and "Foreign tax withheld can&#39;t be negative." in (
        refused.text)
    with reading(session_factory) as s:
        assert s.scalars(select(Dividend.withholding_tax)).one() == Decimal("15.00")
    fy = client.get("/?fy=2026", headers=HTML).text
    income = fy.split("<h2>Dividend income</h2>", 1)[1].split("</table>", 1)[0]
    assert "Foreign tax" in income and "22.50" in income


@freeze_time(TODAY)
def test_the_fy_page_has_no_foreign_tax_column_with_nothing_withheld(client,
                                                                     session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME")
        fac.add_trade(s, acme, "2026-01-05", "buy", 100, "5.00")
        fac.add_dividend(s, acme, "2026-03-02", "12.00")
        s.commit()

    fy = client.get("/?fy=2026", headers=HTML).text
    assert "Foreign tax" not in fy.split("<h2>Dividend income</h2>", 1)[1].split(
        "</table>", 1)[0]


@freeze_time(TODAY)
def test_the_edit_page_and_the_api_take_it_too(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        portfolio = bind_to_only_portfolio(s)
        nova = fac.make_instrument(s, "NOVA", exchange="NASDAQ", currency="USD")
        fac.add_trade(s, nova, "2026-01-05", "buy", 100, "5.00", fx_rate="1.50")
        dividend_id = fac.add_dividend(s, nova, "2026-03-02", "85.00", fx_rate="1.50").id
        s.commit()
        key = bearer(issue_key(session_factory, portfolio.id,
                               created_by=portfolio.members[0].user_id, scopes="read,write"))
    page = client.get(f"/dividend/{dividend_id}/edit", headers=HTML).text
    sent = as_sent(page, f"/dividend/{dividend_id}/edit")

    client.post(f"/dividend/{dividend_id}/edit", data={**sent, "withholding_tax": "15.00"},
                headers=HTML)
    made = client.post("/api/v1/dividends", headers=key, json={
        "ticker": "NOVA", "date": "2026-04-01", "cash_amount": "42.50",
        "withholding_tax": "7.50", "fx_rate": "1.50"})
    listed = client.get("/api/v1/dividends", headers=key).json()["dividends"]

    assert made.status_code == 201
    assert [d["withholding_tax"] for d in listed] == [15.0, 7.5]
