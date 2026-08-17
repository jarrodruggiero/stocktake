"""Automatic per-portfolio filtering of the tables that hold personal data.

Every `select()` issued through a session carrying a portfolio id gets an extra
`WHERE portfolio_id = :pid` on the models listed in `SCOPED_MODELS` — top-level
queries, eager loads (`selectinload(Instrument.trades)`) and later lazy loads
alike. That is deliberately belt-and-braces: the read model in queries.py and
fyreport.py walks `instrument.trades` in a dozen places, and one forgotten
filter would show somebody else's portfolio.

Two rules make it fail closed:
  * a select touching a scoped model with NO portfolio id on the session
    raises, rather than quietly returning every portfolio's rows;
  * the only way to opt out is `unscoped_session()`, which is greppable and
    used for exactly one thing — the price feed's FX repair pass, which fixes
    market data across all users.

Writes are NOT covered (SQLAlchemy applies loader criteria to selects only), so
inserts must set `portfolio_id` themselves; `owned()` is the helper (plus a
before_flush net in `install()`, which also stamps `user_id` as "recorded by"
where the model has that column).

TRUST BOUNDARY — read this before assuming more than it gives you. This module
separates users *within the application*: it stops a bug in a route or query
from serving one person's portfolio to another. It is NOT a defence against
anyone holding the database. `python -m app.importer --user someone@else`,
`sqlite3 /data/portfolio.db`, a copy of the PVC or a restic snapshot all read
and write every account's data, and no CLI login could change that — the check
would run in the same process that already has the file. The boundary is
"can exec into the pod / reach the volume" (i.e. cluster admin), not "has an
account". Real separation between users would need per-user encryption keyed on
their password, which would also lock out the price feed, the FY reports and
the backups; not worth it here.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import event
from sqlalchemy import false as sql_false
from sqlalchemy.orm import Session, with_loader_criteria

from .models import (
    Dividend,
    HoldingPref,
    InvestmentPlan,
    PlannedPurchase,
    SavedChart,
    Trade,
)

# Every model with a portfolio_id column. Adding a personal-data table? Add it
# here — that is the whole registration step.
SCOPED_MODELS = (
    Trade,
    Dividend,
    PlannedPurchase,
    HoldingPref,
    InvestmentPlan,
)

# Owned by a person rather than a portfolio (their charts follow them between
# portfolios). Filtered the same way, on user_id — being in this tuple is what
# keeps them inside the fail-closed guarantee rather than relying on every
# query remembering a where clause.
USER_SCOPED_MODELS = (SavedChart,)

_PORTFOLIO_KEY = "portfolio_id"
_USER_KEY = "portfolio_user_id"
_BYPASS_KEY = "portfolio_unscoped"


class TenancyError(RuntimeError):
    """Raised when personal data is queried with no portfolio context."""


_UNBOUND_MESSAGE = (
    "query touches portfolio data with no portfolio on the session — use "
    "portfolio_session(...) or, for market-data maintenance, unscoped_session(...)"
)


def install(session_factory) -> None:
    """Attach the filter to a sessionmaker. Call once at startup."""

    @event.listens_for(session_factory, "before_flush")
    def _stamp_owner(session, _ctx, _instances) -> None:
        """New personal rows inherit the session's portfolio, and record who
        entered them.

        `owned()` says it explicitly at the call sites that matter; this is the
        safety net for bulk paths (the workbook importer creates rows in eight
        places) and for anything added later. Only fills blanks — it never
        reassigns an existing owner.
        """
        portfolio_id = session.info.get(_PORTFOLIO_KEY)
        user_id = session.info.get(_USER_KEY)
        if portfolio_id is None:
            return
        for obj in session.new:
            if not isinstance(obj, SCOPED_MODELS):
                continue
            if obj.portfolio_id is None:
                obj.portfolio_id = portfolio_id
            if user_id is not None and getattr(obj, "user_id", "absent") is None:
                obj.user_id = user_id

    @event.listens_for(session_factory, "do_orm_execute")
    def _apply_portfolio_filter(state) -> None:
        if not state.is_select or state.is_column_load:
            return
        session = state.session
        if session.info.get(_BYPASS_KEY):
            return
        portfolio_id = session.info.get(_PORTFOLIO_KEY)
        # A relationship load inherits the criteria from the query that loaded
        # its parent (propagate_to_loaders), so re-applying them here would
        # double up. But an UNBOUND session has no criteria to inherit, and a
        # lazy `instrument.trades` would then hand back every portfolio's rows
        # — so the refusal below still has to run before we return.
        if state.is_relationship_load:
            if portfolio_id is None and _touches_scoped(state):
                raise TenancyError(_UNBOUND_MESSAGE)
            return
        if portfolio_id is None:
            # No portfolio bound: fine for market data (the price feed runs this
            # way), a bug for anything personal.
            if _touches_scoped(state):
                raise TenancyError(_UNBOUND_MESSAGE)
            return
        # Applied to EVERY statement, not just ones naming a scoped model:
        # `select(Instrument)` carries the criteria forward to a later
        # `instrument.trades` lazy load (propagate_to_loaders). Skipping it here
        # because the statement "doesn't touch trades" is exactly how that lazy
        # load would come back unfiltered.
        for model in SCOPED_MODELS:
            state.statement = state.statement.options(
                with_loader_criteria(
                    model, model.portfolio_id == portfolio_id, include_aliases=True
                )
            )
        # User-scoped models are filtered whether or not a user is bound. With
        # no user, the criterion matches nothing rather than everything: a
        # session carrying a portfolio but no user (an API key whose creator
        # was deleted, nulling `created_by`) would otherwise read EVERY user's
        # saved charts.
        user_id = session.info.get(_USER_KEY)
        for model in USER_SCOPED_MODELS:
            criterion = (
                model.user_id == user_id if user_id is not None else sql_false()
            )
            state.statement = state.statement.options(
                with_loader_criteria(model, criterion, include_aliases=True)
            )


def _touches_scoped(state) -> bool:
    """True when the statement selects, or eager-loads, a scoped model.

    Cheap over-approximation: match on the mapper of every entity in the
    statement. Instrument-only queries (price feed, catalogue) skip the check so
    they keep working outside a request.
    """
    scoped = set(SCOPED_MODELS) | set(USER_SCOPED_MODELS)
    for desc in state.statement.column_descriptions:
        if desc.get("entity") in scoped:
            return True
    # A scoped table reached through FROM or JOIN rather than named as the
    # selected entity: `select(func.count()).select_from(Trade)` selects a
    # number, not a Trade, and `select(Instrument).join(Trade)` selects an
    # instrument — both read personal rows and neither shows up above.
    # Compared by table, because that is what a JOIN carries.
    scoped_tables = {model.__table__ for model in scoped}
    for element in getattr(state.statement, "get_final_froms", lambda: ())():
        if _mentions(element, scoped_tables):
            return True
    # Eager loads name their target in the statement's _with_options; simplest
    # reliable signal is the ORM path they walk.
    for opt in getattr(state.statement, "_with_options", ()):
        for attr in getattr(opt, "path", ()) or ():
            if getattr(getattr(attr, "entity", None), "class_", None) in scoped:
                return True
        target = getattr(getattr(opt, "path", None), "entity", None)
        if getattr(target, "class_", None) in scoped:
            return True
    return False


def _mentions(element, tables: set) -> bool:
    """Whether a FROM element is, or joins, one of these tables."""
    if element in tables:
        return True
    left, right = getattr(element, "left", None), getattr(element, "right", None)
    if left is not None and _mentions(left, tables):
        return True
    if right is not None and _mentions(right, tables):
        return True
    # A subquery keeps its own SELECT, which may reach a scoped table too.
    inner = getattr(getattr(element, "element", None), "get_final_froms", None)
    if inner is not None:
        return any(_mentions(f, tables) for f in inner())
    return False


def bind(session: Session, portfolio_id: int, user_id: int | None = None) -> Session:
    """Confine an already-open session to one portfolio's rows.

    `user_id` is stamped onto new rows as "recorded by" — it plays no part in
    what the session can read.
    """
    session.info[_PORTFOLIO_KEY] = portfolio_id
    session.info[_USER_KEY] = user_id
    return session


def allow_unscoped(session: Session) -> Session:
    """Drop the filter on an open session. Two callers only: the first-run
    claim of pre-auth rows (which have no owner yet, so no filter could find
    them) and the price feed's cross-portfolio FX repair."""
    session.info[_BYPASS_KEY] = True
    return session


