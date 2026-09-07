"""Stocktake data model.

`trade` is the single source of truth (decisions.md #1). Prices and brokerage
are stored in the instrument's native currency with the reporting-currency FX
rate captured at trade date; NULL means unknown and backfillable.

`dividend` records cash at distribution date, and a DRP dividend also links the
trade holding the reinvested units.

The PORTFOLIO is the tenant, not the person — decisions.md #72. `user_id` on
trade and dividend is "who recorded it" and is never what scoping keys off.
"""

from __future__ import annotations

import datetime as dt
import re as _re
import uuid as _uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Time,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from appkit import Base, JSONType

# What a ticker and an exchange may contain. Enforced in code because SQLite
# does not enforce a VARCHAR length: a 400-character "ticker" is accepted there
# and rejected by Postgres with a raw database error, so validating here makes
# both backends behave the same and gives a readable message either way.
TICKER_PATTERN = _re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
EXCHANGE_PATTERN = _re.compile(r"^[A-Z][A-Z0-9.\-]{0,11}$")


def ticker_problem(ticker: str) -> str | None:
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return "Enter a ticker."
    if not TICKER_PATTERN.match(ticker):
        return ("A ticker is up to 12 characters, using letters, digits, "
                "a dot or a hyphen.")
    return None


def exchange_problem(exchange: str) -> str | None:
    exchange = (exchange or "").strip().upper()
    if not exchange:
        return "Enter an exchange."
    if not EXCHANGE_PATTERN.match(exchange):
        return ("An exchange is up to 12 characters, starting with a letter.")
    return None


TRADE_TYPES = ("buy", "sell", "drp")
PLAN_STATUSES = ("planned", "done", "skipped")
MEMBER_ROLES = ("owner", "member", "viewer")

# What each role is CALLED. `user.is_admin` and `member.role == "owner"` are
# different powers that both sound like "admin", so the label names the scope —
# decisions.md #34. Stored values stay owner/member/viewer.
ROLE_LABELS = {
    "owner": "Portfolio admin",
    "member": "Member",
    "viewer": "Viewer",
}
ROLE_BLURBS = {
    "owner": "manages this portfolio's people and API keys",
    "member": "records trades and edits the schedule",
    "viewer": "reads everything, changes nothing",
}
# Deliberately coarse. owner: manage members, keys and portfolio settings.
# member: record trades, edit the plan, import. viewer: read everything, change
# nothing — for showing family the numbers without risking an edit.
WRITE_ROLES = ("owner", "member")
THEMES = ("auto", "light", "dark")
ACCENTS = ("blue", "teal", "violet", "amber")
NAV_STYLES = ("both", "text", "icon")
# Which clock a trade time is shown on. Storage is always "market".
TIME_ZONES_SHOWN = ("market", "local")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# A trade recorded without a time is treated as having happened at the open,
# not at midnight. That matters for ordering: the day's other trades are almost
# always *after* the market opened, so midnight would sort an unspecified buy
# before an explicitly-timed sell that actually preceded it.
MARKET_OPEN = dt.time(10, 0)


def trade_order(t: "Trade") -> tuple:
    """Sort key placing a trade in its instrument's timeline.

    Ordering is (date, time, id), with two wrinkles that are each easy to get
    wrong:

      * a trade with no time sits at `MARKET_OPEN`, so adding times to some
        trades doesn't reshuffle the ones without;
      * an UNSAVED trade (a candidate being validated before insert, `id` still
        None) sorts *after* every saved trade sharing its date and time. It is
        being entered now, so that is when it happened. Sorting it first — the
        old `t.id or 0` — is what made selling on the day you bought impossible:
        the sell was checked as though it preceded its own buy.
    """
    return (
        t.date,
        t.time if t.time is not None else MARKET_OPEN,
        1 if t.id is None else 0,
        t.id or 0,
    )


