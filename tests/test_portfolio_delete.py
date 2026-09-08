"""Removing the last member, and the deletion it offers instead.

A portfolio is reachable only through membership — `load_auth` builds the list
from `portfolio_member`, and there is no admin override. So removing the only
member would leave a portfolio holding every trade and visible to nobody.

The rules that matter are about what cannot happen by accident.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import factories as fac
from app import tenancy
from app.models import Portfolio, PortfolioMember, Trade, User
from test_routes import make_login, session_csrf

HTML = {"accept": "text/html"}


def _members(client, session_factory):
    with session_factory() as db:
        rows = db.scalars(select(PortfolioMember)).all()
        return {r.id: r.role for r in rows}


@pytest.fixture
def owner(client, session_factory):
    make_login(client, session_factory)
    return client


# --------------------------------------------------------------------------- #
# What must not happen by accident
# --------------------------------------------------------------------------- #

def test_the_only_member_is_not_removed_without_saying_delete(owner, session_factory):
    """The portfolio would still exist, hold every trade, and be reachable by
    nobody — membership is the only route to one."""
    with session_factory() as db:
        member_id = db.scalar(select(PortfolioMember)).id

    resp = owner.post(f"/members/{member_id}/remove", follow_redirects=False,
                      data={"_csrf": session_csrf(session_factory)})

    assert "needs+an+owner" in resp.headers["location"]
    with session_factory() as db:
        assert db.scalar(select(PortfolioMember)) is not None
        assert db.scalar(select(Portfolio)) is not None


def test_the_last_owner_is_not_removed_while_others_remain(owner, session_factory):
    """Different case, same refusal: the portfolio would be left with members
    and nobody able to administer it. No deletion is offered — other people
    are still in it."""
    with session_factory() as db:
        second = fac.make_user(db, "second@example.test")
        portfolio = db.scalar(select(Portfolio))
        db.add(PortfolioMember(portfolio_id=portfolio.id, user_id=second.id,
                               role="member"))
        db.commit()
        owner_row = db.scalar(select(PortfolioMember).where(
            PortfolioMember.role == "owner"))
        owner_id = owner_row.id

    resp = owner.post(f"/members/{owner_id}/remove", follow_redirects=False,
                      data={"_csrf": session_csrf(session_factory),
                            "delete_portfolio": "1"})

    # Even ticked: the offer is for an EMPTY portfolio, not a populated one.
    assert "needs+an+owner" in resp.headers["location"]
    with session_factory() as db:
        assert db.scalar(select(Portfolio)) is not None


def test_a_member_who_is_not_the_last_is_removed_normally(owner, session_factory):
    with session_factory() as db:
        second = fac.make_user(db, "second@example.test")
        portfolio = db.scalar(select(Portfolio))
        db.add(PortfolioMember(portfolio_id=portfolio.id, user_id=second.id,
                               role="member"))
        db.commit()
        their_id = db.scalar(select(PortfolioMember).where(
            PortfolioMember.role == "member")).id

    owner.post(f"/members/{their_id}/remove", follow_redirects=False,
               data={"_csrf": session_csrf(session_factory)})

    assert len(_members(owner, session_factory)) == 1


# --------------------------------------------------------------------------- #
# The deletion it offers instead
# --------------------------------------------------------------------------- #

def test_saying_delete_removes_the_portfolio_and_everything_in_it(
    owner, session_factory
):
    import datetime as dt

    with session_factory() as db:
        portfolio = db.scalar(select(Portfolio))
        user = db.scalar(select(User))
        alpha = fac.make_instrument(db, "ALPHA")
        db.flush()
        fac.add_trade(db, alpha, dt.date(2026, 1, 5), "buy", 100, "10.00",
                      portfolio_id=portfolio.id, user_id=user.id)
        db.commit()
        member_id = db.scalar(select(PortfolioMember)).id
    with tenancy.unscoped_session(session_factory) as session:
        assert session.scalar(select(Trade)) is not None

    owner.post(f"/members/{member_id}/remove", follow_redirects=False,
               data={"_csrf": session_csrf(session_factory),
                     "delete_portfolio": "1"})

    with session_factory() as db:
        assert db.scalar(select(Portfolio)) is None
        assert db.scalar(select(PortfolioMember)) is None
    # Trades are portfolio-scoped, so reading them needs a session that says
    # so. The cascade is the whole reason this is one action: a portfolio row
    # left behind with its trades would be unreachable by anyone.
    with tenancy.unscoped_session(session_factory) as session:
        assert session.scalar(select(Trade)) is None


def test_the_account_survives_the_portfolio(owner, session_factory):
    """Deleting a portfolio is not deleting a person. They keep their account,
    their password and any other portfolio they are in."""
    with session_factory() as db:
        member_id = db.scalar(select(PortfolioMember)).id

    owner.post(f"/members/{member_id}/remove", follow_redirects=False,
               data={"_csrf": session_csrf(session_factory),
                     "delete_portfolio": "1"})

    with session_factory() as db:
        assert db.scalar(select(User)) is not None


def test_they_are_not_left_pointing_at_a_portfolio_that_is_gone(
    owner, session_factory
):
    """Deleting the portfolio you are looking at must land you somewhere real.

    Nothing in the delete does that: `load_auth` falls back when the session's
    active portfolio is not one of your memberships — the same path as being
    removed from one. This is here because that is load-bearing and lives a
    long way from the button.
    """
    with session_factory() as db:
        keep = fac.make_portfolio(db, "Still here", owner=db.scalar(select(User)))
        db.commit()
        keep_id = keep.id
        gone = db.scalar(select(PortfolioMember).where(
            PortfolioMember.portfolio_id != keep_id))

    owner.post(f"/members/{gone.id}/remove", follow_redirects=False,
               data={"_csrf": session_csrf(session_factory),
                     "delete_portfolio": "1"})

    assert owner.get("/", headers=HTML, follow_redirects=False).status_code == 200


def test_only_an_owner_can_delete(client, session_factory):
    make_login(client, session_factory, email="member@example.test", admin=False)
    with session_factory() as db:
        row = db.scalar(select(PortfolioMember))
        row.role = "member"
        db.commit()
        member_id = row.id

    resp = client.post(f"/members/{member_id}/remove", follow_redirects=False,
                       data={"_csrf": session_csrf(session_factory),
                             "delete_portfolio": "1"})

    assert resp.status_code == 403
    with session_factory() as db:
        assert db.scalar(select(Portfolio)) is not None


# --------------------------------------------------------------------------- #
# What the page offers
# --------------------------------------------------------------------------- #

def test_the_offer_appears_only_for_the_last_member(owner, session_factory):
    """A tickbox that deletes a portfolio must not be on the row of somebody
    who simply has colleagues."""
    page = owner.get("/members", headers=HTML).text
    assert 'data-last="1"' in page

    with session_factory() as db:
        second = fac.make_user(db, "second@example.test")
        db.add(PortfolioMember(portfolio_id=db.scalar(select(Portfolio)).id,
                               user_id=second.id, role="member"))
        db.commit()

    assert 'data-last="1"' not in owner.get("/members", headers=HTML).text
