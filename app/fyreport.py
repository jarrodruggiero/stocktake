"""FY report: financial-year income, CGT and a year-end snapshot,
plus the AU tax pieces (CGT + dividend/franking income).

CGT method: **FIFO parcel matching** (every buy and DRP allocation is a parcel;
sells consume the oldest first). The engine keeps per-parcel detail so specific
identification can be offered later without schema changes. Rules applied:
  * cost base = parcel outlay incl. buy brokerage, at trade-date fx
  * proceeds = sale amount less sell brokerage (apportioned), at sale-date fx
  * 50% discount for parcels held > 12 months (per parcel, individuals)
  * within a FY, losses offset non-discountable gains first, then discountable,
    and the discount applies to what remains (the taxpayer-favourable order)
  * no loss carry-forward across FYs yet — flagged in the report when relevant
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import clock, queries
from .models import Instrument, Price, Trade, trade_order

ZERO = Decimal(0)
CENTS = Decimal("0.01")

# The day the rules this module implements stop being the rules. The new regime
# is announced, not legislated, so years past this are REFUSED rather than
# computed with a repealed discount — decisions.md #26.
CGT_INDEXATION_START = dt.date(2027, 7, 1)


def regime_warning(fy_end_year: int) -> str | None:
    """Why this financial year's capital gains are not computed, or None.

    Returned rather than raised: the rest of an FY report — dividend income,
    franking, the holdings snapshot — is unaffected and still worth showing.
    """
    start, _end = fy_bounds(fy_end_year)
    if start < CGT_INDEXATION_START:
        return None
    return (
        f"Capital gains are not calculated for {fy_label(fy_end_year)}. From "
        "1 July 2027 the 50% CGT discount is replaced by cost-base indexation "
        "and a 30% minimum tax rate, and this version implements the earlier "
        "rules. The change was announced in the 2026-27 Budget and the detail "
        "is still being settled, so showing a figure here would mean applying "
        "a repealed rule. Everything else on this page is unaffected. Check "
        "for an update before lodging."
    )


def fy_bounds(fy_end_year: int) -> tuple[dt.date, dt.date]:
    """FY2026 = 1 Jul 2025 → 30 Jun 2026."""
    return dt.date(fy_end_year - 1, 7, 1), dt.date(fy_end_year, 6, 30)


def current_fy(today: dt.date) -> int:
    """The financial year `today` falls in, named by the year it ends.

    The AU boundary, and one of the several places T28b's jurisdiction work has
    to reach — a US install's year ends on 31 December.
    """
    return today.year if today.month <= 6 else today.year + 1


def fy_label(fy_end_year: int) -> str:
    return f"FY{str(fy_end_year - 1)[-2:]}/{str(fy_end_year)[-2:]}"


def _rate(obj, inst: Instrument, fx_book) -> Decimal | None:
    """AUD per 1 unit for a trade or dividend, or None when none is known.

    The rate captured on the row, then the stored FX series at its date. An
    AUD row needs neither (decisions.md #6). A foreign one with no rate
    anywhere is left out of every total and named on the page — never booked
    at 1:1, which counts a US dollar as an Australian one (#5).
    """
    if obj.fx_rate is not None:
        return obj.fx_rate
    if inst.currency == queries.REPORTING_CURRENCY:
        return Decimal(1)
    return fx_book.rate(inst.currency, obj.date) if fx_book is not None else None


def _one_year_after(d: dt.date) -> dt.date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year + 1, day=28)


# ---------- CGT ----------


@dataclass
class ParcelUse:
    acquired: dt.date
    quantity: Decimal
    cost_base: Decimal | None  # AUD; None when the parcel's purchase had no rate
    proceeds: Decimal | None  # AUD; None when the sale had no rate
    discountable: bool  # held > 12 months at disposal

    @property
    def gain(self) -> Decimal | None:
        if self.cost_base is None or self.proceeds is None:
            return None
        return self.proceeds - self.cost_base


@dataclass
class Disposal:
    instrument: Instrument
    date: dt.date
    quantity: Decimal
    proceeds: Decimal | None  # AUD, net of sale brokerage; None without a rate
    parcels: list[ParcelUse]

    @property
    def known(self) -> bool:
        """Whether every figure converts: the sale and each parcel it used had
        a rate. One that does not is listed but left out of the totals."""
        return self.proceeds is not None and all(p.cost_base is not None for p in self.parcels)

    @property
    def cost_base(self) -> Decimal | None:
        if any(p.cost_base is None for p in self.parcels):
            return None
        return sum((p.cost_base for p in self.parcels), ZERO)

    @property
    def gain(self) -> Decimal | None:
        return self.proceeds - self.cost_base if self.known else None


@dataclass
class _Parcel:
    """An acquisition still holding units, and what has been billed from it.

    `cost_allotted` is why this is a class rather than three loose values.
    **Rounding each use of a parcel on its own loses cents.** A parcel bought for 10.00 and sold one unit at a time reports
    3.33 three times, so the schedule claims a 9.99 cost base against money
    that was 10.00 — and a cost base a cent light is a gain a cent heavy, on a
    document somebody hands to an accountant.

    The running total fixes it: each use is billed the DIFFERENCE between what
    the parcel owes once that use is included and what it has already given up.
    Rounding still happens, but it happens once against the total instead of
    independently per slice, so the pieces always sum to the whole.
    """

    acquired: dt.date
    remaining: Decimal
    quantity: Decimal          # as acquired; never changes
    cost: Decimal | None       # exact AUD outlay for `quantity`, unrounded; None without a rate
    cost_allotted: Decimal = ZERO

    def take(self, units: Decimal) -> Decimal | None:
        """Consume `units` from this parcel and return their cost base, or None
        when the parcel's cost is not known."""
        self.remaining -= units
        if self.cost is None:
            return None
        consumed = self.quantity - self.remaining
        owed = (self.cost * consumed / self.quantity).quantize(CENTS)
        billed = owed - self.cost_allotted
        self.cost_allotted = owed
        return billed


def instrument_disposals(inst: Instrument, fx_book=None) -> list[Disposal]:
    """FIFO-match this instrument's sells against its acquisition parcels.

    `fx_book` is a `queries.FxBook` used to resolve a trade whose own
    `fx_rate` was never captured. Without one — the direct call in a test —
    only the rates captured on the trades are known. A parcel or a sale with
    no rate keeps its units in the matching, and its AUD figures are None.
    Every session-taking entry point here passes one.
    """
    fifo: list[_Parcel] = []
    disposals: list[Disposal] = []
    # Timeline order, not entry order: two parcels bought on one day at
    # different prices must be consumed in the order they were acquired, or a
    # later sale is matched against the wrong cost base.
    for t in sorted(inst.trades, key=trade_order):
        fx = _rate(t, inst, fx_book)
        if t.type in ("buy", "drp"):
            fifo.append(
                _Parcel(
                    acquired=t.date,
                    remaining=t.quantity,
                    quantity=t.quantity,
                    cost=None if fx is None else (t.quantity * t.unit_price + t.brokerage) * fx,
                )
            )
            continue

        # sell: oldest parcels consumed first. Both sides of a parcel use are
        # apportioned rather than rounded on their own — see `_Parcel.take` for
        # the cost base, and the loop below for the proceeds.
        net_proceeds = (None if fx is None else
                        ((t.quantity * t.unit_price - t.brokerage) * fx).quantize(CENTS))
        remaining = t.quantity
        uses: list[ParcelUse] = []
        while remaining > 0 and fifo:
            parcel = fifo[0]
            take = min(parcel.remaining, remaining)
            uses.append(
                ParcelUse(
                    acquired=parcel.acquired,
                    quantity=take,
                    cost_base=parcel.take(take),
                    proceeds=ZERO,   # apportioned below, once they are all known
                    discountable=t.date > _one_year_after(parcel.acquired),
                )
            )
            remaining -= take
            if parcel.remaining <= 0:
                fifo.pop(0)
        if remaining > 0:  # more sold than held — data problem, surface loudly
            raise ValueError(
                f"{inst.ticker}: sell of {t.quantity} on {t.date} exceeds held parcels"
            )

        # Running-total differencing, so the parts sum to the whole by
        # construction. Per-unit-then-round does not — decisions.md #93.
        sold = ZERO
        allotted = ZERO
        for use in uses:
            sold += use.quantity
            if net_proceeds is None:
                use.proceeds = None
                continue
            owed = (net_proceeds * sold / t.quantity).quantize(CENTS)
            use.proceeds = owed - allotted
            allotted = owed

        disposals.append(
            Disposal(
                instrument=inst,
                date=t.date,
                quantity=t.quantity,
                # The money received, not a sum of rounded parts. The parcels
                # now add to exactly this; `test_cgt.py` asserts they do.
                proceeds=net_proceeds,
                parcels=uses,
            )
        )
    return disposals


@dataclass
class CgtSummary:
    disposals: list[Disposal] = field(default_factory=list)
    gains_discountable: Decimal = ZERO  # gross gains on >12m parcels
    gains_other: Decimal = ZERO
    losses: Decimal = ZERO  # positive number
    losses_applied_other: Decimal = ZERO
    losses_applied_discountable: Decimal = ZERO
    discount: Decimal = ZERO
    net_capital_gain: Decimal = ZERO
    net_capital_loss: Decimal = ZERO  # >0 when the FY nets to a loss
    # Set when this year's rules are not the ones implemented here. When it is
    # set for a whole year, every figure above is zero — the report refuses
    # rather than guessing.
    regime_warning: str | None = None
    # Holdings with a sale this year that has no exchange rate behind it: the
    # sale is listed, and every figure above leaves it out (decisions.md #5).
    withheld: list[str] = field(default_factory=list)


def fy_cgt(session: Session, fy_end_year: int) -> CgtSummary:
    start, end = fy_bounds(fy_end_year)
    out = CgtSummary()
    # A year under rules this version does not implement returns an empty
    # summary carrying the reason, rather than numbers computed under the wrong
    # regime — decisions.md #26.
    out.regime_warning = regime_warning(fy_end_year)
    if out.regime_warning:
        return out
    instruments = session.scalars(
        select(Instrument).options(selectinload(Instrument.trades))
    ).unique().all()
    fx_book = queries.FxBook(session)
    for inst in instruments:
        if not any(t.type == "sell" for t in inst.trades):
            continue
        for d in instrument_disposals(inst, fx_book):
            if not (start <= d.date <= end):
                continue
            out.disposals.append(d)
            if not d.known:
                if inst.ticker not in out.withheld:
                    out.withheld.append(inst.ticker)
                continue
            for p in d.parcels:
                if p.gain >= 0:
                    if p.discountable:
                        out.gains_discountable += p.gain
                    else:
                        out.gains_other += p.gain
                else:
                    out.losses += -p.gain
    # losses offset non-discountable gains first, then discountable
    out.losses_applied_other = min(out.losses, out.gains_other)
    rest = out.losses - out.losses_applied_other
    out.losses_applied_discountable = min(rest, out.gains_discountable)
    unapplied = rest - out.losses_applied_discountable
    disc_after = out.gains_discountable - out.losses_applied_discountable
    out.discount = disc_after / 2
    if unapplied > 0:
        out.net_capital_loss = unapplied
    else:
        out.net_capital_gain = (out.gains_other - out.losses_applied_other) + disc_after - out.discount
    out.disposals.sort(key=lambda d: d.date)
    out.withheld.sort()
    return out


# ---------- dividend / franking income ----------


@dataclass
class IncomeRow:
    instrument: Instrument
    cash: Decimal | None  # None when a dividend this FY has no exchange rate
    franking: Decimal
    franking_missing: int  # dividends this FY with no franking data yet

    @property
    def grossed_up(self) -> Decimal | None:
        return None if self.cash is None else self.cash + self.franking


def fy_income(session: Session, fy_end_year: int) -> list[IncomeRow]:
    start, end = fy_bounds(fy_end_year)
    rows: list[IncomeRow] = []
    instruments = session.scalars(
        select(Instrument).options(selectinload(Instrument.dividends))
    ).unique().all()
    fx_book = queries.FxBook(session)
    for inst in instruments:
        divs = [d for d in inst.dividends if start <= d.date <= end]
        if not divs:
            continue
        rates = [_rate(d, inst, fx_book) for d in divs]
        cash = (None if None in rates else
                sum((d.cash_amount * rate for d, rate in zip(divs, rates)), ZERO))
        franking = sum((d.franking_credits or ZERO for d in divs), ZERO)
        rows.append(
            IncomeRow(
                instrument=inst,
                cash=cash,
                franking=franking,
                franking_missing=sum(1 for d in divs if d.franking_credits is None),
            )
        )
    rows.sort(key=lambda r: r.instrument.ticker)
    return rows


# ---------- FY holdings snapshot + activity ----------


@dataclass
class SnapshotRow:
    instrument: Instrument
    units: Decimal
    invested_cum: Decimal | None  # AUD, buys to date (sheet semantics); None if one had no rate
    price: Decimal | None  # native, close on/before the as-of date
    price_date: dt.date | None
    value_aud: Decimal | None
    gain_aud: Decimal | None

    @property
    def gain_pct(self) -> Decimal | None:
        """Gain as a fraction of what was put in.

        Derived rather than stored, so it cannot drift from the two numbers it
        comes from.

        **None covers two situations and the template tells them apart**: no
        `invested_cum`, so a percentage cannot exist — renders **N/A**; or an
        unpriced holding, where it is computable in principle and simply not
        known yet — renders an em dash. Returning zero would claim the holding
        broke even, which is a statement about the data rather than its absence.
        """
        if self.gain_aud is None or not self.invested_cum:
            return None
        return self.gain_aud / self.invested_cum

    @property
    def percentage_applies(self) -> bool:
        """Whether a gain percentage is a meaningful thing to ask for here.
        An amount invested that is unknown for want of a rate still has one."""
        return self.invested_cum is None or bool(self.invested_cum)


def _price_at(session: Session, instrument_id: int, asof: dt.date):
    return session.execute(
        select(Price.close, Price.date)
        .where(Price.instrument_id == instrument_id, Price.date <= asof)
        .order_by(Price.date.desc())
        .limit(1)
    ).first()


@dataclass
class FyActivity:
    invested: Decimal = ZERO  # AUD buys incl. brokerage
    buys: int = 0
    sells: int = 0
    brokerage: Decimal = ZERO
    proceeds: Decimal = ZERO


def fy_report(session: Session, fy_end_year: int) -> dict:
    start, end = fy_bounds(fy_end_year)
    today = clock.today()
    asof = min(end, today)
    instruments = session.scalars(
        select(Instrument).options(selectinload(Instrument.trades))
    ).unique().all()

    snapshot: list[SnapshotRow] = []
    activity = FyActivity()
    fx_book = queries.FxBook(session)
    # Holdings some AUD figure on this page leaves out for want of a rate,
    # named once at the top of the page (decisions.md #5).
    withheld: set[str] = set()
    for inst in instruments:
        units = ZERO
        invested: Decimal | None = ZERO
        for t in sorted(inst.trades, key=trade_order):
            fx = _rate(t, inst, fx_book)
            in_fy = start <= t.date <= end
            if t.date > asof:
                continue
            if t.type in ("buy", "drp"):
                units += t.quantity
                if t.type == "buy":
                    outlay = t.quantity * t.unit_price + t.brokerage
                    if fx is None:
                        invested = None
                    elif invested is not None:
                        invested += outlay * fx
                    if in_fy:
                        activity.buys += 1
                        if fx is None:
                            withheld.add(inst.ticker)
                        else:
                            activity.invested += outlay * fx
                            activity.brokerage += t.brokerage * fx
            else:
                units -= t.quantity
                if in_fy:
                    activity.sells += 1
                    if fx is None:
                        withheld.add(inst.ticker)
                    else:
                        activity.proceeds += (t.quantity * t.unit_price - t.brokerage) * fx
                        activity.brokerage += t.brokerage * fx
        if units <= 0:
            continue
        price_row = _price_at(session, inst.id, asof)
        price, price_date = (price_row.close, price_row.date) if price_row else (None, None)
        # The stored rate nearest the date, and no value at all for a currency
        # with none: never 1:1 (decisions.md #5).
        rate = fx_book.rate(inst.currency, asof)
        if invested is None or (price is not None and rate is None):
            withheld.add(inst.ticker)
        value = gain = None
        if price is not None and rate is not None:
            value = units * price * rate
            gain = None if invested is None else value - invested
        snapshot.append(
            SnapshotRow(
                instrument=inst,
                units=units,
                invested_cum=invested,
                price=price,
                price_date=price_date,
                value_aud=value,
                gain_aud=gain,
            )
        )

    income = fy_income(session, fy_end_year)
    cgt = fy_cgt(session, fy_end_year)
    withheld.update(r.instrument.ticker for r in income if r.cash is None)
    withheld.update(cgt.withheld)
    return {
        "fy_end_year": fy_end_year,
        "label": fy_label(fy_end_year),
        "start": start,
        "end": end,
        "asof": asof,
        "is_current": end >= today,
        "snapshot": snapshot,
        "total_value": sum((r.value_aud for r in snapshot if r.value_aud is not None), ZERO),
        "total_invested": sum((r.invested_cum for r in snapshot
                               if r.invested_cum is not None), ZERO),
        "activity": activity,
        "income": income,
        "income_cash": sum((r.cash for r in income if r.cash is not None), ZERO),
        "income_franking": sum((r.franking for r in income), ZERO),
        "franking_missing": sum(r.franking_missing for r in income),
        "cgt": cgt,
        "withheld": sorted(withheld),
    }


def available_fys(session: Session) -> list[int]:
    first = session.execute(select(Trade.date).order_by(Trade.date.asc()).limit(1)).scalar()
    if first is None:
        return []
    return list(range(current_fy(clock.today()), current_fy(first) - 1, -1))


# --------------------------------------------------------------------------- #
# Sorting the report's tables
# --------------------------------------------------------------------------- #
# The key names appear in the URL, so this is an allow-list as well as a lookup.

SORTABLE: dict[str, dict[str, object]] = {
    "snapshot": {
        "ticker": lambda r: r.instrument.ticker,
        "units": lambda r: r.units,
        "price": lambda r: r.price,
        "invested_cum": lambda r: r.invested_cum,
        "value_aud": lambda r: r.value_aud,
        "gain_aud": lambda r: r.gain_aud,
        "gain_pct": lambda r: r.gain_pct,
    },
    # Franking and grossed_up are deliberately absent: the FY page no longer
    # shows those columns, and this is an allow-list, so leaving them would let
    # a query reorder the table by a column nobody can see.
    "income": {
        "ticker": lambda r: r.instrument.ticker,
        "cash": lambda r: r.cash,
    },
    "disposals": {
        "date": lambda d: d.date,
        "ticker": lambda d: d.instrument.ticker,
        "quantity": lambda d: d.quantity,
        "cost_base": lambda d: d.cost_base,
        "proceeds": lambda d: d.proceeds,
        "gain": lambda d: d.gain,
    },
}


def sort_tables(report: dict, sorts: dict) -> None:
    """Reorder the report's tables in place, per `sorts`.

    In place because the report is already built and the totals underneath it
    are computed from the same rows — reordering must not be able to change a
    number, and rebinding a list cannot.
    """
    from . import sorting

    for table, sort in sorts.items():
        if not sort.key:
            continue
        getter = SORTABLE[table][sort.key]
        if table == "disposals":
            report["cgt"].disposals = sorting.apply(
                report["cgt"].disposals, getter, descending=sort.descending)
        else:
            report[table] = sorting.apply(
                report[table], getter, descending=sort.descending)