# --------------------------------------------------------------------------- #
# Accounts. The token discipline throughout: only
# SHA-256 hashes of session tokens are stored, never the raw cookie value)
# --------------------------------------------------------------------------- #


def _new_public_id() -> str:
    """A random UUID4 as text.

    Text rather than a native UUID column because Postgres has one and SQLite
    does not, and this app ships on both. 36 characters costs nothing at this
    scale, and it means the value reads the same in a `sqlite3` shell and in
    `psql`.
    """
    return str(_uuid.uuid4())


# Many columns below carry BOTH `default=` and `server_default=`. They are
# different mechanisms and the pair must stay in step — decisions.md #31.
class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    # A stable, opaque identifier for this account, safe to hand to another
    # application. Nothing reads it yet, on purpose — decisions.md #23. The
    # index/constraint split is deliberate too: decisions.md #24.
    public_id: Mapped[str] = mapped_column(
        String(36), index=True, default=_new_public_id
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))  # argon2id
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )
    # The first account (via /setup) is admin: it manages the other accounts.
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # Forces a password change at next login (admin-created / reset accounts).
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # TOTP second factor. The secret is stored as issued — base32, in the clear.
    # Encrypting it here would be theatre: the key would have to live in the
    # same process, and anyone who can read this column can already read the
    # session table and the whole portfolio (see the trust boundary note in
    # tenancy.py). NULL = never enrolled; `totp_enabled_at` set = enforced.
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_enabled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # Appearance is per person, not per portfolio — two people sharing one
    # portfolio each get their own.
    theme: Mapped[str] = mapped_column(          # auto|light|dark
        String(8), default="auto", server_default="auto"
    )
    accent: Mapped[str] = mapped_column(
        String(8), default="blue", server_default="blue"
    )
    nav_style: Mapped[str] = mapped_column(      # both|text|icon
        String(8), default="both", server_default="both"
    )
    # Nav keys this person has hidden. NULL means "hide nothing", so an account
    # that has never opened the setting keeps the bar it has. Per user, like
    # the two above: sharing a portfolio does not mean sharing a top bar.
    nav_hidden: Mapped[list | None] = mapped_column(JSONType)
    # Colours this person has overridden, as {token: "#rrggbb"} — see
    # app/theming.py for which tokens may be set and why the list is short.
    # NULL means "the built-in palette", so a later change to the defaults
    # reaches everyone who has not chosen, and nobody who has.
    theme_colors: Mapped[dict | None] = mapped_column(JSONType)
    # False until the default charts have been laid out for them. Stops a
    # deleted chart quietly reappearing on the next page load.
    charts_seeded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # Which charts this person wants, in their order: {"order": [...],
    # "hidden": [...]}. NULL = the defaults in CHARTS.
    chart_layout: Mapped[dict | None] = mapped_column(JSONType)
    # Which holdings-table columns this person wants, as a list of keys from
    # app/columns.py. NULL = never chosen, which renders the defaults — so a
    # later change to what the defaults are reaches everyone who has not
    # expressed a preference, and nobody who has.
    dashboard_columns: Mapped[list | None] = mapped_column(JSONType)
    # This person's own currency preference. **NULL means "the portfolio's"**
    # and must stay that way — decisions.md #47. Read by T28b, not yet.
    display_currency: Mapped[str | None] = mapped_column(String(3))
    # Which clock trade times are SHOWN on: the exchange's, or this device's.
    # Storage is always the exchange's — see `trade_order`, which sequences a
    # day's trades for FIFO and would reorder them under a moving clock.
    times_in: Mapped[str] = mapped_column(
        String(6), default="market", server_default="market"
    )
    # When this person was last shown their recovery codes. NULL means never,
    # which is the state every account upgrading to this version starts in —
    # and the state a fresh one is in for the few seconds before the wizard
    # shows them. Codes nobody has seen are codes nobody has, so this drives a
    # banner rather than a quiet note on the account page.
    recovery_codes_seen_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_user_public_id"),
    )


