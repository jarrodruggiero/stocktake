"""Mapping a broker's CSV columns onto trades, without writing YAML by hand.

The fields are already delimited, so there is no pattern to infer — only which
column is which. `guess()` reads the header names; `guess_date_format()` reads
the VALUES, because day-first and month-first cannot be told from a header and
the wrong one is silently right for eleven rows in twelve (decisions.md #86).

What comes out is exactly the YAML the shipped formats are written in, so it can
be installed, reused, or sent as a pull request unchanged.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from .brokercsv import strptime_any
from .settings import BrokerFormat

# Enough to see whether the mapping is right, few enough that a decade of
# trading is not read into memory to render a preview table.
# Enough rows to see a pattern rather than a coincidence — a column of five
# identical values says nothing about the sixth, and the sheet below is
# meant to be read like a spreadsheet.
SAMPLE_ROWS = 10

# Every field a `kind: mapped` format understands. `brokerage` is the only
# optional one — plenty of exports have no fee column, and a missing fee is zero
# rather than a reason to refuse the file.
FIELDS = ("date", "action", "ticker", "units", "price", "brokerage")
REQUIRED = ("date", "action", "ticker", "units", "price")

# Header vocabulary, most specific first: the first alias matching an unused
# column wins, so unambiguous names go before loose ones. Case- and
# space-insensitive.
ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("trade date", "transaction date", "settlement date", "date"),
    "action": ("buy/sell", "buy or sell", "direction", "side", "action",
               "transaction type", "type"),
    "ticker": ("asx code", "security code", "ticker", "symbol", "code",
               "instrument", "security"),
    "units": ("units", "quantity", "qty", "shares", "volume"),
    "price": ("unit price", "price per unit", "avg price", "average price",
              "trade price", "price"),
    "brokerage": ("brokerage", "commission", "fee", "fees", "charges"),
}

# Columns that must never be guessed as a unit price. A "Value" or
# "Consideration" column is quantity TIMES price, and reading it as the price
# overstates every holding by the number of units — the most damaging mis-guess
# available here, and an easy one because the words look adjacent.
NEVER_PRICE = ("value", "consideration", "total", "amount", "net", "gross",
               "proceeds", "cost")

# Tried in order against the sample values. Day-first before month-first because
# the app is AU-first; `dates_are_ambiguous()` exists so the interface can say
# when that choice was a coin toss rather than a reading.
DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y",
    "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%Y/%m/%d",
    "%d/%m/%y", "%m/%d/%y",
)


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA. Spreadsheet convention, because that is what
    the sheet is imitating and what a person will say out loud."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


@dataclass
class ReadCsv:
    headers: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    problem: str = ""


def _norm(value: str) -> str:
    return " ".join(str(value).split()).strip().lower()


def read_csv(text: str) -> ReadCsv:
    """The header row and a few sample rows.

    Never raises: everything that can go wrong here is a property of the file
    somebody just uploaded, and each one deserves a sentence rather than a
    traceback.
    """
    # utf-8-sig at the decode step handles most of it, but a BOM also arrives
    # inside an already-decoded string, and it makes the first column's name
    # invisibly different from what it looks like on screen.
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return ReadCsv(problem="This file has no header row — is it a CSV?")
    headers = [h.strip() for h in reader.fieldnames if h is not None]

    rows = []
    for row in reader:
        rows.append({(k.strip() if k else k): v for k, v in row.items()})
        if len(rows) >= SAMPLE_ROWS:
            break
    if not rows:
        return ReadCsv(headers=headers,
                       problem="This file has a header row and no data rows, so "
                               "there is nothing to check a mapping against.")
    return ReadCsv(headers=headers, rows=rows)


def guess(headers: list[str]) -> dict[str, str | None]:
    """Which column is probably which field.

    A column is claimed by at most one field: `Price` matching both `price` and
    `brokerage` would silently make every trade's fee equal to its unit price.

    A field with no plausible column stays `None`. An empty dropdown is a much
    better answer than a confident wrong one — a mis-guessed price column
    produces trades that look entirely right and are not.
    """
    normalised = {h: _norm(h) for h in headers}
    out: dict[str, str | None] = {}
    taken: set[str] = set()

    def find(name: str, exact: bool) -> str | None:
        for alias in ALIASES[name]:
            for header, norm in normalised.items():
                if header in taken:
                    continue
                if norm != alias if exact else alias not in norm:
                    continue
                if name == "price" and any(word in norm for word in NEVER_PRICE):
                    continue
                return header
        return None

    # Two passes, exact before loose. Real exports carry things like
    # "Unit Price (AUD)" and "Trade Date (local)", so a purely exact match
    # misses the common case — but a purely loose one lets "Total Value" claim
    # `price`, which is the worst mis-guess available here. Exact first means
    # the loose pass only ever sees columns nothing precise wanted.
    for exact in (True, False):
        for name in FIELDS:
            if out.get(name):
                continue
            chosen = find(name, exact)
            if chosen:
                taken.add(chosen)
            out[name] = chosen
    return out


def _parse_all(values: list[str], fmt: str) -> bool:
    """Whether every sample reads under this format.

    Tolerant of a trailing time, exactly as the importer is: guessing a format
    the importer would then reject is worse than not guessing. Most exports
    write "2024-02-13 00:00:00", and every date-only candidate failed on the
    time — so the guess fell through to the default and quietly claimed
    day-first for an ISO file.
    """
    for value in values:
        try:
            strptime_any(str(value).strip(), fmt)
        except (ValueError, TypeError):
            return False
    return True


def guess_date_format(values: list[str]) -> str | None:
    """The format every sample value parses under, or None.

    Order matters and is deliberate: the first format that parses every sample
    wins, and day-first comes before month-first because this app is AU-first.
    Where the samples cannot tell them apart, `dates_are_ambiguous()` reports
    it so the interface can say so rather than quietly choosing.
    """
    usable = [v for v in values if str(v).strip()]
    if not usable:
        return None
    for fmt in DATE_FORMATS:
        if _parse_all(usable, fmt):
            return fmt
    return None


def dates_are_ambiguous(values: list[str]) -> bool:
    """Whether the samples parse as both day-first and month-first.

    `01/02/2026` is the 1st of February and the 2nd of January, and nothing in
    the file says which. Eleven rows in twelve still import under the wrong
    reading, which is why this is worth saying out loud on the form.
    """
    usable = [v for v in values if str(v).strip()]
    if not usable:
        return False
    return _parse_all(usable, "%d/%m/%Y") and _parse_all(usable, "%m/%d/%Y")


def drp_skipped(skipped: list[str]) -> int:
    """How many rows were left out for having no price.

    The reason is per row in `skipped`, which is where it belongs when you are
    reading one; above the table it wants saying once.
    """
    return sum(1 for s in skipped if "no price" in s)


def action_words(rows: list[dict], header: str | None) -> list[str]:
    """The distinct words this file uses in its action column, commonest first.

    What the page offers to map. Sorted by frequency because the two that
    matter are almost always the two most common.
    """
    if not header:
        return []
    counts: dict[str, int] = {}
    for row in rows:
        word = str(row.get(header, "") or "").strip()
        if word:
            counts[word] = counts.get(word, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def broker_yaml(*, name: str, exchange: str, currency: str,
                date_format: str | None, columns: dict[str, str | None],
                actions: dict[str, str] | None = None) -> str:
    """A broker format file, as text, ready to install or attach to a pull
    request.

    Hand-built rather than dumped by the YAML library, for the same reason the
    statement designer does it: the output carries the comments a contributor
    needs, and a file that arrives explaining itself gets reviewed properly.
    """
    lines = [
        "# Generated by the broker CSV column mapper.",
        "# Check the column names below match YOUR export exactly, then see",
        "# docs/contributing/recipe-broker.md to contribute it so the next",
        "# person with this broker does not have to map it again.",
        f"name: {name}",
        "kind: mapped",
        f"exchange: {exchange}",
        f"currency: {currency}",
        # Quoted: an unquoted %d/%m/%Y is legal YAML but reads as a plain
        # scalar that some editors and linters mangle, and the percent signs
        # are the whole content.
        f'date_format: "{date_format or "%d/%m/%Y"}"',
        "columns:",
    ]
    for name_ in FIELDS:
        header = columns.get(name_)
        if header:
            lines.append(f"  {name_}: {header}")
    if actions:
        lines += ["actions:   # this broker's word -> what the app calls it"]
        lines += [f"  {word}: {kind}" for word, kind in sorted(actions.items())]
    return "\n".join(lines) + "\n"


def preview(text: str, *, exchange: str, currency: str, date_format: str,
            columns: dict[str, str | None]):
    """Run the proposed mapping over the uploaded file.

    The point of doing this here rather than at import time: a wrong date format
    or a missing column becomes a message on the form somebody is still filling
    in, instead of a failed import later against a file they have put away.
    """
    from . import brokercsv

    chosen = {k: v for k, v in columns.items() if v}
    missing = [f for f in REQUIRED if f not in chosen]
    if missing:
        result = brokercsv.ParseResult()
        result.errors.append(
            "Choose a column for: " + ", ".join(missing))
        return result

    fmt = BrokerFormat(kind="mapped", exchange=exchange, currency=currency,
                       date_format=date_format, columns=chosen)
    return brokercsv.parse_csv(text, "preview", fmt)
