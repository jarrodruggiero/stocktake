"""Adding an instrument fetches its history, and does not when the feed is off.

A new instrument has no prices, and until it has some it is invisible to every
figure that needs a value — the holding shows no price, the chart cannot value
it, and the trade form has no close to offer for the date you typed. So both
creation paths nudge the feed rather than waiting for the daily run
(decisions.md #53).

**Nothing asserted that.** `conftest._no_network` stubs `_kick_feed` to a no-op
for the whole suite, precisely because a real run outlives the test — so the
call could have been deleted from either route and the suite would have stayed
green. These tests override that stub with a recorder.

The second half is the privacy posture. `price_feed.enabled: false` means this
installation does not talk to a price provider, and that has to hold for a nudge
as much as for the schedule: `lifespan` only gates the LOOPS, so the kick needed
its own check or adding an instrument reached the network on an install that had
switched the feed off.
"""

from __future__ import annotations

import asyncio

import pytest
from freezegun import freeze_time
from sqlalchemy import select

import factories as fac
from app import main as main_module
from app.models import Instrument
from test_routes import bind_to_only_portfolio, make_login, session_csrf

HTML = {"accept": "text/html"}
TODAY = "2026-09-23"

# The REAL function, captured at import. `conftest._no_network` replaces
# `main._kick_feed` with a no-op for every test in the suite, so the guard
# tests below would otherwise be asserting against the stub — which returns
# None whatever the setting says, and made both of them fail identically.
_REAL_KICK = main_module._kick_feed


@pytest.fixture
def kicks(monkeypatch):
    """Record kicks instead of running the feed, overriding conftest's no-op.

    `pricefeed.lookup` is stubbed as well, and has to be: it reaches Yahoo
    through yfinance rather than through `providers._get_json`, so
    `conftest._no_network` does not cover it. Left real under `freeze_time`
    this does not fail — it takes the interpreter down with SIGILL, which
    reads like a broken test file rather than a live network call.
    """
    monkeypatch.setattr(
        main_module.pricefeed, "lookup",
        lambda ticker, exchange: {"symbol": f"{ticker.upper()}.AX",
                                  "name": None, "currency": "AUD", "found": True},
    )
    seen: list[bool] = []
    monkeypatch.setattr(main_module, "_kick_feed", lambda: seen.append(True))
    return seen


@freeze_time(TODAY)
def test_adding_an_instrument_from_manage_holdings_fetches_its_history(
    client, session_factory, kicks
):
    make_login(client, session_factory)

    client.post("/holdings/add",
                data={"ticker": "ACME", "exchange": "ASX", "asset_class": "share",
                      "currency": "AUD", "_csrf": session_csrf(session_factory)},
                headers=HTML, follow_redirects=False)

    assert kicks == [True]
    with session_factory() as s:
        assert s.scalars(select(Instrument)).one().ticker == "ACME"


@freeze_time(TODAY)
def test_adding_one_inline_while_recording_a_trade_fetches_its_history(
    client, session_factory, kicks
):
    """The path the live report came through: a brand-new instrument created by
    the trade form itself, which is the case most likely to be charted before
    any price exists for it."""
    make_login(client, session_factory)

    resp = client.post(
        "/trade/new",
        data={"instrument_id": "new", "new_ticker": "NOVA", "new_exchange": "ASX",
              "new_asset_class": "share", "new_currency": "AUD",
              "type": "buy", "trade_date": "2026-07-01", "quantity": "10",
              "unit_price": "4.00", "brokerage": "0",
              "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    assert kicks == [True]


@freeze_time(TODAY)
def test_an_existing_instrument_does_not_kick_the_feed(client, session_factory, kicks):
    """Only a NEW instrument has nothing to show. Recording an ordinary trade
    against one already in the catalogue must not fetch anything."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME", asset_class="share")
        s.commit()
        acme_id = acme.id

    client.post("/trade/new",
                data={"instrument_id": str(acme_id), "type": "buy",
                      "trade_date": "2026-07-01", "quantity": "10",
                      "unit_price": "4.00", "brokerage": "0",
                      "_csrf": session_csrf(session_factory)},
                headers=HTML, follow_redirects=False)

    assert kicks == []


# --------------------------------------------------------------------------- #
# Inert when the feed is off
# --------------------------------------------------------------------------- #

def _kick_inside_a_loop() -> bool:
    """Call `_kick_feed` with a real event loop running, and report whether it
    dispatched.

    It needs a running loop to hand work to an executor, and there is none in a
    sync test — so without this both sides of the guard return the same thing
    for different reasons. An earlier version faked the loop by patching
    `asyncio.get_running_loop`; because `main.asyncio` *is* the asyncio module,
    that replaced it for the whole process and crashed the interpreter with
    SIGILL. Use a real loop.
    """
    async def _call() -> bool:
        return _REAL_KICK()

    return asyncio.run(_call())


@pytest.fixture
def quiet_feed(monkeypatch):
    """A feed run that does nothing, so a dispatch is harmless."""
    monkeypatch.setattr(main_module, "_run_feed", lambda: None)
    main_module.feed_status["running"] = False


def test_the_kick_does_nothing_when_the_feed_is_disabled(monkeypatch, quiet_feed):
    """`price_feed.enabled: false` is a promise about the network, not a
    statement about a schedule. `lifespan` gates only the LOOPS, so without a
    check here adding an instrument reached a price provider on an install that
    had deliberately switched the feed off."""
    monkeypatch.setattr(main_module.settings.price_feed, "enabled", False)

    assert _kick_inside_a_loop() is False


def test_the_kick_runs_the_feed_when_it_is_enabled(monkeypatch, quiet_feed):
    """The other side of the guard, so it cannot be quietly turned into a
    no-op that would make the test above pass for the wrong reason."""
    monkeypatch.setattr(main_module.settings.price_feed, "enabled", True)

    assert _kick_inside_a_loop() is True
