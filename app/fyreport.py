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
  * a year's net capital loss carries forward, and is applied in later years
    after their own losses and before the discount (s102-5(1)); the app sees
    only the sales recorded in it
  * from 1 July 2027 (decisions.md #150): a parcel held through 30 June 2027 is
    taken to be sold just before 1 July 2027 for its market value and bought
    again on it, the gain to then deferred to the real sale and discounted;
    after it, the cost base is indexed by the CPI instead of discounted, and
    the year's losses reduce the deferred gains first (s102-5(1) as amended)
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import clock, cpi, queries
from .models import CorporateAction, Instrument, Price, Trade

ZERO = Decimal(0)
CENTS = Decimal("0.01")

# The day the 2027 rules start (Treasury Laws Amendment (Tax Reform No. 1) Act
# 2026): an asset held through 30 June 2027 is taken to be sold just before it
# and bought again on it, and a cost base is indexed from it. decisions.md #150.
NEW_RULES = dt.date(2027, 7, 1)


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


def income_date(dividend) -> dt.date:
    """The date that decides a distribution's financial year: its period's
    end where it has one, as a trust's income belongs to the trust's year
    (decisions.md #144), else the day it was paid."""
    return dividend.period_end or dividend.date


def period_problem(period_end: dt.date | None, paid: dt.date) -> str | None:
    """Why `period_end` cannot be the end of a distribution paid on `paid`."""
    if period_end is not None and (period_end > paid or _one_year_after(period_end) < paid):
        return ("A distribution's period ends on or before its payment date, and within "
                "a year of it.")
    return None


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
    return fx_book.rate(inst.currency, obj.date, inst.ticker) if fx_book is not None else None


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
    # AUD; None when the parcel's purchase had no rate. From 1 July 2027, for a
    # parcel held through 30 June 2027, what it was worth then (#150).
    cost_base: Decimal | None
    proceeds: Decimal | None  # AUD; None when the sale had no rate
    discountable: bool  # held > 12 months at disposal
    # A sale from 1 July 2027 (decisions.md #150):
    new_rules: bool = False
    held_through_2027: bool = False
    deferred_proceeds: Decimal | None = None   # its value just before 1 July 2027
    deferred_cost: Decimal | None = None       # its cost base then
    factor: Decimal | None = None              # indexation; None when not indexed
    awaiting_cpi: dt.date | None = None        # a quarter whose CPI is not out yet

    @property
    def deferred(self) -> Decimal | None:
        """The gain or loss to 30 June 2027, taxed when the parcel is sold."""
        if self.deferred_proceeds is None or self.deferred_cost is None:
            return None
        return self.deferred_proceeds - self.deferred_cost

    @property
    def indexed_cost(self) -> Decimal | None:
        if self.cost_base is None or self.factor is None:
            return self.cost_base
        return (self.cost_base * self.factor).quantize(CENTS)

    @property
    def indexed_gain(self) -> Decimal | None:
        """The gain from 1 July 2027, over the indexed cost base. Indexing
        cannot make a loss: a loss is worked from the cost base as it was, and
        between the two there is neither (s 110-36(1A), the reduced cost base)."""
        if self.cost_base is None or self.proceeds is None or self.awaiting_cpi:
            return None
        if self.proceeds >= self.indexed_cost:
            return self.proceeds - self.indexed_cost
        if self.proceeds < self.cost_base:
            return self.proceeds - self.cost_base
        return ZERO

    @property
    def gain(self) -> Decimal | None:
        if not self.new_rules:
            if self.cost_base is None or self.proceeds is None:
                return None
            return self.proceeds - self.cost_base
        after = self.indexed_gain
        if after is None or not self.held_through_2027:
            return after
        return None if self.deferred is None else self.deferred + after

    @property
    def cost(self) -> Decimal | None:
        """What the units cost, as their holding counts it. A parcel held
        through 30 June 2027 has a cost base from its value then, so this
        differs from it; proceeds − cost − indexation is the gain (#150)."""
        if not self.held_through_2027:
            return self.cost_base
        if None in (self.deferred_cost, self.cost_base, self.deferred_proceeds):
            return None
        return self.deferred_cost + self.cost_base - self.deferred_proceeds

    @property
    def indexation(self) -> Decimal | None:
        """How far indexing lowered the gain, which can be less than it raised
        the cost base; None when the cost base was not indexed."""
        if self.factor is None or self.indexed_gain is None:
            return None
        return self.proceeds - self.cost_base - self.indexed_gain

    @property
    def discount(self) -> Decimal | None:
        """The 50% discount on this parcel's own gain, before any loss: from
        1 July 2027, on its gain to 30 June 2027 only (#150)."""
        if self.gain is None:
            return None
        halved = self.deferred if self.new_rules else self.gain
        return halved / 2 if self.discountable and halved and halved > 0 else ZERO


@dataclass
class Disposal:
    instrument: Instrument
    date: dt.date
    quantity: Decimal
    proceeds: Decimal | None  # AUD, net of sale brokerage; None without a rate
    parcels: list[ParcelUse]
    # "sale", or the corporate action whose reduction went beyond a parcel's
    # cost base: what went beyond it is a gain on the day (CGT events G1 and
    # E10), and `quantity` is the parcel's units, none of them sold (#146).
    event: str = "sale"

    @property
    def known(self) -> bool:
        """Whether every figure converts: the sale and each parcel it used had
        a rate. One that does not is listed but left out of the totals."""
        return self.proceeds is not None and all(p.gain is not None for p in self.parcels)

    @property
    def cost_base(self) -> Decimal | None:
        if any(p.cost_base is None for p in self.parcels):
            return None
        return sum((p.cost_base for p in self.parcels), ZERO)

    @property
    def cost(self) -> Decimal | None:
        """What its units cost, as their holding counts it (`ParcelUse.cost`)."""
        if any(p.cost is None for p in self.parcels):
            return None
        return sum((p.cost for p in self.parcels), ZERO)

    @property
    def gain(self) -> Decimal | None:
        return sum((p.gain for p in self.parcels), ZERO) if self.known else None

    @property
    def deferred_proceeds(self) -> Decimal | None:
        """What its parcels held through 30 June 2027 were worth then (#150);
        None when it had none, or one could not be valued."""
        held = [p for p in self.parcels if p.held_through_2027]
        if not held or any(p.deferred_proceeds is None for p in held):
            return None
        return sum((p.deferred_proceeds for p in held), ZERO)

    @property
    def indexation(self) -> Decimal | None:
        """How far indexing lowered its gain; None when none of it was indexed."""
        indexed = [p.indexation for p in self.parcels if p.factor is not None]
        if not indexed or None in indexed:
            return None
        return sum(indexed, ZERO)


@dataclass
class _Share:
    """An amount a parcel's units owe between them, billed by running total as
    they go so the pieces add up to the whole (see `_Parcel`)."""

    total: Decimal | None
    allotted: Decimal = ZERO

    @property
    def held(self) -> Decimal | None:
        return None if self.total is None else self.total - self.allotted

    def bill(self, consumed: Decimal, quantity: Decimal) -> Decimal | None:
        if self.total is None:
            return None
        owed = (self.total * consumed / quantity).quantize(CENTS)
        billed = owed - self.allotted
        self.allotted = owed
        return billed

    def restart(self, change: Decimal | None = ZERO) -> Decimal:
        """Begin again from what is held, moved by `change` (None: unknown).
        Returns how far that took it below nil, and it is then nil."""
        held = self.held
        self.total = None if held is None or change is None else held + change
        self.allotted = ZERO
        if self.total is not None and self.total < 0:
            beyond, self.total = -self.total, ZERO
            return beyond
        return ZERO


@dataclass
class _Deemed:
    """A parcel held through 30 June 2027, as taken to be sold just before
    1 July 2027 and bought again on it (ITAA 1997 s 112-155)."""

    value: _Share   # its market value then: the deferred gain's proceeds
    cost: _Share    # its cost base then: the deferred gain's cost
    after: _Share   # its cost base from 1 July 2027: that value, moved by later changes


@dataclass
class _Billed:
    """What a parcel's units owe as they go: the cost (what was paid), and for
    a parcel held through 30 June 2027 its three 2027 amounts."""

    cost: Decimal | None
    value: Decimal | None = None
    deferred_cost: Decimal | None = None
    after: Decimal | None = None


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
    native: Decimal = ZERO     # the same outlay in the instrument's own currency
    trade_id: int | None = None
    deemed: _Deemed | None = None
    # The amounts in the cost base from 1 July 2027, as (when, AUD, indexed):
    # what its indexation factor is weighed over (#150). None: one is unknown.
    spent: list[tuple[dt.date, Decimal, bool]] | None = field(default_factory=list)

    @property
    def cost_held(self) -> Decimal | None:
        """The AUD cost of the units still held: what has not been billed yet."""
        return None if self.cost is None else self.cost - self.cost_allotted

    @property
    def native_held(self) -> Decimal:
        return self.native * self.remaining / self.quantity

    def restart(self, units: Decimal, native: Decimal = ZERO,
                cost: Decimal | None = ZERO, on: dt.date | None = None) -> Decimal:
        """Begin again from what is still held, as `units` of it, with its cost
        moved by `native` and `cost`, the AUD change (None: unknown) made on
        `on`. The date stays, so the discount clock does. Returns how far the
        cost base went below nil, which is a capital gain, and it is then nil:
        from 1 July 2027 a deemed parcel's cost base is the one from then."""
        held = self.cost_held
        share = self.remaining / self.quantity
        self.native = max(self.native_held + native, ZERO)
        self.cost = None if held is None or cost is None else held + cost
        self.quantity = self.remaining = units
        self.cost_allotted = ZERO
        beyond = ZERO
        if self.cost is not None and self.cost < 0:
            beyond, self.cost = -self.cost, ZERO
        if self.spent is not None:
            self.spent = [(when, amount * share, indexed)
                          for when, amount, indexed in self.spent]
            if cost is None:
                self.spent = None
            elif cost and on is not None and on >= NEW_RULES:
                # What is spent is indexed; what comes back is not (#150).
                self.spent.append((on, cost, cost > 0))
        if self.deemed is None:
            return beyond
        self.deemed.value.restart()
        self.deemed.cost.restart()
        return self.deemed.after.restart(cost)

    def absorb(self, other: _Parcel) -> None:
        """Take on the cost of a parcel a consolidation left without a unit."""
        self.native += other.native
        self.cost = None if self.cost is None or other.cost is None else self.cost + other.cost
        if self.spent is not None:
            self.spent = None if other.spent is None else self.spent + other.spent
        if self.deemed is not None and other.deemed is not None:
            for mine, theirs in ((self.deemed.value, other.deemed.value),
                                 (self.deemed.cost, other.deemed.cost),
                                 (self.deemed.after, other.deemed.after)):
                mine.total = (None if mine.total is None or theirs.held is None
                              else mine.total + theirs.held)

    def take(self, units: Decimal) -> _Billed:
        """Consume `units` from this parcel and bill them their cost (None when
        it is not known), and their 2027 amounts if it has them."""
        self.remaining -= units
        consumed = self.quantity - self.remaining
        billed = _Billed(None)
        if self.cost is not None:
            owed = (self.cost * consumed / self.quantity).quantize(CENTS)
            billed.cost = owed - self.cost_allotted
            self.cost_allotted = owed
        if self.deemed is not None:
            billed.value = self.deemed.value.bill(consumed, self.quantity)
            billed.deferred_cost = self.deemed.cost.bill(consumed, self.quantity)
            billed.after = self.deemed.after.bill(consumed, self.quantity)
        return billed


@dataclass(frozen=True)
class Oversold:
    """A sale of more units than were held at the time.

    The trade forms and the importer refuse one (`queries.balance_breach`), but
    a ledger written before they did can hold one. What it sold has no parcel
    to match, and neither does any later sale of the holding.
    """

    instrument: Instrument
    trade: Trade
    held: Decimal  # units held just before it

    def sentence(self) -> str:
        return (f"{self.instrument.ticker}'s sell of {queries._units(self.trade.quantity)} "
                f"units on {self.trade.date.isoformat()} is more than the "
                f"{queries._units(self.held)} units held then.")


class OversoldError(ValueError):
    """An `Oversold` sale. Matching only the parcels there are would understate
    the gain, so the engine stops instead of guessing at the missing one."""

    def __init__(self, sale: Oversold):
        super().__init__(f"{sale.instrument.ticker}: sell of {sale.trade.quantity} on "
                         f"{sale.trade.date} exceeds held parcels")
        self.sale = sale


@dataclass
class Walk:
    disposals: list[Disposal]
    held: list[_Parcel]        # what is left after every sale, oldest first


def instrument_disposals(inst: Instrument, fx_book=None,
                         until: dt.date | None = None) -> list[Disposal]:
    """This instrument's sales, each matched FIFO to its parcels: `walk`."""
    return walk(inst, fx_book, until).disposals


def walk(inst: Instrument, fx_book=None, until: dt.date | None = None) -> Walk:
    """FIFO-match this instrument's sells against its acquisition parcels, and
    keep what is left: the cost of the units still held, which is what a
    holding's unrealised gain is measured from (decisions.md #54).

    `fx_book` is a `queries.FxBook` used to resolve a trade whose own
    `fx_rate` was never captured. Without one — the direct call in a test —
    only the rates captured on the trades are known. A parcel or a sale with
    no rate keeps its units in the matching, and its AUD figures are None.
    Every session-taking entry point here passes one.

    `until` ends the walk after that day, so a later `Oversold` sale does not
    stop a year that sold nothing after it.
    """
    fifo: list[_Parcel] = []
    disposals: list[Disposal] = []
    # Timeline order, not entry order: two parcels bought on one day at
    # different prices must be consumed in the order they were acquired, or a
    # later sale is matched against the wrong cost base.
    deemed = False
    for t in queries.timeline(inst.trades, inst.actions):
        if until is not None and t.date > until:
            break
        if not deemed and t.date >= NEW_RULES:
            _deem(fifo, inst, fx_book)
            deemed = True
        if isinstance(t, CorporateAction):
            disposals += _act(t, fifo, inst, fx_book)
            continue
        fx = _rate(t, inst, fx_book)
        if t.type in ("buy", "drp"):
            outlay = t.quantity * t.unit_price + t.brokerage
            cost = None if fx is None else outlay * fx
            fifo.append(
                _Parcel(
                    acquired=t.date,
                    remaining=t.quantity,
                    quantity=t.quantity,
                    cost=cost,
                    native=outlay,
                    trade_id=t.id,
                    # Bought from 1 July 2027: indexed from its own quarter.
                    spent=(None if cost is None else
                           [(t.date, cost, True)] if t.date >= NEW_RULES else []),
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
            billed = parcel.take(take)
            use = ParcelUse(
                acquired=parcel.acquired,
                quantity=take,
                cost_base=billed.cost,
                proceeds=ZERO,   # apportioned below, once they are all known
                discountable=t.date > _one_year_after(parcel.acquired),
            )
            if t.date >= NEW_RULES:
                _split(use, parcel, billed, t.date, fx_book)
            uses.append(use)
            remaining -= take
            if parcel.remaining <= 0:
                fifo.pop(0)
        if remaining > 0:
            raise OversoldError(Oversold(inst, t, held=t.quantity - remaining))

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
    # Held past 1 July 2027 with nothing since: what is held is still deemed,
    # for whatever sells it later.
    if not deemed and (until or clock.today()) >= NEW_RULES:
        _deem(fifo, inst, fx_book)
    return Walk(disposals, fifo)


def _deem(fifo: list[_Parcel], inst, fx_book) -> None:
    """Every parcel held through 30 June 2027, taken to be sold just before
    1 July 2027 for its market value and bought again on it (ITAA 1997
    s 112-155): its units at the last close in the week before, at that day's
    rate. Unknown without one, which the year names (#150)."""
    eve = NEW_RULES - dt.timedelta(days=1)
    close = fx_book.close_before(inst.id, NEW_RULES) if fx_book is not None else None
    rate = fx_book.rate(inst.currency, eve, inst.ticker) if fx_book is not None else None
    for parcel in fifo:
        parcel.restart(parcel.remaining)
        value = None if close is None or rate is None else parcel.remaining * close * rate
        parcel.deemed = _Deemed(value=_Share(value), cost=_Share(parcel.cost_held),
                                after=_Share(value))
        parcel.spent = None if value is None else [(NEW_RULES, value, True)]


def _split(use: ParcelUse, parcel: _Parcel, billed: _Billed, sold_on: dt.date,
           fx_book) -> None:
    """A sale from 1 July 2027: the parcel's deferred gain to then, if it was
    held through 30 June 2027, and its cost base from then, indexed if it was
    acquired a year before the sale, counting from the real acquisition
    (s 114-10(9))."""
    use.new_rules = True
    if parcel.deemed is not None:
        use.held_through_2027 = True
        use.deferred_proceeds, use.deferred_cost = billed.value, billed.deferred_cost
        use.cost_base = billed.after
    if use.discountable:
        use.factor, use.awaiting_cpi = _indexation(parcel, sold_on, fx_book)


def as_if_sold(parcel: _Parcel, value: Decimal | None, on: dt.date, fx_book) -> ParcelUse:
    """A parcel still held, worked out as if sold whole on `on` for `value`
    (AUD), by the rules of that day: what a sale would realise."""
    use = ParcelUse(acquired=parcel.acquired, quantity=parcel.remaining,
                    cost_base=parcel.cost_held, proceeds=value,
                    discountable=on > _one_year_after(parcel.acquired))
    if on >= NEW_RULES:
        deemed = parcel.deemed
        billed = _Billed(parcel.cost_held) if deemed is None else _Billed(
            parcel.cost_held, deemed.value.held, deemed.cost.held, deemed.after.held)
        _split(use, parcel, billed, on, fx_book)
    return use


def _indexation(parcel: _Parcel, sold_on: dt.date,
                fx_book) -> tuple[Decimal | None, dt.date | None]:
    """The factor a parcel's cost base from 1 July 2027 is indexed by: each
    amount spent by its own quarter's, weighed by the amounts, and what came
    back not at all. With it, the quarter whose CPI is missing, if one is."""
    if parcel.spent is None:
        return None, None
    book = fx_book.cpi if fx_book is not None else None
    weighed = total = ZERO
    for when, amount, indexed in parcel.spent:
        factor = Decimal(1)
        if indexed:
            factor = None if book is None else book.factor(when, sold_on)
            if factor is None:
                missing = (cpi.quarter(sold_on) if book is None or book.index(sold_on) is None
                           else cpi.quarter(when))
                return None, missing
        weighed += amount * factor
        total += amount
    if total <= 0:
        return None, None
    return weighed / total, None


def _act(action: CorporateAction, fifo: list[_Parcel], inst: Instrument,
         fx_book) -> list[Disposal]:
    """A corporate action on the parcels held on its day (decisions.md #146).

    A split shares the holding's new units between them in proportion, with
    their costs and dates as they were. A return of capital lowers each one's
    cost by its share of the units, and an AMIT adjustment raises or lowers it;
    whatever takes a cost below nil is a gain on the day, as a Disposal."""
    held = sum((p.remaining for p in fifo), ZERO)
    if not held:
        return []
    if action.kind == "split":
        total, seen, given = queries.split_units(held, action), ZERO, ZERO
        for i, parcel in enumerate(fifo):
            seen += parcel.remaining
            share = (total if i == len(fifo) - 1
                     else (total * seen / held).quantize(queries.UNIT)) - given
            given += share
            parcel.restart(share)
        # A parcel too small to keep a unit after a consolidation gives its
        # cost to the next one, so none of the cost is lost.
        for i, parcel in reversed(list(enumerate(fifo))):
            if not parcel.remaining and len(fifo) > 1:
                into = fifo[i + 1] if i + 1 < len(fifo) else fifo[i - 1]
                into.absorb(parcel)
                fifo.pop(i)
        return []
    # Paid back, the cash lowers the cost base; an adjustment carries its sign.
    change = -action.amount if action.kind == "capital_return" else action.amount
    rate = _rate(action, inst, fx_book)
    gains, seen, given = [], ZERO, ZERO
    for parcel in fifo:
        seen += parcel.remaining
        part = change * seen / held - given
        given += part
        # A reduction with no AUD figure on either side may or may not go
        # below nil: listed unpriced, so the year names it, as a sale (#5).
        base = parcel.deemed.after.held if parcel.deemed is not None else parcel.cost_held
        unpriced = part < 0 and (rate is None or base is None)
        beyond = parcel.restart(parcel.remaining, part, None if rate is None else part * rate,
                                on=action.date)
        if beyond or unpriced:
            gain = beyond.quantize(CENTS) if beyond else None
            gains.append(Disposal(
                instrument=inst, date=action.date, quantity=parcel.remaining, proceeds=gain,
                parcels=[ParcelUse(acquired=parcel.acquired, quantity=parcel.remaining,
                                   cost_base=None if unpriced else ZERO, proceeds=gain,
                                   discountable=action.date > _one_year_after(parcel.acquired),
                                   new_rules=action.date >= NEW_RULES)],
                event=action.kind))
    return gains


@dataclass
class CgtSummary:
    disposals: list[Disposal] = field(default_factory=list)
    # Gross gains on >12m parcels, and the rest. From 1 July 2027 these are the
    # deferred gains, to 30 June 2027, and the gains after it are apart:
    # indexed where the parcel was held a year, and never discounted (#150).
    gains_discountable: Decimal = ZERO
    gains_other: Decimal = ZERO
    gains_from_july_2027: Decimal = ZERO
    losses: Decimal = ZERO  # positive number
    losses_applied_other: Decimal = ZERO
    losses_applied_discountable: Decimal = ZERO
    losses_applied_from_july_2027: Decimal = ZERO
    discount: Decimal = ZERO
    net_capital_gain: Decimal = ZERO
    net_capital_loss: Decimal = ZERO  # >0 when the FY nets to a loss
    # Earlier years' net capital losses: what was left at the start of this
    # year, what this year used of it, and what goes on to the next, this
    # year's own loss included (s102-5(1)).
    losses_carried_in: Decimal = ZERO
    losses_carried_applied: Decimal = ZERO
    losses_carried_out: Decimal = ZERO
    # The gains from 1 July 2027 left after the losses: what the 30% minimum
    # tax is worked on (ITAA 1997 s 119-5). Nil before 1 July 2027.
    minimum_tax_gain: Decimal = ZERO
    # Holdings with a sale this year that has no exchange rate behind it: the
    # sale is listed, and every figure above leaves it out (decisions.md #5).
    withheld: list[str] = field(default_factory=list)
    # The same, for want of the CPI of a quarter not published yet, or of a
    # close in the week before 1 July 2027 to value a parcel at (#150).
    awaiting_cpi: list[str] = field(default_factory=list)
    unvalued: list[str] = field(default_factory=list)
    # A sale of more than was held, which this year's gains depend on. When
    # there is one, every figure above is zero: the year's gains are refused,
    # not worked out without it, and the sale is named for somebody to fix.
    oversold: list[Oversold] = field(default_factory=list)


def cgt_instruments(session: Session) -> list[Instrument]:
    """Every instrument with its trades and actions: what `fy_cgt` walks."""
    return session.scalars(
        select(Instrument).options(selectinload(Instrument.trades),
                                   selectinload(Instrument.actions))
    ).unique().all()


def fy_cgt(session: Session, fy_end_year: int, fx_book=None,
           instruments=None) -> CgtSummary:
    """This financial year's capital gains, with every earlier year's net
    capital loss carried in: each year is worked out in turn from the first
    sale, so a loss left over reaches the years after it (decisions.md #77).

    `instruments` replaces what is walked: the sale simulator passes stand-ins
    carrying a sell that is never recorded (decisions.md #148)."""
    if instruments is None:
        instruments = cgt_instruments(session)
    fx_book = fx_book or queries.FxBook(session)
    sales = [t.date for inst in instruments for t in inst.trades if t.type == "sell"]
    first = min(current_fy(min(sales)), fy_end_year) if sales else fy_end_year
    carried = ZERO
    left_out = {"withheld": set(), "awaiting_cpi": set(), "unvalued": set()}
    for year in range(first, fy_end_year + 1):
        out = _year_cgt(instruments, year, fx_book, carried)
        if out.oversold:
            # Refused, and so is every year after it: whether it made a loss
            # to carry on is not known.
            return out
        carried = out.losses_carried_out
        if year < fy_end_year:
            for name, tickers in left_out.items():
                tickers.update(getattr(out, name))
    # A sale left out of an earlier year's figures is left out of the loss it
    # carried too.
    for name, tickers in left_out.items():
        setattr(out, name, sorted(set(getattr(out, name)) | tickers))
    return out


def _year_cgt(instruments, fy_end_year: int, fx_book, carried: Decimal) -> CgtSummary:
    start, end = fy_bounds(fy_end_year)
    out = CgtSummary(losses_carried_in=carried)
    for inst in instruments:
        # A sale, or an action that can take a cost base below nil (#146).
        if not (any(t.type == "sell" and start <= t.date <= end for t in inst.trades)
                or any(a.kind != "split" and start <= a.date <= end for a in inst.actions)):
            continue
        try:
            found = instrument_disposals(inst, fx_book, until=end)
        except OversoldError as err:
            out.oversold.append(err.sale)
            continue
        for d in found:
            if not (start <= d.date <= end):
                continue
            out.disposals.append(d)
            if not d.known:
                missing = (out.awaiting_cpi if any(p.awaiting_cpi for p in d.parcels) else
                           out.unvalued if any(p.held_through_2027 and p.deferred_proceeds is None
                                               for p in d.parcels) else out.withheld)
                if inst.ticker not in missing:
                    missing.append(inst.ticker)
                continue
            for p in d.parcels:
                for amount, bucket in _parts(p):
                    if amount >= 0:
                        name = f"gains_{bucket}"
                        setattr(out, name, getattr(out, name) + amount)
                    else:
                        out.losses += -amount
    if out.oversold:
        # Refused: gains without the oversold holding are not the year's gains.
        return CgtSummary(oversold=sorted(out.oversold, key=lambda sale: sale.trade.date))
    # This year's losses reduce its gains with no discount first, then the
    # discountable ones, then those from 1 July 2027; earlier years' losses come
    # next, in the same order; and only then is the discount applied. Before
    # 1 July 2027 that is the taxpayer-favourable order of s102-5(1), and from
    # it the order the amended s102-5(1) sets, the deferred gains first (#150).
    order = ("other", "discountable", "from_july_2027")
    left = {b: getattr(out, f"gains_{b}") for b in order}
    rest = out.losses
    for b in order:
        used = min(rest, left[b])
        setattr(out, f"losses_applied_{b}", used)
        left[b] -= used
        rest -= used
    unapplied, carry = rest, carried
    for b in order:
        used = min(carry, left[b])
        left[b] -= used
        carry -= used
    out.losses_carried_applied = carried - carry
    out.discount = left["discountable"] / 2
    if unapplied > 0:
        out.net_capital_loss = unapplied
    else:
        out.net_capital_gain = (left["other"] + left["discountable"] - out.discount
                                + left["from_july_2027"])
        out.minimum_tax_gain = left["from_july_2027"]
    out.losses_carried_out = carried - out.losses_carried_applied + unapplied
    out.disposals.sort(key=lambda d: d.date)
    for names in (out.withheld, out.awaiting_cpi, out.unvalued):
        names.sort()
    return out


def _parts(use: ParcelUse) -> list[tuple[Decimal, str]]:
    """A known use's gains or losses, each with the bucket it goes in: before
    1 July 2027 one, discountable or not; from it, the gain to 30 June 2027
    if the parcel was held then, deferred and discountable as before, and the
    indexed gain after (#150)."""
    if not use.new_rules:
        return [(use.gain, "discountable" if use.discountable else "other")]
    parts = []
    if use.held_through_2027:
        parts.append((use.deferred, "discountable" if use.discountable else "other"))
    parts.append((use.indexed_gain, "from_july_2027"))
    return parts


# ---------- dividend / franking income ----------


@dataclass
class IncomeRow:
    instrument: Instrument
    cash: Decimal | None  # None when a dividend this FY has no exchange rate
    franking: Decimal
    franking_missing: int  # dividends this FY with no franking data yet
    # AUD foreign tax withheld, which is income as well (decisions.md #145).
    # None, as `cash` is, when a dividend this FY has no exchange rate.
    withholding: Decimal | None = ZERO

    @property
    def grossed_up(self) -> Decimal | None:
        if self.cash is None or self.withholding is None:
            return None
        return self.cash + self.franking + self.withholding


def fy_income(session: Session, fy_end_year: int, fx_book=None) -> list[IncomeRow]:
    start, end = fy_bounds(fy_end_year)
    rows: list[IncomeRow] = []
    instruments = session.scalars(
        select(Instrument).options(selectinload(Instrument.dividends))
    ).unique().all()
    fx_book = fx_book or queries.FxBook(session)
    for inst in instruments:
        divs = [d for d in inst.dividends if start <= income_date(d) <= end]
        if not divs:
            continue
        rates = [_rate(d, inst, fx_book) for d in divs]
        cash = (None if None in rates else
                sum((d.cash_amount * rate for d, rate in zip(divs, rates)), ZERO))
        withheld = (None if None in rates else
                    sum(((d.withholding_tax or ZERO) * rate for d, rate in zip(divs, rates)),
                        ZERO))
        franking = sum((d.franking_credits or ZERO for d in divs), ZERO)
        rows.append(
            IncomeRow(
                instrument=inst,
                cash=cash,
                franking=franking,
                franking_missing=sum(1 for d in divs if d.franking_credits is None),
                withholding=withheld,
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
    gain_aud: Decimal | None  # unrealised: value less `cost_base`
    # AUD, what the units held then cost, the oldest sold first (decisions.md
    # #54). None without a rate, or for a sale of more than was held.
    cost_base: Decimal | None = None

    @property
    def gain_pct(self) -> Decimal | None:
        """Unrealised gain as a fraction of what the units held cost.

        Derived rather than stored, so it cannot drift from the two numbers it
        comes from.

        **None covers two situations and the template tells them apart**: a
        cost base of nothing, so a percentage cannot exist — renders **N/A**;
        or an unpriced holding, where it is computable in principle and simply
        not known yet — renders an em dash. Returning zero would claim the
        holding broke even, which is a statement about the data rather than
        its absence.
        """
        if self.gain_aud is None or not self.cost_base:
            return None
        return self.gain_aud / self.cost_base

    @property
    def percentage_applies(self) -> bool:
        """Whether a gain percentage is a meaningful thing to ask for here.
        A cost base that is unknown for want of a rate still has one."""
        return self.cost_base is None or bool(self.cost_base)


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
        select(Instrument).options(selectinload(Instrument.trades),
                                   selectinload(Instrument.actions))
    ).unique().all()

    snapshot: list[SnapshotRow] = []
    activity = FyActivity()
    fx_book = queries.FxBook(session)
    # Holdings some AUD figure on this page leaves out for want of a rate,
    # named once at the top of the page (decisions.md #5).
    withheld: set[str] = set()
    # Held at the as-of date with a sale of more than was held behind them.
    oversold: set[str] = set()
    for inst in instruments:
        units = ZERO
        invested: Decimal | None = ZERO
        for t in queries.timeline(inst.trades, inst.actions):
            if t.date > asof:
                continue
            if isinstance(t, CorporateAction):
                if t.kind == "split":
                    units = queries.split_units(units, t)
                continue
            fx = _rate(t, inst, fx_book)
            in_fy = start <= t.date <= end
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
        # What the units held then cost, from the same FIFO walk as the gains.
        try:
            held = walk(inst, fx_book, until=asof).held
        except OversoldError:
            held = None
            oversold.add(inst.ticker)
        cost_base = (None if held is None or any(p.cost_held is None for p in held)
                     else sum((p.cost_held for p in held), ZERO))
        price_row = _price_at(session, inst.id, asof)
        price, price_date = (price_row.close, price_row.date) if price_row else (None, None)
        # The stored rate nearest the date, and no value at all for a currency
        # with none: never 1:1 (decisions.md #5).
        # Named only for a value it converts: with no price there is none.
        rate = fx_book.rate(inst.currency, asof, inst.ticker if price is not None else None)
        if (cost_base is None and held is not None) or (price is not None and rate is None):
            withheld.add(inst.ticker)
        value = gain = None
        if price is not None and rate is not None:
            value = units * price * rate
            gain = None if cost_base is None else value - cost_base
        snapshot.append(
            SnapshotRow(
                instrument=inst,
                units=units,
                invested_cum=invested,
                price=price,
                price_date=price_date,
                value_aud=value,
                gain_aud=gain,
                cost_base=cost_base,
            )
        )

    income = fy_income(session, fy_end_year, fx_book)
    cgt = fy_cgt(session, fy_end_year, fx_book)
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
        "total_cost_base": sum((r.cost_base for r in snapshot
                                if r.cost_base is not None), ZERO),
        "oversold_held": sorted(oversold),
        "activity": activity,
        "income": income,
        "income_cash": sum((r.cash for r in income if r.cash is not None), ZERO),
        "income_franking": sum((r.franking for r in income), ZERO),
        "income_withholding": sum((r.withholding for r in income
                                   if r.withholding is not None), ZERO),
        "franking_missing": sum(r.franking_missing for r in income),
        "cgt": cgt,
        "withheld": sorted(withheld),
        # From 1 July 2027 (#150): the year's rules, and the sales its gains
        # leave out for want of a quarter's CPI or of a June 2027 close.
        "new_rules": start >= NEW_RULES,
        "awaiting_cpi": cgt.awaiting_cpi,
        "unvalued": cgt.unvalued,
        # Converted at a rate more than a week from the day: a hole in the
        # stored series, or one that does not reach back that far (#124).
        "far": sorted(fx_book.far),
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
        "cost_base": lambda r: r.cost_base,
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
        "withholding": lambda r: r.withholding,
    },
    "disposals": {
        "date": lambda d: d.date,
        "ticker": lambda d: d.instrument.ticker,
        "quantity": lambda d: d.quantity,
        "cost_base": lambda d: d.cost,
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
