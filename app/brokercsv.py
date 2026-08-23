"""Broker CSV import: parse SelfWealth/CommSec exports into candidate trades.

Neither broker offers an API, so the flow is: download the CSV → upload it on
/imports-exports → review the parsed preview
(duplicates and unknown tickers are flagged) → commit. Column layouts live in
config.yaml (`imports.brokers`), so a changed export format is a YAML fix.

Parsing is strict on purpose: an unrecognised header or row fails loudly with
the offending text rather than guessing — first runs against a new broker
export are expected to need a mapping tweak.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from .markettime import to_market
from .models import Instrument, Trade, exchange_problem, ticker_problem
from .pricefeed import MARKETS
from .settings import BrokerFormat, ImportSettings
from .tenancy import owned

# CommSec transactions export: Date,Reference,Details,Debit($),Credit($),Balance($)
# with trade rows like "B 50 ALPHA @ 94.500000" / "S 174 ACME @ 8.550000".
COMMSEC_DETAILS = re.compile(
    r"^\s*(?P<action>[BS])\s+(?P<units>[\d,]+)\s+(?P<ticker>[A-Z0-9]{2,6})\s+@\s+(?P<price>[\d,.]+)\s*$"
)


# Broker exports very commonly pad a date with a zero time —
# "2024-02-13 00:00:00" — so a date-only format has to tolerate one.
# The suffixes are tried in order after the bare format, longest first so
# "%H:%M:%S" is not matched as "%H:%M" with seconds left over.
TIME_SUFFIXES = (" %H:%M:%S", " %H:%M", "T%H:%M:%S", "T%H:%M")


def strptime_any(text: str, date_format: str) -> dt.datetime:
    """`date_format`, or it with a time on the end."""
    try:
        return dt.datetime.strptime(text, date_format)
    except ValueError:
        for suffix in TIME_SUFFIXES:
            try:
                return dt.datetime.strptime(text, date_format + suffix)
            except ValueError:
                continue
        raise


def in_trading_hours(clock: dt.time | None, exchange: str) -> tuple[dt.time | None, bool]:
    """A trade time inside the exchange's session, and whether it had to move.

    Before the open becomes the open, after the close becomes the close. A time
    outside the session is not a trading time: it is a padded date ("00:00:00",
    which is most exports), a settlement stamp, or a clock in another timezone.

    Storing it as given would misorder the day. `time` feeds one thing —
    `models.trade_order`, which sequences same-day trades for FIFO — so a
    midnight stamp sorts an imported trade ahead of every hand-entered one on
    that date, and picks a different parcel for a same-day sale.

    The open comes back as None, which means exactly the same thing to
    `trade_order` and keeps a column of "10:00" out of the interface.

    Markets that never close (crypto) have no session to be outside of, so
    their times are taken as given.
    """
    if clock is None:
        return None, False
    market = MARKETS.get(exchange.upper())
    if market is None or market.open is None or market.close is None:
        return clock, False
    if clock < market.open:
        return None, True
    if clock > market.close:
        return market.close, True
    return (None, False) if clock == market.open else (clock, False)


def parse_when(value: str, date_format: str) -> tuple[dt.date, dt.time | None]:
    """The date, and the time if the cell carried a real one.

    Midnight is no time at all: a broker writing "2024-02-13 00:00:00" is
    padding a date, not claiming a trade at midnight. It is separated from a
    stated out-of-hours time here — see `in_trading_hours` — so that a
    date-only export does not report every one of its rows as adjusted.
    """
    stamp = strptime_any(value.strip(), date_format)  # raises for the caller
    clock = stamp.time()
    return stamp.date(), None if clock == dt.time(0) else clock


@dataclass
class CandidateTrade:
    date: dt.date
    ticker: str
    type: str  # buy / sell
    quantity: Decimal
    unit_price: Decimal
    brokerage: Decimal
    currency: str
    exchange: str
    time: dt.time | None = None  # only when the export carried a real one
    status: str = "new"  # new / duplicate / unknown-instrument
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "time": self.time.isoformat() if self.time else None,
            "ticker": self.ticker,
            "type": self.type,
            "quantity": str(self.quantity),
            "unit_price": str(self.unit_price),
            "brokerage": str(self.brokerage),
            "currency": self.currency,
            "exchange": self.exchange,
            "status": self.status,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, d: dict) -> CandidateTrade:
        return cls(
            date=dt.date.fromisoformat(d["date"]),
            time=dt.time.fromisoformat(d["time"]) if d.get("time") else None,
            ticker=d["ticker"],
            type=d["type"],
            quantity=Decimal(d["quantity"]),
            unit_price=Decimal(d["unit_price"]),
            brokerage=Decimal(d["brokerage"]),
            currency=d["currency"],
            exchange=d["exchange"],
            status=d["status"],
            detail=d["detail"],
        )


@dataclass
class ParseResult:
    candidates: list[CandidateTrade] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # non-trade rows (cash etc.)
    errors: list[str] = field(default_factory=list)
    adjusted: int = 0  # times moved into trading hours; see `in_trading_hours`


def _num(raw: str) -> Decimal:
    return Decimal(str(raw).replace(",", "").replace("$", "").strip())


def parse_csv(text: str, broker: str, fmt: BrokerFormat) -> ParseResult:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return ParseResult(errors=["empty file"])
    headers = [h.strip() for h in reader.fieldnames]
    result = ParseResult()

    if fmt.kind == "mapped":
        missing = [v for v in fmt.columns.values() if v not in headers]
        if missing:
            result.errors.append(
                f"{broker}: expected columns {missing} not in CSV header {headers} — "
                f"fix the mapping under imports.brokers.{broker}.columns in config.yaml"
            )
            return result
        col = fmt.columns
        for i, row in enumerate(reader, start=2):
            try:
                action = trade_type(row[col["action"]], fmt.actions)
                if action is None:
                    result.skipped.append(f"line {i}: action {row[col['action']]!r}")
                    continue
                # A DRP allotment carries units and no price: the registry
                # bought them out of the distribution, and the cost base is in
                # the statement rather than this export. Booking it at zero
                # would understate the cost base of every parcel and overstate
                # the gain on sale — decisions.md #100.
                if action == "drp" and not str(row.get(col["price"], "")).strip():
                    result.skipped.append(
                        f"line {i}: DRP allotment with no price — import the "
                        f"dividend statement, which carries the cost base")
                    continue
                when, clock = parse_when(row[col["date"]], fmt.date_format)
                if clock and fmt.times_zone:
                    clock = to_market(clock, when, fmt.times_zone, fmt.exchange)
                clock, moved = in_trading_hours(clock, fmt.exchange)
                result.adjusted += moved
                result.candidates.append(
                    CandidateTrade(
                        date=when,
                        time=clock,
                        ticker=row[col["ticker"]].strip().upper(),
                        type=action,
                        quantity=_num(row[col["units"]]),
                        unit_price=_num(row[col["price"]]),
                        brokerage=_num(row.get(col.get("brokerage", ""), "0") or "0"),
                        currency=fmt.currency,
                        exchange=fmt.exchange,
                    )
                )
            except (KeyError, ValueError, InvalidOperation) as exc:
                result.errors.append(f"line {i}: {exc!r} in {row}")
        return result

    if fmt.kind == "commsec_transactions":
        if "Details" not in headers or "Debit($)" not in headers:
            result.errors.append(
                f"commsec: expected the transactions export header (Date, Reference, "
                f"Details, Debit($), Credit($), Balance($)); got {headers}"
            )
            return result
        for i, row in enumerate(reader, start=2):
            details = (row.get("Details") or "").strip()
            m = COMMSEC_DETAILS.match(details)
            if not m:
                result.skipped.append(f"line {i}: {details!r}")
                continue
            try:
                qty = _num(m.group("units"))
                price = _num(m.group("price"))
                is_buy = m.group("action") == "B"
                # Brokerage isn't a column: recover it from the cash movement.
                # buy: debit = qty*price + brokerage; sell: credit = qty*price - brokerage.
                gross = qty * price
                when, clock = parse_when(row["Date"], fmt.date_format)
                clock, moved = in_trading_hours(clock, fmt.exchange)
                result.adjusted += moved
                cash = _num(row.get("Debit($)") or "0") if is_buy else _num(row.get("Credit($)") or "0")
                brokerage = (cash - gross) if is_buy else (gross - cash)
                if brokerage < 0 or brokerage > max(Decimal("100"), gross / 10):
                    result.errors.append(
                        f"line {i}: implausible brokerage {brokerage} from {details!r} "
                        f"(cash {cash}, gross {gross}) — check the export"
                    )
                    continue
                result.candidates.append(
                    CandidateTrade(
                        date=when,
                        time=clock,
                        ticker=m.group("ticker"),
                        type="buy" if is_buy else "sell",
                        quantity=qty,
                        unit_price=price,
                        brokerage=brokerage.quantize(Decimal("0.01")),
                        currency=fmt.currency,
                        exchange=fmt.exchange,
                    )
                )
            except (ValueError, InvalidOperation) as exc:
                result.errors.append(f"line {i}: {exc!r} in {row}")
        return result

    result.errors.append(f"unknown broker format kind {fmt.kind!r}")
    return result


BUILTIN_ACTIONS = {"buy": "buy", "b": "buy", "sell": "sell", "s": "sell"}


def trade_type(raw: str, mapped: dict[str, str] | None = None) -> str | None:
    """This broker's word for a trade type, as one of ours, or None to skip.

    The format's own vocabulary wins, matched without case, so a broker calling
    a DRP allotment "In" needs no code. Everything else falls back to the words
    every broker agrees on.
    """
    word = raw.strip()
    for source, target in (mapped or {}).items():
        if source.strip().lower() == word.lower():
            return target if target in ("buy", "sell", "drp") else None
    return BUILTIN_ACTIONS.get(word.lower())


def unresolved(candidates: list[CandidateTrade]) -> list[tuple[str, str, int]]:
    """The distinct (ticker, exchange) pairs that would be created, with counts.

    Grouped rather than per row: a file with ninety trades in six instruments
    asks six questions, not ninety.
    """
    seen: dict[tuple[str, str], int] = {}
    for c in candidates:
        if c.status == "unknown-instrument":
            seen[(c.ticker, c.exchange)] = seen.get((c.ticker, c.exchange), 0) + 1
    return [(t, e, n) for (t, e), n in sorted(seen.items())]


def relabel(candidates: list[CandidateTrade], moves: dict[tuple[str, str], tuple[str, str]]) -> int:
    """Apply corrected tickers and exchanges to every row that carried the old.

    Returns how many rows moved. A correction is per instrument, so fixing
    ALPHA on the wrong exchange fixes every trade of it at once.
    """
    changed = 0
    for c in candidates:
        new = moves.get((c.ticker, c.exchange))
        if new and new != (c.ticker, c.exchange):
            c.ticker, c.exchange = new
            changed += 1
    return changed


def annotate(session: Session, candidates: list[CandidateTrade], imports: ImportSettings) -> None:
    """Mark each candidate as new / duplicate / unknown-instrument.

    Re-entrant: status and detail are reset first, because the resolve step
    calls this again after a correction and a stale "unknown-instrument" would
    keep refusing a ticker that now resolves.
    """
    for c in candidates:
        c.status, c.detail = "new", ""
        inst = session.scalars(
            select(Instrument).where(Instrument.ticker == c.ticker, Instrument.exchange == c.exchange)
        ).first()
        if inst is None:
            c.status = "unknown-instrument"
            c.detail = (
                "will be created"
                if imports.allow_new_instruments
                else "not in the instrument list; imports.allow_new_instruments is off"
            )
            continue
        dupe = session.scalars(
            select(Trade).where(
                Trade.instrument_id == inst.id,
                Trade.date == c.date,
                Trade.type == c.type,
                Trade.quantity == c.quantity,
                Trade.unit_price == c.unit_price,
            )
        ).first()
        if dupe is not None:
            c.status = "duplicate"
            c.detail = f"matches trade #{dupe.id}"


def commit(session: Session, candidates: list[CandidateTrade], imports: ImportSettings, source: str) -> dict[str, int]:
    counts = {"inserted": 0, "duplicates_skipped": 0, "instruments_created": 0}
    for c in candidates:
        if c.status == "duplicate":
            counts["duplicates_skipped"] += 1
            continue
        inst = session.scalars(
            select(Instrument).where(Instrument.ticker == c.ticker, Instrument.exchange == c.exchange)
        ).first()
        if inst is None:
            if not imports.allow_new_instruments:
                raise ValueError(f"unknown instrument {c.ticker} ({c.exchange})")
            # A CSV is arbitrary input; a malformed ticker must not become a row.
            problem = ticker_problem(c.ticker) or exchange_problem(c.exchange)
            if problem:
                raise ValueError(f"{problem} (got {c.ticker!r} on {c.exchange!r})")
            inst = Instrument(
                ticker=c.ticker,
                exchange=c.exchange,
                currency=c.currency,
                asset_class="share",
                drp=False,
                yahoo_symbol=f"{c.ticker}.AX" if c.exchange == "ASX" else c.ticker,
                note=f"created by {source} import",
            )
            session.add(inst)
            session.flush()
            counts["instruments_created"] += 1
        session.add(
            owned(
                session,
                Trade(
                    instrument=inst,
                    date=c.date,
                    # Only when the export carried one; unset means MARKET_OPEN.
                    **({"time": c.time} if c.time else {}),
                    type=c.type,
                    quantity=c.quantity,
                    unit_price=c.unit_price,
                    brokerage=c.brokerage,
                    fx_rate=Decimal(1) if c.currency == "AUD" else None,  # feed backfills
                    note=f"imported from {source} CSV",
                ),
            )
        )
        counts["inserted"] += 1
    session.flush()  # the caller's scoped session owns the commit
    return counts
