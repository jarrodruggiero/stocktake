"""OFX import: one reader for every broker that offers the format.

The closest thing to a standard in this space. A broker that exports OFX needs
no template and no column mapping, which makes this the highest-leverage
importer here and the one most likely to work for somebody in a country nobody
has written a broker format for.

**OFX 1.x is not XML.** It is SGML with unclosed tags, so an XML parser rejects
it outright and every OFX library does its own tokenising anyway. 2.x *is* XML;
the reader below handles both by treating the whole thing as tag soup, which is
what it is.

**Parsing untrusted XML is a security decision, and this reads an upload.**
Handing that to a general XML parser opens external entity expansion (reading
`/etc/passwd` into the document) and entity-expansion denial of service — both
live in Python's stdlib parsers unless deliberately disabled. A reader that
understands only tags and text has no entity machinery to abuse. **Please do not
"improve" this by swapping in ElementTree.**

`SECLIST` maps a security id to its ticker; `INVTRANLIST` holds the
transactions. Buys and sells become candidate trades; everything else —
dividends, interest, transfers, fees — is reported as skipped rather than
silently dropped, because a statement whose cash movements vanish looks like a
successful import until the numbers stop matching.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation

from .brokercsv import CandidateTrade, ParseResult

# One tag and whatever text follows it before the next tag. Deliberately not a
# parser: it does not care about nesting, which is what makes it immune to the
# malformed-but-common shapes real brokers emit.
_TAG = re.compile(r"<([A-Za-z0-9._]+)>([^<]*)")

# Transaction wrappers that mean units changed hands. OFX distinguishes stocks,
# mutual funds, options and "other"; for this app they are all the same event.
BUY_TAGS = ("BUYSTOCK", "BUYMF", "BUYOTHER", "BUYOPT", "BUYDEBT")
SELL_TAGS = ("SELLSTOCK", "SELLMF", "SELLOTHER", "SELLOPT", "SELLDEBT")
# A reinvested distribution is units in, but it is not a purchase with money —
# it maps to the app's `drp` type and is handled separately from a buy.
REINVEST_TAGS = ("REINVEST",)
# Everything else that can appear in a transaction list, named so the skip
# message can say what it was rather than "unrecognised".
OTHER_TAGS = {
    "INCOME": "income (dividend or interest)",
    "INVEXPENSE": "an expense",
    "MARGININTEREST": "margin interest",
    "TRANSFER": "a transfer",
    "INVBANKTRAN": "a cash movement",
    "RETOFCAP": "a return of capital",
    "SPLIT": "a share split",
    "CLOSUREOPT": "an option closure",
    "JRNLSEC": "a journal entry",
    "JRNLFUND": "a journal entry",
}


def _pairs(text: str) -> list[tuple[str, str]]:
    return [(m.group(1).upper(), m.group(2).strip()) for m in _TAG.finditer(text)]


def _blocks(text: str, tag: str) -> list[str]:
    """Every `<TAG> … </TAG>` region, tolerating the unclosed 1.x style.

    When a closing tag is absent the block runs to the next opening of the same
    tag, or to the end — which is how SGML OFX is meant to be read.
    """
    out = []
    opens = [m.start() for m in re.finditer(rf"<{tag}>", text, re.I)]
    for i, start in enumerate(opens):
        close = text.lower().find(f"</{tag.lower()}>", start)
        nxt = opens[i + 1] if i + 1 < len(opens) else len(text)
        end = close if 0 <= close < nxt else nxt
        out.append(text[start:end])
    return out


def _value(block: str, tag: str) -> str | None:
    for name, value in _pairs(block):
        if name == tag:
            return value or None
    return None


def _date(raw: str | None) -> dt.date | None:
    """OFX timestamps are YYYYMMDD with optional time and `[-5:EST]` zone.

    The zone is discarded on purpose. A trade date is the date the broker says
    it is; re-interpreting it against a timezone would move trades across
    financial-year boundaries for no gain.
    """
    if not raw or len(raw) < 8 or not raw[:8].isdigit():
        return None
    try:
        return dt.datetime.strptime(raw[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _decimal(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(raw.replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def security_tickers(text: str) -> dict[str, str]:
    """UNIQUEID → ticker, from the security list.

    Transactions reference a security by id (usually a CUSIP or ISIN), never by
    ticker, so without this every row is unidentifiable.
    """
    tickers: dict[str, str] = {}
    for block in _blocks(text, "SECINFO"):
        unique_id = _value(block, "UNIQUEID")
        ticker = _value(block, "TICKER")
        if unique_id and ticker:
            # ASX tickers arrive from some brokers as "ALPHA.AX"; the app stores
            # the bare code and keeps the exchange separately.
            tickers[unique_id] = ticker.split(".")[0].upper()
    return tickers


def parse_ofx(text: str, *, exchange: str = "ASX", currency: str = "AUD") -> ParseResult:
    """Every buy and sell in an OFX file, plus an account of what was skipped."""
    result = ParseResult()
    if "<OFX" not in text.upper():
        result.errors.append(
            "that does not look like an OFX file — expected a document opening "
            "with <OFX>. Export 'OFX' or 'Money/Quicken' format from your broker."
        )
        return result

    tickers = security_tickers(text)
    default_currency = _value(text, "CURDEF") or currency
    found_any = False

    for tag in BUY_TAGS + SELL_TAGS + REINVEST_TAGS:
        for block in _blocks(text, tag):
            found_any = True
            candidate, problem = _transaction(
                block, tag, tickers, exchange, default_currency
            )
            if candidate is not None:
                result.candidates.append(candidate)
            else:
                result.skipped.append(problem)

    for tag, description in OTHER_TAGS.items():
        for block in _blocks(text, tag):
            found_any = True
            # Named, not silently dropped: an import whose cash movements
            # vanish looks successful until the numbers stop matching.
            when = _date(_value(block, "DTTRADE") or _value(block, "DTPOSTED"))
            result.skipped.append(
                f"{when or 'undated'}: {description} — record it separately"
            )

    if not found_any:
        result.errors.append(
            "no investment transactions in that file. A cash-only OFX export "
            "(bank transactions) has nothing this app can import."
        )
    result.candidates.sort(key=lambda c: (c.date, c.ticker))
    return result


def _transaction(block, tag, tickers, exchange, currency):
    """One transaction block as a candidate trade, or a reason it was skipped."""
    unique_id = _value(block, "UNIQUEID")
    ticker = tickers.get(unique_id or "")
    when = _date(_value(block, "DTTRADE") or _value(block, "DTSETTLE"))
    units = _decimal(_value(block, "UNITS"))
    price = _decimal(_value(block, "UNITPRICE"))
    commission = _decimal(_value(block, "COMMISSION")) or Decimal(0)

    if ticker is None:
        return None, (
            f"a {tag.lower()} for security {unique_id or 'unknown'} — it is not "
            "in the file's security list, so there is no ticker to import it as"
        )
    if when is None or units is None or price is None:
        missing = [n for n, v in (("date", when), ("units", units), ("price", price))
                   if v is None]
        return None, f"{ticker}: a {tag.lower()} missing its {', '.join(missing)}"

    if tag in REINVEST_TAGS:
        kind = "drp"
    elif tag in SELL_TAGS:
        kind = "sell"
    else:
        kind = "buy"

    # OFX signs UNITS by direction: negative on a sell. The app stores quantity
    # as a positive magnitude with the direction in `type`, so the sign is
    # dropped here rather than becoming a negative holding.
    return CandidateTrade(
        date=when,
        ticker=ticker,
        type=kind,
        quantity=abs(units),
        unit_price=abs(price),
        brokerage=abs(commission),
        currency=(_value(block, "CURSYM") or currency).upper(),
        exchange=exchange,
        detail=(_value(block, "MEMO") or "").strip(),
    ), ""
