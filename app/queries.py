"""Read-model for the UI: everything is computed from trades + prices.

The dataset is tiny (hundreds of rows), so these pull the rows and compute in
Python rather than pushing aggregation into SQL — clearer to evolve while the
model is still growing (FY reports, CGT parcels, charts all come later).
"""

from __future__ import annotations

import bisect
import datetime as dt
import json
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from . import clock
from .models import FxRate, HoldingPref, Instrument, Price, Trade, trade_order

ZERO = Decimal(0)
ONE = Decimal(1)

# What every `*_aud` figure is expressed in. Named rather than spelled "AUD" at
# each comparison so the T28b work has one place to look — `portfolio.
# reporting_currency` already exists in the schema and this is what it will
# feed. Until then the app is AUD-only and says so here rather than in a dozen
# string literals.
REPORTING_CURRENCY = "AUD"


def prefs_by_instrument(session: Session) -> dict[int, HoldingPref]:
    """This user's per-holding settings (DRP participation + private note),
    keyed by instrument id. Tenancy filtering happens in tenancy.py."""
    return {p.instrument_id: p for p in session.scalars(select(HoldingPref)).all()}


@dataclass
class Holding:
    instrument: Instrument
    units: Decimal
    avg_price: Decimal | None  # native ccy, over buys + DRP (matches the sheet)
    cost: Decimal  # native ccy, buys incl. brokerage
    dividends_cash: Decimal  # native ccy
    price: Decimal | None  # latest close, native ccy
    price_date: dt.date | None
    value: Decimal | None  # native ccy
    gain: Decimal | None  # value - cost (open positions)
    gain_pct: Decimal | None
    # AUD view: cost at trade-date fx, value at latest fx. None = fx missing.
    cost_aud: Decimal | None = None
    value_aud: Decimal | None = None
    gain_aud: Decimal | None = None
    dividends_aud: Decimal | None = None
    # The per-unit pair, converted. These exist because a USD holding's `Avg
    # price` and `Price` answer in dollars nobody here paid or received, and
    # the portfolio's own totals are in AUD — so the two columns that say what
    # ONE unit cost and is worth were the only ones with no reporting-currency
    # counterpart. See app/columns.py for the columns themselves.
    avg_price_aud: Decimal | None = None
    price_aud: Decimal | None = None
    # What the share registry is still holding: the DRP residual left by the
    # most recent distribution. **A balance, not income** — the whole
    # distribution is already assessable through `cash_amount`, so this must
    # never be added to a dividend total. None means no distribution has ever
    # recorded one, which is a different fact from a residual of zero.
    drp_residual: Decimal | None = None
    # Daily move: latest close vs the one before (sheet's "% Increase Today").
    day_change_aud: Decimal | None = None
    day_pct: Decimal | None = None
    # This user's own view of the holding (holding_pref) — never shared.
    note: str | None = None
    drp: bool = False


def _latest_price(session: Session, instrument_id: int) -> tuple[Decimal, dt.date] | None:
    row = session.execute(
        select(Price.close, Price.date)
        .where(Price.instrument_id == instrument_id)
        .order_by(Price.date.desc())
        .limit(1)
    ).first()
    return (row.close, row.date) if row else None


def _last_two_closes(session: Session, instrument_id: int) -> list[Decimal]:
    return list(
        session.execute(
            select(Price.close)
            .where(Price.instrument_id == instrument_id)
            .order_by(Price.date.desc())
            .limit(2)
        ).scalars()
    )


def latest_fx(session: Session, currency: str) -> Decimal | None:
    """AUD per 1 unit of `currency` (latest stored close); 1 for AUD."""
    if currency == "AUD":
        return ONE
    return session.execute(
        select(FxRate.rate)
        .where(FxRate.pair == f"{currency}AUD")
        .order_by(FxRate.date.desc())
        .limit(1)
    ).scalar()


def in_aud(amount: Decimal, currency: str, stored_rate: Decimal | None) -> Decimal | None:
    """`amount` converted to AUD, or None when the rate is genuinely unknown.

    A same-currency row needs no stored rate; a foreign one with no rate is
    still withheld. Both halves matter — decisions.md #5 and #6.
    """
    if currency == REPORTING_CURRENCY:
        return amount
    if stored_rate is None:
        return None
    return amount * stored_rate


