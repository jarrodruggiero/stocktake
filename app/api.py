"""/api/v1 — machine access to one portfolio, authenticated by API key.

JSON in, JSON out, no cookies and no CSRF: a key is only ever sent
deliberately, so there is no ambient authority to abuse.

`Authorization: Bearer pfk_...` or `X-API-Key: pfk_...`. A key belongs to
exactly one portfolio, so nothing here takes a portfolio parameter — the key
decides what it can see and `tenancy` enforces it.

Amounts are AUD unless a `currency` field says otherwise; dates are ISO-8601.

Append-only, deliberately — there is no PATCH or DELETE to find:
decisions.md #88.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from . import auth, clock, fyreport, plans, queries
from .models import Dividend, Instrument, Trade, ticker_problem
from .tenancy import owned

router = APIRouter(prefix="/api/v1", tags=["api"])


def _num(value: Decimal | None) -> float | None:
    return round(float(value), 6) if value is not None else None


@router.get("/portfolio")
def get_portfolio(request: Request) -> dict:
    """Headline numbers — what a budgeting app wants for a net-worth line."""
    with auth.api_session(request) as (key, db):
        holdings = queries.all_holdings(db)
        open_positions, closed = queries.split_positions(holdings)
        totals = queries.totals(open_positions)
        return {
            "portfolio": {"id": key.portfolio_id, "name": key.portfolio.name},
            "as_at": clock.today().isoformat(),
            "currency": "AUD",
            "cost": _num(totals.cost),
            "value": _num(totals.value),
            "gain": _num(totals.gain),
            "gain_pct": _num(totals.gain_pct),
            "day_change": _num(totals.day_change),
            "day_change_pct": _num(totals.day_pct),
            "dividends_received": _num(totals.dividends),
            "open_positions": len(open_positions),
            "closed_positions": len(closed),
            # Tickers with no usable price/FX yet — their value is NOT in the
            # totals above, so a consumer can flag rather than silently
            # under-report.
            "excluded": totals.excluded,
        }


@router.get("/holdings")
def get_holdings(request: Request) -> dict:
    with auth.api_session(request) as (key, db):
        open_positions, _ = queries.split_positions(queries.all_holdings(db))
        return {
            "holdings": [
                {
                    "ticker": h.instrument.ticker,
                    "name": h.instrument.name,
                    "exchange": h.instrument.exchange,
                    "asset_class": h.instrument.asset_class,
                    "currency": h.instrument.currency,
                    "units": _num(h.units),
                    "avg_price": _num(h.avg_price),
                    "last_price": _num(h.price),
                    "price_date": h.price_date.isoformat() if h.price_date else None,
                    "cost_aud": _num(h.cost_aud),
                    "value_aud": _num(h.value_aud),
                    "gain_aud": _num(h.gain_aud),
                    "gain_pct": _num(h.gain_pct),
                    "day_change_aud": _num(h.day_change_aud),
                    "dividends": _num(h.dividends_cash),
                }
                for h in open_positions
            ]
        }


@router.get("/trades")
def get_trades(
    request: Request, since: str | None = None, ticker: str | None = None
) -> dict:
    with auth.api_session(request) as (key, db):
        stmt = select(Trade).order_by(Trade.date, Trade.id)
        if since:
            try:
                stmt = stmt.where(Trade.date >= dt.date.fromisoformat(since))
            except ValueError:
                raise HTTPException(400, "since must be YYYY-MM-DD")
        rows = db.scalars(stmt).all()
        if ticker:
            wanted = ticker.strip().upper()
            rows = [t for t in rows if t.instrument.ticker == wanted]
        return {
            "trades": [
                {
                    "id": t.id,
                    "date": t.date.isoformat(),
                    "ticker": t.instrument.ticker,
                    "type": t.type,
                    "units": _num(t.quantity),
                    "unit_price": _num(t.unit_price),
                    "brokerage": _num(t.brokerage),
                    "currency": t.instrument.currency,
                    "fx_rate": _num(t.fx_rate),
                    "note": t.note,
                }
                for t in rows
            ]
        }


@router.get("/dividends")
def get_dividends(request: Request, since: str | None = None) -> dict:
    with auth.api_session(request) as (key, db):
        stmt = select(Dividend).order_by(Dividend.date, Dividend.id)
        if since:
            try:
                stmt = stmt.where(Dividend.date >= dt.date.fromisoformat(since))
            except ValueError:
                raise HTTPException(400, "since must be YYYY-MM-DD")
        return {
            "dividends": [
                {
                    "id": d.id,
                    "date": d.date.isoformat(),
                    "ticker": d.instrument.ticker,
                    "cash_amount": _num(d.cash_amount),
                    "currency": d.instrument.currency,
                    "fx_rate": _num(d.fx_rate),
                    "franking_credits": _num(d.franking_credits),
                    "reinvested": d.reinvest_trade_id is not None,
                    "note": d.note,
                }
                for d in db.scalars(stmt).all()
            ]
        }


@router.get("/fy/{year}")
def get_fy(request: Request, year: int) -> dict:
    """FY summary incl. CGT — the retirement/tax planning view.

    `year` is the FY *ending* year: 2026 = 1 Jul 2025 → 30 Jun 2026.
    """
    if not 2000 <= year <= 2100:
        raise HTTPException(400, "year out of range")
    with auth.api_session(request) as (key, db):
        r = fyreport.fy_report(db, year)
        cgt = r["cgt"]
        return {
            "fy": r["label"],
            "start": r["start"].isoformat(),
            "end": r["end"].isoformat(),
            "as_at": r["asof"].isoformat(),
            "is_current": r["is_current"],
            "value": _num(r["total_value"]),
            "invested": _num(r["total_invested"]),
            "invested_this_fy": _num(r["activity"].invested),
            "proceeds_this_fy": _num(r["activity"].proceeds),
            "brokerage_this_fy": _num(r["activity"].brokerage),
            "income": {
                "cash": _num(r["income_cash"]),
                "franking_credits": _num(r["income_franking"]),
                "grossed_up": _num(r["income_cash"] + r["income_franking"]),
                "dividends_missing_franking": r["franking_missing"],
            },
            "cgt": {
                "disposals": len(cgt.disposals),
                "gains_discountable": _num(cgt.gains_discountable),
                "gains_other": _num(cgt.gains_other),
                "losses": _num(cgt.losses),
                "discount": _num(cgt.discount),
                "net_capital_gain": _num(cgt.net_capital_gain),
                "net_capital_loss": _num(cgt.net_capital_loss),
            },
        }


@router.get("/schedule")
def get_plan(request: Request) -> dict:
    """Upcoming scheduled buys — what a budgeting app needs to reserve cash."""
    with auth.api_session(request) as (key, db):
        sched = plans.schedule(db, upcoming=6)
        plan = sched["plan"]
        return {
            "plan": None
            if plan is None
            else {
                "name": plan.name,
                "interval_days": plan.interval_days,
                "amount": _num(plan.amount),
                "brokerage": _num(plan.brokerage),
                "rotation": [e.instrument.ticker for e in plan.entries],
            },
            "upcoming": [
                {
                    "ticker": b.ticker,
                    "due_date": b.due_date.isoformat(),
                    "amount": _num(b.amount),
                }
                for b in sched["next"]
            ],
        }


# --------------------------------------------------------------------------- #
# Writes (keys with the "write" scope)
# --------------------------------------------------------------------------- #

class TradeIn(BaseModel):
    ticker: str
    type: str = Field(pattern="^(buy|sell|drp)$")
    date: dt.date
    units: Decimal = Field(gt=0)
    unit_price: Decimal = Field(gt=0)
    brokerage: Decimal = Field(default=Decimal(0), ge=0)
    fx_rate: Decimal | None = Field(default=None, gt=0)
    note: str | None = None


class DividendIn(BaseModel):
    ticker: str
    date: dt.date
    cash_amount: Decimal = Field(gt=0)
    franking_credits: Decimal | None = Field(default=None, ge=0)
    fx_rate: Decimal | None = Field(default=None, gt=0)
    note: str | None = None


def _instrument(db, ticker: str) -> Instrument:
    problem = ticker_problem(ticker)
    if problem:
        raise HTTPException(400, problem)
    inst = db.scalar(
        select(Instrument).where(Instrument.ticker == ticker.strip().upper())
    )
    if inst is None:
        raise HTTPException(
            404, f"unknown instrument {ticker!r} — add it in the app first"
        )
    return inst


@router.post("/trades", status_code=201)
def create_trade(request: Request, body: TradeIn) -> dict:
    with auth.api_session(request, write=True) as (key, db):
        inst = _instrument(db, body.ticker)
        if body.date > clock.today():
            raise HTTPException(400, "date is in the future")
        trade = owned(
            db,
            Trade(
                instrument_id=inst.id,
                date=body.date,
                type=body.type,
                quantity=body.units,
                unit_price=body.unit_price,
                brokerage=body.brokerage,
                fx_rate=Decimal(1) if inst.currency == "AUD" else body.fx_rate,
                note=body.note or "via API",
            ),
        )
        # Same guard, and the same explanation, as the web form: a sell that
        # drives the running balance negative would later break the FY report's
        # FIFO matcher.
        held = db.scalars(select(Trade).where(Trade.instrument_id == inst.id)).all()
        breach = queries.balance_breach(list(held), trade)
        if breach is not None:
            raise HTTPException(409, queries.breach_message(inst.ticker, breach))
        db.add(trade)
        db.flush()
        return {"id": trade.id, "ticker": inst.ticker, "date": trade.date.isoformat()}


@router.post("/dividends", status_code=201)
def create_dividend(request: Request, body: DividendIn) -> dict:
    with auth.api_session(request, write=True) as (key, db):
        inst = _instrument(db, body.ticker)
        existing = db.scalar(
            select(Dividend).where(
                Dividend.instrument_id == inst.id,
                Dividend.date == body.date,
                Dividend.cash_amount == body.cash_amount,
            )
        )
        if existing is not None:
            raise HTTPException(
                409, f"a {inst.ticker} dividend of {body.cash_amount} on {body.date} exists"
            )
        dividend = owned(
            db,
            Dividend(
                instrument_id=inst.id,
                date=body.date,
                cash_amount=body.cash_amount,
                fx_rate=Decimal(1) if inst.currency == "AUD" else body.fx_rate,
                franking_credits=body.franking_credits,
                note=body.note or "via API",
            ),
        )
        db.add(dividend)
        db.flush()
        return {"id": dividend.id, "ticker": inst.ticker, "date": body.date.isoformat()}
