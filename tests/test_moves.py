"""Choosing what moves between portfolios, and what has to follow it.

Issue #36. The reporter expected the hard part to be copying trade details onto
a new instrument id in the destination. It is not: `Instrument` is a shared
catalogue, unique on `(exchange, ticker)` and deliberately NOT portfolio-scoped,
so a move is `portfolio_id = target` and nothing is copied at all.

The hard part is deciding what travels together.

  * **Balance.** Take a buy out and a sell left behind can exceed what is held.
    `queries.balance_breach` reports the FIRST such point, so the set is a
    fixpoint rather than one query — pull the offender in, walk again.
  * **Pairs.** A reinvested distribution is one event in two tables, joined by
    `Dividend.reinvest_trade_id`. Every reader assumes the two sit in the same
    portfolio, so the pair is coupled in BOTH directions and cannot be split.
  * **Cash.** Distributions do not touch the unit balance, so nothing forces
    them — but a portfolio that no longer holds the units has no business
    holding the income either, so they are offered.

Which DRP buy "belongs" to which portfolio is NOT derivable, and this module
does not guess: a DRP arose from units held at a record date, and once those
units are being split the attribution is a judgement. The dialog offers them
and a person decides. What is enforced is only what would otherwise leave the
data inconsistent.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

import factories as fac
from app import moves, tenancy
from app.models import PortfolioMember, Trade


@pytest.fixture
def home(db):
    """A portfolio with ACME: a buy, a later sell, a DRP pair and cash."""
    user = fac.make_user(db, "mover@example.test")
    source = fac.make_portfolio(db, "Source", owner=user)
    target = fac.make_portfolio(db, "Target", owner=user)
    db.flush()
    tenancy.bind(db, source.id, user.id)
    acme = fac.make_instrument(db, "ACME", asset_class="share")
    return db, user, source, target, acme


# --------------------------------------------------------------------------- #
# What the dialog is offered
# --------------------------------------------------------------------------- #

def test_every_trade_and_distribution_for_the_instrument_is_offered(home):
    db, _user, _source, _target, acme = home
    fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    fac.add_trade(db, acme, "2026-03-02", "sell", 40, "5.00")
    fac.add_drp(db, acme, "2026-04-01", "12.50", 3, "4.20")
    fac.add_dividend(db, acme, "2026-07-01", "25.00")
    db.flush()

    rows = moves.movable_rows(db, acme.id)

    # FOUR rows for five database rows: the reinvested distribution has no row
    # of its own, because it is half of the `drp` trade that carries the units.
    assert [r.kind for r in rows] == ["buy", "sell", "drp", "dividend"]
    assert [r.date for r in rows] == [
        dt.date(2026, 1, 5), dt.date(2026, 3, 2),
        dt.date(2026, 4, 1), dt.date(2026, 7, 1),
    ]
    drp = next(r for r in rows if r.kind == "drp")
    assert drp.pairs_with is not None


def test_a_row_carries_what_tells_two_trades_apart(home):
    """Units, value, date and note — enough to pick the right one of two buys
    of the same instrument, which is the whole reason this is a list."""
    db, _user, _source, _target, acme = home
    fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00",
                  brokerage="9.50", note="first tranche")
    db.flush()

    (row,) = moves.movable_rows(db, acme.id)

    assert row.units == Decimal("100")
    assert row.value == Decimal("400.00")   # units x price, brokerage excluded
    assert row.note == "first tranche"
    # Unset, not ten o'clock: MARKET_OPEN is what an unticked "set a time" box
    # stores, so reporting it as a time would show one on every trade.
    assert row.time is None


def test_a_cash_distribution_has_no_units_and_shows_its_cash(home):
    db, _user, _source, _target, acme = home
    fac.add_dividend(db, acme, "2026-07-01", "25.00")
    db.flush()

    (row,) = moves.movable_rows(db, acme.id)

    assert row.units is None
    assert row.value == Decimal("25.00")


def test_only_this_portfolios_rows_are_offered(home):
    """The tenancy filter does the work; this pins that nobody bypassed it."""
    db, user, source, target, acme = home
    fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    tenancy.bind(db, target.id, user.id)
    fac.add_trade(db, acme, "2026-02-05", "buy", 7, "9.00")
    tenancy.bind(db, source.id, user.id)
    db.flush()

    rows = moves.movable_rows(db, acme.id)

    assert [r.units for r in rows] == [Decimal("100")]


# --------------------------------------------------------------------------- #
# What has to follow what was ticked
# --------------------------------------------------------------------------- #

def test_a_trade_nothing_depends_on_moves_alone(home):
    """The case the issue is actually about: a trade entered against the wrong
    portfolio, noticed before anything was built on it."""
    db, _user, _source, _target, acme = home
    trade = fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    db.flush()

    chosen = moves.expand(db, acme.id, trades={trade.id}, dividends=set())

    assert chosen.trades == {trade.id}
    assert chosen.dividends == set()
    assert chosen.added == set()


def test_a_sell_left_behind_is_pulled_in(home):
    """Move the buy and the sell can no longer be covered, so it comes too."""
    db, _user, _source, _target, acme = home
    buy = fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    sell = fac.add_trade(db, acme, "2026-03-02", "sell", 100, "5.00")
    db.flush()

    chosen = moves.expand(db, acme.id, trades={buy.id}, dividends=set())

    assert chosen.trades == {buy.id, sell.id}
    assert chosen.added == {("trade", sell.id)}


def test_the_pull_in_repeats_until_the_source_balances(home):
    """`balance_breach` reports only the FIRST breach, so one pass is not
    enough — this is why the set is a fixpoint and not a query."""
    db, _user, _source, _target, acme = home
    buy = fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    first = fac.add_trade(db, acme, "2026-02-02", "sell", 60, "5.00")
    second = fac.add_trade(db, acme, "2026-03-02", "sell", 40, "6.00")
    db.flush()

    chosen = moves.expand(db, acme.id, trades={buy.id}, dividends=set())

    assert chosen.trades == {buy.id, first.id, second.id}


def test_a_buy_that_is_not_needed_is_left_alone(home):
    """Only what actually breaks follows. Moving the later buy strands
    nothing, because the earlier one still covers the sell."""
    db, _user, _source, _target, acme = home
    fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    later = fac.add_trade(db, acme, "2026-02-05", "buy", 100, "4.50")
    fac.add_trade(db, acme, "2026-03-02", "sell", 80, "5.00")
    db.flush()

    chosen = moves.expand(db, acme.id, trades={later.id}, dividends=set())

    assert chosen.trades == {later.id}
    assert chosen.added == set()


def test_pulling_in_can_be_turned_off(home):
    """The pre-tick is a DEFAULT, not a decision.

    `expand` fills the dialog's tickboxes, so it pulls dependents in. The
    submitted form is the person's answer, and re-running the pull-in there
    would silently restore anything they unticked — so the POST path asks for
    `pull_in=False` and is validated instead. See `source_problem`.
    """
    db, _user, _source, _target, acme = home
    buy = fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    fac.add_trade(db, acme, "2026-03-02", "sell", 100, "5.00")
    db.flush()

    chosen = moves.expand(db, acme.id, trades={buy.id}, dividends=set(),
                          pull_in=False)

    assert chosen.trades == {buy.id}
    assert chosen.added == set()


def test_a_pair_is_still_coupled_with_the_pull_in_off(home):
    """Unticking is allowed for a balance dependent and not for a pair: one is
    a judgement, the other the data model cannot represent."""
    db, _user, _source, _target, acme = home
    trade, dividend = fac.add_drp(db, acme, "2026-04-01", "12.50", 3, "4.20")
    db.flush()

    chosen = moves.expand(db, acme.id, trades={trade.id}, dividends=set(),
                          pull_in=False)

    assert chosen.dividends == {dividend.id}


# --------------------------------------------------------------------------- #
# Whether the SOURCE can spare it
# --------------------------------------------------------------------------- #

def test_a_selection_that_strands_the_source_is_reported(home):
    """What replaces the pull-in when somebody unticks a dependent: the move
    is refused and the message names the sell that would be left short."""
    db, _user, _source, _target, acme = home
    buy = fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    fac.add_trade(db, acme, "2026-03-02", "sell", 100, "5.00")
    db.flush()

    problem = moves.source_problem(db, acme.id, trades={buy.id})

    assert problem is not None
    assert "ACME" in problem


def test_a_selection_the_source_can_spare_is_accepted(home):
    db, _user, _source, _target, acme = home
    buy = fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    sell = fac.add_trade(db, acme, "2026-03-02", "sell", 100, "5.00")
    db.flush()

    assert moves.source_problem(db, acme.id,
                                trades={buy.id, sell.id}) is None


# --------------------------------------------------------------------------- #
# The DRP pair, which cannot be split
# --------------------------------------------------------------------------- #

def test_ticking_a_reinvested_buy_brings_its_distribution(home):
    db, _user, _source, _target, acme = home
    trade, dividend = fac.add_drp(db, acme, "2026-04-01", "12.50", 3, "4.20")
    db.flush()

    chosen = moves.expand(db, acme.id, trades={trade.id}, dividends=set())

    assert chosen.dividends == {dividend.id}
    assert chosen.added == {("dividend", dividend.id)}


def test_ticking_the_distribution_brings_its_reinvested_buy(home):
    """Coupled both ways. Half a pair in each portfolio would leave the source
    showing income whose units are invisible, and the target the reverse."""
    db, _user, _source, _target, acme = home
    trade, dividend = fac.add_drp(db, acme, "2026-04-01", "12.50", 3, "4.20")
    db.flush()

    chosen = moves.expand(db, acme.id, trades=set(), dividends={dividend.id})

    assert chosen.trades == {trade.id}


def test_the_balance_walk_can_only_ever_pull_in_a_sell(home):
    """Why one coupling pass is enough, stated as a test.

    A second pass after the walk looks necessary — it would matter if the walk
    could drag in a DRP trade, whose dividend nobody ticked. It cannot: only a
    sell drives the running total negative (buys and DRPs add to it, and
    `quantity > 0` is a CHECK constraint), and a sell is never half of a pair.

    Here the source keeps the DRP's 3 units and balances on them, so the pair
    stays put while the sell follows the buy out.
    """
    db, _user, _source, _target, acme = home
    buy = fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    drp_trade, drp_dividend = fac.add_drp(db, acme, "2026-02-01", "12.50", 3, "4.20")
    sell = fac.add_trade(db, acme, "2026-03-02", "sell", 103, "5.00")
    db.flush()

    chosen = moves.expand(db, acme.id, trades={buy.id}, dividends=set())

    assert chosen.trades == {buy.id, sell.id}
    assert drp_trade.id not in chosen.trades
    assert drp_dividend.id not in chosen.dividends
    assert all(kind == "trade" for kind, _ in chosen.added)


def test_cash_is_never_pulled_in_on_its_own(home):
    """A distribution does not affect the balance, so nothing forces it. It is
    offered, not assumed — moving it is a judgement about where the income
    belongs, and the dialog asks."""
    db, _user, _source, _target, acme = home
    buy = fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    fac.add_dividend(db, acme, "2026-07-01", "25.00")
    db.flush()

    chosen = moves.expand(db, acme.id, trades={buy.id}, dividends=set())

    assert chosen.dividends == set()


# --------------------------------------------------------------------------- #
# Whether the destination can take it
# --------------------------------------------------------------------------- #

def test_a_selection_the_target_cannot_absorb_is_refused(home):
    """A sell moved into a portfolio holding nothing would go negative there.
    Fixing it means ticking more buys, which is a choice — so this refuses and
    names the trade rather than guessing at the answer."""
    db, user, _source, target, acme = home
    fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    sell = fac.add_trade(db, acme, "2026-03-02", "sell", 40, "5.00")
    db.flush()

    problem = moves.target_problem(
        db, acme.id, target.id, user_id=user.id,
        trades={sell.id}, dividends=set(),
    )

    assert problem is not None
    assert "ACME" in problem


def test_a_selection_the_target_can_absorb_is_accepted(home):
    db, user, _source, target, acme = home
    buy = fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    sell = fac.add_trade(db, acme, "2026-03-02", "sell", 40, "5.00")
    db.flush()

    problem = moves.target_problem(
        db, acme.id, target.id, user_id=user.id,
        trades={buy.id, sell.id}, dividends=set(),
    )

    assert problem is None


def test_the_target_check_leaves_the_session_where_it_found_it(home):
    """It reads another portfolio to do its job. The request carries on
    afterwards, so the binding has to come back."""
    db, user, source, target, acme = home
    buy = fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    db.flush()

    moves.target_problem(db, acme.id, target.id, user_id=user.id,
                         trades={buy.id}, dividends=set())

    assert tenancy.current_portfolio_id(db) == source.id


def test_a_target_the_user_cannot_write_to_is_refused(home):
    """Authorisation is not the template's job — the check is server-side."""
    db, user, _source, _target, acme = home
    outsider = fac.make_portfolio(db, "Someone else's")
    db.add(PortfolioMember(portfolio_id=outsider.id, user_id=user.id,
                           role="viewer"))
    buy = fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    db.flush()

    with pytest.raises(tenancy.TenancyError):
        moves.target_problem(db, acme.id, outsider.id, user_id=user.id,
                             trades={buy.id}, dividends=set())


# --------------------------------------------------------------------------- #
# Performing it
# --------------------------------------------------------------------------- #

def test_the_move_reassigns_the_rows_and_nothing_else(home):
    db, user, source, target, acme = home
    buy = fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    kept = fac.add_trade(db, acme, "2026-02-05", "buy", 10, "4.50")
    db.flush()
    buy_id, kept_id = buy.id, kept.id

    moves.perform(db, target.id, user_id=user.id,
                  trades={buy_id}, dividends=set())
    db.flush()

    assert db.get(Trade, buy_id).portfolio_id == target.id
    assert db.get(Trade, kept_id).portfolio_id == source.id


def test_the_instrument_is_not_touched(home):
    """No copying, no new instrument, no second catalogue row — the whole
    premise of the issue, pinned so a future refactor cannot reintroduce it."""
    db, user, _source, target, acme = home
    buy = fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")
    db.flush()
    before = acme.id

    moves.perform(db, target.id, user_id=user.id,
                  trades={buy.id}, dividends=set())
    db.flush()

    assert db.get(Trade, buy.id).instrument_id == before