def build_holding(
    session: Session, inst: Instrument, pref: HoldingPref | None = None
) -> Holding:
    buys = [t for t in inst.trades if t.type == "buy"]
    accumulating = [t for t in inst.trades if t.type in ("buy", "drp")]
    sells = [t for t in inst.trades if t.type == "sell"]

    units = sum((t.quantity for t in accumulating), ZERO) - sum(
        (t.quantity for t in sells), ZERO
    )
    cost = sum((t.quantity * t.unit_price + t.brokerage for t in buys), ZERO)
    acc_units = sum((t.quantity for t in accumulating), ZERO)
    avg = (
        (sum((t.quantity * t.unit_price for t in accumulating), ZERO) / acc_units)
        if acc_units
        else None
    )
    dividends_cash = sum((d.cash_amount for d in inst.dividends), ZERO)

    latest = _latest_price(session, inst.id)
    price, price_date = latest if latest else (None, None)
    value = gain = gain_pct = None
    if price is not None and units:
        value = units * price
        gain = value - cost
        gain_pct = gain / cost if cost else None

    # AUD view: cost at each trade's own fx, value at the latest fx.
    fx_now = latest_fx(session, inst.currency)
    cost_aud = ZERO
    for t in buys:
        part = in_aud(t.quantity * t.unit_price + t.brokerage, inst.currency, t.fx_rate)
        if part is None:
            cost_aud = None
            break
        cost_aud += part
    value_aud = value * fx_now if (value is not None and fx_now is not None) else None
    gain_aud = (
        value_aud - cost_aud if (value_aud is not None and cost_aud is not None) else None
    )
    dividends_aud = ZERO
    for d in inst.dividends:
        part = in_aud(d.cash_amount, inst.currency, d.fx_rate)
        if part is None:
            dividends_aud = None
            break
        dividends_aud += part

    # The average paid per unit, each trade converted at its OWN rate and on
    # the same basis as the native `avg_price` above. Deliberately not
    # `cost_aud / units`, which is a third number — decisions.md #50.
    avg_price_aud = None
    if acc_units:
        paid_aud = ZERO
        for t in accumulating:
            part = in_aud(t.quantity * t.unit_price, inst.currency, t.fx_rate)
            if part is None:
                paid_aud = None
                break
            paid_aud += part
        if paid_aud is not None:
            avg_price_aud = paid_aud / acc_units
    price_aud = price * fx_now if (price is not None and fx_now is not None) else None

    # The LATEST residual, not a sum. Every dividend stores the balance the
    # registry holds *after* it, so adding them together would count the same
    # few cents once per distribution.
    last_dividend = max(inst.dividends, key=lambda d: (d.date, d.id), default=None)
    drp_residual = last_dividend.residual_carried if last_dividend else None

    day_change_aud = day_pct = None
    if units:
        closes = _last_two_closes(session, inst.id)
        if len(closes) == 2 and closes[1]:
            day_pct = (closes[0] - closes[1]) / closes[1]
            if fx_now is not None:
                day_change_aud = (closes[0] - closes[1]) * units * fx_now

    return Holding(
        instrument=inst,
        units=units,
        avg_price=avg,
        cost=cost,
        dividends_cash=dividends_cash,
        price=price,
        price_date=price_date,
        value=value,
        gain=gain,
        gain_pct=gain_pct,
        cost_aud=cost_aud,
        value_aud=value_aud,
        gain_aud=gain_aud,
        dividends_aud=dividends_aud,
        avg_price_aud=avg_price_aud,
        price_aud=price_aud,
        drp_residual=drp_residual,
        day_change_aud=day_change_aud,
        day_pct=day_pct,
        note=pref.note if pref else None,
        drp=pref.drp if pref else inst.drp,
    )


def all_holdings(session: Session) -> list[Holding]:
    instruments = (
        session.scalars(
            select(Instrument)
            .options(selectinload(Instrument.trades), selectinload(Instrument.dividends))
            .order_by(Instrument.asset_class, Instrument.ticker)
        )
        .unique()
        .all()
    )
    prefs = prefs_by_instrument(session)
    return [build_holding(session, inst, prefs.get(inst.id)) for inst in instruments]


@dataclass
class RealisedPosition:
    instrument: Instrument
    proceeds: Decimal | None  # native ccy; None when sale not yet recorded
    outlay: Decimal
    profit: Decimal | None
    note: str | None = None  # the holder's own note (sale details etc.)


def split_positions(
    holdings: list[Holding],
) -> tuple[list[Holding], list[RealisedPosition]]:
    """Open positions vs closed/sold ones (zero units or marked inactive)."""
    open_, closed = [], []
    for h in holdings:
        if h.units > 0 and h.instrument.active:
            open_.append(h)
        elif h.instrument.trades:  # ignore instruments with no history (e.g. ZULU)
            sells = [t for t in h.instrument.trades if t.type == "sell"]
            proceeds = (
                sum((t.quantity * t.unit_price - t.brokerage for t in sells), ZERO)
                if sells
                else None
            )
            profit = (proceeds + h.dividends_cash - h.cost) if proceeds is not None else None
            closed.append(
                RealisedPosition(
                    instrument=h.instrument,
                    proceeds=proceeds,
                    outlay=h.cost,
                    profit=profit,
                    note=h.note,
                )
            )
    return open_, closed


@dataclass
class DashboardTotals:
    """AUD totals over open positions whose AUD value is computable."""

    cost: Decimal
    value: Decimal
    gain: Decimal
    gain_pct: Decimal | None
    dividends: Decimal
    excluded: list[str]  # tickers without a usable AUD valuation yet
    # Separate from `excluded`: a holding can be valued perfectly and still
    # have one unconvertible distribution. Reported so the dividend total
    # cannot shrink without saying so.
    dividends_excluded: list[str] = field(default_factory=list)
    day_change: Decimal = ZERO  # AUD move since the previous close
    day_pct: Decimal | None = None


def totals(open_positions: list[Holding]) -> DashboardTotals:
    cost = value = dividends = ZERO
    excluded: list[str] = []
    dividends_excluded: list[str] = []
    for h in open_positions:
        if h.cost_aud is not None and h.value_aud is not None:
            cost += h.cost_aud
            value += h.value_aud
        else:
            # No stored fx/price yet (feed hasn't run since this was added).
            excluded.append(h.instrument.ticker)
        if h.dividends_aud is not None:
            dividends += h.dividends_aud
        elif h.instrument.dividends:
            dividends_excluded.append(h.instrument.ticker)
    gain = value - cost
    day_change = sum(
        (h.day_change_aud for h in open_positions if h.day_change_aud is not None), ZERO
    )
    yesterday = value - day_change
    return DashboardTotals(
        cost=cost,
        value=value,
        gain=gain,
        gain_pct=(gain / cost) if cost else None,
        dividends=dividends,
        excluded=excluded,
        dividends_excluded=dividends_excluded,
        day_change=day_change,
        day_pct=(day_change / yesterday) if yesterday else None,
    )


