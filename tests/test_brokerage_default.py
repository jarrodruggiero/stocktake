"""The brokerage a form offers is the one this portfolio last paid.

Every form started at $9.50, one broker's fee, which test users kept having
to correct. Before the first trade there is nothing to offer; after it, the
most recent buy or sell says what the next one probably costs. A reinvested
distribution costs nothing, so it says nothing about the broker.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

import factories as fac
from app.models import Instrument, Portfolio
from test_routes import bind_to_only_portfolio, make_login

HTML = {"accept": "text/html"}


def _offered(page: str) -> str:
    found = re.search(r'<input name="brokerage"[^>]*\bvalue="([^"]*)"', page)
    assert found, "no brokerage field on the page"
    return found.group(1)


@pytest.fixture
def acme(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "ACME")
        s.commit()
        return inst.id


def _trade(session_factory, instrument_id, date, kind="buy", brokerage="9.50"):
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = s.get(Instrument, instrument_id)
        fac.add_trade(s, inst, date, kind, 10, "5.00", brokerage=brokerage)
        s.commit()


@pytest.mark.parametrize("path", ["/trade/new", "/", "/schedule"])
def test_before_the_first_trade_nothing_is_offered(client, acme, path):
    assert _offered(client.get(path, headers=HTML).text) == ""


@pytest.mark.parametrize("path", ["/trade/new", "/", "/schedule"])
def test_the_most_recent_trade_sets_it(client, session_factory, acme, path):
    _trade(session_factory, acme, "2026-01-05", brokerage="9.50")
    _trade(session_factory, acme, "2026-03-02", kind="sell", brokerage="5.00")
    assert Decimal(_offered(client.get(path, headers=HTML).text)) == Decimal("5.00")


def test_recorded_late_is_not_most_recent(client, session_factory, acme):
    """Most recent by the trade's own date: back-filling an old contract note
    should not set what the next trade is offered."""
    _trade(session_factory, acme, "2026-03-02", brokerage="5.00")
    _trade(session_factory, acme, "2020-06-01", brokerage="19.95")
    assert Decimal(_offered(client.get("/trade/new", headers=HTML).text)) == Decimal("5.00")


def test_a_reinvested_distribution_says_nothing_about_the_broker(
        client, session_factory, acme):
    _trade(session_factory, acme, "2026-01-05", brokerage="5.00")
    _trade(session_factory, acme, "2026-03-02", kind="drp", brokerage="0")
    assert Decimal(_offered(client.get("/trade/new", headers=HTML).text)) == Decimal("5.00")


def test_another_portfolios_trades_offer_nothing(client, session_factory, acme):
    with session_factory() as s:
        other = Portfolio(name="Elsewhere")
        s.add(other)
        s.flush()
        fac.add_trade(s, s.get(Instrument, acme), "2026-03-02", "buy", 10, "5.00",
                      brokerage="12.00", portfolio_id=other.id)
        s.commit()
    assert _offered(client.get("/trade/new", headers=HTML).text) == ""


def test_a_refused_save_keeps_the_instrument_list(client, session_factory, acme):
    """Found while making the above: the refusal re-rendered the form without
    its options, so the chosen instrument was lost and only "Something not
    listed…" could be picked."""
    from test_routes import session_csrf

    refused = client.post("/trade/new", data={
        "_csrf": session_csrf(session_factory), "instrument_id": str(acme),
        "type": "buy", "trade_date": "2026-03-02", "quantity": "0",
        "unit_price": "5.00", "brokerage": "5.00"}, headers=HTML)

    assert refused.status_code == 200 and "greater than zero" in refused.text
    picker = re.search(r'<select name="instrument_id".*?</select>', refused.text, re.S).group(0)
    assert re.search(rf'<option value="{acme}"[^>]*\bselected', picker), picker
