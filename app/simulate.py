"""Selling today, worked out and not recorded: what a sale of some units of
each holding would bring home after capital gains tax (decisions.md #148).

The sale goes through the same FIFO walk and the same netting of the year as
the FY report, over each holding's own trades plus one sell, dated today at the
latest price and today's rate. The sells are plain objects on stand-ins for the
instruments, so nothing here ever reaches the session.

The tax is what the sale ADDS to this financial year's net capital gain, at the
income tax rate and the Medicare levy. The year's sales so far and the losses carried in are counted as
the ATO counts them, so a gain a carried loss already covers is not taxed again,
and a loss that lowers the year's gain lowers its tax.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from types import SimpleNamespace

from . import brokerage as fees
from . import clock, cpi, fyreport, queries, tenancy
from .models import Portfolio

ZERO = Decimal(0)
CENTS = Decimal("0.01")
# The least the gains from 1 July 2027 are taxed at, in percent (#151).
MINIMUM_RATE = Decimal(30)


@dataclass
class Line:
    """One holding, and what selling `units` of it would do. Money is AUD
    unless it says native."""

    holding: queries.Holding
    units: Decimal = ZERO
    brokerage: Decimal | None = None      # native: as typed, or else suggested
    suggested: Decimal | None = None      # native: what the trade form would offer
    value: Decimal | None = None
    brokerage_aud: Decimal | None = None
    cost_base: Decimal | None = None      # what was paid: `Disposal.cost`
    indexation: Decimal | None = None     # what indexing took off the gain
    gain: Decimal | None = None           # None when a parcel sold has no rate
    # Why it is not offered: any figure for it would be a guess (decisions.md #5).
    why_not: str | None = None


@dataclass
class Sale:
    lines: list[Line] = field(default_factory=list)
    income_tax_rate: Decimal | None = None
    medicare_levy: Decimal | None = None
    value: Decimal = ZERO
    brokerage: Decimal = ZERO
    cost_base: Decimal | None = ZERO
    indexation: Decimal = ZERO
    gain: Decimal | None = ZERO
    # What the sale does to this financial year. None when the year cannot be
    # worked out: a sale of more than was held.
    discount: Decimal | None = None
    losses_used: Decimal | None = None
    taxable: Decimal | None = None          # added to the year's net capital gain
    income_tax: Decimal | None = None
    levy: Decimal | None = None             # None when no levy is set
    minimum_tax: Decimal | None = None      # what it adds to the year's top-up
    tax: Decimal | None = None              # the three together
    take_home: Decimal | None = None
    year_so_far: Decimal | None = None      # the year's earlier sales, net
    carried_in: Decimal | None = None       # losses carried in from earlier years
    carried_forward: Decimal | None = None  # added to the loss carried on
    withheld: list[str] = field(default_factory=list)
    awaiting_cpi: list[str] = field(default_factory=list)
    unvalued: list[str] = field(default_factory=list)
    oversold: list = field(default_factory=list)
    # The quarter whose CPI stood in for the sale's, not published yet (#150).
    cpi_estimate: dt.date | None = None

    @property
    def selling(self) -> list[Line]:
        return [line for line in self.lines if line.units]


def why_not(holding: queries.Holding) -> str | None:
    """Why this holding cannot be sold here, or None."""
    if holding.oversold:
        return "a sale of more than was held"
    if holding.price is None:
        return "no price"
    if holding.value_aud is None:
        return "no exchange rate"
    return None


def fill(holdings: list[queries.Holding], percent: Decimal) -> dict[int, Decimal]:
    """The units of each holding that sell `percent` of the portfolio: that
    share of each, rounded down so a sale is never more than was asked for, in
    whole units of a share or a fund and a coin's to its eighth place."""
    out = {}
    for holding in holdings:
        if not holding.units or why_not(holding):
            continue
        if percent >= 100:
            out[holding.instrument.id] = holding.units
            continue
        step = queries.UNIT if holding.instrument.asset_class == "crypto" else Decimal(1)
        out[holding.instrument.id] = (holding.units * percent / 100).quantize(
            step, rounding=ROUND_DOWN)
    return out


