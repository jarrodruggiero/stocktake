"""Falling back when a market-data source is down.

Yahoo is unofficial and breaks periodically. These tests are about what happens
then — and, just as much, about what happens when it *doesn't*: a fallback that
fires when the primary is merely quiet would rewrite the same rows with a
different `source` every run, and nobody would ever notice Yahoo had died.

Every payload here is canned. `conftest._no_network` blocks the real HTTP
helper for the whole suite, which exists because it is otherwise easy to have
had a test silently passing against live Frankfurter data.
"""

from __future__ import annotations

import datetime as dt
import urllib.error
from decimal import Decimal

import pytest

from app import providers

START = dt.date(2026, 3, 1)
END = dt.date(2026, 3, 5)


@pytest.fixture
def registry(monkeypatch):
    """Swap the provider table for named fakes, so ordering can be asserted
    without caring who the real providers are."""
    def _install(kind, entries):
        monkeypatch.setitem(providers.PROVIDERS, kind, entries)
    return _install


def rows(n=2):
    return [(START + dt.timedelta(days=i), Decimal("1.50")) for i in range(n)]


def dead(exc=None):
    def _call(_symbol, _start, _end):
        raise exc or urllib.error.URLError("connection refused")
    return _call


# --------------------------------------------------------------------------- #
# Order and fallback
# --------------------------------------------------------------------------- #

def test_the_first_provider_that_answers_wins(registry):
    registry("equity", [("primary", lambda *_: rows()), ("backup", dead())])

    result = providers.fetch("equity", "ACME.AX", START, END)

    assert result.ok
    assert result.source == "primary"
    assert result.summary() == "primary"


def test_a_dead_primary_falls_through_to_the_backup(registry):
    registry("equity", [("primary", dead()), ("backup", lambda *_: rows(3))])

    result = providers.fetch("equity", "ACME.AX", START, END)

    assert result.source == "backup"
    assert len(result.rows) == 3
    # The summary names both, so a silent fallback is impossible to miss.
    assert result.summary().startswith("backup (primary failed: unreachable")


def test_the_backup_is_not_called_when_the_primary_works(registry):
    """Otherwise every run would double the outbound traffic, and two free
    services would be getting hit for nothing."""
    calls = []
    registry("equity", [
        ("primary", lambda *_: rows()),
        ("backup", lambda *_: calls.append("backup") or rows()),
    ])

    providers.fetch("equity", "ACME.AX", START, END)

    assert calls == []


def test_an_empty_answer_is_treated_as_no_answer(registry):
    """A provider that returns nothing has not served the request — try the
    next one before concluding the market simply didn't trade."""
    registry("equity", [("primary", lambda *_: []), ("backup", lambda *_: rows())])

    result = providers.fetch("equity", "ACME.AX", START, END)

    assert result.source == "backup"
    assert "primary failed: no rows" in result.summary()


def test_a_short_answer_is_still_an_answer(registry):
    """Markets close. Fewer rows than the span is normal, and falling back on
    it would flap between sources and rewrite `source` on every run."""
    calls = []
    registry("equity", [
        ("primary", lambda *_: rows(1)),
        ("backup", lambda *_: calls.append("backup") or rows(5)),
    ])

    result = providers.fetch("equity", "ACME.AX", START, END)

    assert result.source == "primary"
    assert len(result.rows) == 1
    assert calls == []


def test_every_provider_failing_is_reported_not_raised(registry):
    """A feed run touches a dozen instruments; one unreachable source must not
    end the run for the other eleven."""
    registry("equity", [("primary", dead()), ("backup", dead(ValueError("bad json")))])

    result = providers.fetch("equity", "ACME.AX", START, END)

    assert not result.ok
    assert result.rows == []
    assert result.source is None
    assert "primary failed" in result.summary()
    assert "ValueError: bad json" in result.summary()


def test_a_kind_with_no_providers_says_so(registry):
    registry("equity", [])

    result = providers.fetch("equity", "ACME.AX", START, END)

    assert not result.ok
    assert result.summary() == "no providers configured"


def test_a_provider_raising_something_unexpected_is_caught(registry):
    """Bare `except Exception`, deliberately: a provider is parsing somebody
    else's JSON and can fail in ways we cannot enumerate."""
    registry("equity", [("primary", dead(KeyError("prices"))), ("backup", lambda *_: rows())])

    result = providers.fetch("equity", "ACME.AX", START, END)

    assert result.source == "backup"


# --------------------------------------------------------------------------- #
# Which list serves what
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("exchange,expected", [
    ("ASX", "equity"), ("NASDAQ", "equity"), ("NYSE", "equity"),
    ("CRYPTO", "crypto"), ("crypto", "crypto"), ("", "equity"),
])
def test_the_exchange_picks_the_provider_list(exchange, expected):
    assert providers.kind_for(exchange) == expected


