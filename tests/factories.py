"""Builders for test data.

Everything here is synthetic. Tickers are deliberately fictional (ALPHA, BETAX,
GAMMA, OMEGA, ZULU) so no fixture can be mistaken for a real portfolio, and so
a stray copy-paste from production data would stand out immediately.

Conventions:
  * dates accept a `datetime.date` or an ISO string — `d("2024-06-10")`
  * money accepts str/int/Decimal; floats are converted via `str()` so no
    binary-float artefacts reach a Numeric column
  * `add_trade`/`add_dividend` stamp the session's portfolio via `tenancy.owned`
    unless `portfolio_id=` is passed explicitly (which is how tenancy tests
    plant rows in a *different* portfolio)
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app import tenancy
from app.models import (
    Dividend,
    FxRate,
    Instrument,
    MarketDividend,
    Portfolio,
    PortfolioMember,
    Price,
    Trade,
    User,
)


def d(value: dt.date | str) -> dt.date:
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(value)


def dec(value) -> Decimal:
    """Decimal from anything, via str() so 0.1 doesn't become 0.1000000000000000055."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #

def make_user(db, email: str, *, name: str | None = None, password_hash: str = "x",
              is_admin: bool = False, is_active: bool = True, **kwargs) -> User:
    user = User(
        email=email.lower(),
        name=name or email.split("@")[0],
        password_hash=password_hash,
        is_admin=is_admin,
        is_active=is_active,
        **kwargs,
    )
    db.add(user)
    db.flush()
    return user


def make_portfolio(db, name: str, *, owner: User | None = None,
                   role: str = "owner") -> Portfolio:
    portfolio = Portfolio(name=name)
    db.add(portfolio)
    db.flush()
    if owner is not None:
        db.add(PortfolioMember(portfolio_id=portfolio.id, user_id=owner.id, role=role))
        db.flush()
    return portfolio


def add_member(db, portfolio: Portfolio, user: User, role: str = "member") -> PortfolioMember:
    member = PortfolioMember(portfolio_id=portfolio.id, user_id=user.id, role=role)
    db.add(member)
    db.flush()
    return member


# --------------------------------------------------------------------------- #
# Market data (shared — never portfolio-scoped)
# --------------------------------------------------------------------------- #

def make_instrument(db, ticker: str, *, exchange: str = "ASX", asset_class: str = "etf",
                    currency: str = "AUD", name: str | None = None, drp: bool = False,
                    active: bool = True, yahoo_symbol: str | None = "auto") -> Instrument:
    if yahoo_symbol == "auto":
        yahoo_symbol = f"{ticker}.AX" if exchange == "ASX" else ticker
    inst = Instrument(
        ticker=ticker,
        exchange=exchange,
        asset_class=asset_class,
        currency=currency,
        name=name,
        drp=drp,
        active=active,
        yahoo_symbol=yahoo_symbol,
    )
    db.add(inst)
    db.flush()
    return inst


def add_price(db, inst: Instrument, date, close, source: str = "test") -> Price:
    row = Price(instrument_id=inst.id, date=d(date), close=dec(close), source=source)
    db.add(row)
    db.flush()
    return row


def add_prices(db, inst: Instrument, rows) -> None:
    """`rows` is an iterable of (date, close) pairs."""
    for date, close in rows:
        db.add(Price(instrument_id=inst.id, date=d(date), close=dec(close), source="test"))
    db.flush()


def add_fx(db, pair: str, date, rate) -> FxRate:
    row = FxRate(pair=pair, date=d(date), rate=dec(rate), source="test")
    db.add(row)
    db.flush()
    return row


def add_fx_series(db, pair: str, rows) -> None:
    for date, rate in rows:
        db.add(FxRate(pair=pair, date=d(date), rate=dec(rate), source="test"))
    db.flush()


def add_market_dividend(db, inst: Instrument, ex_date, amount) -> MarketDividend:
    row = MarketDividend(instrument_id=inst.id, ex_date=d(ex_date), amount=dec(amount),
                         source="test")
    db.add(row)
    db.flush()
    return row


# --------------------------------------------------------------------------- #
# Personal data (portfolio-scoped)
# --------------------------------------------------------------------------- #