class Portfolio(Base):
    """The tenant. One person's holdings, or a shared one several contribute to."""

    __tablename__ = "portfolio"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    # What totals are computed and shown in. `money.REPORTING` is still the
    # default and most of the app still assumes it — see T28b, which is the
    # task that unpicks the ~110 hard-coded places. Storing it here is what
    # makes that task a rewiring rather than a redesign.
    reporting_currency: Mapped[str] = mapped_column(
        String(3), default="AUD", server_default="AUD"
    )
    # Which tax rules apply: the FY boundary, the CGT rules, and the words the
    # reports use. AU today. Named for tax residency rather than an address,
    # and the page may still label it "Country" — decisions.md #46.
    jurisdiction: Mapped[str] = mapped_column(
        String(2), default="AU", server_default="AU"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    members: Mapped[list[PortfolioMember]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )


class PortfolioMember(Base):
    __tablename__ = "portfolio_member"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(
        String(8), default="member", server_default="member"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    portfolio: Mapped[Portfolio] = relationship(back_populates="members")
    user: Mapped[User] = relationship()

    __table_args__ = (
        UniqueConstraint("portfolio_id", "user_id", name="uq_member_portfolio_user"),
        CheckConstraint("role IN ('owner','member','viewer')", name="member_role"),
    )


class ApiKey(Base):
    """Machine access to ONE portfolio, for the other apps in the family
    (budgeting, retirement planning).

    Same token discipline as sessions: the raw key is shown once at creation and
    only its SHA-256 is stored. `prefix` is the leading public chunk, kept so a
    key is identifiable in the UI and in logs without being usable.
    """

    __tablename__ = "api_key"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))
    # "read" or "read,write" — write lets another app POST trades/dividends.
    scopes: Mapped[str] = mapped_column(
        String(32), default="read", server_default="read"
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    portfolio: Mapped[Portfolio] = relationship()

    @property
    def can_write(self) -> bool:
        return "write" in self.scopes.split(",")

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class UserSession(Base):
    """Server-side session; the cookie holds a 256-bit token, we store its
    SHA-256. Sliding expiry. (Named UserSession so it never reads as a
    SQLAlchemy Session in this codebase.)"""

    __tablename__ = "user_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    # Which portfolio this session is acting in. Scoped queries take the id from
    # HERE, never from a form field or query string.
    active_portfolio_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolio.id", ondelete="SET NULL")
    )
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    # The recovery-codes banner, put away for THIS session. Deliberately not on
    # the user: dismissing it should last until they sign in again, not
    # forever — codes nobody has saved are codes nobody has.
    codes_banner_hidden: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # Where this session was created. Captured once at login and never updated:
    # it answers "what is this session on the list?", not "where is it now".
    # The UA is truncated — it is only parsed into "Firefox on macOS" for
    # display, and the full string is a fingerprint with no reason to be kept.
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(200))
    # Enforces ONE recovery code per sign-in — decisions.md #45.
    recovery_spent: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # Half-authenticated: the password was right but the second factor hasn't
    # been given yet. `auth.load_session` refuses these outright, so a session
    # in this state can reach exactly one route (the code form) and nothing
    # else — the refusal lives at the single choke point on purpose.
    awaiting_totp: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )

    user: Mapped[User] = relationship()