@dataclass(frozen=True)
class Breach:
    """Where a timeline first goes negative, and what tipped it over."""

    trade: Trade  # the sell that can't be covered
    balance: Decimal  # the (negative) running balance right after it
    held_before: Decimal  # units held immediately before it
    is_candidate: bool  # is the offender the row being added/edited?


def _walk(
    trades: list[Trade], candidate: Trade | None, exclude_ids: frozenset[int]
) -> tuple[Decimal, Breach | None]:
    rows = sorted(
        [
            *(t for t in trades if t.id is None or t.id not in exclude_ids),
            *([candidate] if candidate is not None else []),
        ],
        key=trade_order,
    )
    units = ZERO
    for t in rows:
        before = units
        units += t.quantity if t.type in ("buy", "drp") else -t.quantity
        if units < 0:
            return units, Breach(
                trade=t,
                balance=units,
                held_before=before,
                is_candidate=t is candidate,
            )
    return units, None


def balance_after(
    trades: list[Trade],
    candidate: Trade | None = None,
    *,
    exclude_ids: frozenset[int] | set[int] = frozenset(),
) -> Decimal | None:
    """Running unit balance walked in timeline order, or None if it ever goes
    negative.

    Guards manual sell entry: `fyreport.instrument_disposals` raises when a sell
    exceeds the parcels held, which would take out the whole FY report. Checking
    the *running* balance (not just the total) also catches a backdated sell
    that only balances because of units bought later.

    `exclude_ids` drops saved rows from the walk, which is how an edit is
    checked: the original comes out, the candidate goes in, and the whole
    timeline is re-walked. Validating only the edited row would miss the case
    that matters — cutting an old buy's quantity strands a sell years later.
    """
    units, breach = _walk(trades, candidate, frozenset(exclude_ids))
    return None if breach is not None else units


def balance_breach(
    trades: list[Trade],
    candidate: Trade | None = None,
    *,
    exclude_ids: frozenset[int] | set[int] = frozenset(),
) -> Breach | None:
    """The first point the timeline goes negative, or None if it never does.

    Same walk as `balance_after`, but it keeps the detail needed to say *which*
    trade breaks and by how much — "that sell would leave a negative balance"
    is true but unhelpful when the offender is a row from three years ago.
    """
    return _walk(trades, candidate, frozenset(exclude_ids))[1]


def _units(value: Decimal) -> str:
    """Units as someone wrote them: 100, not 100.00000000."""
    return f"{value.normalize():f}"


def breach_message(ticker: str, breach: Breach) -> str:
    """Plain-English refusal naming the trade that has to change first."""
    when = breach.trade.date.isoformat()
    kind = "sell" if breach.trade.type == "sell" else breach.trade.type
    if breach.is_candidate:
        return (
            f"That {kind} of {_units(breach.trade.quantity)} units is more than "
            f"the {_units(breach.held_before)} units of {ticker} held on {when} "
            "— check the date and units against the ledger."
        )
    return (
        f"That change would leave {ticker} at {_units(breach.balance)} units "
        f"from {when}: the {kind} of {_units(breach.trade.quantity)} units on "
        f"{when} would then be more than the {_units(breach.held_before)} units "
        f"held. Edit or delete that {kind} first."
    )


@dataclass
class LedgerEvent:
    date: dt.date
    kind: str  # buy / sell / drp / dividend
    quantity: Decimal | None
    unit_price: Decimal | None
    brokerage: Decimal | None
    cash: Decimal | None
    note: str | None
    # Per-parcel performance at the latest price (buy/drp rows only) — the
    # sheet's "% / $ Inc for this trade" rows.
    gain: Decimal | None = None
    gain_pct: Decimal | None = None
    # Which row the edit/delete buttons act on. A DRP row shows the trade but
    # points at its DIVIDEND: the pair is one statement event, and editing half
    # of it would leave the units and the cash disagreeing.
    row_kind: str | None = None  # "trade" | "dividend"
    row_id: int | None = None
    time: dt.time | None = None


def ledger(
    session: Session, ticker: str
) -> tuple[Instrument, Holding, list[LedgerEvent]] | None:
    inst = session.scalars(
        select(Instrument)
        .where(Instrument.ticker == ticker)
        .options(selectinload(Instrument.trades), selectinload(Instrument.dividends))
    ).first()
    if inst is None:
        return None
    pref = prefs_by_instrument(session).get(inst.id)
    latest = _latest_price(session, inst.id)
    latest_close = latest[0] if latest else None

    def _parcel_gain(t: Trade) -> tuple[Decimal | None, Decimal | None]:
        if latest_close is None or t.type not in ("buy", "drp"):
            return None, None
        outlay = t.quantity * t.unit_price
        gain = t.quantity * latest_close - outlay
        return gain, (gain / outlay if outlay else None)

    # A DRP trade is edited through the dividend that reinvested into it.
    dividend_of_trade = {
        d.reinvest_trade_id: d.id for d in inst.dividends if d.reinvest_trade_id
    }
    events = [
        LedgerEvent(
            date=t.date,
            kind=t.type,
            quantity=t.quantity,
            unit_price=t.unit_price,
            brokerage=t.brokerage,
            cash=None,
            note=t.note,
            gain=_parcel_gain(t)[0],
            gain_pct=_parcel_gain(t)[1],
            row_kind="dividend" if t.id in dividend_of_trade else "trade",
            row_id=dividend_of_trade.get(t.id, t.id),
            time=t.time,
        )
        for t in inst.trades
    ] + [
        LedgerEvent(
            date=d.date,
            kind="dividend",
            quantity=None,
            unit_price=None,
            brokerage=None,
            cash=d.cash_amount,
            note=d.note,
            row_kind="dividend",
            row_id=d.id,
        )
        for d in inst.dividends
        if d.reinvest_trade_id is None  # DRP cash shows through its trade row
    ]
    events.sort(key=lambda e: (e.date, e.kind), reverse=True)
    return inst, build_holding(session, inst, pref), events


