"""A distribution recorded by hand, without a registry statement to upload.

There was no form for one: a US dividend, or one whose PDF nobody has, could
not be entered at all. The holding page now offers the statement form for
its own holding, in a dialog, and /dividend/new is the same form as a page
for a browser without scripting.
"""

from __future__ import annotations

import re
from decimal import Decimal

from formdata import as_sent
from sqlalchemy import select

import factories as fac
from app.models import Dividend
from test_routes import bind_to_only_portfolio, make_login, reading

HTML = {"accept": "text/html"}
SAVE = "/imports-exports/statement/commit"


def _acme(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME", name="Acme Industries")
        fac.add_trade(s, acme, "2026-01-05", "buy", 100, "5.00")
        s.commit()


def test_the_page_is_the_statement_form_for_that_holding(client, session_factory):
    _acme(client, session_factory)

    page = client.get("/dividend/new?ticker=ACME", headers=HTML)

    assert page.status_code == 200
    assert "<h1>Record a distribution</h1>" in page.text
    assert as_sent(page.text, SAVE)["ticker"] == "ACME"


def test_the_holding_page_offers_the_same_form_in_a_dialog(client, session_factory):
    _acme(client, session_factory)

    page = client.get("/holding/ACME", headers=HTML).text

    dialog = re.search(r'<dialog id="distdialog".*?</dialog>', page, re.S)
    assert dialog and as_sent(dialog.group(0), SAVE)["ticker"] == "ACME"
    assert re.search(r'<button type="button"[^>]*data-opens="distdialog"[^>]*>Record a distribution',
                     page)
    assert '<noscript><a href="/dividend/new?ticker=ACME">' in page


def test_a_cash_distribution_saved_from_it_lands_on_the_holding(client, session_factory):
    _acme(client, session_factory)
    page = client.get("/dividend/new?ticker=ACME", headers=HTML).text
    sent = {**as_sent(page, SAVE), "payment_date": "2026-03-15", "net_amount": "12.50",
            "franking_credits": "5.36"}

    resp = client.post(SAVE, data=sent, headers=HTML, follow_redirects=False)

    assert (resp.status_code, resp.headers["location"]) == (303, "/holding/ACME")
    with reading(session_factory) as s:
        dividend = s.scalars(select(Dividend)).one()
    assert (dividend.cash_amount, dividend.franking_credits, dividend.reinvest_trade_id) == (
        Decimal("12.50"), Decimal("5.36"), None)


def test_a_refused_one_comes_back_as_the_form_it_was_typed_in(client, session_factory):
    """Not as "Dividend statement" with no statement behind it."""
    _acme(client, session_factory)
    page = client.get("/dividend/new?ticker=ACME", headers=HTML).text
    sent = {**as_sent(page, SAVE), "payment_date": "2026-03-15", "net_amount": "-1.00"}

    resp = client.post(SAVE, data=sent, headers=HTML)

    assert resp.status_code == 400
    assert "<h1>Record a distribution</h1>" in resp.text
    assert "Net amount has to be more than zero." in resp.text
    assert as_sent(resp.text, SAVE) == sent
