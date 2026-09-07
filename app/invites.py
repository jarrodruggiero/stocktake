"""Invitations to a portfolio.

Membership used to require the account to exist first — an owner picked a name
from a list — so there was no way to bring in somebody who had never signed in.
An invite is that missing piece, and it is deliberately not an OIDC feature: a
household that never configures a provider gets the same link, and the person
opening it creates an ordinary local account.

It is a **credential**, so it behaves like one: random, stored hashed, single
use, revocable, and shown exactly once. Whoever holds a live link gets the role
it names, which is why the person creating it is recorded and kept.

Accepting is one of two things, decided by whether the visitor already has an
account rather than by which link was sent:

* **no account** — create one, then join the portfolio at the invited role.
* **an account** — join the portfolio at the invited role, keeping everything
  they already had. Somebody already in another portfolio must not need a
  second identity to be handed a second one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from appkit import ensure_utc

from .models import MEMBER_ROLES, PortfolioInvite, PortfolioMember, User

# Long enough to send and act on, short enough that a forwarded email does not
# stay live for a year. Owners can always issue another.
INVITE_TTL = dt.timedelta(days=7)


class InviteError(Exception):
    """An invite that cannot be used. The message is safe to show."""


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create(db: DbSession, *, portfolio_id: int, role: str, created_by: int) -> str:
    """Issue an invite and return the token — the ONLY time it is readable.

    Stored hashed, exactly like a session token: a database copy is a list of
    invitations that were made, not a set of links that can be used.
    """
    if role not in MEMBER_ROLES:
        raise InviteError("Unknown role.")
    raw = secrets.token_urlsafe(32)
    db.add(PortfolioInvite(
        token_hash=_hash(raw),
        portfolio_id=portfolio_id,
        role=role,
        created_by=created_by,
        expires_at=_utcnow() + INVITE_TTL,
    ))
    db.flush()
    return raw


def lookup(db: DbSession, raw: str | None) -> PortfolioInvite:
    """The live invite behind this token, or raise saying which way it failed.

    Spent, withdrawn and expired are told apart on purpose. "This link has
    already been used" and "the owner withdrew it" send somebody to different
    next steps, and a single vague refusal sends them to neither.
    """
    if not raw:
        raise InviteError("That invitation link is not valid.")
    row = db.scalar(
        select(PortfolioInvite).where(PortfolioInvite.token_hash == _hash(raw)))
    if row is None:
        raise InviteError("That invitation link is not valid.")
    if row.revoked_at is not None:
        raise InviteError("That invitation was withdrawn.")
    if row.used_at is not None:
        raise InviteError("That invitation has already been used.")
    if ensure_utc(row.expires_at) < _utcnow():
        raise InviteError("That invitation has expired. Ask for a new one.")
    return row


def accept(db: DbSession, raw: str | None, user: User) -> PortfolioInvite:
    """Spend the invite, giving this account the role it names.

    Marked used BEFORE anything else can fail, so a link cannot be spent twice
    by two requests arriving together. Already being a member is not an error:
    the invite is still consumed, because it was offered and answered.
    """
    invite = lookup(db, raw)
    invite.used_at = _utcnow()
    invite.used_by = user.id
    db.flush()

    existing = db.scalar(
        select(PortfolioMember).where(
            PortfolioMember.portfolio_id == invite.portfolio_id,
            PortfolioMember.user_id == user.id,
        )
    )
    if existing is None:
        db.add(PortfolioMember(
            portfolio_id=invite.portfolio_id, user_id=user.id, role=invite.role))
    # An existing membership is left ALONE rather than overwritten: an invite
    # at a lower role must not quietly demote somebody who is already an owner.
    db.flush()
    return invite


def revoke(db: DbSession, *, invite_id: int, portfolio_id: int) -> bool:
    """Withdraw an unused invite. Scoped to the portfolio it belongs to, so an
    id from a form cannot reach somebody else's."""
    row = db.scalar(
        select(PortfolioInvite).where(
            PortfolioInvite.id == invite_id,
            PortfolioInvite.portfolio_id == portfolio_id,
        )
    )
    if row is None or row.used_at is not None or row.revoked_at is not None:
        return False
    row.revoked_at = _utcnow()
    db.flush()
    return True


def outstanding(db: DbSession, portfolio_id: int) -> list[PortfolioInvite]:
    """Invites still worth showing an owner: live, or recently spent.

    A withdrawn one disappears — it was withdrawn — but a used one stays
    visible, because "who did I let in, and when" is the question the list is
    really answering.
    """
    rows = db.scalars(
        select(PortfolioInvite)
        .where(PortfolioInvite.portfolio_id == portfolio_id,
               PortfolioInvite.revoked_at.is_(None))
        .order_by(PortfolioInvite.created_at.desc())
    ).all()
    return [row for row in rows
            if row.used_at is not None or ensure_utc(row.expires_at) >= _utcnow()]


def state(invite: PortfolioInvite) -> str:
    """What to call this one in a list: used, expired or live."""
    if invite.used_at is not None:
        return "used"
    if invite.revoked_at is not None:
        return "withdrawn"
    if ensure_utc(invite.expires_at) < _utcnow():
        return "expired"
    return "live"