class LoginAttempt(Base):
    """One row per login attempt, for lockout. Not a long-term audit log."""

    __tablename__ = "login_attempt"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)  # lowercased
    ip: Mapped[str] = mapped_column(String(45))
    success: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class RecoveryCode(Base):
    """A single-use way back in when the authenticator app is gone.

    Hashed like a session token and for the same reason: a leaked database
    should not hand over working credentials. Marked used rather than deleted,
    so "you have 3 of 16 left" is answerable and a replay is distinguishable
    from a code that was never issued.
    """

    __tablename__ = "recovery_code"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), index=True)  # sha256
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class WebauthnCredential(Base):
    """One enrolled passkey or security key. Written by `app/passkeys.py`.

    It cannot be used until the app is served over **HTTPS**: WebAuthn requires
    a secure context, so a plain-HTTP host is a hard no — see the
    `auth.webauthn` block in config.yaml for what has to be true first, and why
    `rp_id` must be declared in config rather than read off the Host header.

    Credentials bind permanently to the rp_id they were registered under, so a
    passkey created at `portfolio.example.com` will not work from
    another. The canonical domain has to be settled before anyone
    enrols, which is the other reason this is schema-only for now.
    """

    __tablename__ = "webauthn_credential"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    # Base64url of the raw credential id handed back by the authenticator.
    credential_id: Mapped[str] = mapped_column(String(255), index=True)
    public_key: Mapped[str] = mapped_column(String(1024))  # COSE key, base64url
    # Replay defence: authenticators that implement it return a counter that
    # must never go backwards. Some (most passkeys) always send 0, which is
    # allowed and means "no counter" rather than "replayed".
    sign_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    transports: Mapped[str | None] = mapped_column(String(80))  # usb,nfc,ble,internal
    name: Mapped[str | None] = mapped_column(String(80))  # "YubiKey 5C", "iPhone"
    rp_id: Mapped[str] = mapped_column(String(253))  # what it was registered under
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("credential_id", name="uq_webauthn_credential_id"),
    )


class PortfolioInvite(Base):
    """An offer of access to one portfolio, at one role, for one person.

    It exists because membership previously required the account to exist
    first: an owner picked a name from a list, so there was no way to bring in
    somebody who had never signed in. That is also what OIDC provisioning needs
    — the invite is the authorisation to have an account at all.

    Single use, revocable, and stored HASHED. The URL is a credential for as
    long as it is live: whoever holds it gets the role it names, so it is shown
    once and a database copy cannot be replayed.
    """

    __tablename__ = "portfolio_invite"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), index=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    # Who is answerable for it. Kept after use, because "who let this person
    # in" is the question an owner asks later.
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    # Spent and withdrawn are different states and both are worth keeping: one
    # is a member who joined, the other is a link somebody thought better of.
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_portfolio_invite_token"),
    )


class WebauthnChallenge(Base):
    """One issued challenge, waiting for the browser to come back with it.

    A WebAuthn ceremony is two requests, and the server has to remember the
    nonce it issued between them. It lives in a row rather than a cookie for
    the same reason a session does: the browser holds only a random token, and
    the token is stored HASHED, so a database copy cannot be replayed.

    `user_id` is null for a sign-in. That ceremony is discoverable — the
    authenticator names the account, so asking for one up front would only
    leak whether an email is registered.
    """

    __tablename__ = "webauthn_challenge"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), index=True)
    challenge: Mapped[str] = mapped_column(String(255))  # base64url
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_webauthn_challenge_token"),
    )


class Instrument(Base):
    """Shared catalogue entry for a tradeable thing. NOT user-scoped — see the
    module docstring. `drp` is the default for a new holder; the per-user answer
    lives in `holding_pref`."""

    __tablename__ = "instrument"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(12))
    exchange: Mapped[str] = mapped_column(String(12))  # ASX / NASDAQ / CRYPTO
    name: Mapped[str | None] = mapped_column(String(80))
    currency: Mapped[str] = mapped_column(String(3), default="AUD")
    asset_class: Mapped[str] = mapped_column(String(10))  # etf / share / crypto
    drp: Mapped[bool] = mapped_column(default=False)  # distributions reinvest?
    active: Mapped[bool] = mapped_column(default=True)  # still listed/tradeable
    yahoo_symbol: Mapped[str | None] = mapped_column(String(20))  # price feed
    note: Mapped[str | None] = mapped_column(String(400))  # catalogue note only

    # Both relationships return ONLY the current user's rows: tenancy.py applies
    # a loader criterion to every select, eager load and lazy load.
    trades: Mapped[list[Trade]] = relationship(back_populates="instrument")
    dividends: Mapped[list[Dividend]] = relationship(back_populates="instrument")

    __table_args__ = (UniqueConstraint("exchange", "ticker"),)