def test_equities_have_no_fallback_and_that_is_deliberate():
    """Pinned so the reasoning survives: Stooq now serves a proof-of-work
    challenge instead of CSV, Alpha Vantage does not document ASX coverage, and
    Twelve Data puts ASX behind a paid add-on. A provider whose coverage of the
    real holdings can't be verified would fail silently and lie in `source`.

    Change this test when a verified equity provider is added — do not delete
    it to make a new one fit."""
    assert [name for name, _ in providers.PROVIDERS["equity"]] == ["yahoo"]
    assert [name for name, _ in providers.PROVIDERS["fx"]] == ["yahoo", "frankfurter"]
    assert [name for name, _ in providers.PROVIDERS["crypto"]] == ["yahoo", "coingecko"]


# --------------------------------------------------------------------------- #
# Frankfurter
# --------------------------------------------------------------------------- #

def canned(monkeypatch, payload, capture=None):
    def _get(url, params):
        if capture is not None:
            capture.append((url, params))
        return payload
    monkeypatch.setattr(providers, "_get_json", _get)


def test_frankfurter_reads_a_date_keyed_rate_series(monkeypatch):
    canned(monkeypatch, {"base": "USD", "rates": {
        "2026-03-02": {"AUD": 1.5123}, "2026-03-01": {"AUD": 1.5000},
    }})

    out = providers.frankfurter_fx("USDAUD", START, END)

    # Sorted by date regardless of dict order, and AUD per 1 USD — the same
    # direction `fx_rate` is stored in, so no inversion.
    assert out == [
        (dt.date(2026, 3, 1), Decimal("1.5")),
        (dt.date(2026, 3, 2), Decimal("1.5123")),
    ]


def test_frankfurter_is_asked_for_the_right_pair_and_window(monkeypatch):
    seen = []
    canned(monkeypatch, {"rates": {}}, capture=seen)

    providers.frankfurter_fx("USDAUD", START, END)

    (url, params) = seen[0]
    assert url.endswith("/2026-03-01..2026-03-05")
    assert params == {"base": "USD", "symbols": "AUD"}


def test_frankfurter_skips_days_it_has_no_rate_for(monkeypatch):
    """Weekends and holidays are absent, not zero. Inventing rows for closed
    days would be inventing data — the caller's stepper handles gaps."""
    canned(monkeypatch, {"rates": {
        "2026-03-01": {"AUD": 1.50}, "2026-03-02": {}, "2026-03-03": {"AUD": None},
    }})

    out = providers.frankfurter_fx("USDAUD", START, END)

    assert [d for d, _ in out] == [dt.date(2026, 3, 1)]


def test_frankfurter_copes_with_a_response_that_has_no_rates(monkeypatch):
    canned(monkeypatch, {})

    assert providers.frankfurter_fx("USDAUD", START, END) == []


# --------------------------------------------------------------------------- #
# CoinGecko
# --------------------------------------------------------------------------- #

def _ms(year, month, day, hour=0):
    return int(dt.datetime(year, month, day, hour, tzinfo=dt.timezone.utc).timestamp() * 1000)


def test_coingecko_returns_one_price_per_day(monkeypatch):
    """The endpoint emits several points for today (the last is "now"), so the
    latest wins — the closest thing it offers to a close."""
    canned(monkeypatch, {"prices": [
        [_ms(2026, 3, 1), 90000.0],
        [_ms(2026, 3, 2, 0), 91000.0],
        [_ms(2026, 3, 2, 14), 92000.5],
    ]})

    out = providers.coingecko_closes("BTC-AUD", START, END)

    assert out == [
        (dt.date(2026, 3, 1), Decimal("90000")),
        (dt.date(2026, 3, 2), Decimal("92000.5")),
    ]


def test_coingecko_drops_points_outside_the_window(monkeypatch):
    canned(monkeypatch, {"prices": [
        [_ms(2026, 2, 20), 80000.0],   # before start
        [_ms(2026, 3, 2), 91000.0],
        [_ms(2026, 3, 20), 99000.0],   # after end
    ]})

    out = providers.coingecko_closes("BTC-AUD", START, END)

    assert [d for d, _ in out] == [dt.date(2026, 3, 2)]


def test_coingecko_asks_in_aud_so_nothing_needs_converting(monkeypatch):
    seen = []
    canned(monkeypatch, {"prices": []}, capture=seen)

    providers.coingecko_closes("ETH-AUD", START, END)

    (url, params) = seen[0]
    assert url.endswith("/coins/ethereum/market_chart")
    assert params["vs_currency"] == "aud"


def test_an_unmapped_coin_fails_rather_than_guessing(monkeypatch):
    """Ticker collisions are rife in crypto. Guessing a slug would silently
    price the wrong asset, which is worse than having no fallback."""
    canned(monkeypatch, {"prices": []})

    with pytest.raises(LookupError, match="WAGMI"):
        providers.coingecko_closes("WAGMI-AUD", START, END)


def test_an_unmapped_coin_is_a_reported_failure_not_a_crash(registry, monkeypatch):
    canned(monkeypatch, {"prices": []})
    registry("crypto", [("yahoo", dead()), ("coingecko", providers.coingecko_closes)])

    result = providers.fetch("crypto", "WAGMI-AUD", START, END)

    assert not result.ok
    assert "LookupError" in result.summary()


