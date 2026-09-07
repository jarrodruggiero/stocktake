"""Signing in through somebody else's identity provider.

`appkit/oidc.py` does the protocol and knows nothing about this app. This file
is the other half: which account an identity belongs to, whether a new one may
be created, and what is remembered afterwards.

Two rules carry the security of the whole feature:

* **Accounts are matched on `(issuer, subject)`, never on email.** An address
  is something a directory can often be *told*, so matching on it would let
  anybody who can set an email in the provider take over an existing local
  account — decisions.md #119.
* **`provisioning` decides who may sign in at all.** `off` admits only linked
  accounts; `invite` admits a stranger holding a live invitation; `open`
  admits anyone the provider authenticates. The default is `off`, because the
  other two are statements about who a household's directory contains.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from appkit import ensure_utc, oidc

from .models import ExternalIdentity, OidcState, PortfolioInvite, User
from .settings import PortfolioSettings

# Long enough for a slow provider and a password prompt, short enough that an
# abandoned attempt is not a live row for the afternoon.
STATE_TTL = dt.timedelta(minutes=15)


class FederationError(Exception):
    """A sign-in that cannot be completed. The message is safe to show."""


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Whether it can be offered at all
# --------------------------------------------------------------------------- #

def configured(settings: PortfolioSettings) -> bool:
    conf = settings.auth.oidc
    return bool(conf.enabled and conf.issuer and conf.client_id
                and conf.client_secret and conf.redirect_uri)


def unavailable_reason(settings: PortfolioSettings) -> str | None:
    """Why sign-in cannot be offered, in a sentence, or None."""
    conf = settings.auth.oidc
    if not conf.enabled:
        return "Turn on single sign-on in Admin → Settings."
    if not conf.issuer or not conf.client_id:
        return "Set the provider URL and client ID in Admin → Settings."
    if not conf.client_secret:
        return "Set auth.oidc.client_secret in your configuration file."
    if not conf.redirect_uri:
        return "Set the redirect URL in Admin → Settings."
    return None


def provider(settings: PortfolioSettings) -> oidc.Provider:
    conf = settings.auth.oidc
    return oidc.Provider(
        issuer=conf.issuer or "",
        client_id=conf.client_id or "",
        client_secret=conf.client_secret or "",
        redirect_uri=conf.redirect_uri or "",
        scopes=tuple(conf.scopes),
    )


# --------------------------------------------------------------------------- #
# The redirect out
# --------------------------------------------------------------------------- #

def begin(db: DbSession, settings: PortfolioSettings,
          invite: PortfolioInvite | None = None) -> tuple[str, str]:
    """Where to send the browser, and the token that remembers this attempt."""
    if not configured(settings):
        raise FederationError(unavailable_reason(settings) or "Not available.")
    url, pending = oidc.begin(provider(settings))
    raw = secrets.token_urlsafe(32)
    db.add(OidcState(
        token_hash=_hash(raw),
        state=pending["state"],
        nonce=pending["nonce"],
        verifier=pending["verifier"],
        invite_id=invite.id if invite else None,
        expires_at=_utcnow() + STATE_TTL,
    ))
    db.flush()
    return url, raw


def take_state(db: DbSession, raw: str | None, state: str) -> OidcState:
    """The attempt behind this token, consumed as it is read.

    The `state` the provider echoed must match the one issued: that is what
    stops a callback somebody else constructed from completing a sign-in here.
    """
    if not raw:
        raise FederationError("That sign-in attempt has expired. Try again.")
    row = db.scalar(select(OidcState).where(OidcState.token_hash == _hash(raw)))
    if row is None or ensure_utc(row.expires_at) < _utcnow():
        if row is not None:
            db.delete(row)
            db.flush()
        raise FederationError("That sign-in attempt has expired. Try again.")
    # Compared before the row is spent, so a mismatch cannot be used to burn
    # somebody else's attempt.
    if not secrets.compare_digest(row.state, state or ""):
        raise FederationError("That sign-in did not match the request that started it.")
    return row


def purge_expired_states(db: DbSession) -> int:
    """Attempts nobody came back from. Runs with maintenance."""
    result = db.execute(delete(OidcState).where(OidcState.expires_at < _utcnow()))
    return result.rowcount or 0


# --------------------------------------------------------------------------- #
# The callback
# --------------------------------------------------------------------------- #

def resolve(db: DbSession, settings: PortfolioSettings,
            identity: oidc.Identity, invite: PortfolioInvite | None) -> User:
    """The account this identity signs in as, creating one only if allowed.

    Never matched on email. A local account with the same address is a
    different thing until somebody links them deliberately.
    """
    known = db.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.issuer == identity.issuer,
            ExternalIdentity.subject == identity.subject,
        )
    )
    if known is not None:
        known.last_used_at = _utcnow()
        known.email = identity.email
        db.flush()
        user = db.get(User, known.user_id)
        if user is None or not user.is_active:
            raise FederationError("That account is no longer active.")
        return user

    mode = settings.auth.oidc.provisioning
    if mode == "invite" and invite is None:
        raise FederationError(
            "This app does not know you yet. Ask for an invitation link.")
    if mode not in ("invite", "open"):
        raise FederationError(
            "This app does not know you yet. An administrator has to link your "
            "account first.")
    return provision(db, identity)


def provision(db: DbSession, identity: oidc.Identity) -> User:
    """Create an account for an identity nobody has seen before.

    The email is stored because an account needs one to be addressed by, and
    an account with a colliding address is refused rather than joined: two
    people are not one person because a directory says so.
    """
    email = (identity.email or f"{identity.subject}@{identity.issuer}").lower()
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise FederationError(
            "An account already uses that email address. Sign in with it, then "
            "link your provider from your profile.")
    # No password: `auth.verify_password` refuses a NULL hash, so this account
    # signs in one way until somebody sets one — decisions.md #120.
    user = User(email=email, name=identity.name or email, password_hash=None)
    db.add(user)
    db.flush()
    link(db, user, identity)
    return user


def link(db: DbSession, user: User, identity: oidc.Identity) -> ExternalIdentity:
    """Attach an identity to an account. Refuses one already attached
    elsewhere — a subject belongs to exactly one account."""
    existing = db.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.issuer == identity.issuer,
            ExternalIdentity.subject == identity.subject,
        )
    )
    if existing is not None and existing.user_id != user.id:
        raise FederationError("That provider account is already linked to "
                              "somebody else here.")
    if existing is not None:
        return existing
    row = ExternalIdentity(user_id=user.id, issuer=identity.issuer,
                           subject=identity.subject, email=identity.email,
                           last_used_at=_utcnow())
    db.add(row)
    db.flush()
    return row


def identities_for(db: DbSession, user: User) -> list[ExternalIdentity]:
    return list(db.scalars(
        select(ExternalIdentity)
        .where(ExternalIdentity.user_id == user.id)
        .order_by(ExternalIdentity.created_at)
    ))


def unlink(db: DbSession, user: User, identity_id: int) -> bool:
    """Detach one provider from this account.

    Refused when it is the only way in: an account with no password and no
    other identity would be unreachable, and "you can no longer sign in" is
    not something a settings page should be able to do quietly.
    """
    rows = identities_for(db, user)
    row = next((r for r in rows if r.id == identity_id), None)
    if row is None:
        return False
    if user.password_hash is None and len(rows) == 1:
        raise FederationError(
            "Set a password first — this is the only way into your account.")
    db.delete(row)
    db.flush()
    return True
