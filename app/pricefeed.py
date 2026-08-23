"""Price/FX feed: daily closes from Yahoo Finance.

Only ticker symbols are sent — never quantities or holdings.

One run: daily closes per instrument from the later of (last stored date + 1)
and `price_feed.backfill_start`; the same for one FX pair per non-AUD currency;
distribution history per instrument; then a repair pass giving any trade with a
null `fx_rate` the stored rate nearest on-or-before its date.

Run ad hoc with `python -m app.pricefeed`; the app also runs it daily and at
startup. Writes commit per instrument rather than once at the end —
decisions.md #83.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from appkit import dialect_insert, load_config, make_session_factory

from . import clock, memory, providers, tenancy
from .models import FxRate, Instrument, MarketDividend, Price, Trade
from .settings import PortfolioSettings

log = logging.getLogger(__name__)

# What served each symbol on the most recent run, keyed "kind:symbol". Module
# state rather than a return value because the status page reads it out of band
# — the same reason `main.feed_status` lives where it does.
last_run_sources: dict[str, str] = {}

# yfinance is imported on FIRST USE — ~101 MiB of pandas and numpy, and this
# module is imported at startup (decisions.md #17). The module global is the
# seam the tests stub, which keeps the suite off the network.
_yf = None


def _yahoo():
    global _yf
    if _yf is None:
        import yfinance

        _yf = yfinance
    return _yf


def _closes(symbol: str, start: dt.date, end: dt.date) -> list[tuple[dt.date, Decimal]]:  # pragma: no cover - the one function that actually calls Yahoo, and reshapes whatever DataFrame it returns. Covering it means either a network call or a mock of pandas' shape, which tests yfinance rather than this app; every caller stubs it.
    frame = _yahoo().download(
        symbol,
        start=start.isoformat(),
        end=(end + dt.timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if frame is None or frame.empty:
        return []
    closes = frame["Close"]
    if hasattr(closes, "columns"):  # yfinance MultiIndex frame for single ticker
        closes = closes[symbol] if symbol in closes.columns else closes.iloc[:, 0]
    out = []
    for ts, value in closes.dropna().items():
        out.append((ts.date(), Decimal(str(round(float(value), 6)))))
    return out


@dataclass(frozen=True)
class Market:
    """Where an exchange is, when it opens, and when its trading day ends.

    `suffix` is Yahoo's. `zone = None` means the market never closes (crypto).
    Why an exchange's own clock rather than the portfolio's: decisions.md #10.
    """

    suffix: str
    zone: str | None
    close: dt.time | None
    # Only used to decide whether a live quote is worth asking for.
    open: dt.time | None = None


# The bell is not a settled close; asking in the gap stores a nearly-final
# number as final. Only has to cover restarts — the daily run is hours later.
SETTLE = dt.timedelta(minutes=30)

# Adding an exchange means adding a line here. Close times are local wall clock,
# so daylight saving is handled by the zone rather than by arithmetic.
MARKETS: dict[str, Market] = {
    "ASX": Market(".AX", "Australia/Sydney", dt.time(16, 0), dt.time(10, 0)),
    "NASDAQ": Market("", "America/New_York", dt.time(16, 0), dt.time(9, 30)),
    "NYSE": Market("", "America/New_York", dt.time(16, 0), dt.time(9, 30)),
    # None BY DECISION, not by omission — decisions.md #14. Do not "fix".
    "CRYPTO": Market("-AUD", None, None),
}


def yahoo_symbol_for(ticker: str, exchange: str) -> str:
    ticker = ticker.strip().upper()
    market = MARKETS.get(exchange.strip().upper())
    return f"{ticker}{market.suffix if market else ''}"


def last_close_date(exchange: str, now: dt.datetime | None = None) -> dt.date | None:
    """The most recent date this market can have a CLOSING price for.

    None when the market's hours are unknown, or it never closes; the caller
    then falls back to the portfolio's own today. Weekends roll back to Friday,
    public holidays deliberately do not — decisions.md #10 and #11.
    """
    market = MARKETS.get((exchange or "").strip().upper())
    if market is None or market.zone is None or market.close is None:
        return None
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(market.zone))
    day = local.date()
    published = dt.datetime.combine(day, market.close, tzinfo=local.tzinfo) + SETTLE
    if local < published:
        day -= dt.timedelta(days=1)
    while day.weekday() >= 5:  # Saturday, Sunday
        day -= dt.timedelta(days=1)
    return day


def lookup(ticker: str, exchange: str) -> dict:
    """Ask Yahoo what this instrument is: name, currency, and whether the
    symbol resolves at all.

    Used to fill the add-instrument form and to backfill names nobody typed in.
    Only the ticker leaves the network — the same privacy line the price feed
    draws. Failure is not an error: the caller falls back to what was typed.
    """
    symbol = yahoo_symbol_for(ticker, exchange)
    out = {"symbol": symbol, "name": None, "currency": None, "found": False}
    try:
        info = _yahoo().Ticker(symbol).get_info() or {}
    except Exception as exc:
        log.warning("yahoo lookup failed for %s: %s", symbol, exc)
        return out
    name = info.get("longName") or info.get("shortName")
    currency = (info.get("currency") or "").upper() or None
    # A quote with neither a name nor a price is Yahoo echoing the symbol back.
    if name or info.get("regularMarketPrice") is not None:
        out.update(name=name, currency=currency, found=True)
    return out


def fetch_dividends(session: Session, inst: Instrument) -> int:
    """Store the instrument's published distribution history.

    This is what lets the calendar predict payments for a holding bought last
    month: the cycle is a property of the security, not of when you happened to
    buy it. Yahoo gives ex-dates and per-unit amounts; upcoming dates aren't in
    any free feed, which is why the calendar projects rather than announces.
    """
    try:
        series = _yahoo().Ticker(inst.yahoo_symbol).dividends
    except Exception as exc:
        log.warning("dividend history failed for %s: %s", inst.ticker, exc)
        return 0
    if series is None or series.empty:
        return 0
    stored = 0
    for ts, amount in series.items():
        value = Decimal(str(round(float(amount), 8)))
        if value <= 0:
            continue
        session.execute(
            dialect_insert(session, MarketDividend)
            .values(
                instrument_id=inst.id,
                ex_date=ts.date(),
                amount=value,
                source="yfinance",
            )
            .on_conflict_do_update(
                index_elements=["instrument_id", "ex_date"],
                set_={"amount": value, "source": "yfinance"},
            )
        )
        stored += 1
    return stored


def backfill_names(session: Session) -> int:
    """Give unnamed instruments their real names. Runs with the price feed, so
    anything added by a CSV import gets tidied up without being asked."""
    filled = 0
    for inst in session.scalars(
        select(Instrument).where(Instrument.name.is_(None))
    ).all():
        found = lookup(inst.ticker, inst.exchange)
        if found["name"]:
            inst.name = found["name"][:80]
            filled += 1
            log.info("named %s: %s", inst.ticker, inst.name)
            session.commit()  # don't hold the write lock across lookups
    return filled


def _series_shape(session: Session, model, *conditions):
    first = session.execute(
        select(model.date).where(*conditions).order_by(model.date.asc()).limit(1)
    ).scalar()
    last = session.execute(
        select(model.date).where(*conditions).order_by(model.date.desc()).limit(1)
    ).scalar()
    count = session.execute(
        select(func.count()).select_from(model).where(*conditions)
    ).scalar()
    return first, last, count


def _fetch_start(first, last, count, backfill_start: dt.date) -> dt.date:
    """Incremental from the last stored close — unless the series is sparse
    (e.g. only sheet-seeded EOFY prices exist), in which case refetch the lot
    from backfill_start; the upsert makes that idempotent. Density is judged
    against the span actually stored, so instruments listed after
    backfill_start (fetched dense from their listing date) stay incremental."""
    if first is None or last is None or not count:
        return backfill_start
    if count < 30:  # a handful of sheet-seeded points, not a real series
        return backfill_start
    weekdays = max(1, (last - first).days * 5 // 7)
    if count / weekdays < 0.6:
        return backfill_start
    return last + dt.timedelta(days=1)


# Below three, every public holiday becomes a permanent "gap" — decisions.md #11.
GAP_WEEKDAYS = 3


def _earliest_gap(dates: list[dt.date]) -> dt.date | None:
    """The first day of the first real hole in a stored series, or None.

    A gap at the END fixes itself: the next run resumes from the last stored
    date and asks for the whole span at once, however long the app was off. A
    hole in the MIDDLE is stepped over, and nothing else would ever notice it.
    """
    for prev, following in zip(dates, dates[1:]):
        missing = []
        day = prev + dt.timedelta(days=1)
        while day < following:
            if day.weekday() < 5:
                missing.append(day)
            day += dt.timedelta(days=1)
        if len(missing) >= GAP_WEEKDAYS:
            return missing[0]
    return None


def settled_through(exchange: str, now: dt.datetime | None = None) -> dt.date:
    """The last date whose price for this exchange is final.

    Yesterday, for a market with no known bell: it is the latest date that
    cannot still move.
    """
    known = last_close_date(exchange, now)
    return known if known is not None else clock.today() - dt.timedelta(days=1)


def is_trading(exchange: str, now: dt.datetime | None = None) -> bool:
    """Whether this exchange is in session right now.

    A market with no known hours counts as trading: crypto genuinely is, and an
    exchange missing from `MARKETS` is unknown rather than shut — never showing
    it a live price would be a worse failure than one wasted request.
    """
    market = MARKETS.get((exchange or "").strip().upper())
    if market is None or market.zone is None or market.open is None or market.close is None:
        return True
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(market.zone))
    if local.weekday() >= 5:
        return False
    return market.open <= local.time() < market.close


def any_market_trading(session: Session, now: dt.datetime | None = None) -> bool:
    """Whether anything held is on an exchange that is currently trading."""
    exchanges = session.scalars(
        select(Instrument.exchange)
        .where(Instrument.yahoo_symbol.is_not(None))
        .distinct()
    ).all()
    return any(is_trading(e, now) for e in exchanges)


def refresh_quotes(session: Session, settings: PortfolioSettings) -> int:
    """Write today's LIVE price for anything whose market is still going.

    Writes only dates the exchange has NOT settled, so it can never overwrite a
    close — decisions.md #12 and #13. One batched request for the whole
    portfolio; failure is quiet, because the page falls back to the last close.
    """
    instruments = session.scalars(
        select(Instrument).where(Instrument.yahoo_symbol.is_not(None))
    ).all()
    by_symbol = {i.yahoo_symbol: i for i in instruments}
    if not by_symbol:
        return 0
    try:
        quotes = providers.yahoo_quotes(sorted(by_symbol))
    except Exception as exc:
        log.warning("quote refresh failed: %s: %s", type(exc).__name__, exc)
        return 0

    written = 0
    for symbol, (when, price) in quotes.items():
        inst = by_symbol.get(symbol)
        if inst is None:
            continue
        market = MARKETS.get((inst.exchange or "").strip().upper())
        zone = ZoneInfo(market.zone) if market and market.zone else clock.zone()
        day = when.astimezone(zone).date()
        # The line that keeps history clean: a settled day belongs to the feed.
        if day <= settled_through(inst.exchange):
            continue
        session.execute(
            dialect_insert(session, Price)
            .values(instrument_id=inst.id, date=day, close=price,
                    source="yahoo-live", provisional=True)
            .on_conflict_do_update(
                index_elements=["instrument_id", "date"],
                set_={"close": price, "source": "yahoo-live", "provisional": True},
            )
        )
        written += 1
    session.commit()
    log.info("quote refresh: %s live prices", written)
    return written


def run_feed(session: Session, settings: PortfolioSettings) -> dict[str, int]:
    memory.log_snapshot("before feed run")
    today = clock.today()
    counts = {"prices": 0, "fx": 0, "dividends": 0, "trade_fx_backfilled": 0, "names_filled": 0}
    # Who served what, keyed "kind:symbol". Published on the status page so a
    # fallback is visible: a primary that has been quietly dead for a month is
    # the failure this whole provider layer exists to make noticeable.
    sources: dict[str, str] = {}
    last_run_sources.clear()

    instruments = session.scalars(
        select(Instrument).where(Instrument.yahoo_symbol.is_not(None))
    ).all()

    for inst in instruments:
        # Only SETTLED rows decide where to resume, so a live row is always
        # asked for again and replaced — decisions.md #13.
        settled_dates = session.scalars(
            select(Price.date)
            .where(Price.instrument_id == inst.id, Price.provisional.is_(False))
            .order_by(Price.date)
        ).all()
        first, last, count = (
            (settled_dates[0], settled_dates[-1], len(settled_dates))
            if settled_dates else (None, None, 0)
        )
        start = _fetch_start(first, last, count, settings.price_feed.backfill_start)
        # `_fetch_start` only knows where the series ends, so reach back to a
        # hole in the middle. The upsert makes re-covering old ground free.
        gap = _earliest_gap(settled_dates)
        if gap is not None and gap < start:
            log.info("%s: closes missing from %s — backfilling", inst.ticker, gap)
            start = gap
        # Only what this market has FINISHED; the unfinished day belongs to
        # `refresh_quotes`.
        end = settled_through(inst.exchange)
        if start > end:
            continue
        kind = providers.kind_for(inst.exchange)
        result = providers.fetch(kind, inst.yahoo_symbol, start, end)
        sources[f"{kind}:{inst.ticker}"] = result.summary()
        if not result.ok:  # one bad symbol shouldn't kill the run
            log.warning("price fetch failed for %s (%s): %s",
                        inst.ticker, inst.yahoo_symbol, result.summary())
            continue
        rows, source = result.rows, result.source
        for date, close in rows:
            # The update half is the point: a live price written earlier in the
            # day is replaced here by the close the market actually made.
            session.execute(
                dialect_insert(session, Price)
                .values(instrument_id=inst.id, date=date, close=close, source=source,
                        provisional=False)
                .on_conflict_do_update(
                    index_elements=["instrument_id", "date"],
                    set_={"close": close, "source": source, "provisional": False},
                )
            )
        counts["prices"] += len(rows)
        # Commit per instrument. SQLite allows one writer, and this loop spends
        # most of its time on network calls — holding the write lock across all
        # of them blocked page loads with "database is locked" (busy_timeout is
        # 5s, a full run is far longer).
        session.commit()

    pairs = {
        f"{currency}AUD": f"{currency}AUD=X"
        for currency in session.scalars(
            select(Instrument.currency).where(Instrument.currency != "AUD").distinct()
        ).all()
    }
    for pair, symbol in pairs.items():
        first, last, count = _series_shape(session, FxRate, FxRate.pair == pair)
        start = _fetch_start(first, last, count, settings.price_feed.backfill_start)
        if start > today:
            continue
        # Frankfurter takes the 6-letter pair, Yahoo takes USDAUD=X; the
        # provider layer is handed both and each uses the one it understands.
        result = providers.fetch_fx(pair, symbol, start, today)
        sources[f"fx:{pair}"] = result.summary()
        if not result.ok:
            log.warning("fx fetch failed for %s: %s", pair, result.summary())
            continue
        source = result.source
        for date, rate in result.rows:
            session.execute(
                dialect_insert(session, FxRate)
                .values(pair=pair, date=date, rate=rate, source=source)
                .on_conflict_do_update(
                    index_elements=["pair", "date"], set_={"rate": rate, "source": source}
                )
            )
        counts["fx"] += len(result.rows)
        session.commit()

    counts["names_filled"] = backfill_names(session)
    session.commit()
    for inst in instruments:
        counts["dividends"] += fetch_dividends(session, inst)
        session.commit()

    # Repair: trades whose fx was unknown at import time.
    for trade in session.scalars(
        select(Trade).join(Instrument).where(Trade.fx_rate.is_(None))
    ).all():
        pair = f"{trade.instrument.currency}AUD"
        rate = session.execute(
            select(FxRate.rate)
            .where(FxRate.pair == pair, FxRate.date <= trade.date)
            .order_by(FxRate.date.desc())
            .limit(1)
        ).scalar()
        if rate is not None:
            session.execute(
                update(Trade).where(Trade.id == trade.id).values(fx_rate=rate)
            )
            counts["trade_fx_backfilled"] += 1

    session.commit()
    last_run_sources.update(sources)
    log.info("price feed run: %s", counts)
    fallbacks = {k: v for k, v in sources.items() if "failed" in v or "no rows" in v}
    if fallbacks:
        log.warning("price feed used fallbacks or lost sources: %s", fallbacks)
    # The frames pandas built for this run are dead now. Hand the pages back
    # rather than holding the high-water mark until the next restart — see
    # app/memory.py for why gc alone doesn't do it.
    memory.release("feed run")
    return counts


def main() -> int:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_config(PortfolioSettings)
    factory = make_session_factory(settings.database)
    tenancy.install(factory)
    # Prices and FX are shared, and the trade-FX repair spans every user — this
    # job is the sanctioned exception to per-user scoping.
    with tenancy.unscoped_session(factory) as session:
        counts = run_feed(session, settings)
    print(counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
