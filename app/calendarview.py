"""Month calendar: planned buys and dividend dates, past and expected.

*Upcoming* distribution dates aren't in any free feed, so expected ones are
projected: take the gaps between past payments, use the median, and step
forward from the last one. Vanguard's quarterly cycle shows up as ~91 days and
lands within a few days of the real date.

The history comes from `market_dividend` — the security's own published record,
fetched by the price feed — falling back to the holder's recorded payments.
That ordering matters: a holding bought last month has no personal history to
find a cycle in, but the ETF has been paying quarterly for a decade. Nothing is
projected for a position that's been sold, two payments are the minimum, and
every projected entry is labelled "expected" while recorded ones aren't.
"""

from __future__ import annotations

import calendar as _calendar
import datetime as dt
import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import clock, plans
from .models import Dividend, Instrument, MarketDividend, Trade

# Beyond this the projection is noise rather than a forecast.
PROJECT_AHEAD_DAYS = 400
MIN_HISTORY = 2


@dataclass
class CalEvent:
    date: dt.date
    kind: str  # buy | buy-planned | sell | dividend | dividend-expected
    ticker: str
    detail: str = ""
    amount: Decimal | None = None
    # Which currency `amount` is in. Recorded trades and distributions are in
    # the instrument's own currency; a scheduled buy is the plan's amount,
    # which is in the reporting currency. None means the reporting one, so
    # this list cannot render a US dividend with an AUD symbol.
    currency: str | None = None

    @property
    def expected(self) -> bool:
        return self.kind.endswith("-expected") or self.kind.endswith("-planned")


@dataclass
class Day:
    date: dt.date
    in_month: bool
    is_today: bool
    events: list[CalEvent] = field(default_factory=list)


def _median_gap(dates: list[dt.date]) -> int | None:
    if len(dates) < MIN_HISTORY:
        return None
    gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 20]
    if not gaps:
        return None
    return int(statistics.median(gaps))


def projected_dividends(session: Session, until: dt.date) -> list[CalEvent]:
    """Next expected payment(s) per still-held instrument, up to `until`."""
    today = clock.today()
    out: list[CalEvent] = []
    instruments = (
        session.scalars(
            select(Instrument)
            .options(selectinload(Instrument.dividends), selectinload(Instrument.trades))
            .order_by(Instrument.ticker)
        )
        .unique()
        .all()
    )
    # One query for every instrument's market history, rather than one each.
    market: dict[int, list[dt.date]] = {}
    for iid, ex_date in session.execute(
        select(MarketDividend.instrument_id, MarketDividend.ex_date).order_by(
            MarketDividend.ex_date
        )
    ):
        market.setdefault(iid, []).append(ex_date)

    for inst in instruments:
        units = sum(
            (t.quantity if t.type in ("buy", "drp") else -t.quantity for t in inst.trades),
            Decimal(0),
        )
        if units <= 0:  # sold out — nothing to pay
            continue
        own = sorted({d.date for d in inst.dividends})
        published = market.get(inst.id, [])
        # The security's own record first: it sees the cycle even when this
        # holder has only been on the register a month.
        dates = published if len(published) >= MIN_HISTORY else own
        gap = _median_gap(dates)
        if gap is None:
            continue
        # Amount per unit: the published figure when we have one, else derived
        # from this holder's last payment. Scaled to units held now — an
        # estimate, and shown as one.
        per_unit = None
        if published:
            rows = session.execute(
                select(MarketDividend.amount)
                .where(MarketDividend.instrument_id == inst.id)
                .order_by(MarketDividend.ex_date.desc())
                .limit(1)
            ).scalar()
            per_unit = rows
        if per_unit is None and inst.dividends:
            last = max(inst.dividends, key=lambda d: d.date)
            prior_units = sum(
                (
                    t.quantity if t.type in ("buy", "drp") else -t.quantity
                    for t in inst.trades
                    if t.date <= last.date
                ),
                Decimal(0),
            )
            per_unit = (last.cash_amount / prior_units) if prior_units > 0 else None

        nxt = dates[-1] + dt.timedelta(days=gap)
        while nxt <= until:
            if nxt >= today:
                out.append(
                    CalEvent(
                        date=nxt,
                        kind="dividend-expected",
                        ticker=inst.ticker,
                        detail=(
                            f"expected distribution (~{gap}-day cycle"
                            + (", published history)" if published else ", your history)")
                        ),
                        amount=(per_unit * units) if per_unit is not None else None,
                        currency=inst.currency,
                    )
                )
            nxt += dt.timedelta(days=gap)
    return out


