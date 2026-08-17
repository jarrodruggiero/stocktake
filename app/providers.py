"""Where market data comes from, and what happens when a source is down.

Yahoo is unofficial, breaks periodically, and is a single point of failure for
prices, FX *and* crypto. This makes the source a list rather than an assumption:
each kind has an ordered set, the first success wins, and the `source` column
records who actually served each row.

**Fallback happens on error or empty, never on "fewer rows than I hoped".** A
provider returning three days when five were asked for is normal — markets
close. Treating that as failure would flap between sources and rewrite the same
rows with a different `source` every run.

| Kind | Primary | Fallback | Why |
| --- | --- | --- | --- |
| FX | Yahoo | **Frankfurter** | Keyless, documented, AUD covered, no quotas, central-bank sourced. |
| Crypto | Yahoo | **CoinGecko** | Keyless, documented, prices natively in AUD. |
| Equities | Yahoo | **none** | See below. |
| Dividends | Yahoo | none | No free source publishes distribution history. |

**There is no equity fallback, deliberately.** Stooq's CSV endpoint now answers
with a JavaScript proof-of-work challenge and publishes no API, so using it
would mean defeating an anti-bot measure. Alpha Vantage does not document ASX
coverage. Twelve Data lists ASX only on a paid add-on. A provider whose coverage
of the actual holdings cannot be verified is worse than none: it fails silently
while `source` claims the numbers came from somewhere real.

The seam works — `PROVIDERS["equity"]` is a list, and adding a keyed provider is
one function plus a config entry. **Symbols only leave**: no provider is ever
told a quantity, a holding or who is asking.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

log = logging.getLogger(__name__)

# Every outbound call is bounded. A hung provider must not hold the feed open:
# the run holds no write transaction while fetching, but it does block the
# thread the scheduler gave it, and a stuck socket would stall until the next
# restart rather than the next day.
TIMEOUT_SECONDS = 20

# Sent so a provider can see who is calling and rate-limit or contact us rather
# than silently blocking. Politeness, and it makes us a good citizen of two
# free services.
USER_AGENT = "portfolio-tracker/1.0 (self-hosted; +https://github.com/)"

Rows = list[tuple[dt.date, Decimal]]


@dataclass
class Attempt:
    """What one provider did, for the status page and the log."""

    provider: str
    ok: bool
    rows: int = 0
    error: str | None = None

    def __str__(self) -> str:
        if self.ok:
            return f"{self.provider} ({self.rows} rows)"
        return f"{self.provider} failed: {self.error}"


@dataclass
class Fetch:
    """The result of asking every provider in turn until one answered."""

    rows: Rows = field(default_factory=list)
    source: str | None = None  # who served it; None = nobody could
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.source is not None

    def summary(self) -> str:
        """"frankfurter (yahoo failed: timed out)" — what the page shows.

        Named sources rather than a bare success flag: a silent fallback is how
        you end up not noticing that the primary has been dead for a month.
        """
        if not self.attempts:
            return "no providers configured"
        served = [a for a in self.attempts if a.ok]
        failed = [a for a in self.attempts if not a.ok]
        if not served:
            return "; ".join(str(a) for a in failed) or "no data"
        head = served[-1].provider
        return f"{head} ({'; '.join(str(a) for a in failed)})" if failed else head


def _get_json(url: str, params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}", headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode())


def _decimal(value, places: int = 8) -> Decimal | None:
    try:
        out = Decimal(str(round(float(value), places)))
    except (TypeError, ValueError, InvalidOperation):
        return None
    return out if out > 0 else None


# --------------------------------------------------------------------------- #
# Yahoo — primary for everything, via the existing yfinance path
# --------------------------------------------------------------------------- #

def yahoo_closes(symbol: str, start: dt.date, end: dt.date) -> Rows:
    """Delegates to `pricefeed._closes` rather than importing yfinance here.

    Imported inside the function on purpose: `pricefeed` imports this module,
    and yfinance stays lazy (see app/memory.py). It also means the tests that
    stub `pricefeed._closes` keep working through this layer unchanged.
    """
    from . import pricefeed

    return pricefeed._closes(symbol, start, end)


# --------------------------------------------------------------------------- #
# Frankfurter — FX fallback (ECB and 83 other central banks, no key)
# --------------------------------------------------------------------------- #

FRANKFURTER_URL = "https://api.frankfurter.dev/v1"


def frankfurter_fx(pair: str, start: dt.date, end: dt.date) -> Rows:
    """Daily rates for a 6-letter pair like USDAUD, as AUD per 1 USD.

    Frankfurter quotes `base` → `symbols`, which is the same direction the app
    stores (`fx_rate` is AUD per 1 unit of the native currency), so no
    inversion is needed. Weekends and holidays are simply absent — the caller's
    stepper already handles gaps, and inventing rows for closed days would be
    inventing data.
    """
    base, quote = pair[:3], pair[3:]
    payload = _get_json(
        f"{FRANKFURTER_URL}/{start.isoformat()}..{end.isoformat()}",
        {"base": base, "symbols": quote},
    )
    out: Rows = []
    for day, rates in sorted((payload.get("rates") or {}).items()):
        value = _decimal((rates or {}).get(quote), 6)
        if value is not None:
            out.append((dt.date.fromisoformat(day), value))
    return out


# --------------------------------------------------------------------------- #
# CoinGecko — crypto fallback (no key, prices natively in AUD)
# --------------------------------------------------------------------------- #

COINGECKO_URL = "https://api.coingecko.com/api/v3"

# CoinGecko keys on its own slugs, not tickers. A short explicit map beats
# calling /coins/list (a ~2 MB response) on every run, and beats guessing:
# "eth" is Ethereum here but a dozen other things elsewhere. Anything missing
# simply has no fallback, which is reported rather than approximated.
COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "ADA": "cardano",
    "DOT": "polkadot",
    "XRP": "ripple",
    "LTC": "litecoin",
    "DOGE": "dogecoin",
    "MATIC": "matic-network",
    "LINK": "chainlink",
}


def coingecko_closes(symbol: str, start: dt.date, end: dt.date) -> Rows:
    """Daily AUD closes for a crypto ticker.

    `symbol` arrives in the app's Yahoo form (`BTC-AUD`); the ticker is the part
    before the dash. The endpoint takes a lookback in days rather than a date
    range, so we ask for the span we need and filter — the free tier caps how
    far back it will go, and a short answer is a normal answer here.
    """
    ticker = symbol.split("-")[0].upper()
    coin = COINGECKO_IDS.get(ticker)
    if coin is None:
        raise LookupError(f"no CoinGecko id known for {ticker}")
    days = max(1, (end - start).days + 1)
    payload = _get_json(
        f"{COINGECKO_URL}/coins/{coin}/market_chart",
        {"vs_currency": "aud", "days": days, "interval": "daily"},
    )
    # [[epoch_ms, price], ...]. Several points can share a date (the last is
    # "now"); the last one wins, which is the closest thing to a close.
    by_date: dict[dt.date, Decimal] = {}
    for stamp, price in payload.get("prices") or []:
        value = _decimal(price, 6)
        if value is None:
            continue
        day = dt.datetime.fromtimestamp(stamp / 1000, dt.timezone.utc).date()
        if start <= day <= end:
            by_date[day] = value
    return sorted(by_date.items())


# --------------------------------------------------------------------------- #
# Live quotes — a different question from a daily close
# --------------------------------------------------------------------------- #

YAHOO_SPARK_URL = "https://query1.finance.yahoo.com/v8/finance/spark"


def yahoo_quotes(symbols: list[str]) -> dict[str, tuple[dt.datetime, Decimal]]:
    """The latest traded price for many symbols, in ONE request.

    `{symbol: (when, price)}` with `when` an aware UTC datetime — the caller
    turns that into a date in the market's own zone, because which day a quote
    belongs to is a question about the exchange.

    Batched, plain JSON, and no fallback chain: one call covers the portfolio,
    nothing here needs pandas, and a quote that fails costs nothing because the
    page shows the last close. Why that differs from the close feed:
    decisions.md #15.

    `v7/finance/quote` is NOT used — it returns 401 without a cookie/crumb,
    which is the machinery yfinance exists to carry. `spark` needs none.
    """
    if not symbols:
        return {}
    payload = _get_json(
        YAHOO_SPARK_URL,
        {"symbols": ",".join(symbols), "range": "1d", "interval": "1d"},
    )
    out: dict[str, tuple[dt.datetime, Decimal]] = {}
    for symbol, block in (payload or {}).items():
        if not isinstance(block, dict):
            continue
        stamps, closes = block.get("timestamp") or [], block.get("close") or []
        if not stamps or not closes:
            continue
        price = _decimal(closes[-1], 6)
        if price is None:
            continue
        out[symbol] = (
            dt.datetime.fromtimestamp(int(stamps[-1]), dt.timezone.utc),
            price,
        )
    return out


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #

# Ordered: first success wins. Equities are Yahoo-only — see the module
# docstring for what was researched and why nothing else made the cut.
PROVIDERS: dict[str, list[tuple[str, object]]] = {
    "equity": [("yahoo", yahoo_closes)],
    "fx": [("yahoo", yahoo_closes), ("frankfurter", frankfurter_fx)],
    "crypto": [("yahoo", yahoo_closes), ("coingecko", coingecko_closes)],
}


def _try_each(kind: str, symbol_for, start: dt.date, end: dt.date) -> Fetch:
    """Walk the provider list for `kind`, stopping at the first real answer.

    `symbol_for(name)` because providers disagree about what a symbol looks
    like — Yahoo wants `USDAUD=X` where Frankfurter wants `USDAUD`.

    Never raises. A feed run touching a dozen instruments must not be ended by
    one dead source, so every failure becomes an `Attempt` and the caller reads
    `Fetch.ok` to decide what to say about it.
    """
    result = Fetch()
    for name, call in PROVIDERS.get(kind, []):
        try:
            rows = call(symbol_for(name), start, end)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            result.attempts.append(Attempt(name, ok=False, error=f"unreachable: {exc}"))
            continue
        except Exception as exc:  # a parse error, a missing coin id, a 500
            result.attempts.append(
                Attempt(name, ok=False, error=f"{type(exc).__name__}: {exc}")
            )
            continue
        if not rows:
            # Empty is not an error, but it is also not an answer: try the next
            # source before concluding the market simply didn't trade.
            result.attempts.append(Attempt(name, ok=False, error="no rows"))
            continue
        result.attempts.append(Attempt(name, ok=True, rows=len(rows)))
        result.rows, result.source = rows, name
        return result
    return result


def fetch(kind: str, symbol: str, start: dt.date, end: dt.date) -> Fetch:
    """Prices for one instrument, from whichever provider answers first."""
    return _try_each(kind, lambda _name: symbol, start, end)


def fetch_fx(pair: str, yahoo_symbol: str, start: dt.date, end: dt.date) -> Fetch:
    """Rates for one pair. Yahoo is handed `USDAUD=X`, everyone else `USDAUD`."""
    return _try_each(
        "fx", lambda name: yahoo_symbol if name == "yahoo" else pair, start, end
    )


def kind_for(exchange: str, is_fx: bool = False) -> str:
    """Which provider list serves this instrument."""
    if is_fx:
        return "fx"
    return "crypto" if (exchange or "").strip().upper() == "CRYPTO" else "equity"
