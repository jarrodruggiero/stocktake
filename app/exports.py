"""CSV / Excel exports.

The report set follows what Sharesight offers, cut down to what this app can
compute honestly from its own data:

  transactions      every buy/sell/DRP — the raw ledger, for a new tool or an
                    accountant who wants the source rows
  holdings          current valuation per open position
  dividends         income by payment, with franking credits and the gross-up
  realised_cgt      DISPOSALS matched FIFO to their parcels: cost base,
                    proceeds, gross gain, and the gain after the 50% discount
  unrealised_cgt    what a sale today would look like, per parcel still held
  performance       per-FY invested / value / gain, the annual summary
  closed_positions  one row per instrument fully exited

Every money column is AUD (converted at each row's own FX rate) unless the
header says "native", so a spreadsheet can total a column without thinking.

The discount columns are the honest-but-careful bit: the 50% CGT discount is
applied per parcel where it was held over twelve months, which is what the ATO
allows *before* losses are netted off. The FY report nets losses first (the
taxpayer-favourable order), so its bottom line is the number to lodge — that
caveat travels with the file.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import clock, fyreport, queries
from .models import Dividend, Instrument, Trade, trade_order

ZERO = Decimal(0)
Report = tuple[list[str], list[list]]

# report key -> (title, builder). `fy` is passed to the ones that take a year.
TITLES = {
    "transactions": "All trades",
    "holdings": "Current holdings",
    "dividends": "Dividends and franking",
    "realised_cgt": "Realised capital gains",
    "unrealised_cgt": "Unrealised capital gains",
    "performance": "Performance by financial year",
    "closed_positions": "Closed positions",
}


def _d(value: Decimal | float | None, places: str = "0.01"):
    if value is None:
        return ""
    return Decimal(value).quantize(Decimal(places))


def _aud(value: Decimal | None, fx: Decimal | None) -> Decimal | None:
    """Convert, or return None so the cell comes out blank.

    Multiplying by 1 when the rate is unknown would put a native-currency
    number under a column headed "(AUD)", and a spreadsheet will happily total
    that column. Blank is the honest answer; the caller resolves what it can
    through `queries.FxBook` before getting here.
    """
    if value is None or fx is None:
        return None
    return value * fx


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

def transactions(session: Session, ticker: str | None = None, **_) -> Report:
    headers = [
        "Date", "Ticker", "Name", "Exchange", "Type", "Units",
        "Price (native)", "Brokerage (native)", "Currency", "FX rate to AUD",
        "Value (AUD)", "Recorded by", "Note",
    ]
    stmt = select(Trade).options(selectinload(Trade.instrument)).order_by(Trade.date, Trade.id)
    fxbook = queries.FxBook(session)
    rows = []
    for t in session.scalars(stmt):
        if ticker and t.instrument.ticker != ticker:
            continue
        gross = t.quantity * t.unit_price
        signed = gross + t.brokerage if t.type in ("buy", "drp") else gross - t.brokerage
        # The FX column shows what the trade actually captured; the AUD column
        # may additionally fall back to the stored series for that date.
        rows.append([
            t.date.isoformat(), t.instrument.ticker, t.instrument.name or "",
            t.instrument.exchange, t.type, _d(t.quantity, "0.00000001"),
            _d(t.unit_price, "0.000001"), _d(t.brokerage), t.instrument.currency,
            _d(t.fx_rate, "0.000001"),
            _d(_aud(signed, fxbook.of(t, t.instrument.currency))),
            t.user_id or "", t.note or "",
        ])
    return headers, rows


def holdings(session: Session, **_) -> Report:
    headers = [
        "Ticker", "Name", "Class", "Currency", "Units", "Avg price (native)",
        "Last price (native)", "Price date", "Cost (AUD)", "Value (AUD)",
        "Capital gain (AUD)", "Gain %", "Dividends (native)", "DRP", "Note",
    ]
    open_positions, _closed = queries.split_positions(queries.all_holdings(session))
    rows = []
    for h in open_positions:
        rows.append([
            h.instrument.ticker, h.instrument.name or "", h.instrument.asset_class,
            h.instrument.currency, _d(h.units, "0.00000001"),
            _d(h.avg_price, "0.000001"), _d(h.price, "0.000001"),
            h.price_date.isoformat() if h.price_date else "",
            _d(h.cost_aud), _d(h.value_aud), _d(h.gain_aud),
            _d(h.gain_pct * 100, "0.01") if h.gain_pct is not None else "",
            _d(h.dividends_cash), "yes" if h.drp else "no", h.note or "",
        ])
    return headers, rows


def dividends(session: Session, fy: int | None = None, **_) -> Report:
    headers = [
        "Payment date", "Ticker", "Name", "Cash (native)", "Currency",
        "FX rate to AUD", "Cash (AUD)", "Franking credits (AUD)",
        "Grossed up (AUD)", "Reinvested", "Financial year", "Note",
    ]
    stmt = (
        select(Dividend)
        .options(selectinload(Dividend.instrument))
        .order_by(Dividend.date, Dividend.id)
    )
    fxbook = queries.FxBook(session)
    rows = []
    for d in session.scalars(stmt):
        year = d.date.year + (0 if d.date.month <= 6 else 1)
        if fy and year != fy:
            continue
        cash_aud = _aud(d.cash_amount, fxbook.of(d, d.instrument.currency))
        # NOT `or ZERO`. `franking_credits` is None when no statement has ever
        # supplied one and 0 when the distribution was unfranked, and ABOUT
        # (below) promises the reader those are different cells. Flattening
        # them here turned "we don't know" into "there was none" — a tax figure
        # somebody would have taken at face value.
        franking = d.franking_credits
        rows.append([
            d.date.isoformat(), d.instrument.ticker, d.instrument.name or "",
            _d(d.cash_amount, "0.0001"), d.instrument.currency,
            _d(d.fx_rate, "0.000001"), _d(cash_aud), _d(franking),
            # No AUD cash figure means no honest total — and neither does an
            # unrecorded credit, for the same reason: the grossed-up amount IS
            # cash + franking, so it cannot be known while half of it is not.
            _d(None if cash_aud is None or franking is None
               else cash_aud + franking),
            "yes" if d.reinvest_trade_id else "no",
            fyreport.fy_label(year), d.note or "",
        ])
    return headers, rows


def realised_cgt(session: Session, fy: int | None = None, **_) -> Report:
    """One row per parcel consumed by a sale — the tax-time report.

    "Gain after discount" is the per-parcel figure: half the gain when the
    parcel was held more than twelve months. The FY report applies losses
    before the discount, so use its total when lodging.
    """
    headers = [
        "Sale date", "Ticker", "Name", "Units", "Acquired", "Days held",
        "Cost base (AUD)", "Proceeds (AUD)", "Gross gain (AUD)",
        "Discount eligible", "CGT discount (AUD)", "Gain after discount (AUD)",
        "Financial year",
    ]
    instruments = (
        session.scalars(select(Instrument).options(selectinload(Instrument.trades)))
        .unique()
        .all()
    )
    fxbook = queries.FxBook(session)
    rows = []
    for inst in instruments:
        if not any(t.type == "sell" for t in inst.trades):
            continue
        for disposal in fyreport.instrument_disposals(inst, fxbook):
            year = disposal.date.year + (0 if disposal.date.month <= 6 else 1)
            if fy and year != fy:
                continue
            for parcel in disposal.parcels:
                gain = parcel.gain
                discount = (gain / 2) if (parcel.discountable and gain > 0) else ZERO
                rows.append([
                    disposal.date.isoformat(), inst.ticker, inst.name or "",
                    _d(parcel.quantity, "0.00000001"), parcel.acquired.isoformat(),
                    (disposal.date - parcel.acquired).days,
                    _d(parcel.cost_base), _d(parcel.proceeds), _d(gain),
                    "yes" if parcel.discountable else "no",
                    _d(discount), _d(gain - discount), fyreport.fy_label(year),
                ])
    rows.sort(key=lambda r: (r[0], r[1]))
    return headers, rows


def unrealised_cgt(session: Session, **_) -> Report:
    """What a sale at today's price would realise, per parcel still held."""
    headers = [
        "Ticker", "Name", "Acquired", "Units", "Days held", "Cost base (AUD)",
        "Market value (AUD)", "Unrealised gain (AUD)", "Discount eligible",
        "Gain after discount (AUD)",
    ]
    today = clock.today()
    instruments = (
        session.scalars(select(Instrument).options(selectinload(Instrument.trades)))
        .unique()
        .all()
    )
    fxbook = queries.FxBook(session)
    rows = []
    for inst in instruments:
        latest = queries._latest_price(session, inst.id)
        if latest is None:
            continue
        price, _date = latest
        fx_now = queries.latest_fx(session, inst.currency)
        # Walk the same FIFO the CGT engine uses, keeping what survives.
        fifo: list[list] = []
        for t in sorted(inst.trades, key=trade_order):  # same FIFO order as the CGT engine
            # None here means no rate was captured on the trade and none is
            # stored for the pair, so this parcel has no honest AUD cost base.
            fx = fxbook.of(t, inst.currency)
            if t.type in ("buy", "drp"):
                unit_cost = (
                    None if fx is None
                    else (t.quantity * t.unit_price + t.brokerage) * fx / t.quantity
                )
                fifo.append([t.date, t.quantity, unit_cost])
            else:
                remaining = t.quantity
                while remaining > 0 and fifo:
                    take = min(fifo[0][1], remaining)
                    remaining -= take
                    if take == fifo[0][1]:
                        fifo.pop(0)
                    else:
                        fifo[0][1] -= take
        for acquired, units, unit_cost in fifo:
            # A spreadsheet will happily add a USD number to an AUD column, so
            # a figure we can't convert is left EMPTY rather than converted at
            # 1:1 — the same refusal the dashboard makes via `totals.excluded`.
            cost = None if unit_cost is None else units * unit_cost
            value = None if fx_now is None else units * price * fx_now
            gain = None if (cost is None or value is None) else value - cost
            eligible = today > fyreport._one_year_after(acquired)
            discount = (gain / 2) if (gain is not None and eligible and gain > 0) else ZERO
            rows.append([
                inst.ticker, inst.name or "", acquired.isoformat(),
                _d(units, "0.00000001"), (today - acquired).days,
                _d(cost), _d(value), _d(gain),
                "yes" if eligible else "no",
                "" if gain is None else _d(gain - discount),
            ])
    rows.sort(key=lambda r: (r[0], r[2]))
    return headers, rows