# ---------- chart series (computed from trades + daily prices/fx) ----------


def _fx_series(session: Session) -> dict[str, list[tuple[dt.date, Decimal]]]:
    rows = session.execute(
        select(FxRate.pair, FxRate.date, FxRate.rate).order_by(FxRate.date)
    ).all()
    out: dict[str, list[tuple[dt.date, Decimal]]] = {}
    for pair, date, rate in rows:
        out.setdefault(pair, []).append((date, rate))
    return out


class _Stepper:
    """Walk a date-sorted series, returning the latest value on-or-before d.

    Forward-only: `at()` must be called with non-decreasing dates. That suits
    the daily walks it is used for and keeps them linear. Anything that needs
    random access wants `FxBook` instead.
    """

    def __init__(self, rows: list[tuple[dt.date, Decimal]]):
        self.rows = rows
        self.i = 0
        self.current: Decimal | None = None

    def at(self, d: dt.date) -> Decimal | None:
        while self.i < len(self.rows) and self.rows[self.i][0] <= d:
            self.current = self.rows[self.i][1]
            self.i += 1
        return self.current


class FxBook:
    """Every stored exchange rate, and an honest answer for any date.

    For a pair with rows: the nearest rate on or before the date, and failing
    that the earliest there is — a rate a few days out is a rounding error,
    where 1:1 would be a fabrication (decisions.md #5).

    For a pair with none, `rate()` returns None and the caller leaves the AUD
    figure out. `known()` decides that once per instrument rather than per day.

    Random-access by `bisect`, not a cursor: trade and price dates interleave,
    and a cursor returns a stale rate the first time it looks backwards.
    """

    def __init__(self, session: Session):
        self._rows = _fx_series(session)
        self._dates = {pair: [d for d, _ in rows] for pair, rows in self._rows.items()}

    def known(self, currency: str) -> bool:
        """Whether an AUD figure can be produced for this currency at all."""
        return currency == "AUD" or bool(self._rows.get(f"{currency}AUD"))

    def rate(self, currency: str, on: dt.date) -> Decimal | None:
        if currency == "AUD":
            return ONE
        pair = f"{currency}AUD"
        rows = self._rows.get(pair)
        if not rows:
            return None
        i = bisect.bisect_right(self._dates[pair], on)
        # i == 0 means every stored rate is later than `on`: use the earliest.
        return rows[i - 1][1] if i else rows[0][1]

    def of(self, obj, currency: str) -> Decimal | None:
        """The rate to book a trade or dividend at.

        Its own captured `fx_rate` wins — that is the rate that actually
        applied on the day, and the ATO wants the transaction-date rate. Only
        when it was never recorded (an import, or a trade entered before the
        feed had that pair) does this fall back to the stored series.
        """
        if obj.fx_rate is not None:
            return obj.fx_rate
        return self.rate(currency, obj.date)


# The daily series walks every instrument across every priced day (~1s on a
# six-year portfolio) and is asked for several times per page. Cache it per
# portfolio against a fingerprint of the inputs, so a new trade, a new price or
# a new dividend invalidates it and nothing else has to remember to.
_series_cache: dict[int, tuple[tuple, dict]] = {}


def _table_fingerprint(session: Session, model) -> tuple:
    """Enough of a table's shape to notice any change to it.

    Count alone was correct only while rows were append-only. Since trades and
    dividends became editable and deletable it takes three numbers: the count
    (adds and deletes), the max id (a delete-then-add that leaves the count
    unchanged) and the max `updated_at` (an edit, which changes neither).
    """
    return tuple(
        session.execute(
            select(func.count(model.id), func.max(model.id), func.max(model.updated_at))
        ).one()
    )


def _series_fingerprint(session: Session) -> tuple:
    from .models import Dividend

    return (
        session.execute(select(func.max(Price.date))).scalar(),
        # FX gets its own terms rather than riding on the price date. A holding
        # excluded for want of a rate comes back the moment one is stored, and
        # a feed run that fetches only FX — or backfills an old pair — moves no
        # price date at all, so the charts would keep serving the gap.
        session.execute(select(func.count(), func.max(FxRate.date))
                        .select_from(FxRate)).one(),
        *_table_fingerprint(session, Trade),
        *_table_fingerprint(session, Dividend),
    )


_holdings_cache: dict[int, tuple[tuple, list]] = {}


def cached_holdings(session: Session) -> list[Holding]:
    """`all_holdings` behind the same fingerprint cache as the daily series.

    The charts page runs one spec per chart and several are per-holding; each
    was rebuilding every position from its trades.
    """
    from .tenancy import current_portfolio_id

    key = current_portfolio_id(session)
    fingerprint = _series_fingerprint(session)
    hit = _holdings_cache.get(key)
    if hit is not None and hit[0] == fingerprint:
        return hit[1]
    holdings = all_holdings(session)
    _holdings_cache[key] = (fingerprint, holdings)
    return holdings


