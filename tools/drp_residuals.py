#!/usr/bin/env python3
"""Report the DRP residual balance per holding, and flag rows that cannot be right.

A registry allots **whole units only**. The leftover cents stay with it and are
credited to the next distribution, so over years of quarterly distributions
there is a small, permanent, unaccounted balance. This reads it out of data the
app already holds.

    residual_after(n) = residual_after(n-1) + cash_amount(n) - units(n) x price(n)

which telescopes to **sum(cash) - sum(units x price)** per instrument.

**Read this before trusting the number.** The derivation is only correct if
`cash_amount` recorded the FULL distribution. If it recorded the amount that
was actually applied to units — which is what a spreadsheet built from
allotment advices tends to hold — the derivation returns approximately zero and
looks perfectly healthy while being empty. Two invariants tell one case from
the other, and they are the point of this script:

  * `0 <= residual < unit_price` at every step. A residual at least as large as
    one unit's price means an allotment was missed or units were under-recorded
    — the registry would have bought another unit with that money.
  * **A negative running residual means `cash_amount` was under-recorded.** You
    cannot apply more money to units than the distribution paid. Those rows
    need the statement; nothing else does.

So the output is a triage list, not a repair: it sorts holdings into "derivable
from what we have" and "needs the registry's own statements", and the second
list is the one to go and find documents for.

Read-only. It opens the database, prints, and changes nothing.

    python tools/drp_residuals.py                     # the configured database
    python tools/drp_residuals.py --db /data/copy.db  # a backup, off to one side
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

ZERO = Decimal("0")
CENT = Decimal("0.01")


def _rows(conn: sqlite3.Connection) -> list[tuple]:
    """Every dividend with its reinvestment, oldest first.

    `residual_carried` arrived in migration 0018, and the obvious thing to
    point this at is a backup taken before the repair that added it — so the
    column is selected only when it exists rather than making the tool refuse
    the database it is most useful on.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(dividend)")}
    stored = "d.residual_carried" if "residual_carried" in columns else "NULL"
    return conn.execute(f"""
        SELECT i.ticker, d.date, d.cash_amount, {stored},
               t.quantity, t.unit_price
        FROM dividend d
        JOIN instrument i ON i.id = d.instrument_id
        LEFT JOIN trade t ON t.id = d.reinvest_trade_id
        ORDER BY i.ticker, d.date, d.id
    """).fetchall()


def _dec(value) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="path to a SQLite database file")
    args = parser.parse_args()

    path = args.db
    if not path:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app.settings import Settings  # noqa: PLC0415 - optional import

        settings = Settings()
        if settings.database.type != "sqlite" or not settings.database.path:
            print("This reads a SQLite file. Pass --db for anything else.")
            return 2
        path = settings.database.path

    if not Path(path).exists():
        print(f"no such database: {path}")
        return 2

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    by_ticker: dict[str, list] = defaultdict(list)
    for ticker, date, cash, stored, units, price in _rows(conn):
        by_ticker[ticker].append((str(date)[:10], _dec(cash), stored,
                                  _dec(units), _dec(price)))

    print(f"DRP residuals from {path}\n")
    print(f"{'holding':8} {'distributions':>14} {'derived residual':>18} "
          f"{'recorded':>12}  verdict")

    needs_statements: list[str] = []
    disagreements: list[str] = []
    for ticker, entries in sorted(by_ticker.items()):
        drp = [e for e in entries if e[3] != ZERO]
        if not drp:
            continue                      # cash-only holding: no residual exists
        running = ZERO
        breaches: list[str] = []
        exact = 0
        for date, cash, _stored, units, price in entries:
            if units != ZERO and abs(cash - units * price) < CENT:
                exact += 1
            running += cash - units * price
            if running < -CENT:
                breaches.append(f"{date}: running residual {running} is negative "
                                f"— cash_amount is less than the money applied")
            elif price and running >= price:
                breaches.append(f"{date}: residual {running} exceeds the unit "
                                f"price {price} — an allotment looks missing")

        # The quiet failure, and the one the invariants above cannot see. A
        # registry allotting whole units leaves a remainder essentially every
        # time; a distribution that lands on an exact multiple of the unit
        # price, repeatedly, does not happen. When most rows do, `cash_amount`
        # is holding units x price — the amount APPLIED — and the derivation
        # is measuring nothing. It returns a healthy-looking figure near zero,
        # which is why this check exists rather than trusting the total.
        applied_amounts = len(drp) >= 3 and exact >= len(drp) * 2 // 3
        if applied_amounts:
            breaches.insert(0, (
                f"{exact} of {len(drp)} allotments have cash_amount exactly "
                f"equal to units x price — that is the amount applied, not the "
                f"distribution, so the residual below is not measuring anything"))

        recorded = [e[2] for e in entries if e[2] is not None]
        stored_note = (f"{_dec(recorded[-1])}" if recorded else "none")

        # A recorded balance came off the registry's own statement and beats
        # anything derived here, so once every distribution has one this stops
        # reporting problems it can no longer help with. The derived column
        # stays as a cross-check — that comparison is what found a mistyped
        # allotment price that had been invisible for sixteen months.
        # "The latest distribution has one", not "every row has one" — a
        # holding paid in cash before it joined the DRP has early rows that
        # correctly carry no residual at all, and demanding one from them would
        # send you looking for statements you already have.
        complete = entries[-1][2] is not None
        if complete:
            verdict = "from statements"
            gap = abs(running - _dec(recorded[-1]))
            if gap > CENT * 5:
                verdict = "from statements — CROSS-CHECK DISAGREES"
                disagreements.append(ticker)
                breaches = [
                    f"derived {running} vs recorded {_dec(recorded[-1])}, a gap of "
                    f"{gap}. Either a figure is wrong, or this holding was paid "
                    f"in CASH before it joined the DRP — money that was paid out "
                    f"looks identical here to money that was carried forward, and "
                    f"the derivation cannot span that switch."]
            else:
                breaches = []
        else:
            verdict = "derivable" if not breaches else "NEEDS STATEMENTS"
        if breaches and not complete:
            needs_statements.append(ticker)
        print(f"{ticker:8} {len(entries):>14} {running:>18} "
              f"{stored_note:>12}  {verdict}")
        for line in breaches[:3]:
            print(f"         {line}")
        if len(breaches) > 3:
            print(f"         ... and {len(breaches) - 3} more")

    print()
    if needs_statements:
        print("Go and find the registry statements for: "
              + ", ".join(sorted(set(needs_statements))))
        print("Everything else can be derived from rows already in the database.")
    elif disagreements:
        print("Balances are recorded from statements. The independent derivation "
              "disagrees for: " + ", ".join(sorted(set(disagreements))))
        print("Check the note above before trusting either figure.")
    else:
        print("Every holding's residual derives cleanly from the recorded rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
