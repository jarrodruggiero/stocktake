"""Authentication: password hashing, sessions, CSRF, login lockout.

argon2id hashing with constant-time verify. No account enumeration — an unknown
email and a wrong password give the same error, and an unknown email still pays
a dummy verify so the timing matches. Sessions are server-side: a 256-bit token
in an HttpOnly SameSite=Lax cookie, of which only the SHA-256 is stored, with
sliding expiry. CSRF is session-bound, except pre-auth forms which double-submit
a cookie token. API keys hash the same way and scope to one portfolio.

Every refusal lives in `load_session`, never in a route (decisions.md #3).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import ipaddress
import logging
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from appcore import ensure_utc

from . import tenancy
from .models import (
    WRITE_ROLES,
    ApiKey,
    LoginAttempt,
    Portfolio,
    PortfolioMember,
    User,
    UserSession,
)
from .settings import PortfolioSettings

log = logging.getLogger(__name__)

_ph = PasswordHasher()
# Equalises timing when the email is unknown — verifying this costs the same as
# verifying a real hash, so a failure reveals nothing about account existence.
_DUMMY_HASH = _ph.hash("portfolio-timing-equaliser")

PRE_AUTH_CSRF_COOKIE = "pf_csrf"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Check a password, in constant-ish time whether or not the account exists.

    The `is None` test is load-bearing and must not become a falsy test. An
    EMPTY stored hash is falsy but is still a stored hash: treating it as
    "absent" verifies the supplied password against `_DUMMY_HASH` and then
    reports success, so anyone who knows the equaliser passphrase — a literal
    in this file, and public the moment this repository is — could sign in to
    any account whose hash was blank.
    """
    target = _DUMMY_HASH if stored_hash is None else stored_hash
    try:
        _ph.verify(target, password)
        return stored_hash is not None
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def password_problem(password: str) -> str | None:
    if len(password or "") < 10:
        return "Password must be at least 10 characters."
    return None


def email_problem(email: str) -> str | None:
    email = (email or "").strip()
    if "@" not in email or "." not in email.split("@")[-1] or len(email) < 6:
        return "Enter a valid email address."
    return None


# --------------------------------------------------------------------------- #
# Tokens / sessions
# --------------------------------------------------------------------------- #