_grouped_cache: dict[tuple, tuple[tuple, dict]] = {}


def cached_grouped_series(session: Session, by: str, filters: dict | None = None) -> dict:
    """`grouped_series` behind the same fingerprint cache, keyed also on the
    split and filter so different charts don't evict each other."""
    from .tenancy import current_portfolio_id

    key = (current_portfolio_id(session), by, json.dumps(filters or {}, sort_keys=True))
    fingerprint = _series_fingerprint(session)
    hit = _grouped_cache.get(key)
    if hit is not None and hit[0] == fingerprint:
        return hit[1]
    series = grouped_series(session, by, filters)
    _grouped_cache[key] = (fingerprint, series)
    return series


def cached_portfolio_series(session: Session) -> dict:
    """`portfolio_series` with a fingerprint cache. Use this from request paths."""
    from .tenancy import current_portfolio_id

    key = current_portfolio_id(session)
    fingerprint = _series_fingerprint(session)
    hit = _series_cache.get(key)
    if hit is not None and hit[0] == fingerprint:
        return hit[1]
    series = portfolio_series(session)
    _series_cache[key] = (fingerprint, series)
    return series


def _instrument_group(inst, by: str) -> str:
    return {
        "ticker": inst.ticker,
        "asset_class": inst.asset_class,
        "currency": inst.currency,
        "exchange": inst.exchange,
    }[by]


def _matches(inst, filters: dict | None) -> bool:
    """Whether an instrument survives the builder's filter shelf."""
    if not filters:
        return True
    for key, allowed in filters.items():
        if not allowed:
            continue
        if _instrument_group(inst, key) not in allowed:
            return False
    return True


def grouped_series(session: Session, by: str, filters: dict | None = None) -> dict:
    """The daily series split by a holding attribute — value per ticker, per
    asset class, and so on.

    Same walk as `portfolio_series`, but every accumulator is kept per group so
    a chart can compare holdings over time rather than only in total. Groups
    with no history at all are dropped: an empty line in a legend is noise.
    """
    instruments = (
        session.scalars(
            select(Instrument)
            .options(selectinload(Instrument.trades), selectinload(Instrument.dividends))
        )
        .unique()
        .all()
    )
    instruments = [i for i in instruments if _matches(i, filters)]
    price_rows = session.execute(
        select(Price.instrument_id, Price.date, Price.close).order_by(Price.date)
    ).all()
    wanted = {i.id for i in instruments}
    prices: dict[int, list[tuple[dt.date, Decimal]]] = {}
    date_set: set[dt.date] = set()
    for iid, date, close in price_rows:
        if iid not in wanted:
            continue
        prices.setdefault(iid, []).append((date, close))
        date_set.add(date)
    fxbook = FxBook(session)

    # Same rule as portfolio_series: no stored rate for the pair, no honest AUD
    # figure, so the instrument is named rather than guessed at.
    excluded = sorted(
        {inst.ticker for inst in instruments if not fxbook.known(inst.currency)}
    )
    instruments = [inst for inst in instruments if inst.ticker not in excluded]

    events = []
    for inst in instruments:
        for t in inst.trades:
            events.append((t.date, "t", inst, t))
        for d in inst.dividends:
            if d.reinvest_trade_id is None:  # reinvested cash is already units
                events.append((d.date, "d", inst, d))
    if not events:
        return {"dates": [], "groups": {}, "excluded": excluded}
    events.sort(key=lambda e: e[0])
    first = events[0][0]
    dates = sorted(d for d in date_set if d >= first)

    steppers = {iid: _Stepper(rows) for iid, rows in prices.items()}
    labels = sorted({_instrument_group(i, by) for i in instruments})
    def blank() -> dict[str, list]:
        return {k: [] for k in ("value", "invested", "gain", "gain_pct", "flow_in", "cash_div")}

    out = {label: blank() for label in labels}

    units: dict[int, Decimal] = {}
    invested = {label: ZERO for label in labels}
    proceeds = {label: ZERO for label in labels}
    divs = {label: ZERO for label in labels}
    prev_invested = {label: ZERO for label in labels}
    ei = 0

    for d in dates:
        day_flow = {label: ZERO for label in labels}
        day_div = {label: ZERO for label in labels}
        while ei < len(events) and events[ei][0] <= d:
            _, kind, inst, obj = events[ei]
            label = _instrument_group(inst, by)
            fxr = fxbook.of(obj, inst.currency)  # never None: unknown pairs are excluded
            if kind == "t":
                if obj.type in ("buy", "drp"):
                    units[inst.id] = units.get(inst.id, ZERO) + obj.quantity
                    if obj.type == "buy":
                        spend = (obj.quantity * obj.unit_price + obj.brokerage) * fxr
                        invested[label] += spend
                        day_flow[label] += spend
                else:
                    units[inst.id] = units.get(inst.id, ZERO) - obj.quantity
                    proceeds[label] += (obj.quantity * obj.unit_price - obj.brokerage) * fxr
            else:
                divs[label] += obj.cash_amount * fxr
                day_div[label] += obj.cash_amount * fxr
            ei += 1

        value = {label: ZERO for label in labels}
        for inst in instruments:
            u = units.get(inst.id, ZERO)
            if not u or inst.id not in steppers:
                continue
            close = steppers[inst.id].at(d)
            if close is None:
                continue
            value[_instrument_group(inst, by)] += u * close * fxbook.rate(inst.currency, d)

        for label in labels:
            gain = value[label] + proceeds[label] + divs[label] - invested[label]
            g = out[label]
            g["value"].append(round(float(value[label]), 2))
            g["invested"].append(round(float(invested[label]), 2))
            g["gain"].append(round(float(gain), 2))
            g["gain_pct"].append(
                round(float(gain / invested[label]), 4) if invested[label] else 0.0
            )
            g["flow_in"].append(round(float(day_flow[label]), 2))
            g["cash_div"].append(round(float(day_div[label]), 2))
            prev_invested[label] = invested[label]

    live = {k: v for k, v in out.items() if any(v["invested"]) or any(v["value"])}
    return {"dates": [d.isoformat() for d in dates], "groups": live, "excluded": excluded}


