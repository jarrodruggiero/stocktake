"""Broker CSV import: parse SelfWealth/CommSec exports into candidate trades.

Neither broker has an API (checked 2026-07-16), so the flow is: download the
CSV from the broker → upload it on /imports-exports → review the parsed preview
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

from .models import Instrument, Trade, exchange_problem, ticker_problem
from .settings import BrokerFormat, ImportSettings
from .tenancy import owned

# CommSec transactions export: Date,Reference,Details,Debit($),Credit($),Balance($)
# with trade rows like "B 50 ALPHA @ 94.500000" / "S 174 ACME @ 8.550000".
COMMSEC_DETAILS = re.compile(
    r"^\s*(?P<action>[BS])\s+(?P<units>[\d,]+)\s+(?P<ticker>[A-Z0-9]{2,6})\s+@\s+(?P<price>[\d,.]+)\s*$"
)


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
    status: str = "new"  # new / duplicate / unknown-instrument
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
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
                action = row[col["action"]].strip().lower()
                if action not in ("buy", "sell", "b", "s"):
                    result.skipped.append(f"line {i}: action {row[col['action']]!r}")
                    continue
                result.candidates.append(
                    CandidateTrade(
                        date=dt.datetime.strptime(row[col["date"]].strip(), fmt.date_format).date(),
                        ticker=row[col["ticker"]].strip().upper(),
                        type="buy" if action.startswith("b") else "sell",
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
                        date=dt.datetime.strptime(row["Date"].strip(), fmt.date_format).date(),
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


def annotate(session: Session, candidates: list[CandidateTrade], imports: ImportSettings) -> None:
    """Mark each candidate as new / duplicate / unknown-instrument."""
    for c in candidates:
        inst = session.scalars(
            select(Instrument).where(Instrument.ticker == c.ticker, Instrument.exchange == c.exchange)
        ).first()
        if inst is None:
            c.status = "unknown-instrument"
            c.detail = (
                "will be created on commit" if imports.allow_new_instruments
                else "not in the instrument list; set imports.allow_new_instruments or add it first"
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