def new_token() -> tuple[str, str]:
    """(raw_token, sha256_hex) — store the hash, hand out the raw token."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_session(
    db: DbSession,
    user: User,
    settings: PortfolioSettings,
    active_portfolio_id: int | None = None,
    awaiting_totp: bool = False,
    ip: str | None = None,
    user_agent: str | None = None,
    recovery_spent: bool = False,
) -> str:
    raw, token_hash = new_token()
    now = _utcnow()
    db.add(
        UserSession(
            token_hash=token_hash,
            user_id=user.id,
            active_portfolio_id=active_portfolio_id,
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            last_seen_at=now,
            expires_at=now + dt.timedelta(days=settings.auth.session_ttl_days),
            awaiting_totp=awaiting_totp,
            # A code already spent to get here cannot be followed by a second
            # one at the 2FA step — see UserSession.recovery_spent.
            recovery_spent=recovery_spent,
            ip=ip,
            user_agent=(user_agent or "")[:200] or None,
        )
    )
    db.flush()
    return raw


def session_deadlines(
    row: UserSession, settings: PortfolioSettings
) -> tuple[dt.datetime, dt.datetime]:
    """When this session dies from inactivity, and when it dies regardless.

    Returned together because the effective expiry is whichever comes first,
    and every caller — the middleware, the keepalive endpoint, the countdown
    the browser renders — has to agree on that answer.
    """
    idle_deadline = ensure_utc(row.last_seen_at) + dt.timedelta(
        minutes=settings.auth.session_idle_minutes
    )
    hard_deadline = ensure_utc(row.created_at) + dt.timedelta(
        days=settings.auth.session_absolute_days
    )
    return idle_deadline, hard_deadline


def session_expired(row: UserSession, settings: PortfolioSettings) -> bool:
    """Whether this session is over, by any of the three clocks.

    Three, not one: the stored `expires_at`, the idle window, and the absolute
    cap. The sliding renewal can push the first two out indefinitely — a
    session used every day would otherwise live forever, which is precisely the
    stolen-cookie case the absolute cap exists for.
    """
    now = _utcnow()
    if ensure_utc(row.expires_at) <= now:
        return True
    idle_deadline, hard_deadline = session_deadlines(row, settings)
    return idle_deadline <= now or hard_deadline <= now


def _live_session(
    db: DbSession, raw_token: str | None, settings: PortfolioSettings
) -> UserSession | None:
    """The session row for this token if it exists and hasn't expired.

    Says nothing about whether it is *authenticated* — see `load_session`.
    Split out so the second-factor step has a way to reach its own
    half-authenticated session without weakening the check everything else
    goes through.
    """
    if not raw_token:
        return None
    row = db.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(raw_token))
    )
    if row is None:
        return None
    # `settings` is REQUIRED rather than defaulted. Making it optional would
    # mean a caller that forgot it silently lost the idle and absolute
    # timeouts, and a security control that fails open when you forget an
    # argument is worse than no control — you would believe you had one.
    if session_expired(row, settings):
        db.delete(row)
        db.flush()
        return None
    return row


def load_session(
    db: DbSession, raw_token: str | None, settings: PortfolioSettings
) -> UserSession | None:
    """The session behind this token, or None.

    A session still `awaiting_totp` is treated as no session at all. That
    refusal lives HERE, at the one place every route resolves a cookie, rather
    than in each route — the password step alone must never reach a page, and a
    check that has to be remembered is a check that will eventually be
    forgotten. The same argument applies to the timeouts.
    """
    row = _live_session(db, raw_token, settings)
    if row is None or row.awaiting_totp:
        return None
    return row


def load_pending_session(
    db: DbSession, raw_token: str | None, settings: PortfolioSettings
) -> UserSession | None:
    """The half-authenticated session, for the code-entry step ONLY.

    Deliberately narrow and deliberately greppable: this is the single function
    that can see a session that has not finished authenticating, and it refuses
    a fully-authenticated one so it can't be used to sidestep anything.
    """
    row = _live_session(db, raw_token, settings)
    if row is None or not row.awaiting_totp:
        return None
    return row


def destroy_session(db: DbSession, raw_token: str | None) -> None:
    if not raw_token:
        return
    row = db.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(raw_token))
    )
    if row is not None:
        db.delete(row)
        db.flush()


def destroy_sessions_for(db: DbSession, user_id: int) -> None:
    """Log a user out everywhere — after a password change or deactivation."""
    for row in db.scalars(select(UserSession).where(UserSession.user_id == user_id)):
        db.delete(row)
    db.flush()


# --------------------------------------------------------------------------- #
# Lockout
# --------------------------------------------------------------------------- #

def is_locked(db: DbSession, email: str, ip: str, settings: PortfolioSettings) -> bool:
    rl = settings.auth.rate_limit
    window_start = _utcnow() - dt.timedelta(minutes=rl.window_minutes)
    fails = db.scalars(
        select(LoginAttempt.created_at)
        .where(
            LoginAttempt.email == email.lower(),
            LoginAttempt.ip == ip,
            LoginAttempt.success.is_(False),
            LoginAttempt.created_at >= window_start,
        )
        .order_by(LoginAttempt.created_at.desc())
    ).all()
    if len(fails) < rl.max_attempts:
        return False
    return _utcnow() < ensure_utc(fails[0]) + dt.timedelta(minutes=rl.lockout_minutes)


def record_attempt(db: DbSession, email: str, ip: str, success: bool) -> None:
    db.add(LoginAttempt(email=email.lower(), ip=ip, success=success, created_at=_utcnow()))
    db.flush()


# --------------------------------------------------------------------------- #
# Request-scoped context
# --------------------------------------------------------------------------- #

@dataclass
class Membership:
    portfolio_id: int
    name: str
    role: str


@dataclass
class AuthContext:
    user: User
    session: UserSession
    memberships: list[Membership]
    active_portfolio_id: int | None
    role: str | None  # role in the ACTIVE portfolio

    @property
    def is_admin(self) -> bool:
        """App-wide account administration (adding people), not portfolio rights."""
        return bool(self.user.is_admin)

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    @property
    def can_write(self) -> bool:
        """Viewers read everything and change nothing."""
        return self.role in WRITE_ROLES

    @property
    def active(self) -> Membership | None:
        return next(
            (m for m in self.memberships if m.portfolio_id == self.active_portfolio_id),
            None,
        )


def _is_trusted(peer: str, trusted: list[str]) -> bool:
    """Whether a request arrived from a peer allowed to speak for someone else."""
    if not trusted:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for entry in trusted:
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            log.warning("ignoring unparseable auth.trusted_proxies entry %r", entry)
    return False


def is_secure_request(request: Request, settings: PortfolioSettings) -> bool:
    """Whether this request actually reached us over TLS.

    Behind a proxy the app always sees plain HTTP, so the answer comes from
    `X-Forwarded-Proto` — and that header is believed on exactly the same terms
    as `X-Forwarded-For`: only from a configured proxy. Anyone can set it, and
    believing it from an arbitrary peer would let a caller assert their
    connection is encrypted when it is not.
    """
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else None
    trusted = list(settings.auth.trusted_proxies)
    if peer and _is_trusted(peer, trusted):
        return request.headers.get("x-forwarded-proto", "").lower() == "https"
    return False


def insecure_login_problem(request: Request, settings: PortfolioSettings) -> str | None:
    """Why signing in here cannot work, or None.

    The failure this catches is silent and baffling. With `cookie_secure` on,
    a browser on plain HTTP **discards the session cookie**: the password is
    accepted, `Set-Cookie` is sent, the browser throws it away, and the person
    lands back on the login page with nothing to explain it. They will try
    their password again, and again.

    No second factor helps — the cookie cannot be stored however you
    authenticated — so the only useful response is to say so before they try.
    """
    if not settings.auth.cookie_secure or is_secure_request(request, settings):
        return None
    return (
        "This instance is configured for HTTPS, but you have reached it over "
        "plain HTTP. Your browser would throw the session cookie away, so "
        "signing in cannot work from this address — you would simply arrive "
        "back here. Use the https:// address instead. "
        "(If you are the administrator and there is no HTTPS yet, set "
        "auth.cookie_secure to false.)"
    )


def client_ip(request: Request) -> str:
    """The address this request came from, for lockout counting.

    X-Forwarded-For is set by whoever sends it, so it is believed ONLY when the
    immediate peer is a configured proxy. Otherwise anyone could pick their own
    address per request — which both defeats lockout entirely (a fresh identity
    each attempt) and turns it into a weapon (forge somebody else's address and
    lock them out).

    Every reverse proxy APPENDS the peer it received from, so the original
    client is the leftmost entry.
    """
    settings = getattr(getattr(request.app, "state", None), "settings", None)
    trusted = list(settings.auth.trusted_proxies) if settings is not None else []
    peer = request.client.host if request.client else None
    fwd = request.headers.get("x-forwarded-for")
    if fwd and peer and _is_trusted(peer, trusted):
        return fwd.split(",")[0].strip()
    return peer or "unknown"


# Paths that must NOT count as activity — decisions.md #19. Anything the page
# polls on a timer belongs here; `/session/keepalive` deliberately does not,
# because a button press is a person.
SLIDING_EXEMPT = frozenset(
    {"/session/status", "/healthz", "/readyz", "/feed/status",
     # The log console tails on a self-rescheduling timer, so leaving Admin →
     # Settings open renewed the session indefinitely. Found by deriving the
     # list from the scripts instead of maintaining it by hand.
     "/admin/logs.json"}
)


def slides_window(path: str) -> bool:
    """Whether a request to this path counts as the user being present."""
    return path not in SLIDING_EXEMPT and not path.startswith("/static/")


def load_auth(request: Request, db: DbSession) -> AuthContext | None:
    """Resolve the logged-in user from the cookie, sliding the expiry.

    "Sliding" means real activity only — see `SLIDING_EXEMPT`.
    """
    settings: PortfolioSettings = request.app.state.settings
    row = load_session(db, request.cookies.get(settings.auth.cookie_name), settings)
    if row is None:
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        db.delete(row)
        db.flush()
        return None
    now = _utcnow()
    if slides_window(request.url.path):
        row.last_seen_at = now
        # Never past the absolute cap: the whole point of that cap is that
        # renewal cannot push it out. Without this clamp a daily user's session
        # would be immortal, which is the stolen-cookie case it exists for.
        _, hard_deadline = session_deadlines(row, settings)
        row.expires_at = min(
            now + dt.timedelta(days=settings.auth.session_ttl_days), hard_deadline
        )

    rows = db.execute(
        select(PortfolioMember, Portfolio)
        .join(Portfolio, Portfolio.id == PortfolioMember.portfolio_id)
        .where(PortfolioMember.user_id == user.id)
        .order_by(Portfolio.name)
    ).all()
    memberships = [Membership(p.id, p.name, m.role) for (m, p) in rows]

    active_id = row.active_portfolio_id
    valid = {m.portfolio_id for m in memberships}
    if active_id not in valid:
        # Removed from that portfolio (or it was deleted) — fall back rather
        # than leaving the session pointed at something it can't read.
        active_id = next(iter(sorted(valid)), None)
        row.active_portfolio_id = active_id
    role = next((m.role for m in memberships if m.portfolio_id == active_id), None)
    return AuthContext(
        user=user,
        session=row,
        memberships=memberships,
        active_portfolio_id=active_id,
        role=role,
    )


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #

def expected_csrf(request: Request, db: DbSession) -> str | None:
    settings: PortfolioSettings = request.app.state.settings
    row = load_session(db, request.cookies.get(settings.auth.cookie_name), settings)
    if row is not None:
        return row.csrf_token
    return request.cookies.get(PRE_AUTH_CSRF_COOKIE)


async def verify_csrf(request: Request, db: DbSession) -> None:
    """403 unless the request echoes the session's CSRF token (X-CSRF-Token
    header or a `_csrf` form field)."""
    provided = request.headers.get("x-csrf-token")
    if provided is None:
        form = await request.form()
        value = form.get("_csrf")
        provided = value if isinstance(value, str) else None
    expected = expected_csrf(request, db)
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")


def ensure_pre_auth_csrf(request: Request) -> str:
    return request.cookies.get(PRE_AUTH_CSRF_COOKIE) or secrets.token_urlsafe(32)


async def verify_pre_auth_csrf(request: Request) -> None:
    """The double-submit check on its own, with no database in the picture.

    `verify_csrf` prefers a session token and needs a session to look one up.
    The setup wizard's first steps run before there is a database at all, let
    alone a session — so they need the half of the check that does not touch
    one. Same guarantee for those steps: a cross-site form post carries no
    cookie it can echo, so it cannot produce a match.
    """
    form = await request.form()
    value = form.get("_csrf")
    provided = value if isinstance(value, str) else request.headers.get("x-csrf-token")
    expected = request.cookies.get(PRE_AUTH_CSRF_COOKIE)
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")


# --------------------------------------------------------------------------- #
# Session helpers shared by every router
# --------------------------------------------------------------------------- #

@contextmanager
def scoped_session(request: Request) -> Iterator[tuple[AuthContext, DbSession]]:
    """DB session confined to the logged-in user, plus their auth context.

    Commits on success. Routes reach this only behind the require_login
    middleware, so a missing session means it expired mid-request — 401 beats
    quietly running the query unscoped.
    """
    db = request.app.state.session_factory()
    try:
        ctx = load_auth(request, db)
        if ctx is None:
            raise HTTPException(401, "session expired — reload and log in")
        if ctx.active_portfolio_id is None:
            raise HTTPException(403, "your account isn't a member of any portfolio")
        tenancy.bind(db, ctx.active_portfolio_id, ctx.user.id)
        yield ctx, db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def open_session(request: Request) -> Iterator[DbSession]:
    """Unbound session for the pre-login flows (users, sessions, attempts).
    Personal-data queries raise on it — see tenancy.py."""
    db = request.app.state.session_factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# API keys — machine access for the other apps in the family
# --------------------------------------------------------------------------- #

API_KEY_PREFIX = "pfk"


def new_api_key() -> tuple[str, str, str]:
    """Return (raw_key, sha256_hex, public_prefix).

    Format `pfk_<8-char id>_<secret>`: the middle chunk is stored so a key can
    be named and revoked from the UI without keeping anything usable.
    """
    ident = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    raw = f"{API_KEY_PREFIX}_{ident}_{secret}"
    return raw, hash_token(raw), f"{API_KEY_PREFIX}_{ident}"


def key_from_request(request: Request) -> str | None:
    """`Authorization: Bearer <key>` or `X-API-Key: <key>`."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.headers.get("x-api-key") or None


def load_api_key(db: DbSession, raw: str | None) -> ApiKey | None:
    """Resolve a raw key to a live ApiKey row, touching last_used_at."""
    if not raw:
        return None
    row = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_token(raw)))
    if row is None or row.revoked_at is not None:
        return None
    row.last_used_at = _utcnow()
    return row


@contextmanager
def api_session(request: Request, write: bool = False) -> Iterator[tuple[ApiKey, DbSession]]:
    """Session bound to the key's portfolio. 401 without a valid key, 403 when
    a read-only key attempts a write."""
    db = request.app.state.session_factory()
    try:
        key = load_api_key(db, key_from_request(request))
        if key is None:
            raise HTTPException(
                401, "missing or invalid API key (Authorization: Bearer pfk_...)"
            )
        if write and not key.can_write:
            raise HTTPException(403, "this key is read-only")
        tenancy.bind(db, key.portfolio_id, key.created_by)
        yield key, db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