def recorded_events(session: Session, start: dt.date, end: dt.date) -> list[CalEvent]:
    """Trades and dividends that actually happened in the window."""
    out: list[CalEvent] = []
    trades = session.scalars(
        select(Trade)
        .where(Trade.date >= start, Trade.date <= end)
        .options(selectinload(Trade.instrument))
    ).all()
    for t in trades:
        out.append(
            CalEvent(
                date=t.date,
                kind="sell" if t.type == "sell" else "buy",
                ticker=t.instrument.ticker,
                detail=f"{t.type} {t.quantity.normalize():f} @ {t.unit_price}",
                amount=t.quantity * t.unit_price,
                currency=t.instrument.currency,
            )
        )
    dividends = session.scalars(
        select(Dividend)
        .where(Dividend.date >= start, Dividend.date <= end)
        .options(selectinload(Dividend.instrument))
    ).all()
    for d in dividends:
        out.append(
            CalEvent(
                date=d.date,
                kind="dividend",
                ticker=d.instrument.ticker,
                detail="reinvested" if d.reinvest_trade_id else "paid",
                amount=d.cash_amount,
                currency=d.instrument.currency,
            )
        )
    return out


def planned_buys(session: Session, until: dt.date) -> list[CalEvent]:
    """Upcoming rotation slots as far as `until`."""
    sched = plans.schedule(session, upcoming=40)
    out = []
    for buy in sched["next"]:
        if buy.due_date > until:
            break
        out.append(
            CalEvent(
                date=buy.due_date,
                kind="buy-planned",
                ticker=buy.ticker,
                detail="scheduled buy",
                amount=buy.amount,
            )
        )
    return out


def month_grid(session: Session, year: int, month: int) -> dict:
    """A Monday-first month view with every event that touches those weeks."""
    first = dt.date(year, month, 1)
    last = dt.date(year, month, _calendar.monthrange(year, month)[1])
    grid_start = first - dt.timedelta(days=first.weekday())
    grid_end = last + dt.timedelta(days=6 - last.weekday())
    today = clock.today()

    events = recorded_events(session, grid_start, grid_end)
    events += [e for e in planned_buys(session, grid_end) if e.date >= grid_start]
    events += [
        e
        for e in projected_dividends(session, grid_end)
        if grid_start <= e.date <= grid_end
    ]

    by_date: dict[dt.date, list[CalEvent]] = {}
    for event in events:
        by_date.setdefault(event.date, []).append(event)
    for day_events in by_date.values():
        day_events.sort(key=lambda e: (e.expected, e.kind, e.ticker))

    weeks: list[list[Day]] = []
    day = grid_start
    while day <= grid_end:
        week = []
        for _ in range(7):
            week.append(
                Day(
                    date=day,
                    in_month=(day.month == month),
                    is_today=(day == today),
                    events=by_date.get(day, []),
                )
            )
            day += dt.timedelta(days=1)
        weeks.append(week)

    prev_month = (first - dt.timedelta(days=1)).replace(day=1)
    next_month = (last + dt.timedelta(days=1)).replace(day=1)
    horizon = today + dt.timedelta(days=PROJECT_AHEAD_DAYS)
    return {
        "weeks": weeks,
        "month_label": first.strftime("%B %Y"),
        "year": year,
        "month": month,
        "prev": (prev_month.year, prev_month.month),
        "next": (next_month.year, next_month.month),
        "today": today,
        "upcoming": sorted(
            (
                e
                for e in planned_buys(session, horizon)
                + projected_dividends(session, horizon)
                if e.date >= today
            ),
            key=lambda e: e.date,
        )[:12],
    }
