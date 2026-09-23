"""Reading a second portfolio on purpose, which the tenancy filter forbids.

`tenancy` is built to make a cross-portfolio read impossible: a session is
confined to one portfolio and a scoped query with none bound raises
(decisions.md #2). Moving a holding between two portfolios is the one operation
that legitimately needs both — it has to validate the destination's timeline
before it writes into it.

The existing escape hatch is the wrong tool. `unscoped_session` drops the
filter entirely, for every portfolio at once, and its docstring says never to
use it to serve a request. What a move needs is narrower: *this user's other
portfolio, for the length of one block.*

So `as_portfolio` checks the membership ITSELF rather than trusting the caller.
A primitive that rebinds the filter on request is a cross-tenant read waiting
for one careless call site; one that refuses unless the user is a member with
the right role cannot be misused that way, and the check lives beside the thing
it guards.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import factories as fac
from app import tenancy
from app.models import WRITE_ROLES, PortfolioMember, Trade


@pytest.fixture
def two_portfolios(db):
    """One user in two portfolios, each holding units of the same instrument.

    Returns (session, user, first, second), left bound to `first` the way a
    request arrives. The instrument is shared because that is the real case —
    `Instrument` is a catalogue, not portfolio-scoped, so what differs between
    the two is only which trades each can see.
    """
    user = fac.make_user(db, "both@example.test")
    first = fac.make_portfolio(db, "First", owner=user)
    second = fac.make_portfolio(db, "Second", owner=user)
    db.flush()

    tenancy.bind(db, first.id, user.id)
    acme = fac.make_instrument(db, "ACME", asset_class="share")
    fac.add_trade(db, acme, "2026-01-05", "buy", 100, "4.00")

    tenancy.bind(db, second.id, user.id)
    fac.add_trade(db, acme, "2026-02-05", "buy", 50, "5.00")

    tenancy.bind(db, first.id, user.id)
    db.flush()
    return db, user, first, second


def _units(session) -> list:
    """Quantities visible to this session, newest last. A plain select, never
    `db.get`: `get` can answer from the identity map and would return a row
    this session is not allowed to see."""
    return [t.quantity for t in session.scalars(select(Trade).order_by(Trade.id))]


# --------------------------------------------------------------------------- #
# What it does
# --------------------------------------------------------------------------- #

def test_the_other_portfolios_rows_become_visible_inside_the_block(two_portfolios):
    session, user, _first, second = two_portfolios

    with tenancy.as_portfolio(session, second.id, user_id=user.id):
        inside = _units(session)

    assert inside == [50]


def test_the_original_portfolio_is_restored_afterwards(two_portfolios):
    session, user, first, second = two_portfolios

    with tenancy.as_portfolio(session, second.id, user_id=user.id):
        pass

    assert tenancy.current_portfolio_id(session) == first.id
    assert _units(session) == [100]


def test_the_binding_is_restored_even_when_the_block_raises(two_portfolios):
    """A move that fails validation must not leave the request reading somebody
    else's portfolio for the rest of its life."""
    session, user, first, second = two_portfolios

    with pytest.raises(ValueError):
        with tenancy.as_portfolio(session, second.id, user_id=user.id):
            raise ValueError("validation said no")

    assert tenancy.current_portfolio_id(session) == first.id
    assert _units(session) == [100]


# --------------------------------------------------------------------------- #
# What it refuses
# --------------------------------------------------------------------------- #

def test_a_portfolio_the_user_is_not_in_is_refused(db):
    """The whole point: this cannot be used to read a stranger's portfolio."""
    intruder = fac.make_user(db, "intruder@example.test")
    mine = fac.make_portfolio(db, "Mine", owner=intruder)
    stranger = fac.make_user(db, "stranger@example.test")
    theirs = fac.make_portfolio(db, "Theirs", owner=stranger)
    db.flush()
    tenancy.bind(db, mine.id, intruder.id)

    with pytest.raises(tenancy.TenancyError):
        with tenancy.as_portfolio(db, theirs.id, user_id=intruder.id):
            pass

    assert tenancy.current_portfolio_id(db) == mine.id


def test_a_viewer_is_refused_when_write_access_is_asked_for(db):
    """Reading the target is not enough to move something into it."""
    user = fac.make_user(db, "viewer@example.test")
    home = fac.make_portfolio(db, "Home", owner=user)
    other = fac.make_portfolio(db, "Other")
    db.add(PortfolioMember(portfolio_id=other.id, user_id=user.id, role="viewer"))
    db.flush()
    tenancy.bind(db, home.id, user.id)

    # Visible for reading…
    with tenancy.as_portfolio(db, other.id, user_id=user.id):
        pass

    # …but not for being written into.
    with pytest.raises(tenancy.TenancyError):
        with tenancy.as_portfolio(db, other.id, user_id=user.id, roles=WRITE_ROLES):
            pass


def test_a_missing_user_is_refused_before_any_lookup(db):
    """No user id is refused on its own terms, not by the query missing.

    The message is asserted because it is the only thing that tells the two
    refusals apart, and the distinction is the point: `user_id == None` renders
    as `IS NULL`, which finds no member and raises anyway. So the early return
    looks redundant and is not — it is what keeps this a refusal rather than an
    accident of how a non-nullable column compares. Delete it and this test
    goes green, which is how the first version of it passed.
    """
    user = fac.make_user(db, "someone@example.test")
    home = fac.make_portfolio(db, "Home", owner=user)
    db.flush()
    tenancy.bind(db, home.id, user.id)

    with pytest.raises(tenancy.TenancyError, match="no user"):
        with tenancy.as_portfolio(db, home.id, user_id=None):
            pass