def portfolio_series(session: Session) -> dict:
    """Daily AUD series since the first trade: invested, value, net gain.

    invested = cumulative buy cost (incl. brokerage) at trade-date fx — the
    sheet's "Total Spent". net gain = value + realised proceeds + CASH
    dividends - invested.

    Only *cash* dividends count: a reinvested one already shows up as units in
    `value`, so adding its cash too would count it twice (
    worth ~9k on a DRP-heavy portfolio).

    Also returns the per-day external flow and cash income the time-weighted
    return needs: `flow` is money in (buys) less money out (sale proceeds),
    `cash_div` is income that left the portfolio rather than buying more units.
    """
    instruments = session.scalars(
        select(Instrument).options(selectinload(Instrument.trades), selectinload(Instrument.dividends))
    ).unique().all()
    price_rows = session.execute(
        select(Price.instrument_id, Price.date, Price.close).order_by(Price.date)
    ).all()
    prices: dict[int, list[tuple[dt.date, Decimal]]] = {}
    date_set: set[dt.date] = set()
    for iid, date, close in price_rows:
        prices.setdefault(iid, []).append((date, close))
        date_set.add(date)
    fxbook = FxBook(session)

    # An instrument whose currency has no stored rate at all cannot be put into
    # an AUD total honestly, so it is left out of every figure here and named in
    # `excluded` for the page to say so. Its own trades might carry rates, but
    # its market VALUE could not be converted, and a series mixing converted
    # cost with unconverted value is worse than one that admits the gap.
    excluded = sorted(
        {inst.ticker for inst in instruments if not fxbook.known(inst.currency)}
    )
    priced = [inst for inst in instruments if inst.ticker not in excluded]

    events = []  # (date, kind, inst, trade/dividend)
    for inst in priced:
        for t in inst.trades:
            events.append((t.date, "t", inst, t))
        for d in inst.dividends:
            events.append((d.date, "d", inst, d))
    if not events:
        return {"dates": [], "invested": [], "value": [], "gain": [], "gain_pct": [],
                "flow": [], "flow_in": [], "cash_div": [], "excluded": excluded}
    events.sort(key=lambda e: e[0])
    first = events[0][0]
    dates = sorted(d for d in date_set if d >= first)

    steppers = {iid: _Stepper(rows) for iid, rows in prices.items()}
    units: dict[int, Decimal] = {}
    invested = proceeds = divs = Decimal(0)
    ei = 0
    out_dates, out_invested, out_value, out_gain, out_gain_pct = [], [], [], [], []
    out_flow, out_flow_in, out_cash_div = [], [], []

    prev_invested = prev_proceeds = Decimal(0)
    for d in dates:
        day_cash_div = Decimal(0)
        while ei < len(events) and events[ei][0] <= d:
            _, kind, inst, obj = events[ei]
            # Never None here: `priced` excluded the unknown pairs already.
            fxr = fxbook.of(obj, inst.currency)
            if kind == "t":
                if obj.type in ("buy", "drp"):
                    units[inst.id] = units.get(inst.id, ZERO) + obj.quantity
                    if obj.type == "buy":
                        invested += (obj.quantity * obj.unit_price + obj.brokerage) * fxr
                else:
                    units[inst.id] = units.get(inst.id, ZERO) - obj.quantity
                    proceeds += (obj.quantity * obj.unit_price - obj.brokerage) * fxr
            else:
                # Reinvested distributions are already in `value` as units.
                if obj.reinvest_trade_id is None:
                    divs += obj.cash_amount * fxr
                    day_cash_div += obj.cash_amount * fxr
            ei += 1

        value = Decimal(0)
        for inst in priced:
            u = units.get(inst.id, ZERO)
            if not u:
                continue
            close = steppers[inst.id].at(d) if inst.id in steppers else None
            if close is None:
                continue
            value += u * close * fxbook.rate(inst.currency, d)
        gain = value + proceeds + divs - invested
        # Two flows, deliberately: NET (buys less sale proceeds) is what a
        # time-weighted return must remove, but GROSS buys are what "money you
        # put in" means to a person — netting sales off it shrinks the
        # denominator and flatters the percentage.
        out_flow.append(round(float((invested - prev_invested) - (proceeds - prev_proceeds)), 2))
        out_flow_in.append(round(float(invested - prev_invested), 2))
        out_cash_div.append(round(float(day_cash_div), 2))
        prev_invested, prev_proceeds = invested, proceeds
        out_dates.append(d.isoformat())
        out_invested.append(round(float(invested), 2))
        out_value.append(round(float(value), 2))
        out_gain.append(round(float(gain), 2))
        # Return on what went in — the honest denominator for "how am I doing",
        # and what the sheet's percentage columns meant.
        out_gain_pct.append(round(float(gain / invested), 4) if invested else 0.0)
    return {
        "dates": out_dates,
        "invested": out_invested,
        "value": out_value,
        "gain": out_gain,
        "gain_pct": out_gain_pct,
        "flow": out_flow,
        "flow_in": out_flow_in,
        "cash_div": out_cash_div,
        # Tickers left out of every number above, for the page to disclose.
        "excluded": excluded,
    }


