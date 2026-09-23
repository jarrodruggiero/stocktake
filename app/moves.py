"""Moving a holding's rows from one portfolio to another — issue #36.

The move itself is one column: `portfolio_id = target`. `Instrument` is a shared
catalogue, unique on `(exchange, ticker)` and deliberately not portfolio-scoped,
so nothing is copied and no instrument is created in the destination. The
reporter expected that to be the hard part; it is not.

What is hard is deciding what travels together, and there are exactly three
rules. Two are enforced because breaking them leaves the data inconsistent, and
the third is left to a person because the data does not contain the answer.

**Balance (enforced, and it pulls rows in).** Take a buy out of a portfolio and
a sell left behind may exceed what is still held. `queries.balance_breach`
reports only the FIRST such point, so the set of rows that must follow is a
fixpoint: pull the offender in, walk the timeline again, repeat.

**Pairs (enforced, and invisible).** A reinvested distribution is one event
across two tables, joined by `Dividend.reinvest_trade_id`, and every reader
assumes the halves share a portfolio. So `movable_rows` offers a pair as ONE
row — the `drp` trade, which carries the units — and there is no way to tick
half of it. `_couple` still applies the rule in both directions server-side,
because the posted ids are user input and a hand-made request must not be able
to split what the dialog cannot.

**Cash (offered, never assumed).** A distribution does not touch the unit
balance, so nothing forces it to move. Which DRP buy "belongs" to which
portfolio is likewise not derivable — a DRP arose from units held at a record
date, and once those units are being split the attribution is a judgement. The
dialog offers them and a person decides.

The destination is validated but never auto-corrected. A sell moved somewhere
holding nothing goes negative there, and the fix is to tick more buys — which
is a choice, so `target_problem` says what is wrong and stops.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import queries, tenancy
from .models import (
    MARKET_OPEN,
    WRITE_ROLES,
    Dividend,
    Instrument,
    Trade,
    trade_order,
)


@dataclass(frozen=True)
class Movable:
    """One row the dialog offers, trade or distribution alike.

    `kind` is what it is called on screen (buy / sell / drp / dividend) and
    `is_trade` says which table `row_id` names — the two are not the same
    question, because a DRP is a trade whose partner is a dividend.
    """

    kind: str
    row_id: int
    is_trade: bool
    date: dt.date
    time: dt.time | None
    units: Decimal | None      # None for cash: there are no units to show
    value: Decimal             # trade value, or the distribution's cash
    note: str | None
    pairs_with: int | None = None   # the other half of a reinvested pair

    @property
    def token(self) -> str:
        """What the tickbox is worth: `trade:12` or `dividend:5`.

        One checkbox name across both tables, because on screen they are one
        list. Built here rather than in the template so the form and
        `_parse_rows` cannot disagree about the spelling.
        """
        return f"{'trade' if self.is_trade else 'dividend'}:{self.row_id}"


@dataclass
class Selection:
    """What will actually move, and what the person did not tick themselves.

    `added` exists so the dialog can pre-tick the rest and say it did, rather
    than either refusing with a puzzle or moving rows nobody chose. It holds
    both kinds; the caller counts only the trades, because a coupled
    distribution has no tickbox of its own to explain.
    """

    trades: set[int] = field(default_factory=set)
    dividends: set[int] = field(default_factory=set)
    added: set[tuple[str, int]] = field(default_factory=set)


def movable_rows(session: Session, instrument_id: int) -> list[Movable]:
    """Everything this portfolio holds for one instrument, in timeline order.

    Trades and distributions in one list because they are one decision on
    screen. Only the current portfolio's rows: the tenancy filter does that,
    and nothing here works around it.
    """
    trades = list(session.scalars(
        select(Trade).where(Trade.instrument_id == instrument_id)
    ))
    dividends = list(session.scalars(
        select(Dividend).where(Dividend.instrument_id == instrument_id)
    ))
    reinvested = {d.reinvest_trade_id: d.id for d in dividends if d.reinvest_trade_id}

    rows = [
        Movable(
            kind=t.type,
            row_id=t.id,
            is_trade=True,
            date=t.date,
            # MARKET_OPEN is the "no time given" sentinel, not ten o'clock —
            # `_parse_time` stores it whenever the box is unticked. Reported as
            # unset so the dialog shows a time only where one was chosen.
            time=None if t.time == MARKET_OPEN else t.time,
            units=t.quantity,
            # Brokerage excluded: this is what the units were worth, which is
            # what tells two trades apart. The cost base is the report's job.
            value=(t.quantity * t.unit_price),
            note=t.note,
            pairs_with=reinvested.get(t.id),
        )
        for t in trades
    ] + [
        Movable(
            kind="dividend",
            row_id=d.id,
            is_trade=False,
            date=d.date,
            time=None,
            units=None,
            value=d.cash_amount,
            note=d.note,
        )
        # A REINVESTED distribution gets no row of its own. It is half of the
        # DRP trade above, which carries the units and points back at it, so
        # two rows would be two tickboxes for one indivisible thing. The
        # holding ledger shows the pair the same way, for the same reason.
        for d in dividends
        if d.reinvest_trade_id is None
    ]
    rows.sort(key=lambda r: (r.date, 0 if r.is_trade else 1, r.row_id))
    return rows


def _couple(session: Session, instrument_id: int, chosen: Selection) -> None:
    """Bring the other half of every reinvested pair in, both directions.

    Read from the table rather than from `movable_rows`, which offers a pair as
    a single row and so cannot express half of one. The dividend-to-trade
    direction is therefore unreachable from the dialog and kept anyway: the
    posted ids are user input, and a request naming a reinvested dividend on
    its own must not be able to split the pair.
    """
    pairs = session.execute(
        select(Dividend.reinvest_trade_id, Dividend.id).where(
            Dividend.instrument_id == instrument_id,
            Dividend.reinvest_trade_id.is_not(None),
        )
    ).all()
    for trade_id, dividend_id in pairs:
        if trade_id in chosen.trades and dividend_id not in chosen.dividends:
            chosen.dividends.add(dividend_id)
            chosen.added.add(("dividend", dividend_id))
        elif dividend_id in chosen.dividends and trade_id not in chosen.trades:
            chosen.trades.add(trade_id)
            chosen.added.add(("trade", trade_id))


def expand(
    session: Session,
    instrument_id: int,
    *,
    trades: set[int],
    dividends: set[int],
    pull_in: bool = True,
) -> Selection:
    """Everything that must move, given what was ticked.

    `pull_in` is the difference between filling in a form and reading one back.

      * **True** (opening the dialog) — dependents are added, so the tickboxes
        arrive with the rows that cannot be left behind already ticked.
      * **False** (the submitted form) — only pairs are coupled. The ticks are
        the person's answer, and a dependent they unticked deliberately must
        not be silently restored; `source_problem` refuses the move instead
        and says which sell would be left short.

    A pair is coupled either way. Unticking a balance dependent is a judgement
    somebody is allowed to make and be refused for; splitting a reinvested
    distribution is not a judgement, it is a shape the data cannot hold.

    Couple first, then walk the balance — and nothing after, which is worth
    saying because a second coupling pass looks obviously necessary and is not.
    **The balance walk can only ever add a sell.** `_walk` drives the running
    total down on sells alone; buys and DRPs add to it, and `quantity > 0` is a
    CHECK constraint, so neither can be the row that goes negative. A sell is
    never half of a reinvested pair, so the walk cannot drag one in, so there
    is nothing left for a second pass to find.

    Coupling before the walk matters for the opposite reason: it can add a DRP
    *trade*, and that trade has to be out of the source's timeline before the
    balance is judged.
    """
    chosen = Selection(trades=set(trades), dividends=set(dividends))
    _couple(session, instrument_id, chosen)
    if not pull_in:
        return chosen

    held = list(session.scalars(
        select(Trade).where(Trade.instrument_id == instrument_id)
    ))
    # Each pass drops the moving rows and asks what breaks next. Terminates:
    # every iteration adds an id from a finite set, and a row already moving
    # cannot be the breach because it is not in the walk.
    while True:
        remaining = [t for t in held if t.id not in chosen.trades]
        breach = queries.balance_breach(remaining)
        if breach is None:
            break
        chosen.trades.add(breach.trade.id)
        chosen.added.add(("trade", breach.trade.id))

    return chosen


def source_problem(
    session: Session, instrument_id: int, *, trades: set[int]
) -> str | None:
    """What this portfolio cannot spare, in words, or None if it can.

    The counterpart to `target_problem`, and the reason `pull_in=False` is
    safe: whatever somebody unticks, the move still cannot leave either side
    holding a negative number of units. The difference is that they are told,
    rather than overruled.
    """
    remaining = list(session.scalars(
        select(Trade).where(
            Trade.instrument_id == instrument_id, Trade.id.not_in(trades or {-1})
        )
    ))
    instrument = session.get(Instrument, instrument_id)   # catalogue, unscoped
    breach = queries.balance_breach(sorted(remaining, key=trade_order))
    if breach is None:
        return None
    return queries.breach_message(
        instrument.ticker if instrument else "this holding", breach
    )


def target_problem(
    session: Session,
    instrument_id: int,
    target_id: int,
    *,
    user_id: int | None,
    trades: set[int],
    dividends: set[int],
) -> str | None:
    """What the destination cannot accept, in words, or None if it can.

    Reads the destination through `tenancy.as_portfolio`, which checks the
    membership itself and restores the binding afterwards — the request carries
    on with this session. `roles=WRITE_ROLES` because this is a precondition
    for writing there, so the authorisation lives with the scope change rather
    than in a template that decided whether to draw a button.
    """
    # A select, not `session.get` per id: `get` can answer from the identity
    # map without going near the tenancy filter, so it would happily return a
    # row belonging to some other portfolio. The route filters the posted ids
    # too; this does not depend on it having done so.
    moving = list(session.scalars(
        select(Trade).where(
            Trade.instrument_id == instrument_id, Trade.id.in_(trades or {-1})
        )
    ))
    instrument = session.get(Instrument, instrument_id)   # catalogue, unscoped
    ticker = instrument.ticker if instrument else "this holding"

    with tenancy.as_portfolio(session, target_id, user_id=user_id,
                              roles=WRITE_ROLES):
        # A plain select: `session.get` can answer from the identity map, which
        # would hand back the source's rows it has already loaded.
        existing = list(session.scalars(
            select(Trade).where(Trade.instrument_id == instrument_id)
        ))
    # Rows still belonging to the source are not in `existing` yet, so the walk
    # is the destination's timeline plus what is about to land in it.
    combined = sorted(existing + moving, key=trade_order)
    breach = queries.balance_breach(combined)
    return None if breach is None else queries.breach_message(ticker, breach)


def perform(
    session: Session,
    target_id: int,
    *,
    user_id: int | None,
    trades: set[int],
    dividends: set[int],
) -> None:
    """Reassign the chosen rows. Validation is the caller's; this only writes.

    A DCA step that recorded one of these trades is left pointing at a row its
    own portfolio can no longer see, so the link is cleared and the step goes
    back to planned — see `main.py`, which owns that because it owns the plan.
    """
    # Selects rather than `session.get`, for the reason in `target_problem`:
    # `get` can return a row from the identity map that this portfolio does not
    # own, and this function WRITES. The filter is the last thing standing
    # between a posted id and somebody else's trade.
    for row in session.scalars(
        select(Trade).where(Trade.id.in_(trades or {-1}))
    ):
        row.portfolio_id = target_id
    for row in session.scalars(
        select(Dividend).where(Dividend.id.in_(dividends or {-1}))
    ):
        row.portfolio_id = target_id
