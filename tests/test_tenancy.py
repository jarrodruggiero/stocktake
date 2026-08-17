"""app/tenancy.py — the fail-closed filter that keeps one portfolio out of another.

This is a security boundary, so it is tested like one. Two guarantees matter and
each is covered on its own:

  * a session with NO portfolio must refuse to load personal data rather than
    quietly return every portfolio's rows;
  * a session bound to a portfolio must return only that portfolio's rows down
    EVERY load path — top-level select, eager load and lazy load. A miss in any
    one of the three serves somebody else's holdings, so all three get their own
    test rather than being folded into one.

Expectations here are structural rather than arithmetic: every assertion is
"these rows and no others", derived by hand from exactly what each fixture
plants. The neighbouring portfolio's rows are deliberately given unmistakable
values (999 units, $99 dividends) so a leak reads as a leak in the failure
message rather than as a subtle off-by-one.

Three tests at the bottom are xfail — holes in the fail-closed guarantee found
while writing this file. They assert the behaviour the module documents, not the
behaviour it currently has.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

import factories as fac
from app import tenancy
from app.models import (
    Dividend,
    FxRate,
    HoldingPref,
    Instrument,
    InvestmentPlan,
    PlannedPurchase,
    Price,
    SavedChart,
    Trade,
)

# Every model the filter is supposed to cover — the five portfolio-scoped ones
# plus the user-scoped chart. Written out rather than read off
# tenancy.SCOPED_MODELS on purpose: dropping a model from that tuple has to make
# a test fail, and a test that imports its own expectation cannot notice.
PERSONAL_MODELS = [Trade, Dividend, PlannedPurchase, HoldingPref, InvestmentPlan, SavedChart]


def _chart(name: str, *, user_id: int | None, portfolio_id: int | None) -> SavedChart:
    """A minimally valid SavedChart. Local helper — the shared factories module
    has no chart builder and must not be edited."""
    return SavedChart(name=name, spec={"grain": "holding", "x": "ticker"},
                      user_id=user_id, portfolio_id=portfolio_id)


@pytest.fixture
def two_portfolios(pf, owner):
    """Two portfolios holding the SAME instrument, session bound to the first.

    The shared instrument is the whole point: isolation then has to come from
    the portfolio filter, not from the two portfolios happening to hold
    different things. Committed and expunged at the end so later loads come from
    SQL rather than the identity map — an identity-map hit would pass these
    tests with the filter switched off entirely.
    """
    user, portfolio = owner
    acme = fac.make_instrument(pf, "ACME", name="Acme Industries")

    fac.add_trade(pf, acme, "2025-01-06", "buy", 10, "5.00", brokerage="9.50")
    fac.add_dividend(pf, acme, "2025-04-01", "12.00")

    other_user = fac.make_user(pf, "neighbour@example.test", name="Neighbour")
    other_portfolio = fac.make_portfolio(pf, "Neighbour portfolio", owner=other_user)
    fac.add_trade(pf, acme, "2025-02-03", "buy", 999, "7.00", brokerage="9.50",
                  portfolio_id=other_portfolio.id, user_id=other_user.id)
    fac.add_dividend(pf, acme, "2025-05-01", "99.00",
                     portfolio_id=other_portfolio.id, user_id=other_user.id)

    pf.commit()
    pf.expunge_all()
    return SimpleNamespace(
        session=pf, instrument=acme, user=user, portfolio=portfolio,
        other_user=other_user, other_portfolio=other_portfolio,
    )


# --------------------------------------------------------------------------- #
# Fail closed: no portfolio on the session
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("model", PERSONAL_MODELS, ids=lambda m: m.__name__)
def test_selecting_personal_data_with_no_portfolio_raises(db, model):
    """Every personal table refuses to load rather than serving all tenants."""
    with pytest.raises(tenancy.TenancyError):
        db.scalars(select(model)).all()


def test_eager_loading_personal_data_with_no_portfolio_raises(db):
    """The eager path is caught too. `select(Instrument)` on its own is legal,
    so the loader option is the only thing separating this from a price-feed
    query the guard must let through."""
    fac.make_instrument(db, "NOVA", name="Nova Metals")
    with pytest.raises(tenancy.TenancyError):
        db.scalars(select(Instrument).options(selectinload(Instrument.trades))).all()


def test_market_data_queries_need_no_portfolio(db):
    """Instruments, prices and FX are shared and the price feed runs with no
    portfolio bound — if these raised, the feed would stop dead."""
    nova = fac.make_instrument(db, "NOVA", name="Nova Metals")
    fac.add_prices(db, nova, [("2026-07-01", "10.00"), ("2026-07-02", "11.00")])
    fac.add_fx_series(db, "USDAUD", [("2026-07-01", "1.50")])
    db.expunge_all()

    assert [i.ticker for i in db.scalars(select(Instrument)).all()] == ["NOVA"]
    assert len(db.scalars(select(Price)).all()) == 2   # exactly the two rows added
    assert len(db.scalars(select(FxRate)).all()) == 1


def test_owned_refuses_to_stamp_a_row_with_no_portfolio(db):
    with pytest.raises(tenancy.TenancyError):
        tenancy.owned(db, Trade())


def test_owned_refuses_to_stamp_a_chart_with_no_user(db):
    """Charts are owned by a person, so a chart with no user context is as
    unstampable as a trade with no portfolio."""
    with pytest.raises(tenancy.TenancyError):
        tenancy.owned(db, _chart("Orphan", user_id=None, portfolio_id=None))


# --------------------------------------------------------------------------- #
# Cross-portfolio isolation, once per load path
# --------------------------------------------------------------------------- #

def test_top_level_select_returns_only_the_bound_portfolios_trades(two_portfolios):
    session = two_portfolios.session
    trades = session.scalars(select(Trade)).all()

    # Only the 10-unit buy planted for the bound portfolio; the neighbour's
    # 999-unit buy on the same instrument must not appear.
    assert [t.quantity for t in trades] == [Decimal("10")]
    assert all(t.portfolio_id == two_portfolios.portfolio.id for t in trades)


def test_eager_load_returns_only_the_bound_portfolios_trades(two_portfolios):
    session = two_portfolios.session
    inst = session.scalars(
        select(Instrument).options(selectinload(Instrument.trades))
    ).one()

    assert [t.quantity for t in inst.trades] == [Decimal("10")]


def test_lazy_load_returns_only_the_bound_portfolios_trades(two_portfolios):
    """The riskiest path: the criteria has to ride along on the parent object
    and be reapplied when the collection is touched later. queries.py and
    fyreport.py walk `instrument.trades` this way in a dozen places."""
    session = two_portfolios.session
    inst = session.scalars(select(Instrument)).one()

    assert [t.quantity for t in inst.trades] == [Decimal("10")]


def test_dividends_are_isolated_by_all_three_paths(two_portfolios):
    """Dividends carry the same leak risk as trades and are loaded the same
    three ways, so they get the same three-way check."""
    session = two_portfolios.session

    top_level = session.scalars(select(Dividend)).all()
    assert [d.cash_amount for d in top_level] == [Decimal("12.0000")]

    eager = session.scalars(
        select(Instrument).options(selectinload(Instrument.dividends))
    ).one()
    assert [d.cash_amount for d in eager.dividends] == [Decimal("12.0000")]

    session.expunge_all()
    lazy = session.scalars(select(Instrument)).one()
    assert [d.cash_amount for d in lazy.dividends] == [Decimal("12.0000")]


def test_joining_to_trades_does_not_reveal_another_portfolios_instruments(two_portfolios):
    """A join reaches the scoped table through the ON clause rather than the
    columns clause. Unfiltered, "instruments I have traded" would list every
    ticker anyone has ever bought."""
    session = two_portfolios.session
    nova = fac.make_instrument(session, "NOVA", name="Nova Metals")
    fac.add_trade(session, nova, "2025-03-04", "buy", 50, "2.00",
                  portfolio_id=two_portfolios.other_portfolio.id)
    session.commit()
    session.expunge_all()

    # NOVA is traded only by the neighbour, so only ACME may come back.
    assert session.scalars(select(Instrument.ticker).join(Trade)).all() == ["ACME"]


def test_selecting_a_column_of_personal_data_is_still_scoped(two_portfolios):
    """A bare column select names no ORM entity to load, so it is worth pinning
    that the criteria still reaches it — `select(Trade.quantity)` must not turn
    into a hole the day somebody writes one."""
    session = two_portfolios.session

    assert session.scalars(select(Trade.quantity)).all() == [Decimal("10")]


def test_counting_trades_counts_only_the_bound_portfolios(two_portfolios):
    """queries.py fingerprints its caches on this exact count. Counting the
    neighbour's trades too would leak how many they have, and would make one
    portfolio's writes invalidate another's cache."""
    session = two_portfolios.session

    # One trade planted for this portfolio; the neighbour's must not be counted.
    assert session.scalar(select(func.count()).select_from(Trade)) == 1


def test_holding_prefs_are_isolated_between_portfolios(two_portfolios):
    """Both portfolios legitimately hold a pref row for the same instrument (the
    unique constraint is per portfolio+instrument) and queries.py keys them by
    instrument id — a leak here puts the neighbour's private note on your
    holding."""
    session = two_portfolios.session
    inst_id = two_portfolios.instrument.id

    session.add(tenancy.owned(session, HoldingPref(instrument_id=inst_id, note="mine")))
    session.add(HoldingPref(instrument_id=inst_id, note="theirs",
                            portfolio_id=two_portfolios.other_portfolio.id))
    session.commit()
    session.expunge_all()

    assert [p.note for p in session.scalars(select(HoldingPref)).all()] == ["mine"]


def test_a_second_portfolio_sees_only_its_own_rows(two_portfolios, session_factory):
    """The mirror image of the tests above — proof the filter keys on whichever
    portfolio is bound, rather than always hiding the higher id."""
    with tenancy.portfolio_session(
        session_factory, two_portfolios.other_portfolio.id, two_portfolios.other_user.id
    ) as other:
        trades = other.scalars(select(Trade)).all()

    assert [t.quantity for t in trades] == [Decimal("999")]


# --------------------------------------------------------------------------- #
# SavedChart: scoped by USER, not by portfolio
# --------------------------------------------------------------------------- #

def test_another_users_charts_are_invisible_even_in_the_same_portfolio(two_portfolios):
    session = two_portfolios.session
    session.add(_chart("Mine", user_id=two_portfolios.user.id,
                       portfolio_id=two_portfolios.portfolio.id))
    # Same portfolio, different person: a portfolio_id filter would let this
    # through, which is exactly why charts are filtered on user_id instead.
    session.add(_chart("Theirs", user_id=two_portfolios.other_user.id,
                       portfolio_id=two_portfolios.portfolio.id))
    session.commit()
    session.expunge_all()

    assert [c.name for c in session.scalars(select(SavedChart)).all()] == ["Mine"]


def test_my_charts_follow_me_into_another_portfolio(two_portfolios):
    """The other half of user-scoping: a chart of mine filed against a different
    portfolio is still mine, so it stays visible."""
    session = two_portfolios.session
    session.add(_chart("Elsewhere", user_id=two_portfolios.user.id,
                       portfolio_id=two_portfolios.other_portfolio.id))
    session.commit()
    session.expunge_all()

    assert [c.name for c in session.scalars(select(SavedChart)).all()] == ["Elsewhere"]


# --------------------------------------------------------------------------- #
# Stamping new rows
# --------------------------------------------------------------------------- #

def test_before_flush_stamps_portfolio_and_recorder_on_new_rows(pf, owner):
    """The safety net behind `owned()`: a row added without it — as the workbook
    importer does in eight places — still lands in the bound portfolio."""
    user, portfolio = owner
    widget = fac.make_instrument(pf, "WIDGET", name="Widget Co")
    trade = Trade(instrument_id=widget.id, date=dt.date(2025, 6, 2), type="buy",
                  quantity=Decimal("4"), unit_price=Decimal("3.00"),
                  brokerage=Decimal("0"), fx_rate=Decimal("1"))
    assert trade.portfolio_id is None

    pf.add(trade)
    pf.flush()

    assert trade.portfolio_id == portfolio.id
    assert trade.user_id == user.id   # audit trail: who entered it


def test_before_flush_never_reassigns_a_row_that_already_has_an_owner(pf, owner):
    """Only blanks are filled. The importer's cross-portfolio runs — and the
    neighbour rows in this very file — depend on that."""
    _, portfolio = owner
    widget = fac.make_instrument(pf, "WIDGET", name="Widget Co")
    other = fac.make_portfolio(pf, "Somebody else")

    trade = fac.add_trade(pf, widget, "2025-06-02", "buy", 4, "3.00",
                          portfolio_id=other.id)

    assert trade.portfolio_id == other.id
    assert trade.portfolio_id != portfolio.id


def test_owned_sets_both_the_portfolio_and_the_recorder(pf, owner):
    user, portfolio = owner
    plan = tenancy.owned(pf, InvestmentPlan(name="Rotation"))

    assert plan.portfolio_id == portfolio.id
    assert plan.user_id == user.id


def test_owned_sets_the_user_on_a_chart(pf, owner):
    user, portfolio = owner
    chart = tenancy.owned(pf, SavedChart(name="Mine", spec={"x": "ticker"}))

    assert chart.user_id == user.id            # the scoping key for charts
    assert chart.portfolio_id == portfolio.id  # recorded, but not what filters


def test_stamping_leaves_models_without_a_recorder_column_alone(pf, owner):
    """HoldingPref has portfolio_id but no user_id. The stamper must fill the
    one and not invent the other."""
    _, portfolio = owner
    widget = fac.make_instrument(pf, "WIDGET", name="Widget Co")
    pref = HoldingPref(instrument_id=widget.id, note="watch the spread")

    pf.add(pref)
    pf.flush()

    assert pref.portfolio_id == portfolio.id
    assert not hasattr(pref, "user_id")


# --------------------------------------------------------------------------- #
# The sanctioned bypass
# --------------------------------------------------------------------------- #

def test_unscoped_session_sees_every_portfolios_rows(two_portfolios, session_factory):
    """The price feed's FX repair pass has to reach trades across all tenants,
    so this must both skip the filter AND skip the fail-closed guard — an
    unscoped session has no portfolio on it either, so the order of those two
    checks is load-bearing."""
    with tenancy.unscoped_session(session_factory) as s:
        quantities = sorted(t.quantity for t in s.scalars(select(Trade)).all())

    assert quantities == [Decimal("10"), Decimal("999")]  # both portfolios' buys


def test_allow_unscoped_drops_the_filter_on_an_already_open_session(two_portfolios):
    """The first-run claim of pre-auth rows takes this route: the rows it hunts
    have no owner yet, so no filter could ever find them."""
    session = two_portfolios.session
    assert len(session.scalars(select(Trade)).all()) == 1  # filtered to this portfolio

    tenancy.allow_unscoped(session)
    session.expunge_all()

    assert len(session.scalars(select(Trade)).all()) == 2  # both portfolios


# --------------------------------------------------------------------------- #
# The catalogue is deliberately shared
# --------------------------------------------------------------------------- #

def test_the_instrument_catalogue_is_shared_between_portfolios(two_portfolios,
                                                               session_factory):
    """One person adding a ticker gives everyone its price history, and nothing
    about a holding is inferable from a ticker's existence. Asserted so that a
    future "scope everything" change has to argue with a test."""
    with tenancy.portfolio_session(
        session_factory, two_portfolios.other_portfolio.id, two_portfolios.other_user.id
    ) as other:
        tickers = [i.ticker for i in other.scalars(select(Instrument)).all()]

    assert tickers == ["ACME"]   # added while bound to the FIRST portfolio


def test_prices_are_shared_between_portfolios(two_portfolios, session_factory):
    session = two_portfolios.session
    inst = session.scalars(select(Instrument)).one()
    fac.add_prices(session, inst, [("2026-07-31", "6.00")])
    session.commit()

    with tenancy.portfolio_session(
        session_factory, two_portfolios.other_portfolio.id, two_portfolios.other_user.id
    ) as other:
        closes = [p.close for p in other.scalars(select(Price)).all()]

    assert closes == [Decimal("6.000000")]   # Numeric(18, 6)


# --------------------------------------------------------------------------- #
# Holes in the fail-closed guarantee — recorded here, not fixed
# --------------------------------------------------------------------------- #

def test_lazy_loading_trades_from_an_unbound_session_is_refused(db, owner):
    """An unbound session may legally load an Instrument — the price feed does.
    Touching `.trades` afterwards is a select against a scoped model with no
    portfolio, which the module says must raise."""
    _, portfolio = owner
    acme = fac.make_instrument(db, "ACME", name="Acme Industries")
    fac.add_trade(db, acme, "2025-02-03", "buy", 999, "7.00", portfolio_id=portfolio.id)
    db.commit()
    db.expunge_all()

    inst = db.scalars(select(Instrument)).one()
    with pytest.raises(tenancy.TenancyError):
        _ = inst.trades


@pytest.mark.parametrize("shape", ["count-over-from", "join"])
def test_reaching_personal_data_through_from_or_join_is_refused(db, owner, shape):
    """Fixed 2026-08-02: `_touches_scoped` looked only at the selected entities
    and eager-load options, so a scoped table reached through FROM or JOIN
    walked straight past the guard."""
    _, portfolio = owner
    acme = fac.make_instrument(db, "ACME", name="Acme Industries")
    fac.add_trade(db, acme, "2025-02-03", "buy", 999, "7.00", portfolio_id=portfolio.id)
    db.commit()

    statement = (
        select(func.count()).select_from(Trade) if shape == "count-over-from"
        else select(Instrument).join(Trade)
    )
    with pytest.raises(tenancy.TenancyError):
        db.execute(statement).all()


def test_charts_are_not_readable_with_a_portfolio_but_no_user(db, owner, session_factory):
    """The user filter must apply even when no user is bound.

    Otherwise a session with a portfolio but no user reads EVERY user's charts
    — reachable through an API key whose creator has been deleted, nulling
    `created_by`. With no user, it must match nothing rather than everything."""
    _, portfolio = owner
    other_user = fac.make_user(db, "neighbour@example.test", name="Neighbour")
    db.add(_chart("Theirs", user_id=other_user.id, portfolio_id=portfolio.id))
    db.commit()

    with tenancy.portfolio_session(session_factory, portfolio.id, None) as s:
        # No user context, so no chart is this session's to read.
        assert [c.name for c in s.scalars(select(SavedChart)).all()] == []