def performance(session: Session, **_) -> Report:
    headers = [
        "Financial year", "Invested in year (AUD)", "Invested cumulative (AUD)",
        "Value at year end (AUD)", "Net gain cumulative (AUD)", "Gain %",
        "Gain in year (AUD)", "Year gain %",
    ]
    annual = queries.annual_series(session)
    rows = []
    for i, label in enumerate(annual["labels"]):
        rows.append([
            label, _d(annual["invested_yr"][i]), _d(annual["invested_cum"][i]),
            _d(annual["value_eofy"][i]), _d(annual["gain_cum"][i]),
            _d(annual["gain_pct"][i] * 100), _d(annual["gain_yr"][i]),
            _d(annual["yr_pct"][i] * 100),
        ])
    return headers, rows


def closed_positions(session: Session, **_) -> Report:
    """Fully exited instruments — outlay, proceeds and realised profit.

    Capital-gain columns come from the FIFO engine, so they line up with the
    realised CGT report rather than being a second opinion.
    """
    headers = [
        "Ticker", "Name", "Units sold", "First bought", "Last sold",
        "Outlay incl. brokerage (AUD)", "Proceeds net of brokerage (AUD)",
        "Dividends (AUD)", "Gross capital gain (AUD)", "CGT discount (AUD)",
        "Capital gain after discount (AUD)", "Total return incl. dividends (AUD)",
        "Note",
    ]
    _open, closed = queries.split_positions(queries.all_holdings(session))
    prefs = queries.prefs_by_instrument(session)
    fxbook = queries.FxBook(session)
    rows = []
    for position in closed:
        inst = position.instrument
        disposals = fyreport.instrument_disposals(inst, fxbook)
        if not disposals:
            continue
        cost_base = sum((d.cost_base for d in disposals), ZERO)
        proceeds = sum((d.proceeds for d in disposals), ZERO)
        gross = proceeds - cost_base
        discount = sum(
            ((p.gain / 2) for d in disposals for p in d.parcels if p.discountable and p.gain > 0),
            ZERO,
        )
        units = sum((d.quantity for d in disposals), ZERO)
        dividends_aud = sum(
            (
                dv.cash_amount * (fxbook.of(dv, inst.currency) or Decimal(1))
                for dv in inst.dividends
            ),
            ZERO,
        )
        buys = [t.date for t in inst.trades if t.type in ("buy", "drp")]
        pref = prefs.get(inst.id)
        rows.append([
            inst.ticker, inst.name or "", _d(units, "0.00000001"),
            min(buys).isoformat() if buys else "",
            max(d.date for d in disposals).isoformat(),
            _d(cost_base), _d(proceeds), _d(dividends_aud), _d(gross),
            _d(discount), _d(gross - discount), _d(gross + dividends_aud),
            (pref.note if pref else "") or "",
        ])
    rows.sort(key=lambda r: r[0])
    return headers, rows