def add_trade(db, inst: Instrument, date, type: str, quantity, unit_price, *,
              brokerage=0, fx_rate="auto", note: str | None = None,
              time: dt.time | None = None,
              portfolio_id: int | None = None, user_id: int | None = None) -> Trade:
    """A buy/sell/drp. `fx_rate="auto"` means 1 for AUD, else None (unknown).

    `time` left out means the column default — market open — exactly as it does
    for a trade recorded through the form without ticking "set a time".
    """
    if fx_rate == "auto":
        fx_rate = Decimal(1) if inst.currency == "AUD" else None
    trade = Trade(
        instrument_id=inst.id,
        date=d(date),
        type=type,
        quantity=dec(quantity),
        unit_price=dec(unit_price),
        brokerage=dec(brokerage),
        fx_rate=None if fx_rate is None else dec(fx_rate),
        note=note,
        **({"time": time} if time is not None else {}),
    )
    if portfolio_id is not None:
        trade.portfolio_id = portfolio_id
        trade.user_id = user_id
    else:
        tenancy.owned(db, trade)
    db.add(trade)
    db.flush()
    return trade


def build_trade(inst: Instrument, date, type: str, quantity, unit_price, *,
                brokerage=0, time: dt.time | None = None) -> Trade:
    """An *unsaved* trade — for `balance_after` candidate checks (the backdated
    sell that must be refused before it ever reaches the database).

    Unsaved means no id and no column defaults applied, so `time` really is
    None here unless one is passed — which is the case `trade_order` has to get
    right, since this is the trade someone is entering right now.
    """
    return Trade(
        instrument_id=inst.id,
        date=d(date),
        type=type,
        quantity=dec(quantity),
        unit_price=dec(unit_price),
        brokerage=dec(brokerage),
        fx_rate=Decimal(1),
        time=time,
    )


def add_dividend(db, inst: Instrument, date, cash_amount, *, fx_rate="auto",
                 franking_credits=None, reinvest_trade: Trade | None = None,
                 note: str | None = None, portfolio_id: int | None = None,
                 user_id: int | None = None) -> Dividend:
    if fx_rate == "auto":
        fx_rate = Decimal(1) if inst.currency == "AUD" else None
    dividend = Dividend(
        instrument_id=inst.id,
        date=d(date),
        cash_amount=dec(cash_amount),
        fx_rate=None if fx_rate is None else dec(fx_rate),
        franking_credits=None if franking_credits is None else dec(franking_credits),
        reinvest_trade=reinvest_trade,
        note=note,
    )
    if portfolio_id is not None:
        dividend.portfolio_id = portfolio_id
        dividend.user_id = user_id
    else:
        tenancy.owned(db, dividend)
    db.add(dividend)
    db.flush()
    return dividend


def add_drp(db, inst: Instrument, date, cash_amount, units, unit_price, **kwargs):
    """A reinvested distribution: the DRP trade holding the units, plus the
    dividend row that points at it.

    This pair is the shape the DRP double-count regression is about — the cash
    must NOT also count as income, because it is already in the unit balance.
    Returns (trade, dividend).
    """
    trade = add_trade(db, inst, date, "drp", units, unit_price, **kwargs)
    dividend = add_dividend(db, inst, date, cash_amount, reinvest_trade=trade, **kwargs)
    return trade, dividend


# --------------------------------------------------------------------------- #
# Price calendars
# --------------------------------------------------------------------------- #

def monthly(start, end) -> list[dt.date]:
    """First of each month from `start` to `end` inclusive."""
    start, end = d(start), d(end)
    out, cur = [], dt.date(start.year, start.month, 1)
    while cur <= end:
        if cur >= start:
            out.append(cur)
        cur = dt.date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
    return out


def daily(start, end) -> list[dt.date]:
    start, end = d(start), d(end)
    out, cur = [], start
    while cur <= end:
        out.append(cur)
        cur += dt.timedelta(days=1)
    return out


def stepped(calendar: list[dt.date], steps: list[tuple]) -> list[tuple[dt.date, Decimal]]:
    """Turn a step function into (date, price) rows.

    `steps` is [(from_date, price), ...] in ascending order: the price applies
    from that date until the next step. Dates before the first step are
    skipped. Step prices (rather than a drifting series) keep every
    intermediate value hand-checkable.
    """
    steps = [(d(when), dec(price)) for when, price in steps]
    out = []
    for day in calendar:
        current = None
        for when, price in steps:
            if day >= when:
                current = price
        if current is not None:
            out.append((day, current))
    return out
