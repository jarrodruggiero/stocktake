"""Recording a trade in an instrument the form does not list yet.

A test user recorded MSFT as an ETF — the class picker defaulted to ETF — and
later added it again as a share. Instruments are unique on exchange and
ticker, so the second "new" one quietly became the first, class and all.
Now the class comes from the provider, an instrument already listed is
refused rather than reused unseen, and saving makes no network call: the
form already asked the provider as the ticker was typed.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select
from test_pricefeed import stub_yf

import factories as fac
from app import pricefeed
from app.models import Instrument, Trade
from test_routes import bind_to_only_portfolio, make_login, reading, session_csrf

HTML = {"accept": "text/html"}


@pytest.mark.parametrize("quote_type, expected", [
    ("EQUITY", "share"), ("ETF", "etf"), ("CRYPTOCURRENCY", "crypto"),
    ("MUTUALFUND", None), ("", None),
])
def test_the_lookup_names_the_class_the_provider_gives(monkeypatch, quote_type, expected):
    stub_yf(monkeypatch, info={"longName": "Microsoft Corporation", "currency": "USD",
                               "quoteType": quote_type})
    assert pricefeed.lookup("MSFT", "NASDAQ").get("asset_class") == expected


def test_a_lookup_that_finds_nothing_names_no_class(monkeypatch):
    stub_yf(monkeypatch, raises=True)
    assert pricefeed.lookup("MSFT", "NASDAQ").get("asset_class") is None


@pytest.fixture
def signed_in(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        s.commit()
    return client


def _new(client, session_factory, ticker="MSFT", **overrides):
    data = {"_csrf": session_csrf(session_factory), "instrument_id": "new",
            "type": "buy", "trade_date": "2026-03-02", "quantity": "10",
            "unit_price": "5.00", "brokerage": "9.50", "new_ticker": ticker,
            "new_exchange": "NASDAQ", "new_asset_class": "share",
            "new_currency": "USD", "new_name": "", "new_yahoo": ""}
    data.update(overrides)
    return client.post("/trade/new", data=data, headers=HTML, follow_redirects=False)


def _listed_here(session_factory, **kwargs):
    """MSFT in this portfolio's list: a holding setting for it, as adding it
    on Manage holdings leaves."""
    from app import tenancy
    from app.models import HoldingPref
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "MSFT", exchange="NASDAQ", currency="USD", **kwargs)
        s.add(tenancy.owned(s, HoldingPref(instrument_id=inst.id)))
        s.commit()


def _trades(session_factory) -> list[Trade]:
    with reading(session_factory) as s:
        return s.scalars(select(Trade)).all()


def test_saving_a_new_instrument_asks_the_provider_nothing(
        signed_in, session_factory, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("the save asked the provider")
    monkeypatch.setattr(pricefeed, "lookup", refuse)

    resp = _new(signed_in, session_factory, new_yahoo="MSFT", new_name="Microsoft")

    assert resp.status_code == 303, resp.text
    with session_factory() as s:
        inst = s.scalar(select(Instrument).where(Instrument.ticker == "MSFT"))
        assert (inst.asset_class, inst.currency, inst.yahoo_symbol, inst.name) == (
            "share", "USD", "MSFT", "Microsoft")


def test_without_a_symbol_the_price_symbol_is_worked_out_not_asked(
        signed_in, session_factory, monkeypatch):
    monkeypatch.setattr(pricefeed, "lookup", lambda *_a, **_k: pytest.fail("asked"))
    assert _new(signed_in, session_factory).status_code == 303
    with session_factory() as s:
        inst = s.scalar(select(Instrument).where(Instrument.ticker == "MSFT"))
        assert inst.yahoo_symbol == pricefeed.yahoo_symbol_for("MSFT", "NASDAQ")


def test_an_instrument_already_listed_is_refused_not_reused(signed_in, session_factory):
    """The MSFT report. The class typed for the second one was thrown away and
    nothing said so."""
    _listed_here(session_factory, asset_class="etf")

    resp = _new(signed_in, session_factory)

    assert resp.status_code == 200
    assert "MSFT is already recorded. Pick it from the list." in resp.text
    assert _trades(session_factory) == []


def test_a_removed_instrument_comes_back_through_the_new_path(signed_in, session_factory):
    """Removed, it is not in the list to pick, so adding it again is how it
    returns — with its own class, as instruments keep theirs."""
    with session_factory() as s:
        inst = fac.make_instrument(s, "MSFT", exchange="NASDAQ", asset_class="etf",
                                   currency="USD")
        inst.active = False
        s.commit()
        inst_id = inst.id

    resp = _new(signed_in, session_factory)

    assert resp.status_code == 303, resp.text
    assert [t.instrument_id for t in _trades(session_factory)] == [inst_id]
    with session_factory() as s:
        back = s.get(Instrument, inst_id)
        assert back.active is True and back.asset_class == "etf"


def test_the_class_is_never_assumed(signed_in, session_factory):
    page = signed_in.get("/trade/new", headers=HTML).text
    picker = re.search(r'<select name="new_asset_class".*?</select>', page, re.S).group(0)
    first = re.search(r"<option[^>]*>", picker).group(0)
    assert 'value=""' in first, "the first choice should be no choice"
    assert "selected" not in picker

    resp = _new(signed_in, session_factory, new_asset_class="")
    assert resp.status_code == 200
    assert "Pick an asset class" in resp.text
    assert _trades(session_factory) == []


def test_the_listed_instruments_carry_their_ticker_and_exchange(signed_in, session_factory):
    """So the form can switch to one when its ticker is typed as new."""
    _listed_here(session_factory)
    page = signed_in.get("/trade/new", headers=HTML).text
    assert re.search(r'<option[^>]*data-ticker="MSFT"[^>]*data-exchange="NASDAQ"', page)