BUILDERS = {
    "transactions": transactions,
    "holdings": holdings,
    "dividends": dividends,
    "realised_cgt": realised_cgt,
    "unrealised_cgt": unrealised_cgt,
    "performance": performance,
    "closed_positions": closed_positions,
}


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def to_csv(headers: list[str], rows: list[list]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    # utf-8-sig: Excel on Windows opens a plain utf-8 CSV as mojibake.
    return buffer.getvalue().encode("utf-8-sig")


def to_xlsx(sheets: dict[str, Report], note: str | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    for title, (headers, rows) in sheets.items():
        # Excel sheet names: 31 chars, no []:*?/\
        safe = "".join(c for c in title if c not in "[]:*?/\\")[:31]
        ws = wb.create_sheet(safe)
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="left")
        for row in rows:
            ws.append(row)
        ws.freeze_panes = "A2"
        for i, header in enumerate(headers, start=1):
            longest = max(
                [len(str(header))] + [len(str(r[i - 1])) for r in rows[:200]] or [0]
            )
            ws.column_dimensions[get_column_letter(i)].width = min(40, longest + 3)
    if note:
        ws = wb.create_sheet("About")
        for line in note.splitlines():
            ws.append([line])
        ws.column_dimensions["A"].width = 100
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


ABOUT = """Stocktake export
Generated by Stocktake, a self-hosted share portfolio tracker. All money
columns are AUD unless the header says "native"; each row is converted at its
own FX rate.

Capital gains: parcels are matched FIFO. The "gain after discount" columns halve
a gain on a parcel held more than twelve months. The ATO applies capital LOSSES
before that discount, which the FY report in the app does — use the FY report's
net figure when lodging, and these rows as the working.

Dividends: franking credits are only present where a statement supplied them;
blank means not recorded, not zero. The grossed-up column is cash plus franking,
so it is blank on those rows too rather than repeating the cash figure.
"""


def build(session: Session, reports: list[str], fy: int | None = None,
          ticker: str | None = None) -> dict[str, Report]:
    return {
        TITLES[key]: BUILDERS[key](session, fy=fy, ticker=ticker)
        for key in reports
        if key in BUILDERS
    }
