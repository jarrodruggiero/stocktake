"""Invitations: single use, revocable, and the same link either way.

The rules worth pinning are the ones that decide who ends up with access to
somebody's holdings, so most of this file is about refusal.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

import factories as fac
from app import invites
from app.models import PortfolioInvite, PortfolioMember, User


@pytest.fixture
def owner_and_portfolio(session_factory):
    with session_factory() as db:
        owner = fac.make_user(db, "owner@example.test")
        portfolio = fac.make_portfolio(db, "Family", owner=owner)
        db.commit()
        return owner.id, portfolio.id


def _invite(db, portfolio_id: int, owner_id: int, role: str = "member") -> str:
    return invites.create(db, portfolio_id=portfolio_id, role=role,
                          created_by=owner_id)


# --------------------------------------------------------------------------- #
# Issuing and accepting
# --------------------------------------------------------------------------- #

def test_an_invite_gives_a_brand_new_account_the_role_it_names(
    session_factory, owner_and_portfolio
):
    owner_id, portfolio_id = owner_and_portfolio
    with session_factory() as db:
        token = _invite(db, portfolio_id, owner_id, role="viewer")
        joiner = fac.make_user(db, "joiner@example.test")
        db.flush()

        invites.accept(db, token, joiner)

        member = db.scalar(select(PortfolioMember).where(
            PortfolioMember.portfolio_id == portfolio_id,
            PortfolioMember.user_id == joiner.id))
        assert member.role == "viewer"


def test_an_existing_account_keeps_what_it_had_and_gains_the_portfolio(
    session_factory, owner_and_portfolio
):
    """Somebody already in another portfolio must not need a second identity to
    be handed a second one — the invite adds access, it does not replace it."""
    owner_id, portfolio_id = owner_and_portfolio
    with session_factory() as db:
        joiner = fac.make_user(db, "joiner@example.test")
        theirs = fac.make_portfolio(db, "Their own", owner=joiner)
        db.flush()
        token = _invite(db, portfolio_id, owner_id)

        invites.accept(db, token, joiner)

        held = {m.portfolio_id: m.role for m in db.scalars(
            select(PortfolioMember).where(PortfolioMember.user_id == joiner.id))}
        assert held == {theirs.id: "owner", portfolio_id: "member"}


def test_an_invite_never_demotes_an_existing_member(
    session_factory, owner_and_portfolio
):
    """A viewer invite sent to somebody who is already an owner must not take
    their portfolio away from them."""
    owner_id, portfolio_id = owner_and_portfolio
    with session_factory() as db:
        owner = db.get(User, owner_id)
        token = _invite(db, portfolio_id, owner_id, role="viewer")

        invites.accept(db, token, owner)

        member = db.scalar(select(PortfolioMember).where(
            PortfolioMember.portfolio_id == portfolio_id,
            PortfolioMember.user_id == owner_id))
        assert member.role == "owner"


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #

def test_an_invite_can_only_be_used_once(session_factory, owner_and_portfolio):
    owner_id, portfolio_id = owner_and_portfolio
    with session_factory() as db:
        token = _invite(db, portfolio_id, owner_id)
        first = fac.make_user(db, "first@example.test")
        second = fac.make_user(db, "second@example.test")
        db.flush()
        invites.accept(db, token, first)

        with pytest.raises(invites.InviteError, match="already been used"):
            invites.accept(db, token, second)

        assert db.scalar(select(PortfolioMember).where(
            PortfolioMember.user_id == second.id)) is None


def test_a_withdrawn_invite_is_refused(session_factory, owner_and_portfolio):
    owner_id, portfolio_id = owner_and_portfolio
    with session_factory() as db:
        token = _invite(db, portfolio_id, owner_id)
        row = db.scalar(select(PortfolioInvite))

        assert invites.revoke(db, invite_id=row.id, portfolio_id=portfolio_id)
        with pytest.raises(invites.InviteError, match="withdrawn"):
            invites.lookup(db, token)


def test_an_expired_invite_is_refused(session_factory, owner_and_portfolio):
    owner_id, portfolio_id = owner_and_portfolio
    with session_factory() as db:
        token = _invite(db, portfolio_id, owner_id)
        db.scalar(select(PortfolioInvite)).expires_at = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
        db.flush()

        with pytest.raises(invites.InviteError, match="expired"):
            invites.lookup(db, token)


def test_the_refusals_are_told_apart(session_factory, owner_and_portfolio):
    """"Already used" and "withdrawn" send somebody to different next steps,
    and one vague refusal sends them to neither."""
    owner_id, portfolio_id = owner_and_portfolio
    with session_factory() as db:
        reasons = set()
        for setup in ("used", "revoked", "junk"):
            token = _invite(db, portfolio_id, owner_id)
            row = db.scalar(select(PortfolioInvite).order_by(
                PortfolioInvite.id.desc()))
            if setup == "used":
                row.used_at = dt.datetime.now(dt.timezone.utc)
            elif setup == "revoked":
                row.revoked_at = dt.datetime.now(dt.timezone.utc)
            else:
                token = "not-a-real-token"
            db.flush()
            with pytest.raises(invites.InviteError) as caught:
                invites.lookup(db, token)
            reasons.add(str(caught.value))

        assert len(reasons) == 3


def test_a_revoked_invite_cannot_be_revoked_into_being_reusable(
    session_factory, owner_and_portfolio
):
    owner_id, portfolio_id = owner_and_portfolio
    with session_factory() as db:
        _invite(db, portfolio_id, owner_id)
        row = db.scalar(select(PortfolioInvite))
        invites.revoke(db, invite_id=row.id, portfolio_id=portfolio_id)

        assert not invites.revoke(db, invite_id=row.id, portfolio_id=portfolio_id)


def test_one_portfolio_cannot_revoke_anothers_invite(
    session_factory, owner_and_portfolio
):
    """The id comes from a form. Scoping the lookup to the portfolio is the
    whole defence against reaching somebody else's row."""
    owner_id, portfolio_id = owner_and_portfolio
    with session_factory() as db:
        other = fac.make_portfolio(db, "Someone else's",
                                   owner=db.get(User, owner_id))
        db.flush()
        _invite(db, portfolio_id, owner_id)
        row = db.scalar(select(PortfolioInvite))

        assert not invites.revoke(db, invite_id=row.id, portfolio_id=other.id)
        assert db.get(PortfolioInvite, row.id).revoked_at is None