class HoldingPref(Base):
    """One user's take on one instrument: whether THEY reinvest distributions,
    plus their private note (sale details, strategy). Notes were on `instrument`
    before multi-user and moved here at first login — they are personal data."""

    __tablename__ = "holding_pref"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio.id", ondelete="CASCADE"), index=True
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instrument.id", ondelete="CASCADE"), index=True
    )
    drp: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    note: Mapped[str | None] = mapped_column(String(400))

    instrument: Mapped[Instrument] = relationship()

    # A unique INDEX, not a unique constraint: migration 0003 had to spell this
    # table out per dialect (batch mode refused it over 0002's unnamed
    # constraints) and made an index. The two enforce the same rule, so this is
    # the model catching up to what the database has actually had since.
    __table_args__ = (
        Index("uq_pref_portfolio_instrument", "portfolio_id", "instrument_id",
              unique=True),
    )


class Trade(Base):
    __tablename__ = "trade"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolio.id", ondelete="CASCADE"), index=True
    )
    # Who entered it — audit only, never the scoping key.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), index=True
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instrument.id"), index=True
    )
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    # Optional intra-day time, so two trades on one day can be ordered by when
    # they happened rather than by which was typed first. Left unset it means
    # MARKET_OPEN — see `trade_order`, which is the only thing that reads it.
    time: Mapped[dt.time | None] = mapped_column(Time, default=MARKET_OPEN)
    type: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8))  # always positive
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 6))  # native ccy
    brokerage: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    # Reporting-currency units per 1 unit of the instrument's native currency,
    # captured at the trade's date and never re-derived — decisions.md #48.
    # NULL = not captured; the price feed backfills it.
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    note: Mapped[str | None] = mapped_column(String(400))
    # Bumped on every write, including Core `update()` (the price feed's FX
    # backfill). The series cache fingerprints on it: without it, editing a
    # trade in place leaves the row count unchanged and every chart, report and
    # export keeps serving the numbers from before the edit.
    updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    instrument: Mapped[Instrument] = relationship(back_populates="trades")

    __table_args__ = (
        CheckConstraint("type IN ('buy','sell','drp')", name="trade_type"),
        CheckConstraint("quantity > 0", name="trade_qty_positive"),
    )


class Dividend(Base):
    __tablename__ = "dividend"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolio.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), index=True
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instrument.id"), index=True
    )
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    cash_amount: Mapped[Decimal] = mapped_column(Numeric(14, 4))  # native ccy
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))  # see Trade.fx_rate
    franking_credits: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))  # v2
    # What the registry kept because it allots whole units only, carried into
    # the next distribution. Native currency, balance AFTER this one. NULL means
    # unknown and 0 means it kept nothing; a balance, never income.
    residual_carried: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    reinvest_trade_id: Mapped[int | None] = mapped_column(
        ForeignKey("trade.id"), unique=True
    )
    note: Mapped[str | None] = mapped_column(String(400))
    updated_at: Mapped[dt.datetime | None] = mapped_column(  # see Trade.updated_at
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    instrument: Mapped[Instrument] = relationship(back_populates="dividends")
    reinvest_trade: Mapped[Trade | None] = relationship()


class Price(Base):
    """Daily close per instrument, in its native currency.

    One row per instrument per trading day. `provisional` is what lets today's
    row exist before the market has produced a close: see below.
    """

    __tablename__ = "price"

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instrument.id"), primary_key=True
    )
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    source: Mapped[str] = mapped_column(String(20), default="yfinance")
    # True while this row is a LIVE price for a session that has not finished.
    # `pricefeed._fetch_start` resumes from the last SETTLED date, which is
    # what guarantees it gets replaced — decisions.md #12 and #13.
    provisional: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )


class MarketDividend(Base):
    """Distribution history straight from the market, per instrument.

    SHARED like `price`: it describes the security, not anyone's holding, and
    it is what lets the calendar project payment dates for a holding you only
    bought last week — the owner's own `dividend` rows may be too few to see a
    cycle in. Amounts are per unit, in the instrument's native currency.
    """

    __tablename__ = "market_dividend"

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instrument.id", ondelete="CASCADE"), primary_key=True
    )
    ex_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 8))  # per unit
    source: Mapped[str] = mapped_column(
        String(20), default="yfinance", server_default="yfinance"
    )


class FxRate(Base):
    """Daily rate for a currency pair, e.g. USDAUD = AUD per 1 USD."""

    __tablename__ = "fx_rate"

    pair: Mapped[str] = mapped_column(String(6), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    source: Mapped[str] = mapped_column(String(20), default="yfinance")


class InvestmentPlan(Base):
    """A repeating buy rotation, edited in the app rather than configured.

    The plan says WHAT to buy in what order and HOW OFTEN; `planned_purchase`
    records what actually happened. One active plan per user is the norm, but
    keeping several lets an old one be retired without deleting its history.
    """

    __tablename__ = "investment_plan"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolio.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(80), default="My plan")
    interval_days: Mapped[int] = mapped_column(Integer, default=28)
    # Typical spend per buy — prefills the record-buy form; not enforced.
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    brokerage: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("9.50"))
    # Anchor for the first due date when there's no history yet.
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    entries: Mapped[list[InvestmentPlanEntry]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="InvestmentPlanEntry.position",
    )


class InvestmentPlanEntry(Base):
    """One slot in the rotation. Order is `position` (0-based, contiguous); the
    same instrument may appear more than once — that is how a 2:1 weighting is
    expressed (the crypto slot alternates OMEGA/OMEGA/ZULU over three passes)."""

    __tablename__ = "investment_plan_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("investment_plan.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"))

    plan: Mapped[InvestmentPlan] = relationship(back_populates="entries")
    instrument: Mapped[Instrument] = relationship()

    __table_args__ = (
        UniqueConstraint("plan_id", "position", name="uq_plan_entry_position"),
    )


class SavedChart(Base):
    """One chart on somebody's charts page. `spec` is the whole definition
    (grain, x, measures, bucket, filters, type) — see app/fields.py for the
    vocabulary, app/charts_build.py for what executes it, and
    app/chart_templates.py for the ones a new account starts with.

    Owned by the USER, not the portfolio: how you like to look at your holdings
    follows you between them, and two people sharing a portfolio each arrange
    their own page. `template_key` records which default it came from (NULL for
    one built from scratch) — the moment it is edited it is simply theirs.
    """

    __tablename__ = "saved_chart"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    spec: Mapped[dict] = mapped_column(JSONType)
    template_key: Mapped[str | None] = mapped_column(String(40))
    # Grid placement: order on the page, and whether the card spans the row.
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    width: Mapped[str] = mapped_column(   # half | full
        String(8), default="half", server_default="half"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class PlannedPurchase(Base):
    """One recorded step of the rotation: bought (with the trade it created) or
    skipped. `plan_entry_id` pins which slot it was, so editing the rotation
    afterwards doesn't shift what comes next."""

    __tablename__ = "planned_purchase"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolio.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), index=True
    )
    due_date: Mapped[dt.date] = mapped_column(Date, index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"))
    plan_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("investment_plan_entry.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(8), default="planned")
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trade.id"))
    note: Mapped[str | None] = mapped_column(String(200))

    instrument: Mapped[Instrument] = relationship()

    __table_args__ = (
        CheckConstraint(
            "status IN ('planned','done','skipped')", name="plan_status"
        ),
    )