@contextmanager
def portfolio_session(
    session_factory, portfolio_id: int, user_id: int | None = None
) -> Iterator[Session]:
    """A session whose reads are confined to one portfolio. Commits on success."""
    session = session_factory()
    session.info[_PORTFOLIO_KEY] = portfolio_id
    session.info[_USER_KEY] = user_id
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def unscoped_session(session_factory) -> Iterator[Session]:
    """Every portfolio's rows — market-data maintenance only (the price feed's
    trade-FX backfill). Never use this to serve a request."""
    session = session_factory()
    session.info[_BYPASS_KEY] = True
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def current_portfolio_id(session: Session) -> int | None:
    return session.info.get(_PORTFOLIO_KEY)


def current_user_id(session: Session) -> int | None:
    return session.info.get(_USER_KEY)


def owned(session: Session, obj):
    """Stamp a new row with the session's portfolio (and recorder) before add."""
    from .models import SavedChart as _SavedChart

    if isinstance(obj, _SavedChart):
        user_id = current_user_id(session)
        if user_id is None:
            raise TenancyError("cannot create a chart with no user context")
        obj.user_id = user_id
        obj.portfolio_id = current_portfolio_id(session)
        return obj
    portfolio_id = current_portfolio_id(session)
    if portfolio_id is None:
        raise TenancyError(
            f"cannot create {type(obj).__name__} with no portfolio context"
        )
    obj.portfolio_id = portfolio_id
    user_id = current_user_id(session)
    if user_id is not None and hasattr(obj, "user_id"):
        obj.user_id = user_id
    return obj