CHART_YEAR_RANGES = (20, 10, 5, 3, 1)

# Windows the period summary offers, longest last so the table reads short→long.
PERIODS = (
    ("1 day", dt.timedelta(days=1)),
    ("1 month", dt.timedelta(days=30)),
    ("6 months", dt.timedelta(days=182)),
    ("1 year", dt.timedelta(days=365)),
    ("3 years", dt.timedelta(days=365 * 3)),
    ("5 years", dt.timedelta(days=365 * 5)),
    ("10 years", dt.timedelta(days=365 * 10)),
    ("20 years", dt.timedelta(days=365 * 20)),
)


def period_summary(session: Session, series: dict | None = None) -> list[dict]:
    """How the portfolio actually performed over each window.

    Two returns, because they answer different questions (decisions.md #95):

      * `on_money_in` — growth over what was at work. The familiar figure.
      * `twr` — time-weighted, contributions removed: how the investments
        performed. `twr_annual` restates long windows per year.

    `growth` is what the market and income actually made — value change less
    what you put in, plus cash income.
    """
    P = series if series is not None else portfolio_series(session)
    dates = P["dates"]
    if not dates:
        return []
    today = clock.today()
    last = len(dates) - 1
    first_date = dt.date.fromisoformat(dates[0])

    def summarise(label: str, start_index: int, window_days: int | None = None) -> dict:
        # Chain daily returns from the day AFTER the opening balance.
        factor = 1.0
        for i in range(start_index + 1, last + 1):
            opening = P["value"][i - 1]
            if opening <= 0:
                continue
            r = (P["value"][i] + P["cash_div"][i] - opening - P["flow"][i]) / opening
            factor *= 1.0 + r
        net_flow = sum(P["flow"][start_index + 1 : last + 1])
        contributed = sum(P["flow_in"][start_index + 1 : last + 1])
        income = sum(P["cash_div"][start_index + 1 : last + 1])
        growth = P["value"][last] - P["value"][start_index] - net_flow + income
        # Capital that was at work: the opening balance plus every dollar put
        # in. Using the NET flow here understated it by the sale proceeds, which
        # is what made the all-time row disagree with the cumulative gain % on
        # the "Overall performance by year" chart.
        money_in = P["value"][start_index] + contributed
        days = (
            dt.date.fromisoformat(dates[last]) - dt.date.fromisoformat(dates[start_index])
        ).days
        # Annualise on the window ASKED FOR, not the span it snapped to —
        # decisions.md #35. The arithmetic still uses the real span.
        asked = days if window_days is None else window_days
        annual = None
        if asked >= 365 and days > 0 and factor > 0:
            annual = round(factor ** (365.0 / days) - 1.0, 4)
        return {
            "label": label,
            "from": dates[start_index],
            "value_then": P["value"][start_index],
            "contributed": round(contributed, 2),
            "growth": round(growth, 2),
            "on_money_in": round(growth / money_in, 4) if money_in else 0.0,
            "twr": round(factor - 1.0, 4),
            "twr_annual": annual,
        }

    rows = []
    for label, delta in PERIODS:
        cutoff = today - delta
        if cutoff < first_date:
            continue  # the record doesn't reach back that far
        start = next((i for i, d in enumerate(dates) if d >= cutoff.isoformat()), None)
        if start is None or start >= last:
            continue
        rows.append(summarise(label, start, window_days=delta.days))
    rows.append(summarise("All time", 0))
    return rows


def chart_ranges(first_date: dt.date | None) -> list[dict]:
    """Range buttons the history can actually fill.

    A "10y" button over six years of trades would just redraw the same chart
    with a misleading label, so a window only appears once the record reaches
    back that far.
    """
    ranges = [{"key": "all", "label": "All"}]
    if first_date is not None:
        span = (clock.today() - first_date).days
        # Longest first, so the bar reads All → 20y → … → 1y → This FY.
        for years in sorted(CHART_YEAR_RANGES, reverse=True):
            if span >= years * 365:
                ranges.append({"key": f"y{years}", "label": f"{years}y"})
    ranges.append({"key": "fy", "label": "This FY"})
    return ranges


def _fy_end(d: dt.date) -> dt.date:
    return dt.date(d.year if d.month <= 6 else d.year + 1, 6, 30)