def test_an_unknown_role_is_refused(session_factory, owner_and_portfolio):
    owner_id, portfolio_id = owner_and_portfolio
    with session_factory() as db:
        with pytest.raises(invites.InviteError):
            invites.create(db, portfolio_id=portfolio_id, role="administrator",
                           created_by=owner_id)


# --------------------------------------------------------------------------- #
# It is a credential
# --------------------------------------------------------------------------- #

def test_the_token_is_not_stored(session_factory, owner_and_portfolio):
    """A database copy is a list of invitations that were made, not a set of
    links somebody can use."""
    owner_id, portfolio_id = owner_and_portfolio
    with session_factory() as db:
        token = _invite(db, portfolio_id, owner_id)
        row = db.scalar(select(PortfolioInvite))

        assert token not in row.token_hash
        assert len(row.token_hash) == 64


def test_the_token_keeps_enough_entropy_to_be_unguessable(
    session_factory, owner_and_portfolio
):
    """It was shortened to keep the link sendable. The floor matters: the URL
    is the whole authorisation, and nothing rate-limits guesses at it."""
    owner_id, portfolio_id = owner_and_portfolio
    with session_factory() as db:
        token = _invite(db, portfolio_id, owner_id)

    assert len(token) >= 22          # >= 128 bits, base64url


def test_the_list_an_owner_sees_keeps_the_used_and_drops_the_withdrawn(
    session_factory, owner_and_portfolio
):
    """"Who did I let in" is what the list is really answering, so a spent
    invite stays. A withdrawn one was withdrawn."""
    owner_id, portfolio_id = owner_and_portfolio
    with session_factory() as db:
        joiner = fac.make_user(db, "joiner@example.test")
        db.flush()
        invites.accept(db, _invite(db, portfolio_id, owner_id), joiner)
        _invite(db, portfolio_id, owner_id)
        third = db.scalar(select(PortfolioInvite).order_by(
            PortfolioInvite.id.desc()))
        _invite(db, portfolio_id, owner_id)
        invites.revoke(db, invite_id=third.id, portfolio_id=portfolio_id)

        states = sorted(invites.state(i) for i in
                        invites.outstanding(db, portfolio_id))
        assert states == ["live", "used"]