def sale(session, units: dict[int, Decimal], *, brokerage: dict[int, Decimal] | None = None,
         income_tax_rate: Decimal | None = None, medicare_levy: Decimal | None = None,
         income_support: bool | None = None,
         holdings: list[queries.Holding] | None = None) -> Sale:
    """Selling `units` of each holding (by instrument id) today, with the
    brokerage typed for some of them, the trade form's suggestion for the rest,
    and the year's tax at the two rates, in percent. Without an income tax rate
    there is no tax figure; without a levy, none is added. The minimum tax is
    lifted by income support, the portfolio's setting unless given.
    `holdings` are the open positions, when the caller has them already."""
    today = clock.today()
    fx_book = queries.FxBook(session)
    # A sale today is in a quarter whose CPI is months away: the latest stands in.
    fx_book.cpi = cpi.CpiBook(session, estimate=True)
    portfolio = session.get(Portfolio, tenancy.current_portfolio_id(session))
    last = {False: queries.last_brokerage(session),
            True: queries.last_brokerage(session, foreign=True)}
    typed = brokerage or {}
    out = Sale(income_tax_rate=income_tax_rate, medicare_levy=medicare_levy)
    standins = {}
    if holdings is None:
        holdings = queries.split_positions(queries.all_holdings(session))[0]
    for holding in holdings:
        inst = holding.instrument
        line = Line(holding, why_not=why_not(holding))
        out.lines.append(line)
        if line.why_not or not units.get(inst.id):
            continue
        line.units = units[inst.id]
        fx = fx_book.rate(inst.currency, today)
        line.suggested = fees.for_trade(portfolio, inst.currency, line.units * holding.price,
                                        last[fees.is_foreign(inst.currency)])
        line.brokerage = typed.get(inst.id, line.suggested if line.suggested is not None
                                   else ZERO)
        # Last in the day's timeline: a sale typed in now happens after
        # anything already recorded for today.
        sell = SimpleNamespace(id=None, date=today, time=dt.time.max, type="sell",
                               quantity=line.units, unit_price=holding.price,
                               brokerage=line.brokerage, fx_rate=fx)
        standins[inst.id] = SimpleNamespace(
            id=inst.id, ticker=inst.ticker, exchange=inst.exchange, currency=inst.currency,
            trades=[*inst.trades, sell], actions=list(inst.actions))
        sold = fyreport.walk(standins[inst.id], fx_book).disposals[-1]
        line.value = (line.units * holding.price * fx).quantize(CENTS)
        line.brokerage_aud = line.value - sold.proceeds
        line.cost_base, line.indexation, line.gain = sold.cost, sold.indexation, sold.gain

    selling = out.selling
    out.value = sum((line.value for line in selling), ZERO)
    out.brokerage = sum((line.brokerage_aud for line in selling), ZERO)
    out.cost_base = _total(line.cost_base for line in selling)
    out.indexation = sum((line.indexation or ZERO for line in selling), ZERO)
    out.gain = _total(line.gain for line in selling)

    fy = fyreport.current_fy(today)
    instruments = fyreport.cgt_instruments(session)
    before = fyreport.fy_cgt(session, fy, fx_book, instruments=instruments)
    after = fyreport.fy_cgt(session, fy, fx_book,
                            instruments=[standins.get(i.id, i) for i in instruments])
    out.cpi_estimate = fx_book.cpi.estimated
    if before.oversold or after.oversold:
        out.oversold = after.oversold or before.oversold
        return out
    for name in ("withheld", "awaiting_cpi", "unvalued"):
        setattr(out, name, sorted(set(getattr(before, name)) | set(getattr(after, name))))
    out.year_so_far = (before.gains_discountable + before.gains_other
                       + before.gains_from_july_2027 - before.losses)
    out.carried_in = before.losses_carried_in
    out.taxable = after.net_capital_gain - before.net_capital_gain
    out.discount = after.discount - before.discount
    out.carried_forward = after.losses_carried_out - before.losses_carried_out
    if out.gain is not None:
        out.losses_used = out.gain - out.discount - out.taxable
    if income_tax_rate is not None:
        out.income_tax = (out.taxable * income_tax_rate / 100).quantize(CENTS)
        if medicare_levy is not None:
            out.levy = (out.taxable * medicare_levy / 100).quantize(CENTS)
        exempt = portfolio.income_support if income_support is None else income_support
        out.minimum_tax = ZERO if exempt else (
            _top_up(after.minimum_tax_gain, income_tax_rate)
            - _top_up(before.minimum_tax_gain, income_tax_rate))
        out.tax = out.income_tax + (out.levy or ZERO) + out.minimum_tax
        out.take_home = out.value - out.brokerage - out.tax
    return out


def _top_up(gain: Decimal, rate: Decimal) -> Decimal:
    """The year's minimum tax on `gain`, its gains from 1 July 2027 after
    losses: 30% of them less the income tax at `rate` on them, rounded down
    to a dollar, when that is more than nil (ITAA 1997 s 119-10). One rate
    stands for the tax the gain adds, as it does for the year's (#147)."""
    extra = gain * (MINIMUM_RATE - rate) / 100
    return extra.quantize(Decimal(1), rounding=ROUND_DOWN) if extra > 0 else ZERO


def _total(parts) -> Decimal | None:
    """The sum, or None when any part is unknown (decisions.md #5)."""
    total = ZERO
    for part in parts:
        if part is None:
            return None
        total += part
    return total
