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


def spans_regime_change(disposals) -> bool:
    """Whether any disposal here is of an asset held across 1 July 2027.

    Those need the transitional apportionment — a market valuation at the
    changeover — which cannot be computed until the method is published. Worth
    flagging even in a year this module still calculates, because the parcel is
    already straddling the boundary.
    """
    return any(
        p.acquired < CGT_INDEXATION_START <= d.date
        for d in disposals for p in d.parcels
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


def _rate(obj, inst: Instrument, fx_book) -> Decimal:
    """AUD per 1 unit for a trade or dividend, resolved as honestly as we can.

    Order: the rate captured on the row, then the stored FX series at its date,
    then 1. That last step is a real fallback rather than a lie only because
    the ATO figures here are cost bases denominated at transaction date — a
    parcel with no rate anywhere is an AUD parcel or a data gap, and the FY
    report flags gaps rather than dropping rows a tax return needs to see.
    """
    if obj.fx_rate is not None:
        return obj.fx_rate
    if fx_book is not None:
        resolved = fx_book.rate(inst.currency, obj.date)
        if resolved is not None:
            return resolved
    return Decimal(1)


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
    cost_base: Decimal  # AUD
    proceeds: Decimal  # AUD
    discountable: bool  # held > 12 months at disposal

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost_base


@dataclass
class Disposal:
    instrument: Instrument
    date: dt.date
    quantity: Decimal
    proceeds: Decimal  # AUD, net of sale brokerage
    parcels: list[ParcelUse]

    @property
    def cost_base(self) -> Decimal:
        return sum((p.cost_base for p in self.parcels), ZERO)

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost_base


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
    cost: Decimal              # exact AUD outlay for `quantity`, unrounded
    cost_allotted: Decimal = ZERO

    def take(self, units: Decimal) -> Decimal:
        """Consume `units` from this parcel and return their cost base."""
        self.remaining -= units
        consumed = self.quantity - self.remaining
        owed = (self.cost * consumed / self.quantity).quantize(CENTS)
        billed = owed - self.cost_allotted
        self.cost_allotted = owed
        return billed


def instrument_disposals(inst: Instrument, fx_book=None) -> list[Disposal]:
    """FIFO-match this instrument's sells against its acquisition parcels.

    `fx_book` is a `queries.FxBook` used to resolve a trade whose own
    `fx_rate` was never captured. Without one — the direct call in a test,
    or an all-AUD portfolio — such a trade falls back to 1:1, which is only
    right for AUD. Every session-taking entry point here passes one.
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
                    cost=(t.quantity * t.unit_price + t.brokerage) * fx,
                )
            )
            continue

        # sell: oldest parcels consumed first. Both sides of a parcel use are
        # apportioned rather than rounded on their own — see `_Parcel.take` for
        # the cost base, and the loop below for the proceeds.
        net_proceeds = ((t.quantity * t.unit_price - t.brokerage) * fx).quantize(CENTS)
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

        # Spread the money ACTUALLY RECEIVED across the parcels by units, in
        # whole cents. Per-unit-then-round gave each parcel its own rounding
        # error: three one-unit parcels out of 14.00 each came to 4.6666… and
        # rounded to 4.67, so the schedule reported 14.01 of proceeds against
        # 14.00 of money. Running the total forward and taking differences
        # means the parts sum to the whole by construction, and the leftover
        # cent lands on a parcel instead of on nobody.
        sold = ZERO
        allotted = ZERO
        for use in uses:
            sold += use.quantity
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
    if spans_regime_change(out.disposals):
        out.regime_warning = (
            "One or more parcels here were acquired before 1 July 2027 and "
            "sold after it. From that date the CGT rules change, and such a "
            "parcel is apportioned using its market value at the changeover — "
            "a method that is not yet published. These figures use the earlier "
            "rules for the whole gain. Check for an update before lodging."
        )
    return out


# ---------- dividend / franking income ----------


@dataclass
class IncomeRow:
    instrument: Instrument
    cash: Decimal
    franking: Decimal
    franking_missing: int  # dividends this FY with no franking data yet

    @property
    def grossed_up(self) -> Decimal:
        return self.cash + self.franking


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
        cash = sum((d.cash_amount * _rate(d, inst, fx_book) for d in divs), ZERO)
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
    invested_cum: Decimal  # AUD, buys to date net of nothing (sheet semantics)
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
        """Whether a gain percentage is a meaningful thing to ask for here."""
        return bool(self.invested_cum)


def _price_at(session: Session, instrument_id: int, asof: dt.date):
    return session.execute(
        select(Price.close, Price.date)
        .where(Price.instrument_id == instrument_id, Price.date <= asof)
        .order_by(Price.date.desc())
        .limit(1)
    ).first()


def _fx_at(session: Session, currency: str, asof: dt.date) -> Decimal:
    if currency == "AUD":
        return Decimal(1)
    from .models import FxRate

    rate = session.execute(
        select(FxRate.rate)
        .where(FxRate.pair == f"{currency}AUD", FxRate.date <= asof)
        .order_by(FxRate.date.desc())
        .limit(1)
    ).scalar()
    return rate if rate is not None else Decimal(1)


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
    for inst in instruments:
        units = invested = ZERO
        for t in sorted(inst.trades, key=trade_order):
            fx = _rate(t, inst, fx_book)
            in_fy = start <= t.date <= end
            if t.date > asof:
                continue
            if t.type in ("buy", "drp"):
                units += t.quantity
                if t.type == "buy":
                    invested += (t.quantity * t.unit_price + t.brokerage) * fx
                    if in_fy:
                        activity.invested += (t.quantity * t.unit_price + t.brokerage) * fx
                        activity.buys += 1
                        activity.brokerage += t.brokerage * fx
            else:
                units -= t.quantity
                if in_fy:
                    activity.sells += 1
                    activity.proceeds += (t.quantity * t.unit_price - t.brokerage) * fx
                    activity.brokerage += t.brokerage * fx
        if units <= 0 and invested == ZERO:
            continue
        price_row = _price_at(session, inst.id, asof)
        price, price_date = (price_row.close, price_row.date) if price_row else (None, None)
        value = gain = None
        if price is not None and units > 0:
            value = units * price * _fx_at(session, inst.currency, asof)
            gain = value - invested
        if units > 0:
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
    return {
        "fy_end_year": fy_end_year,
        "label": fy_label(fy_end_year),
        "start": start,
        "end": end,
        "asof": asof,
        "is_current": end >= today,
        "snapshot": snapshot,
        "total_value": sum((r.value_aud for r in snapshot if r.value_aud is not None), ZERO),
        "total_invested": sum((r.invested_cum for r in snapshot), ZERO),
        "activity": activity,
        "income": income,
        "income_cash": sum((r.cash for r in income), ZERO),
        "income_franking": sum((r.franking for r in income), ZERO),
        "franking_missing": sum(r.franking_missing for r in income),
        "cgt": cgt,
    }


def available_fys(session: Session) -> list[int]:
    first = session.execute(select(Trade.date).order_by(Trade.date.asc()).limit(1)).scalar()
    if first is None:
        return []
    first_fy = first.year + (0 if first.month <= 6 else 1)
    today = clock.today()
    current_fy = today.year + (0 if today.month <= 6 else 1)
    return list(range(current_fy, first_fy - 1, -1))


# --------------------------------------------------------------------------- #
# Sorting the report's tables
# --------------------------------------------------------------------------- #
#
# Three fixed tables of three different row shapes, so they share only the
# ordering (`app/sorting.py`) rather than the holdings table's column registry.
#
# The key names appear in the URL (`?snapshot_sort=units`), so this table is an
# allow-list as well as a lookup: nothing outside it can be sorted by.

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
    # Franking and grossed_up are NOT here, and the omission is deliberate:
    # the FY page stopped showing those two columns (see dashboard_fy.html).
    # This dict is an allow-list as much as a lookup, so leaving them in would
    # let `?income_sort=franking` reorder the table by a column nobody can see
    # — a page whose rows move for no visible reason. `IncomeRow` still CARRIES
    # both figures, and the API still publishes them.
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