def test_coingecko_ignores_junk_prices(monkeypatch):
    canned(monkeypatch, {"prices": [
        [_ms(2026, 3, 1), None], [_ms(2026, 3, 2), 0], [_ms(2026, 3, 3), "x"],
        [_ms(2026, 3, 4), 95000.0],
    ]})

    out = providers.coingecko_closes("BTC-AUD", START, END)

    assert out == [(dt.date(2026, 3, 4), Decimal("95000"))]


# --------------------------------------------------------------------------- #
# FX symbol shapes
# --------------------------------------------------------------------------- #

def test_each_fx_provider_gets_the_symbol_it_understands(registry):
    """Yahoo wants USDAUD=X and Frankfurter wants USDAUD. Handing either the
    other's format is a silent no-data run."""
    seen = {}

    def _yahoo(symbol, *_):
        seen["yahoo"] = symbol
        raise urllib.error.URLError("down")

    def _frank(symbol, *_):
        seen["frankfurter"] = symbol
        return rows()

    registry("fx", [("yahoo", _yahoo), ("frankfurter", _frank)])

    result = providers.fetch_fx("USDAUD", "USDAUD=X", START, END)

    assert seen == {"yahoo": "USDAUD=X", "frankfurter": "USDAUD"}
    assert result.source == "frankfurter"


# --------------------------------------------------------------------------- #
# What the feed records
# --------------------------------------------------------------------------- #

def test_the_feed_records_which_provider_served_each_row(session_factory, monkeypatch, registry):
    """`source` is on the row so a chart can be traced back to who supplied it.
    Hardcoding "yfinance" while a fallback served the data would be a lie in
    the database."""
    from types import SimpleNamespace

    from sqlalchemy import select

    import factories as fac
    from app import pricefeed, tenancy
    from app.models import FxRate, Price
    from app.settings import PriceFeedSettings

    registry("equity", [("yahoo", dead()), ("standin", lambda *_: [
        (dt.date(2026, 3, 2), Decimal("10.00"))])])
    registry("fx", [("yahoo", dead()), ("frankfurter", lambda *_: [
        (dt.date(2026, 3, 2), Decimal("1.50"))])])
    monkeypatch.setattr(pricefeed, "_yf", SimpleNamespace(Ticker=lambda _s: None))
    settings = SimpleNamespace(price_feed=PriceFeedSettings(backfill_start=dt.date(2026, 1, 1)))

    with tenancy.unscoped_session(session_factory) as session:
        fac.make_instrument(session, "ACME")
        fac.make_instrument(session, "NOVA", exchange="NASDAQ", currency="USD")
        session.commit()
        pricefeed.run_feed(session, settings)
        assert set(session.scalars(select(Price.source)).all()) == {"standin"}
        assert set(session.scalars(select(FxRate.source)).all()) == {"frankfurter"}

    # And the run says who served what, for the status page.
    assert pricefeed.last_run_sources["fx:USDAUD"].startswith("frankfurter (yahoo failed")
    assert "standin" in pricefeed.last_run_sources["equity:ACME"]


def test_crypto_instruments_use_the_crypto_provider_list(session_factory, monkeypatch, registry):
    from types import SimpleNamespace

    import factories as fac
    from app import pricefeed, tenancy
    from app.settings import PriceFeedSettings

    asked = []
    registry("crypto", [("coingecko", lambda s, *_: asked.append(("crypto", s)) or [])])
    registry("equity", [("yahoo", lambda s, *_: asked.append(("equity", s)) or [])])
    monkeypatch.setattr(pricefeed, "_yf", SimpleNamespace(Ticker=lambda _s: None))
    settings = SimpleNamespace(price_feed=PriceFeedSettings(backfill_start=dt.date(2026, 1, 1)))

    with tenancy.unscoped_session(session_factory) as session:
        fac.make_instrument(session, "BTC", exchange="CRYPTO", yahoo_symbol="BTC-AUD")
        session.commit()
        pricefeed.run_feed(session, settings)

    assert asked == [("crypto", "BTC-AUD")]


def test_the_dashboard_reports_a_degraded_source(client, session_factory, monkeypatch):
    """A fallback that nobody is told about is how a dead primary goes
    unnoticed for a month. The feed still reports "ok" — the run did finish —
    so the disclosure has to be separate from the success flag."""
    from test_routes import make_login

    make_login(client, session_factory)
    import app.main as main

    monkeypatch.setitem(
        main.feed_status, "degraded",
        {"fx:USDAUD": "frankfurter (yahoo failed: unreachable)"},
    )

    page = client.get("/", headers={"accept": "text/html"}).text

    # The panel was cut back to two times and nothing else,
    # so there is no explanatory sentence introducing this list. What
    # must survive is the disclosure itself — which source served, and what the
    # usual one did — because that is the whole reason it exists.
    assert "fx:USDAUD" in page
    assert "frankfurter (yahoo failed: unreachable)" in page


def test_the_dashboard_says_nothing_when_every_source_behaved(client, session_factory):
    from test_routes import make_login

    make_login(client, session_factory)

    page = client.get("/", headers={"accept": "text/html"}).text

    assert "usual source" not in page