def annual_series(session: Session, series: dict | None = None) -> dict:
    """Per-AU-FY aggregates from the daily series (current FY = to date).

    Takes the series when the caller already has it — recomputing it here cost
    ~750ms on every charts page load.
    """
    daily = series if series is not None else portfolio_series(session)
    if not daily["dates"]:
        return {"labels": [], "invested_yr": [], "invested_cum": [], "value_eofy": [],
                "value_gain_cum": [], "gain_cum": [], "gain_pct": [], "gain_yr": [], "yr_pct": []}
    idx_by_fy: dict[dt.date, int] = {}
    for i, ds in enumerate(daily["dates"]):
        idx_by_fy[_fy_end(dt.date.fromisoformat(ds))] = i  # last index in each FY
    labels, invested_yr, invested_cum, value_eofy = [], [], [], []
    value_gain_cum, gain_cum, gain_pct, gain_yr, yr_pct = [], [], [], [], []
    prev_invested = prev_gain = 0.0
    for fy in sorted(idx_by_fy):
        i = idx_by_fy[fy]
        inv, val, gain = daily["invested"][i], daily["value"][i], daily["gain"][i]
        labels.append(f"FY{str(fy.year - 1)[-2:]}/{str(fy.year)[-2:]}")
        invested_yr.append(round(inv - prev_invested, 2))
        invested_cum.append(inv)
        value_eofy.append(val)
        value_gain_cum.append(round(val - inv, 2))
        gain_cum.append(gain)
        gain_pct.append(round(gain / inv, 4) if inv else 0)
        gain_yr.append(round(gain - prev_gain, 2))
        yr_pct.append(round((gain - prev_gain) / inv, 4) if inv else 0)
        prev_invested, prev_gain = inv, gain
    return {"labels": labels, "invested_yr": invested_yr, "invested_cum": invested_cum,
            "value_eofy": value_eofy, "value_gain_cum": value_gain_cum, "gain_cum": gain_cum,
            "gain_pct": gain_pct, "gain_yr": gain_yr, "yr_pct": yr_pct}


def allocation_series(session: Session) -> dict:
    """Invested (all-time buys) and current value by asset class, AUD."""
    holdings = all_holdings(session)
    classes = ["etf", "share", "crypto"]
    invested = {c: Decimal(0) for c in classes}
    value = {c: Decimal(0) for c in classes}
    for h in holdings:
        c = h.instrument.asset_class
        if h.cost_aud is not None:
            invested[c] += h.cost_aud
        if h.value_aud is not None and h.units > 0 and h.instrument.active:
            value[c] += h.value_aud
    return {
        "labels": ["ETFs", "Shares", "Crypto"],
        "invested": [round(float(invested[c]), 2) for c in classes],
        "value": [round(float(value[c]), 2) for c in classes],
    }


def today_moves(session: Session) -> dict:
    """Last close vs previous close per open instrument (native ccy, %)."""
    out = {"labels": [], "pct": []}
    for h in all_holdings(session):
        if h.units <= 0 or not h.instrument.active:
            continue
        rows = session.execute(
            select(Price.close)
            .where(Price.instrument_id == h.instrument.id)
            .order_by(Price.date.desc())
            .limit(2)
        ).scalars().all()
        if len(rows) == 2 and rows[1]:
            out["labels"].append(h.instrument.ticker)
            out["pct"].append(round(float((rows[0] - rows[1]) / rows[1]), 4))
    return out


def instrument_series(session: Session, inst: Instrument) -> dict:
    """Daily holding value + cumulative invested for one instrument (native ccy)."""
    price_rows = session.execute(
        select(Price.date, Price.close).where(Price.instrument_id == inst.id).order_by(Price.date)
    ).all()
    trades = sorted(inst.trades, key=trade_order)
    if not trades or not price_rows:
        return {"dates": [], "invested": [], "value": []}
    first = trades[0].date
    units = invested = Decimal(0)
    ti = 0
    dates, inv_out, val_out = [], [], []
    for date, close in price_rows:
        if date < first:
            continue
        while ti < len(trades) and trades[ti].date <= date:
            t = trades[ti]
            if t.type in ("buy", "drp"):
                units += t.quantity
                if t.type == "buy":
                    invested += t.quantity * t.unit_price + t.brokerage
            else:
                units -= t.quantity
            ti += 1
        dates.append(date.isoformat())
        inv_out.append(round(float(invested), 2))
        val_out.append(round(float(units * close), 2))
    return {"dates": dates, "invested": inv_out, "value": val_out}


def latest_prices(session: Session, instrument_ids: list[int]) -> dict[int, Decimal]:
    """The most recent stored close for each instrument, by id.

    One query with a window function rather than one per instrument: the
    holdings page asks for every instrument it lists, and a loop there would be
    a query per row. Instruments with no stored price are simply absent, which
    the template renders as an em dash — "no price yet" and "price of zero" are
    different facts.
    """
    if not instrument_ids:
        return {}
    ranked = (
        select(
            Price.instrument_id,
            Price.close,
            func.row_number()
            .over(partition_by=Price.instrument_id, order_by=Price.date.desc())
            .label("rank"),
        )
        .where(Price.instrument_id.in_(instrument_ids))
        .subquery()
    )
    return {
        row.instrument_id: row.close
        for row in session.execute(select(ranked).where(ranked.c.rank == 1))
    }


def latest_fx_rates(session: Session, reporting: str) -> dict[str, str]:
    """The most recent stored rate for each currency, into the reporting one.

    Keyed by the FOREIGN currency, because that is what the caller has — an
    instrument knows it is USD, not that it wants "USDAUD". Values are strings
    ready for a form field, normalised so a `Numeric(12,6)` does not fill the
    box with `0.650000`.
    """
    ranked = (
        select(
            FxRate.pair,
            FxRate.rate,
            func.row_number()
            .over(partition_by=FxRate.pair, order_by=FxRate.date.desc())
            .label("rank"),
        )
        .where(FxRate.pair.like(f"%{reporting}"))
        .subquery()
    )
    out: dict[str, str] = {}
    for row in session.execute(select(ranked).where(ranked.c.rank == 1)):
        foreign = row.pair[: -len(reporting)]
        if foreign:
            out[foreign] = f"{row.rate.normalize():f}"
    return out
