"""The price/FX feed.

Nothing here touches the network: `_closes` and `yf` are stubbed, which is also
how the feed is meant to be reasoned about — Yahoo is an unofficial source that
will break periodically, so the code's job is to fail one symbol at a time and
keep going.

The feed is the one sanctioned user of an unscoped session (market data is
shared, and the FX repair pass spans every portfolio), so these tests run
through `tenancy.unscoped_session` exactly as `pricefeed.main` does.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time
from sqlalchemy import func, select

import factories as fac
from app import pricefeed, queries, tenancy
from app.models import FxRate, MarketDividend, Price
from app.settings import PriceFeedSettings

BACKFILL = dt.date(2019, 1, 1)
SETTINGS = SimpleNamespace(price_feed=PriceFeedSettings(backfill_start=BACKFILL))


@pytest.fixture
def feed_session(session_factory):
    """An unscoped session — what the feed runs on."""
    with tenancy.unscoped_session(session_factory) as session:
        yield session


# --------------------------------------------------------------------------- #
# Symbols
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "ticker,exchange,expected",
    [
        ("acme", "ASX", "ACME.AX"),      # ASX listings carry the suffix
        ("NOVA", "NASDAQ", "NOVA"),      # US listings are bare
        ("NOVA", "NYSE", "NOVA"),
        ("btc", "CRYPTO", "BTC-AUD"),    # crypto is priced against AUD
        (" acme ", "ASX", "ACME.AX"),    # stripped and upper-cased
        ("ACME", "LSE", "ACME"),         # unknown exchange falls through bare
    ],
)
def test_yahoo_symbols_are_built_per_exchange(ticker, exchange, expected):
    assert pricefeed.yahoo_symbol_for(ticker, exchange) == expected


# --------------------------------------------------------------------------- #
# Where a fetch starts
# --------------------------------------------------------------------------- #

def test_with_no_stored_history_the_whole_range_is_fetched():
    assert pricefeed._fetch_start(None, None, 0, BACKFILL) == BACKFILL


def test_a_dense_series_resumes_from_the_day_after_the_last_close():
    first, last = dt.date(2020, 1, 1), dt.date(2020, 12, 31)
    # 365 days spans 365 * 5 // 7 = 260 weekdays; 200 stored closes is
    # 200 / 260 = 0.77, comfortably above the 0.6 density floor.
    assert pricefeed._fetch_start(first, last, 200, BACKFILL) == dt.date(2021, 1, 1)


def test_a_sparse_series_is_refetched_from_the_start():
    """Sheet-seeded prices were a handful of end-of-year points. Resuming from
    the last one would leave the six years in between permanently empty."""
    first, last = dt.date(2020, 1, 1), dt.date(2020, 12, 31)
    # 100 / 260 = 0.38, under the 0.6 floor → start again from backfill_start.
    assert pricefeed._fetch_start(first, last, 100, BACKFILL) == BACKFILL


def test_a_handful_of_points_is_always_treated_as_sparse():
    first, last = dt.date(2020, 1, 1), dt.date(2020, 1, 31)
    # 29 points over a month would pass the density test, but fewer than 30
    # rows is judged "not a real series" outright.
    assert pricefeed._fetch_start(first, last, 29, BACKFILL) == BACKFILL


# --------------------------------------------------------------------------- #
# A feed run
# --------------------------------------------------------------------------- #

def stub_closes(monkeypatch, rows_by_symbol):
    """Replace the network call with a lookup table."""
    def _closes(symbol, start, end):
        if isinstance(rows_by_symbol.get(symbol), Exception):
            raise rows_by_symbol[symbol]
        return [(fac.d(d), Decimal(c)) for d, c in rows_by_symbol.get(symbol, [])]

    monkeypatch.setattr(pricefeed, "_closes", _closes)


def stub_yf(monkeypatch, *, info=None, dividends=None, raises=False):
    """A stand-in for the yfinance module: only Ticker(...) is used.

    Patches `pricefeed._yf`, the cache behind the lazy `_yahoo()` accessor —
    setting it means the real yfinance is never imported, which is both faster
    and the thing `test_memory.py` asserts about a running app.
    """
    class _Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_info(self):
            if raises:
                raise RuntimeError("yahoo is down")
            return info or {}

        @property
        def dividends(self):
            return dividends if dividends is not None else _EmptySeries()

    monkeypatch.setattr(pricefeed, "_yf", SimpleNamespace(Ticker=_Ticker))


class _EmptySeries:
    empty = True

    def items(self):
        return iter(())


class _Series:
    """The shape run_feed uses off a yfinance dividends Series."""

    def __init__(self, pairs):
        self._pairs = pairs
        self.empty = not pairs

    def items(self):
        for when, amount in self._pairs:
            yield SimpleNamespace(date=lambda w=when: w), amount


def test_a_run_stores_closes_and_the_fx_pairs_it_needs(feed_session, monkeypatch):
    acme = fac.make_instrument(feed_session, "ACME", name="Acme")
    nova = fac.make_instrument(feed_session, "NOVA", exchange="NASDAQ", currency="USD",
                               name="Nova")
    feed_session.commit()
    stub_closes(monkeypatch, {
        "ACME.AX": [("2026-08-01", "10.00"), ("2026-08-02", "11.00")],
        "NOVA": [("2026-08-01", "20.00")],
        "USDAUD=X": [("2026-08-01", "1.50")],
    })
    stub_yf(monkeypatch)

    counts = pricefeed.run_feed(feed_session, SETTINGS)

    assert counts["prices"] == 3
    assert counts["fx"] == 1
    # The pair is derived from the instrument's currency, not configured.
    assert feed_session.scalars(select(FxRate.pair)).all() == ["USDAUD"]
    stored = feed_session.scalars(
        select(Price.close).where(Price.instrument_id == acme.id).order_by(Price.date)
    ).all()
    assert stored == [Decimal("10.000000"), Decimal("11.000000")]
    assert nova.id is not None


def test_rerunning_updates_a_close_rather_than_duplicating_it(feed_session, monkeypatch):
    """The upsert is what makes a re-fetch idempotent — without it a repaired
    sparse series would multiply rows on every run."""
    fac.make_instrument(feed_session, "ACME", name="Acme")
    feed_session.commit()
    stub_yf(monkeypatch)

    stub_closes(monkeypatch, {"ACME.AX": [("2026-08-01", "10.00")]})
    pricefeed.run_feed(feed_session, SETTINGS)
    stub_closes(monkeypatch, {"ACME.AX": [("2026-08-01", "12.50")]})
    pricefeed.run_feed(feed_session, SETTINGS)

    assert feed_session.scalar(select(func.count()).select_from(Price)) == 1
    assert feed_session.scalars(select(Price.close)).one() == Decimal("12.500000")


def test_one_dead_symbol_does_not_stop_the_others(feed_session, monkeypatch):
    fac.make_instrument(feed_session, "ACME", name="Acme")
    fac.make_instrument(feed_session, "WIDGET", name="Widget")
    feed_session.commit()
    stub_closes(monkeypatch, {
        "ACME.AX": RuntimeError("delisted"),
        "WIDGET.AX": [("2026-08-01", "3.00")],
    })
    stub_yf(monkeypatch)

    counts = pricefeed.run_feed(feed_session, SETTINGS)

    assert counts["prices"] == 1
    assert feed_session.scalars(select(Price.close)).all() == [Decimal("3.000000")]


# --------------------------------------------------------------------------- #
# The FX repair pass
# --------------------------------------------------------------------------- #

def test_a_trade_with_no_rate_takes_the_one_on_or_before_its_date(feed_session, monkeypatch):
    """Nearest rate *before* the trade, never after: a rate published later
    could not have applied on the day the money moved."""
    # The portfolio is built on the feed's own session: a second open session
    # against the same sqlite file would sit on the single writer lock.
    portfolio = fac.make_portfolio(feed_session, "Owner portfolio")
    nova = fac.make_instrument(feed_session, "NOVA", exchange="NASDAQ", currency="USD",
                               name="Nova")
    trade = fac.add_trade(feed_session, nova, "2026-03-15", "buy", 10, "20.00",
                          fx_rate=None, portfolio_id=portfolio.id)
    # One rate before the trade and one after; only the earlier may be used.
    fac.add_fx(feed_session, "USDAUD", "2026-03-10", "1.40")
    fac.add_fx(feed_session, "USDAUD", "2026-03-20", "1.90")
    feed_session.commit()
    stub_closes(monkeypatch, {})
    stub_yf(monkeypatch)

    counts = pricefeed.run_feed(feed_session, SETTINGS)

    feed_session.refresh(trade)
    assert counts["trade_fx_backfilled"] == 1
    assert trade.fx_rate == Decimal("1.400000")


def test_a_trade_that_already_has_a_rate_is_left_alone(feed_session, monkeypatch):
    portfolio = fac.make_portfolio(feed_session, "Owner portfolio")
    nova = fac.make_instrument(feed_session, "NOVA", exchange="NASDAQ", currency="USD",
                               name="Nova")
    trade = fac.add_trade(feed_session, nova, "2026-03-15", "buy", 10, "20.00",
                          fx_rate="1.55", portfolio_id=portfolio.id)
    fac.add_fx(feed_session, "USDAUD", "2026-03-10", "1.40")
    feed_session.commit()
    stub_closes(monkeypatch, {})
    stub_yf(monkeypatch)

    counts = pricefeed.run_feed(feed_session, SETTINGS)

    feed_session.refresh(trade)
    assert counts["trade_fx_backfilled"] == 0
    assert trade.fx_rate == Decimal("1.550000")


# --------------------------------------------------------------------------- #
# Names and distribution history
# --------------------------------------------------------------------------- #

def test_only_unnamed_instruments_are_named(feed_session, monkeypatch):
    """A CSV import creates bare tickers; the feed tidies them up without
    overwriting anything a person typed."""
    unnamed = fac.make_instrument(feed_session, "ACME", name=None)
    named = fac.make_instrument(feed_session, "WIDGET", name="Widget Industries")
    feed_session.commit()
    monkeypatch.setattr(pricefeed, "lookup",
                        lambda ticker, exchange: {"symbol": f"{ticker}.AX",
                                                  "name": f"{ticker} Discovered",
                                                  "currency": "AUD", "found": True})

    filled = pricefeed.backfill_names(feed_session)

    assert filled == 1
    assert unnamed.name == "ACME Discovered"
    assert named.name == "Widget Industries"


def test_distribution_history_is_stored_and_non_positive_amounts_ignored(feed_session,
                                                                        monkeypatch):
    acme = fac.make_instrument(feed_session, "ACME", name="Acme")
    feed_session.commit()
    stub_yf(monkeypatch, dividends=_Series([
        (dt.datetime(2026, 3, 20), 0.55),
        (dt.datetime(2026, 6, 20), 0.0),     # a zero entry is not a distribution
        (dt.datetime(2026, 9, 20), 0.61),
    ]))

    stored = pricefeed.fetch_dividends(feed_session, acme)

    assert stored == 2
    rows = feed_session.execute(
        select(MarketDividend.ex_date, MarketDividend.amount).order_by(MarketDividend.ex_date)
    ).all()
    assert [r.ex_date for r in rows] == [dt.date(2026, 3, 20), dt.date(2026, 9, 20)]
    assert rows[0].amount == Decimal("0.55000000")


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #

def test_a_lookup_maps_the_name_and_currency(monkeypatch):
    stub_yf(monkeypatch, info={"longName": "Acme Industries Ltd", "currency": "aud",
                               "regularMarketPrice": 10.5})

    found = pricefeed.lookup("acme", "ASX")

    assert found == {"symbol": "ACME.AX", "name": "Acme Industries Ltd",
                     "currency": "AUD", "found": True}


def test_a_dead_feed_does_not_break_the_add_instrument_form(monkeypatch):
    """Failure is not an error here: the caller falls back to whatever was
    typed, so a Yahoo outage must not stop someone adding a holding."""
    stub_yf(monkeypatch, raises=True)

    found = pricefeed.lookup("ACME", "ASX")

    assert found == {"symbol": "ACME.AX", "name": None, "currency": None, "found": False}


def test_a_symbol_yahoo_does_not_recognise_reports_not_found(monkeypatch):
    # Neither a name nor a price: Yahoo echoing the symbol back at us.
    stub_yf(monkeypatch, info={"symbol": "NOSUCH.AX"})

    assert pricefeed.lookup("NOSUCH", "ASX")["found"] is False


# --------------------------------------------------------------------------- #
# Market sessions — what a market has finished, not what day it is here
# --------------------------------------------------------------------------- #
#
# Both bugs this guards were the same mistake: using the portfolio's date to
# decide what a foreign market had done. One asked New York for a session that
# had not started; the other wrote a half-finished ASX day into `price.close`.

def _syd(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=ZoneInfo("Australia/Sydney"))


@pytest.mark.parametrize(
    "exchange,when,expected,why",
    [
        # 18:00 Sydney Wednesday is 04:00 Wednesday in New York: the date there
        # has turned over but the market has not opened. This is the scheduled
        # feed time, and the reason for the whole table.
        ("NASDAQ", "2026-08-12 18:00", "2026-08-11", "NY has not opened yet"),
        # 09:00 Sydney Thursday is 19:00 Wednesday in New York — after the bell.
        ("NASDAQ", "2026-08-13 09:00", "2026-08-12", "NY has closed"),
        # The ASX is mid-session at noon; its close does not exist yet.
        ("ASX", "2026-08-12 12:00", "2026-08-11", "ASX still trading"),
        # The bell is not the same as a settled close, so the half hour after
        # it still reads as yesterday — this is the window a restart at 16:05
        # would otherwise have written a nearly-final number into.
        ("ASX", "2026-08-12 16:00", "2026-08-11", "the bell itself is too early"),
        ("ASX", "2026-08-12 16:29", "2026-08-11", "still settling"),
        ("ASX", "2026-08-12 16:30", "2026-08-12", "settled"),
        ("ASX", "2026-08-12 18:00", "2026-08-12", "the scheduled run"),
        # Saturday and Sunday roll back to Friday rather than naming a day the
        # market was shut.
        ("ASX", "2026-08-15 18:00", "2026-08-14", "Saturday"),
        ("ASX", "2026-08-17 09:00", "2026-08-14", "Monday before the open"),
        # 18:00 Sydney Monday is 04:00 Monday in New York, so the last US close
        # is Friday's — two roll-backs, one for the clock and one for the week.
        ("NASDAQ", "2026-08-17 18:00", "2026-08-14", "Monday, NY not open"),
    ],
)
def test_a_market_is_asked_only_for_sessions_it_has_finished(exchange, when, expected, why):
    assert pricefeed.last_close_date(exchange, _syd(when)) == dt.date.fromisoformat(expected), why


@pytest.mark.parametrize("exchange", ["CRYPTO", "LSE", "", None])
def test_a_market_with_no_known_close_defers_to_the_portfolio(exchange):
    """None means "I don't know", and the caller keeps its old behaviour.

    Crypto never closes and an exchange nobody has added to MARKETS is unknown;
    inventing a session boundary for either would be worse than the bug.
    """
    assert pricefeed.last_close_date(exchange) is None


def stub_windows(monkeypatch, rows_by_symbol):
    """Like `stub_closes`, but records the window asked for and RESPECTS it.

    Respecting `end` is the point: a real provider cannot return a bar for a
    session that has not happened, so a stub that ignores the window would let
    these tests pass while the app still asked for tomorrow.
    """
    asked: dict[str, tuple[dt.date, dt.date]] = {}

    def _closes(symbol, start, end):
        asked[symbol] = (start, end)
        return [
            (fac.d(d), Decimal(c))
            for d, c in rows_by_symbol.get(symbol, [])
            if start <= fac.d(d) <= end
        ]

    monkeypatch.setattr(pricefeed, "_closes", _closes)
    return asked


def seed_dense(session, inst, last: str, days: int = 60) -> None:
    """A real-looking weekday series ending on `last`.

    `_fetch_start` refetches from the beginning when a series looks sparse, so
    a couple of hand-placed rows would send every one of these tests down the
    backfill path and test nothing about resuming.
    """
    day = fac.d(last)
    placed = 0
    while placed < days:
        if day.weekday() < 5:
            session.add(Price(instrument_id=inst.id, date=day,
                              close=Decimal("10.00"), source="yahoo"))
            placed += 1
        day -= dt.timedelta(days=1)


@freeze_time("2026-08-12 08:00:00")  # 18:00 Sydney — the scheduled feed time
def test_the_run_does_not_ask_a_market_for_a_session_it_has_not_had(
        feed_session, monkeypatch):
    """The bug as it actually happened.

    NOVA's last close is Tuesday's, so asking from the portfolio's date requests
    Wednesday — which
    in New York had not opened. Yahoo answered "no rows", the run logged
    `possibly delisted`, and the warning that exists to catch a dead provider
    fired for a market that was merely asleep.
    """
    acme = fac.make_instrument(feed_session, "ACME", name="Acme")
    nova = fac.make_instrument(feed_session, "NOVA", exchange="NASDAQ",
                               currency="USD", name="Nova")
    feed_session.flush()
    seed_dense(feed_session, acme, "2026-08-11")
    seed_dense(feed_session, nova, "2026-08-11")
    feed_session.commit()

    asked = stub_windows(monkeypatch, {"ACME.AX": [("2026-08-12", "11.00")]})
    stub_yf(monkeypatch)

    pricefeed.run_feed(feed_session, SETTINGS)

    # Sydney closed at 16:00, two hours ago, so the ASX listing is fetched.
    assert asked["ACME.AX"] == (dt.date(2026, 8, 12), dt.date(2026, 8, 12))
    # New York has not opened, and its last close is already stored — so it is
    # not asked at all. Before this it was asked for the 12th and failed.
    assert "NOVA" not in asked


@freeze_time("2026-08-12 02:00:00")  # 12:00 Sydney — the ASX is mid-session
def test_a_restart_during_trading_hours_does_not_store_a_partial_day_as_a_close(
        feed_session, monkeypatch):
    """Observed live 2026-08-12: a pod restarted at 10:30 while the ASX was
    open, fetched the day's *moving* bar and wrote 114.08 into `price.close`
    for a day that ended somewhere else.

    The row was never corrected — `_fetch_start` resumes from `last + 1`, so a
    day that already has a price is never asked for again. 99 of 359 settled
    closes were wrong by the time this was found, and nothing had ever reported
    an error.
    """
    acme = fac.make_instrument(feed_session, "ACME", name="Acme")
    feed_session.flush()
    seed_dense(feed_session, acme, "2026-08-11")
    feed_session.commit()

    asked = stub_windows(monkeypatch, {"ACME.AX": [("2026-08-12", "114.08")]})
    stub_yf(monkeypatch)

    counts = pricefeed.run_feed(feed_session, SETTINGS)

    assert "ACME.AX" not in asked, "asked for a day the market had not finished"
    assert counts["prices"] == 0
    assert dt.date(2026, 8, 12) not in feed_session.scalars(select(Price.date)).all()


@freeze_time("2026-08-12 08:00:00")
def test_an_exchange_with_no_known_bell_settles_through_yesterday(
        feed_session, monkeypatch):
    """No entry in MARKETS means we cannot know when its day ended.

    Yesterday is the honest answer — the latest date that certainly cannot move
    any more — and today's figure comes from `refresh_quotes` instead. Guessing
    that an unknown market closes when ours does is how the original bug got
    written.
    """
    inst = fac.make_instrument(feed_session, "LON", exchange="LSE", name="Lon")
    feed_session.flush()
    seed_dense(feed_session, inst, "2026-08-10")
    feed_session.commit()

    asked = stub_windows(monkeypatch, {"LON": [("2026-08-11", "5.00")]})
    stub_yf(monkeypatch)

    pricefeed.run_feed(feed_session, SETTINGS)

    assert asked["LON"] == (dt.date(2026, 8, 11), dt.date(2026, 8, 11))


# Live prices for the session that has not closed yet. The two rules that make
# it safe, and why neither works alone: decisions.md #12 and #13.

def stub_quotes(monkeypatch, by_symbol):
    """`{symbol: (aware datetime, price)}`, as the spark endpoint gives it."""
    monkeypatch.setattr(
        pricefeed.providers, "yahoo_quotes",
        lambda symbols: {s: v for s, v in by_symbol.items() if s in symbols},
    )


@freeze_time("2026-08-12 02:00:00")  # 12:00 Sydney — the ASX is mid-session
def test_a_live_price_is_stored_for_the_unfinished_day(feed_session, monkeypatch):
    acme = fac.make_instrument(feed_session, "ACME", name="Acme")
    feed_session.flush()
    seed_dense(feed_session, acme, "2026-08-11")
    feed_session.commit()

    stub_quotes(monkeypatch, {"ACME.AX": (_syd("2026-08-12 12:00"), Decimal("11.50"))})
    assert pricefeed.refresh_quotes(feed_session, SETTINGS) == 1

    row = feed_session.execute(
        select(Price).where(Price.date == dt.date(2026, 8, 12))
    ).scalar_one()
    assert row.close == Decimal("11.500000")
    assert row.provisional is True, "a live price must never look like a close"
    assert row.source == "yahoo-live"


@freeze_time("2026-08-12 08:00:00")  # 18:00 Sydney — after the bell
def test_a_quote_never_overwrites_a_day_the_market_has_settled(
        feed_session, monkeypatch):
    """The line that keeps history clean. By 18:00 the ASX has closed, so the
    12th belongs to the close feed and a late quote must not touch it."""
    acme = fac.make_instrument(feed_session, "ACME", name="Acme")
    feed_session.flush()
    seed_dense(feed_session, acme, "2026-08-12")
    feed_session.commit()

    stub_quotes(monkeypatch, {"ACME.AX": (_syd("2026-08-12 18:00"), Decimal("99.99"))})
    assert pricefeed.refresh_quotes(feed_session, SETTINGS) == 0

    row = feed_session.execute(
        select(Price).where(Price.date == dt.date(2026, 8, 12))
    ).scalar_one()
    assert row.close == Decimal("10.000000")
    assert row.provisional is False


def test_the_close_run_replaces_a_live_price_with_the_real_close(
        feed_session, monkeypatch):
    """The whole guarantee, end to end: live during the session, close after.

    The failure this pins is a provisional row surviving as if it were a close.
    `_fetch_start` resumes from the last SETTLED date, so the evening run asks
    for the day again even though a row already exists — which is exactly what
    a feed resuming from the last stored day does not do.
    """
    acme = fac.make_instrument(feed_session, "ACME", name="Acme")
    feed_session.flush()
    seed_dense(feed_session, acme, "2026-08-11")
    feed_session.commit()

    with freeze_time("2026-08-12 02:00:00"):  # midday: a live price lands
        stub_quotes(monkeypatch,
                    {"ACME.AX": (_syd("2026-08-12 12:00"), Decimal("11.50"))})
        pricefeed.refresh_quotes(feed_session, SETTINGS)

    with freeze_time("2026-08-12 08:00:00"):  # 18:00: the close arrives
        asked = stub_windows(monkeypatch, {"ACME.AX": [("2026-08-12", "12.25")]})
        stub_yf(monkeypatch)
        pricefeed.run_feed(feed_session, SETTINGS)

    assert asked["ACME.AX"] == (dt.date(2026, 8, 12), dt.date(2026, 8, 12)), \
        "the provisional day must be asked for again"
    row = feed_session.execute(
        select(Price).where(Price.date == dt.date(2026, 8, 12))
    ).scalar_one()
    assert row.close == Decimal("12.250000"), "the close must win"
    assert row.provisional is False
    assert feedcount(feed_session) == 1, "and it must not have duplicated"


def feedcount(session) -> int:
    return session.scalar(
        select(func.count()).select_from(Price)
        .where(Price.date == dt.date(2026, 8, 12))
    )


@pytest.mark.parametrize(
    "exchange,when,trading,why",
    [
        ("ASX", "2026-08-12 12:00", True, "mid-session"),
        ("ASX", "2026-08-12 09:59", False, "before the open"),
        ("ASX", "2026-08-12 16:00", False, "the bell has gone"),
        ("ASX", "2026-08-15 12:00", False, "Saturday"),
        # 18:00 Sydney is 04:00 in New York — the case that makes the gate
        # worth having, because the ASX is shut and the US has not opened.
        ("NASDAQ", "2026-08-12 18:00", False, "NY is asleep"),
        # 00:30 Sydney is 10:30 the previous morning in New York.
        ("NASDAQ", "2026-08-13 00:30", True, "NY mid-session"),
        ("CRYPTO", "2026-08-15 03:00", True, "crypto never shuts"),
        ("LSE", "2026-08-15 03:00", True, "unknown means unknown, not shut"),
    ],
)
def test_the_poll_only_runs_while_a_market_is_actually_trading(
        exchange, when, trading, why):
    assert pricefeed.is_trading(exchange, _syd(when)) is trading, why


@freeze_time("2026-08-12 08:00:00")  # 18:00 Sydney: ASX shut, NY not open
def test_holding_only_shut_markets_means_no_poll(feed_session):
    fac.make_instrument(feed_session, "ACME", name="Acme")
    fac.make_instrument(feed_session, "NOVA", exchange="NASDAQ", currency="USD",
                        name="Nova")
    feed_session.commit()
    assert pricefeed.any_market_trading(feed_session) is False


@freeze_time("2026-08-12 08:00:00")
def test_one_open_market_is_enough_to_poll(feed_session):
    """Crypto trades through the night, so holding any is enough."""
    fac.make_instrument(feed_session, "ACME", name="Acme")
    fac.make_instrument(feed_session, "BTC", exchange="CRYPTO", name="Bitcoin")
    feed_session.commit()
    assert pricefeed.any_market_trading(feed_session) is True


# --------------------------------------------------------------------------- #
# Coming back after downtime
# --------------------------------------------------------------------------- #
#
# This app is meant to run on a NAS that gets switched off, and some people will
# only start it when they want to look. So "the app has been down for months"
# is a normal state, not an incident.

@freeze_time("2026-08-12 08:00:00")
def test_months_of_downtime_backfills_on_the_next_run(feed_session, monkeypatch):
    """The whole gap is asked for in one window, not one day at a time.

    `_fetch_start` resumes from the day after the last stored close and the run
    ends at the last completed session, so however long the app was off, the
    next start asks for exactly the span it missed.
    """
    acme = fac.make_instrument(feed_session, "ACME", name="Acme")
    feed_session.flush()
    seed_dense(feed_session, acme, "2026-05-15")  # last seen three months ago
    feed_session.commit()

    asked = stub_windows(monkeypatch, {"ACME.AX": [("2026-08-11", "12.00")]})
    stub_yf(monkeypatch)

    pricefeed.run_feed(feed_session, SETTINGS)

    assert asked["ACME.AX"] == (dt.date(2026, 5, 16), dt.date(2026, 8, 12)), \
        "one window covering everything missed"


@freeze_time("2026-08-12 08:00:00")
def test_a_hole_in_the_middle_is_found_and_refetched(feed_session, monkeypatch):
    """A trailing gap fixes itself; an interior one would not.

    Resuming from `last + 1` steps straight over a hole that has newer rows
    after it, so the missing days would stay missing forever. This is the check
    that goes looking for them.
    """
    acme = fac.make_instrument(feed_session, "ACME", name="Acme")
    feed_session.flush()
    seed_dense(feed_session, acme, "2026-08-11", days=60)
    # Punch out a working week from the middle.
    for day in ("2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17"):
        feed_session.execute(
            Price.__table__.delete().where(Price.date == fac.d(day))
        )
    feed_session.commit()

    asked = stub_windows(monkeypatch, {"ACME.AX": [("2026-07-13", "9.00")]})
    stub_yf(monkeypatch)

    pricefeed.run_feed(feed_session, SETTINGS)

    assert asked["ACME.AX"][0] == dt.date(2026, 7, 13), \
        "the refetch must start at the hole, not after the last row"


@pytest.mark.parametrize(
    "missing,found,why",
    [
        (["2026-07-14"], False, "one weekday is a public holiday"),
        (["2026-07-14", "2026-07-15"], False, "two is Christmas or Easter"),
        (["2026-07-14", "2026-07-15", "2026-07-16"], True, "three is a real gap"),
    ],
)
def test_a_public_holiday_is_not_mistaken_for_a_gap(missing, found, why):
    """The rule that stops this refetching for ever.

    A market shut for a holiday looks exactly like missing data, and a check
    that flagged it would re-ask the same days on every run, permanently.
    Runs of three or more weekdays are longer than any holiday closure.
    """
    days = [d for d in (dt.date(2026, 7, 13) + dt.timedelta(days=i) for i in range(14))
            if d.weekday() < 5 and d.isoformat() not in missing]
    assert (pricefeed._earliest_gap(days) is not None) is found, why


# --------------------------------------------------------------------------- #
# The "Live" badge speaks for the portfolio, so it is built from HOLDINGS
# --------------------------------------------------------------------------- #

@freeze_time("2026-08-12 08:00:00")  # 18:00 Sydney: ASX shut, New York asleep
def test_something_not_held_cannot_light_the_live_badge(pf):
    """The live failure. `BTC` sat in the instrument table with **zero units** —
    tracked by the feed, owned by nobody — and because crypto never closes it
    announced "Live" over a portfolio of shut-for-the-day ASX holdings, around
    the clock.
    """
    from app.main import _live_exchanges

    held = fac.make_instrument(pf, "ACME", name="Acme")
    crypto = fac.make_instrument(pf, "BTC", exchange="CRYPTO", name="Bitcoin")
    pf.flush()
    fac.add_trade(pf, held, "2026-03-01", "buy", 100, "5.00")
    fac.add_prices(pf, held, [("2026-08-11", "5.00")])
    pf.commit()

    holdings = queries.all_holdings(pf)
    open_positions, _closed = queries.split_positions(holdings)

    assert [h.instrument.ticker for h in open_positions] == ["ACME"]
    assert crypto.id is not None
    assert _live_exchanges(open_positions) == []


@freeze_time("2026-08-12 08:00:00")
def test_crypto_you_actually_hold_does_light_it(pf):
    """Held crypto really is moving at 6pm, and its holder wants to see that.
    The rule is about ownership, not about crypto."""
    from app.main import _live_exchanges

    crypto = fac.make_instrument(pf, "BTC", exchange="CRYPTO", name="Bitcoin")
    pf.flush()
    fac.add_trade(pf, crypto, "2026-03-01", "buy", 1, "50000.00")
    fac.add_prices(pf, crypto, [("2026-08-11", "90000.00")])
    pf.commit()

    open_positions, _closed = queries.split_positions(queries.all_holdings(pf))

    assert _live_exchanges(open_positions) == ["CRYPTO"]


@freeze_time("2026-08-12 02:00:00")  # 12:00 Sydney — the ASX is mid-session
def test_the_badge_names_every_open_exchange(pf):
    """What the popup lists when the dot is clicked."""
    from app.main import _live_exchanges

    asx = fac.make_instrument(pf, "ACME", name="Acme")
    us = fac.make_instrument(pf, "NOVA", exchange="NASDAQ", currency="USD",
                             name="Nova")
    pf.flush()
    for inst in (asx, us):
        fac.add_trade(pf, inst, "2026-03-01", "buy", 10, "5.00")
    pf.commit()

    open_positions, _closed = queries.split_positions(queries.all_holdings(pf))

    # Sydney is trading; New York is asleep at midday here.
    assert _live_exchanges(open_positions) == ["ASX"]
