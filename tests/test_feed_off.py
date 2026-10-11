"""Turn the price feed off and nothing leaves: decisions.md #9, as the docs say.

`price_feed.enabled` governs every outbound request, but two did not ask it.
Signing in asked Yahoo for a quote on every catalogue symbol, on a Sunday as
readily as a Monday, and a ticker typed into either add-instrument form was
looked up. Only symbols left, so the privacy promise held; the docs' "no
outbound requests at all" did not.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from sqlalchemy import select
from test_security import _REAL_REFRESH

from app.models import Instrument
from test_routes import make_login, reading, session_csrf

HTML = {"accept": "text/html"}


@pytest.mark.parametrize("feed, quotes, trading, asked", [
    (True, True, True, True),
    (False, True, True, False),     # the feed off, whatever quotes say
    (True, False, True, False),
    (True, True, False, False),     # every market shut: nothing can have moved
])
def test_a_sign_in_asks_for_quotes_only_while_they_can_change(app_module, monkeypatch, feed,
                                                               quotes, trading, asked):
    ran = threading.Event()
    monkeypatch.setattr(app_module.settings.price_feed, "enabled", feed)
    monkeypatch.setattr(app_module.settings.price_feed, "quotes_enabled", quotes)
    monkeypatch.setattr(app_module.pricefeed, "any_market_trading", lambda session: trading)
    monkeypatch.setattr(app_module, "_run_quotes", lambda: ran.set() or 0)

    async def sign_in_moment():
        _REAL_REFRESH()           # conftest replaces the module's with a no-op
        await asyncio.sleep(0)

    asyncio.run(sign_in_moment())

    assert ran.wait(2 if asked else 0.3) is asked


@pytest.fixture
def looked_up(app_module, monkeypatch):
    calls: list[str] = []

    def lookup(ticker, exchange):
        calls.append(ticker)
        return {"symbol": ticker, "name": "Found", "currency": "USD", "asset_class": "share",
                "found": True}

    monkeypatch.setattr(app_module.pricefeed, "lookup", lookup)
    return calls


@pytest.mark.parametrize("feed", [False, True])
def test_a_ticker_is_looked_up_only_with_the_feed_on(client, session_factory, app_module,
                                                     monkeypatch, looked_up, feed):
    monkeypatch.setattr(app_module.settings.price_feed, "enabled", feed)
    make_login(client, session_factory)

    answer = client.get("/holdings/lookup", params={"ticker": "GOOG", "exchange": "NASDAQ"}).json()

    assert looked_up == (["GOOG"] if feed else [])
    assert answer["found"] is feed and answer.get("off", False) is not feed
    assert answer["symbol"] == "GOOG"            # the guess needs nobody


@pytest.mark.parametrize("feed", [False, True])
def test_adding_an_instrument_asks_nobody_with_the_feed_off(client, session_factory, app_module,
                                                            monkeypatch, looked_up, feed):
    monkeypatch.setattr(app_module.settings.price_feed, "enabled", feed)
    make_login(client, session_factory)

    client.post("/holdings/add", headers=HTML, follow_redirects=False,
                data={"ticker": "GOOG", "exchange": "NASDAQ", "asset_class": "share",
                      "_csrf": session_csrf(session_factory)})

    assert looked_up == (["GOOG"] if feed else [])
    with reading(session_factory) as s:
        added = s.scalars(select(Instrument).where(Instrument.ticker == "GOOG")).one()
    assert (added.name, added.currency) == (("Found", "USD") if feed else (None, "AUD"))
