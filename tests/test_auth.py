"""Authentication units: hashing, tokens, sessions, lockout, API keys.

Route-level login lives in its own module; everything here calls `app.auth`
directly so the boundaries can be pinned to values worked out by hand — the
10-character password, the attempt threshold, the exact instant a session
expires and the exact instant it slides to.

Two deliberate choices:
  * `FakeRequest` rather than a TestClient. `load_auth`, `client_ip` and
    `key_from_request` touch nothing but cookies, headers,
    `app.state.settings` and `client.host`; a real request would drag the
    whole middleware stack into a unit test.
  * every time-dependent assertion runs under `freeze_time`. Sliding expiry
    and lockout are both arithmetic on "now", and a test that reads the wall
    clock can only assert something vague.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from freezegun import freeze_time
from sqlalchemy import select

from app import auth
from app.models import ApiKey, LoginAttempt, User, UserSession
from app.settings import PortfolioSettings
from appcore import ensure_utc
from factories import add_member, make_portfolio, make_user

UTC = dt.timezone.utc


# --------------------------------------------------------------------------- #
# Local helpers (kept in this file — the shared fixtures are off limits)
# --------------------------------------------------------------------------- #

@pytest.fixture
def settings() -> PortfolioSettings:
    """tests/config.test.yaml: 30-day sessions, lockout after 3 failures in 15
    minutes, cookie `pf_session`."""
    return PortfolioSettings()


class FakeRequest:
    """The attributes `app.auth` actually reads off a Request.

    `url.path` is here because sliding the idle window is path-dependent (a
    background poll must not count as the user being present —
    auth.SLIDING_EXEMPT), and `url.scheme` because whether the connection is
    encrypted decides whether signing in can work at all.
    """

    def __init__(self, settings=None, cookies=None, headers=None, client_host=None,
                 path="/", scheme="http"):
        self.cookies = dict(cookies or {})
        # Starlette's headers are case-insensitive; auth.py looks them up in
        # lower case, so lower-casing the keys here is faithful enough.
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.client = SimpleNamespace(host=client_host) if client_host else None
        self.app = SimpleNamespace(state=SimpleNamespace(settings=settings))
        # `scheme` matters because a session cookie marked Secure is discarded
        # by the browser over plain HTTP — see auth.insecure_login_problem.
        self.url = SimpleNamespace(path=path, scheme=scheme)


def request_with(settings, token=None, **kwargs) -> FakeRequest:
    cookies = dict(kwargs.pop("cookies", None) or {})
    if token is not None:
        cookies[settings.auth.cookie_name] = token
    return FakeRequest(settings=settings, cookies=cookies, **kwargs)


def context_for(role, *, memberships=None, active_portfolio_id=1, user=None):
    """An AuthContext for the pure-property tests.

    `session` plays no part in any property asserted below, so it stays None
    rather than dragging a database row into what is arithmetic on a role
    string.
    """
    if memberships is None:
        memberships = [auth.Membership(active_portfolio_id, "Test portfolio", role)]
    return auth.AuthContext(
        user=user or User(email="someone@example.test", name="Someone",
                          password_hash="x"),
        session=None,
        memberships=memberships,
        active_portfolio_id=active_portfolio_id,
        role=role,
    )


def make_api_key(db, portfolio, user, *, scopes="read", revoked_at=None):
    """Mint a key and store it the way the settings router does. Returns
    (raw_key, row)."""
    raw, key_hash, prefix = auth.new_api_key()
    row = ApiKey(
        portfolio_id=portfolio.id,
        name="Budget app",
        key_hash=key_hash,
        prefix=prefix,
        scopes=scopes,
        created_by=user.id,
        revoked_at=revoked_at,
    )
    db.add(row)
    db.flush()
    return raw, row


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #

def test_hash_password_round_trips_through_verify_password():
    stored = auth.hash_password("correct horse battery")

    assert auth.verify_password(stored, "correct horse battery") is True


def test_stored_hash_is_argon2id_and_never_the_plaintext():
    stored = auth.hash_password("correct horse battery")

    assert stored.startswith("$argon2id$")
    assert "correct horse battery" not in stored


def test_verify_password_rejects_the_wrong_password():
    stored = auth.hash_password("correct horse battery")

    assert auth.verify_password(stored, "correct horse batter") is False


def test_verify_password_returns_false_for_a_missing_stored_hash():
    """No account enumeration: an unknown email has no hash, and the caller
    must get a plain False rather than an exception it would have to branch on
    (branching is exactly the timing signal the dummy verify removes)."""
    assert auth.verify_password(None, "anything at all") is False


def test_verify_password_returns_false_for_a_corrupt_stored_hash():
    # argon2 raises InvalidHashError on this; a garbled row must fail the login,
    # not 500 the request.
    assert auth.verify_password("not-an-argon2-hash", "anything at all") is False


def test_the_timing_equaliser_passphrase_cannot_log_in_a_missing_account():
    """The dummy hash's passphrase is a literal in auth.py, so it is public the
    moment this repo is. Guessing it must still not authenticate anybody."""
    assert auth.verify_password(None, "portfolio-timing-equaliser") is False


def test_an_empty_stored_hash_cannot_be_logged_into():
    """Fixed 2026-08-02. `stored_hash or _DUMMY_HASH` treated an EMPTY hash as
    absent: it verified against the dummy and then returned
    `stored_hash is not None` — True. The equaliser passphrase is a literal in
    auth.py, so any account with a blank hash could be signed into by anyone
    who read the source. The test stays as the guard on `is None`."""
    assert auth.verify_password("", "portfolio-timing-equaliser") is False
    assert auth.verify_password("", "anything else") is False


def test_needs_rehash_is_true_for_a_garbage_hash():
    # Unparseable means "not hashed with the current parameters" by definition —
    # the login path rehashes it from the password just verified.
    assert auth.needs_rehash("not-an-argon2-hash") is True


def test_needs_rehash_is_false_for_a_freshly_hashed_password():
    assert auth.needs_rehash(auth.hash_password("correct horse battery")) is False


@pytest.mark.parametrize("password, rejected", [
    (None, True),        # form field missing entirely
    ("", True),          # nothing typed
    ("x" * 9, True),     # 9 characters — one short of the minimum
    ("x" * 10, False),   # 10 characters — the boundary is inclusive
    ("x" * 11, False),
    ("x" * 200, False),  # no upper bound: argon2 hashes any length
])
def test_password_problem_boundary_is_ten_characters(password, rejected):
    assert (auth.password_problem(password) is not None) is rejected


@pytest.mark.parametrize("email, rejected", [
    ("user@example.test", False),
    ("a@b.co", False),                  # 6 characters — the shortest accepted
    ("a@b.c", True),                    # 5 characters — one short
    ("plainaddress", True),             # no @ at all
    ("user@localhost", True),           # no dot in the domain part
    ("  user@example.test  ", False),   # stripped before checking
    ("", True),
    (None, True),
])
def test_email_problem_boundaries(email, rejected):
    assert (auth.email_problem(email) is not None) is rejected


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #

def test_hash_token_is_deterministic_sha256_hex():
    # The published SHA-256 test vector for "abc"; independently checked with
    # `printf 'abc' | shasum -a 256`, not with this app.
    assert auth.hash_token("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_new_token_hands_out_a_raw_token_and_stores_only_its_hash():
    raw, token_hash = auth.new_token()

    assert token_hash != raw
    assert token_hash == auth.hash_token(raw)
    assert len(token_hash) == 64            # sha256 hex = 32 bytes x 2 chars
    assert len(raw) == 43                   # base64url of 32 bytes, padding stripped


def test_new_token_is_unique_per_call():
    # 256 bits of entropy: a collision here means the source of randomness broke.
    assert auth.new_token()[0] != auth.new_token()[0]


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

def test_create_session_stores_the_hash_and_never_the_cookie_value(db, owner, settings):
    user, portfolio = owner

    raw = auth.create_session(db, user, settings, active_portfolio_id=portfolio.id)

    row = db.scalar(select(UserSession))
    assert row.token_hash == auth.hash_token(raw)
    assert row.token_hash != raw
    assert raw not in (row.csrf_token, row.token_hash)
    assert row.user_id == user.id
    assert row.active_portfolio_id == portfolio.id


@freeze_time("2026-08-02 09:00:00")
def test_create_session_expires_after_the_configured_ttl(db, owner, settings):
    user, _ = owner

    auth.create_session(db, user, settings)

    row = db.scalar(select(UserSession))
    # session_ttl_days = 30 in the test config; August has 31 days, so
    # 2 Aug 09:00 + 30 days = 1 Sep 09:00.
    assert ensure_utc(row.expires_at) == dt.datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    assert ensure_utc(row.created_at) == dt.datetime(2026, 8, 2, 9, 0, tzinfo=UTC)


def test_load_session_round_trips_the_raw_token(db, owner, settings):
    user, portfolio = owner
    raw = auth.create_session(db, user, settings, active_portfolio_id=portfolio.id)

    row = auth.load_session(db, raw, settings)

    assert row is not None
    assert row.user_id == user.id


@pytest.mark.parametrize("token", [None, "", "a-token-that-was-never-issued"])
def test_load_session_returns_none_for_a_missing_or_unknown_token(
    db, owner, settings, token
):
    user, _ = owner
    auth.create_session(db, user, settings)  # a real session exists...

    assert auth.load_session(db, token, settings) is None  # ...and is not handed to a stranger


@freeze_time("2026-08-02 09:00:00")
def test_expired_session_with_naive_expires_at_returns_none_not_typeerror(
    db, owner, settings
):
    """The shape of a production incident.

    A `DateTime(timezone=True)` column reads back NAIVE on sqlite and AWARE on
    postgres. Comparing a naive value with an aware `now()` raises
    `TypeError: can't compare offset-naive and offset-aware datetimes` — a 500
    on every request carrying an old cookie. `load_session` runs the value
    through `appcore.ensure_utc` first; this test is what keeps that call there.

    The assertion is on the ANSWER, not on which representation the backend
    happened to return, so it holds on either.
    """
    user, _ = owner
    raw = auth.create_session(db, user, settings)
    db.scalar(select(UserSession)).expires_at = dt.datetime(2026, 8, 1, 9, 0)  # naive
    db.flush()
    db.expire_all()  # force the value back off the database, not out of memory

    # Expired a day ago (1 Aug 09:00 read as UTC vs the frozen 2 Aug 09:00).
    assert auth.load_session(db, raw, settings) is None


@freeze_time("2026-08-02 09:00:00")
def test_load_session_deletes_the_row_it_found_expired(db, owner, settings):
    user, _ = owner
    raw = auth.create_session(db, user, settings)
    db.scalar(select(UserSession)).expires_at = dt.datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    db.flush()

    auth.load_session(db, raw, settings)

    # Dead sessions are reaped on the way past; nothing else sweeps this table.
    assert db.scalar(select(UserSession)) is None


@freeze_time("2026-08-02 09:00:00")
def test_session_expiring_exactly_now_counts_as_expired(db, owner, settings):
    user, _ = owner
    raw = auth.create_session(db, user, settings)
    # The comparison is `expires_at <= now`, so the instant of expiry is out.
    db.scalar(select(UserSession)).expires_at = dt.datetime(2026, 8, 2, 9, 0, tzinfo=UTC)
    db.flush()

    assert auth.load_session(db, raw, settings) is None


@freeze_time("2026-08-02 09:00:00")
def test_live_session_still_loads_whatever_the_backend_returns(db, owner, settings):
    """The other half of the ensure_utc guarantee: a stored expiry must not
    break the *happy* path either, on either backend."""
    user, _ = owner
    raw = auth.create_session(db, user, settings)
    db.flush()
    db.expire_all()

    assert auth.load_session(db, raw, settings) is not None   # expires 1 Sep, frozen at 2 Aug


def test_destroy_session_removes_only_that_session(db, owner, settings):
    user, _ = owner
    laptop = auth.create_session(db, user, settings)
    phone = auth.create_session(db, user, settings)

    auth.destroy_session(db, laptop)

    assert auth.load_session(db, laptop, settings) is None
    assert auth.load_session(db, phone, settings) is not None  # logging out here, not everywhere


@pytest.mark.parametrize("token", [None, "", "a-token-that-was-never-issued"])
def test_destroy_session_is_a_no_op_for_a_token_it_does_not_know(
    db, owner, settings, token
):
    user, _ = owner
    auth.create_session(db, user, settings)

    auth.destroy_session(db, token)

    assert db.scalar(select(UserSession)) is not None


def test_destroy_sessions_for_leaves_other_users_logged_in(db, owner, settings):
    user, _ = owner
    other = make_user(db, "other@example.test")
    auth.create_session(db, user, settings)
    auth.create_session(db, user, settings)
    kept = auth.create_session(db, other, settings)

    auth.destroy_sessions_for(db, user.id)

    rows = db.scalars(select(UserSession)).all()
    assert [r.user_id for r in rows] == [other.id]
    assert auth.load_session(db, kept, settings) is not None


# --------------------------------------------------------------------------- #
# Lockout — test config: 3 failures per (email, ip) in a 15-minute window,
# locked for 15 minutes from the most recent failure.
# --------------------------------------------------------------------------- #

EMAIL = "locked@example.test"
IP = "192.0.2.9"        # RFC 5737 documentation range


@freeze_time("2026-08-02 09:00:00")
def test_not_locked_below_the_attempt_threshold(db, settings):
    for _ in range(2):
        auth.record_attempt(db, EMAIL, IP, success=False)

    # 2 failures < max_attempts 3.
    assert auth.is_locked(db, EMAIL, IP, settings) is False


@freeze_time("2026-08-02 09:00:00")
def test_locked_at_the_attempt_threshold_inside_the_window(db, settings):
    for _ in range(3):
        auth.record_attempt(db, EMAIL, IP, success=False)

    # 3 failures = max_attempts, and now (09:00) < last failure + 15 min lockout.
    assert auth.is_locked(db, EMAIL, IP, settings) is True


def test_failures_older_than_the_window_do_not_count(db, settings):
    with freeze_time("2026-08-02 09:00:00") as clock:
        for _ in range(3):
            auth.record_attempt(db, EMAIL, IP, success=False)
        assert auth.is_locked(db, EMAIL, IP, settings) is True

        clock.move_to("2026-08-02 09:16:00")
        # window_minutes 15 → only failures at/after 09:01 count, and there are
        # none, so the count is 0 rather than 3.
        assert auth.is_locked(db, EMAIL, IP, settings) is False


def test_lockout_ends_before_the_window_does(db, settings):
    """The two clocks are separate: `window_minutes` decides which failures are
    counted, `lockout_minutes` decides how long the count keeps you out."""
    relaxed = settings.model_copy(deep=True)
    relaxed.auth.rate_limit.window_minutes = 60
    relaxed.auth.rate_limit.lockout_minutes = 5

    with freeze_time("2026-08-02 09:00:00") as clock:
        for _ in range(3):
            auth.record_attempt(db, EMAIL, IP, success=False)

        clock.move_to("2026-08-02 09:04:00")
        # Still inside the 5-minute lockout that runs from 09:00.
        assert auth.is_locked(db, EMAIL, IP, relaxed) is True

        clock.move_to("2026-08-02 09:06:00")
        # All 3 failures still count (60-minute window) but the lockout expired
        # at 09:05, so the door is open again.
        assert auth.is_locked(db, EMAIL, IP, relaxed) is False


@freeze_time("2026-08-02 09:00:00")
def test_lockout_does_not_follow_the_email_to_another_ip(db, settings):
    for _ in range(3):
        auth.record_attempt(db, EMAIL, IP, success=False)

    # Same account, different source: locking it out here would let anyone lock
    # a known email out of the house from outside.
    assert auth.is_locked(db, EMAIL, "192.0.2.10", settings) is False


@freeze_time("2026-08-02 09:00:00")
def test_lockout_does_not_follow_the_ip_to_another_email(db, settings):
    for _ in range(3):
        auth.record_attempt(db, EMAIL, IP, success=False)

    assert auth.is_locked(db, "someone-else@example.test", IP, settings) is False


@freeze_time("2026-08-02 09:00:00")
def test_successful_attempts_do_not_count_towards_lockout(db, settings):
    for _ in range(5):
        auth.record_attempt(db, EMAIL, IP, success=True)

    # Well past max_attempts, but none of them were failures.
    assert auth.is_locked(db, EMAIL, IP, settings) is False


@freeze_time("2026-08-02 09:00:00")
def test_attempts_are_recorded_against_the_lowercased_email(db, settings):
    for _ in range(3):
        auth.record_attempt(db, "Locked@Example.Test", IP, success=False)

    assert {row.email for row in db.scalars(select(LoginAttempt))} == {EMAIL}
    # ...which is why typing the address in a different case doesn't reset the
    # counter.
    assert auth.is_locked(db, "LOCKED@EXAMPLE.TEST", IP, settings) is True


# --------------------------------------------------------------------------- #
# load_auth
# --------------------------------------------------------------------------- #

def test_load_auth_returns_the_user_and_their_role_in_the_active_portfolio(
    db, owner, settings
):
    user, portfolio = owner
    raw = auth.create_session(db, user, settings, active_portfolio_id=portfolio.id)

    ctx = auth.load_auth(request_with(settings, raw), db)

    assert ctx.user.id == user.id
    assert ctx.active_portfolio_id == portfolio.id
    assert ctx.role == "owner"
    assert ctx.active.name == "Test portfolio"


def test_load_auth_returns_none_without_a_session_cookie(db, owner, settings):
    user, portfolio = owner
    auth.create_session(db, user, settings, active_portfolio_id=portfolio.id)

    assert auth.load_auth(request_with(settings), db) is None


def test_load_auth_slides_the_expiry_forward(db, owner, settings):
    user, portfolio = owner
    with freeze_time("2026-08-02 09:00:00") as clock:
        raw = auth.create_session(db, user, settings, active_portfolio_id=portfolio.id)
        # Within the 60-minute idle window — a day later would now be logged
        # out, which is what test_sessions.py covers.
        clock.move_to("2026-08-02 09:30:00")
        ctx = auth.load_auth(request_with(settings, raw), db)

    # The window restarts from the visit, not from the login. It is clamped to
    # the absolute cap (7 days from creation = 9 Aug), so a 30-day nominal TTL
    # lands on the cap rather than 1 Sep.
    assert ensure_utc(ctx.session.expires_at) == dt.datetime(2026, 8, 9, 9, 0, tzinfo=UTC)


def test_load_auth_stamps_last_seen_at(db, owner, settings):
    user, portfolio = owner
    with freeze_time("2026-08-02 09:00:00") as clock:
        raw = auth.create_session(db, user, settings, active_portfolio_id=portfolio.id)
        clock.move_to("2026-08-02 09:45:00")
        ctx = auth.load_auth(request_with(settings, raw), db)

    assert ensure_utc(ctx.session.last_seen_at) == dt.datetime(
        2026, 8, 2, 9, 45, tzinfo=UTC
    )


def test_load_auth_logs_out_a_deactivated_user(db, owner, settings):
    user, portfolio = owner
    raw = auth.create_session(db, user, settings, active_portfolio_id=portfolio.id)

    user.is_active = False
    db.flush()

    assert auth.load_auth(request_with(settings, raw), db) is None
    # Deactivation takes effect on the next request, not whenever the cookie
    # happens to expire — so the row goes too.
    assert db.scalar(select(UserSession)) is None


def test_load_auth_falls_back_when_the_active_portfolio_is_no_longer_a_membership(
    db, owner, settings
):
    user, portfolio = owner
    stranger = make_portfolio(db, "Not theirs")  # no membership for this user
    raw = auth.create_session(db, user, settings, active_portfolio_id=stranger.id)

    ctx = auth.load_auth(request_with(settings, raw), db)

    # Their only membership is the one from the `owner` fixture.
    assert ctx.active_portfolio_id == portfolio.id
    assert ctx.role == "owner"
    # And the repair is written back, so the next request doesn't redo it.
    assert ctx.session.active_portfolio_id == portfolio.id


def test_load_auth_picks_a_portfolio_for_a_session_that_has_none(db, owner, settings):
    user, portfolio = owner
    raw = auth.create_session(db, user, settings, active_portfolio_id=None)

    ctx = auth.load_auth(request_with(settings, raw), db)

    assert ctx.active_portfolio_id == portfolio.id


def test_load_auth_leaves_active_none_when_the_user_belongs_to_nothing(db, settings):
    """Not an error here — `scoped_session` is what turns it into a 403."""
    stray = make_user(db, "stray@example.test")
    raw = auth.create_session(db, stray, settings)

    ctx = auth.load_auth(request_with(settings, raw), db)

    assert ctx.active_portfolio_id is None
    assert ctx.role is None
    assert ctx.memberships == []


def test_load_auth_lists_memberships_in_portfolio_name_order(db, owner, settings):
    user, portfolio = owner  # "Test portfolio"
    add_member(db, make_portfolio(db, "Alpha family"), user, role="viewer")
    add_member(db, make_portfolio(db, "Zulu trust"), user, role="member")
    raw = auth.create_session(db, user, settings, active_portfolio_id=portfolio.id)

    ctx = auth.load_auth(request_with(settings, raw), db)

    # Ordered by name so the portfolio switcher doesn't reshuffle itself.
    assert [m.name for m in ctx.memberships] == [
        "Alpha family", "Test portfolio", "Zulu trust",
    ]
    assert [m.role for m in ctx.memberships] == ["viewer", "owner", "member"]


# --------------------------------------------------------------------------- #
# AuthContext
# --------------------------------------------------------------------------- #

def test_a_viewer_cannot_write():
    assert context_for("viewer").can_write is False


@pytest.mark.parametrize("role", ["owner", "member"])
def test_owners_and_members_can_write(role):
    assert context_for(role).can_write is True


def test_can_write_is_false_with_no_role_at_all():
    # No membership in the active portfolio → no rights in it.
    assert context_for(None).can_write is False


def test_is_owner_is_true_only_for_the_owner_role():
    assert context_for("owner").is_owner is True
    assert context_for("member").is_owner is False


def test_is_admin_is_the_account_flag_not_the_portfolio_role():
    """A viewer can still be the app admin who creates accounts, and a portfolio
    owner need not be."""
    admin = User(email="a@example.test", name="A", password_hash="x", is_admin=True)
    plain = User(email="b@example.test", name="B", password_hash="x", is_admin=False)

    assert context_for("viewer", user=admin).is_admin is True
    assert context_for("owner", user=plain).is_admin is False


def test_active_membership_is_the_one_matching_the_active_portfolio():
    memberships = [
        auth.Membership(1, "Alpha family", "viewer"),
        auth.Membership(2, "Zulu trust", "owner"),
    ]

    ctx = context_for("owner", memberships=memberships, active_portfolio_id=2)

    assert ctx.active.name == "Zulu trust"


def test_active_membership_is_none_when_the_active_portfolio_is_unknown():
    ctx = context_for(None, memberships=[], active_portfolio_id=None)

    assert ctx.active is None


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #

def test_new_api_key_has_the_prefix_id_secret_shape():
    raw, key_hash, prefix = auth.new_api_key()

    head, ident, secret = raw.split("_", 2)  # the secret may itself contain "_"
    assert head == "pfk"
    assert len(ident) == 8                   # token_hex(4) = 4 bytes x 2 chars
    int(ident, 16)                           # ...and it is hex
    assert len(secret) == 43                 # base64url of 32 bytes, unpadded
    assert prefix == f"pfk_{ident}"
    assert key_hash == auth.hash_token(raw)
    assert secret not in prefix              # the public chunk carries no secret


def test_api_key_row_stores_only_the_hash(db, owner):
    user, portfolio = owner

    raw, row = make_api_key(db, portfolio, user)

    assert row.key_hash == auth.hash_token(raw)
    assert raw not in (row.key_hash, row.prefix, row.name)
    assert raw.startswith(row.prefix)  # identifiable in the UI, not usable from it


def test_the_stored_prefix_cannot_be_used_as_a_key(db, owner):
    user, portfolio = owner
    _, row = make_api_key(db, portfolio, user)

    assert auth.load_api_key(db, row.prefix) is None


def test_load_api_key_resolves_a_live_key(db, owner):
    user, portfolio = owner
    raw, row = make_api_key(db, portfolio, user)

    assert auth.load_api_key(db, raw) is row


@freeze_time("2026-08-02 09:00:00")
def test_load_api_key_records_when_the_key_was_last_used(db, owner):
    user, portfolio = owner
    raw, row = make_api_key(db, portfolio, user)
    assert row.last_used_at is None

    found = auth.load_api_key(db, raw)

    assert ensure_utc(found.last_used_at) == dt.datetime(2026, 8, 2, 9, 0, tzinfo=UTC)


def test_load_api_key_refuses_a_revoked_key(db, owner):
    user, portfolio = owner
    raw, row = make_api_key(
        db, portfolio, user, revoked_at=dt.datetime(2026, 7, 1, tzinfo=UTC)
    )

    assert auth.load_api_key(db, raw) is None
    # A refused key was never "used" — revoking must not keep bumping the stamp.
    assert row.last_used_at is None


@pytest.mark.parametrize("raw", [None, "", "pfk_deadbeef_never-issued"])
def test_load_api_key_returns_none_for_a_missing_or_unknown_key(db, owner, raw):
    user, portfolio = owner
    make_api_key(db, portfolio, user)  # a valid key exists, but not this one

    assert auth.load_api_key(db, raw) is None


@pytest.mark.parametrize("scopes, writable", [
    ("read", False),
    ("read,write", True),
    ("write", True),
    ("readwrite", False),  # comma-separated scopes, not a substring match
    ("", False),
])
def test_can_write_reflects_the_scopes_string(db, owner, scopes, writable):
    user, portfolio = owner

    _, row = make_api_key(db, portfolio, user, scopes=scopes)

    assert row.can_write is writable


def test_a_key_is_active_until_it_is_revoked(db, owner):
    user, portfolio = owner
    _, live = make_api_key(db, portfolio, user)
    _, dead = make_api_key(
        db, portfolio, user, revoked_at=dt.datetime(2026, 7, 1, tzinfo=UTC)
    )

    assert live.active is True
    assert dead.active is False


# --------------------------------------------------------------------------- #
# Reading the credential off the request
# --------------------------------------------------------------------------- #

def test_key_from_request_reads_the_bearer_header():
    request = FakeRequest(headers={"Authorization": "Bearer pfk_deadbeef_secret"})

    assert auth.key_from_request(request) == "pfk_deadbeef_secret"


def test_key_from_request_falls_back_to_the_x_api_key_header():
    request = FakeRequest(
        headers={"Authorization": "Basic dXNlcjpwdw==", "X-API-Key": "pfk_deadbeef_secret"}
    )

    assert auth.key_from_request(request) == "pfk_deadbeef_secret"


def test_key_from_request_returns_none_when_no_credential_is_offered():
    assert auth.key_from_request(FakeRequest()) is None


def proxied(trusted: list[str]):
    """Settings whose auth block trusts these peers."""
    from types import SimpleNamespace

    base = PortfolioSettings()
    return SimpleNamespace(
        auth=SimpleNamespace(trusted_proxies=trusted, cookie_name=base.auth.cookie_name)
    )


def test_a_forwarded_address_is_believed_from_a_trusted_proxy():
    """Every reverse proxy APPENDS the peer it received from, so the original
    client is the leftmost entry and each one after it is infrastructure.

    Reading the rightmost instead would key every request on the proxy — and
    then one attacker could lock out every account at once, since lockout
    counts per address.
    """
    request = FakeRequest(
        settings=proxied(["198.51.100.0/24"]),
        headers={"X-Forwarded-For": "203.0.113.7, 198.51.100.9"},
        client_host="198.51.100.9",
    )

    assert auth.client_ip(request) == "203.0.113.7"


def test_a_forwarded_address_is_ignored_from_an_untrusted_peer():
    """The spoofing case, and the reason the default is to trust nobody.

    X-Forwarded-For is set by whoever sends it. Believed unconditionally, a
    caller picks a fresh identity per attempt — defeating lockout entirely —
    or forges somebody else's and locks THEM out.
    """
    request = FakeRequest(
        settings=proxied([]),                      # the default: trust nobody
        headers={"X-Forwarded-For": "203.0.113.7"},
        client_host="198.51.100.9",
    )

    assert auth.client_ip(request) == "198.51.100.9"   # the real peer, not the claim


def test_a_peer_outside_the_trusted_range_is_not_believed():
    request = FakeRequest(
        settings=proxied(["10.0.0.0/8"]),
        headers={"X-Forwarded-For": "203.0.113.7"},
        client_host="198.51.100.9",                # not inside 10.0.0.0/8
    )

    assert auth.client_ip(request) == "198.51.100.9"


def test_a_single_trusted_address_works_without_a_prefix():
    request = FakeRequest(
        settings=proxied(["198.51.100.9"]),
        headers={"X-Forwarded-For": "203.0.113.7"},
        client_host="198.51.100.9",
    )

    assert auth.client_ip(request) == "203.0.113.7"


def test_an_unparseable_trusted_entry_is_ignored_not_fatal():
    """A typo in the config must not decide that everyone is trusted, nor stop
    sign-in working."""
    request = FakeRequest(
        settings=proxied(["not-an-address", "198.51.100.9"]),
        headers={"X-Forwarded-For": "203.0.113.7"},
        client_host="198.51.100.9",
    )

    assert auth.client_ip(request) == "203.0.113.7"


def test_client_ip_falls_back_to_the_peer_address():
    assert auth.client_ip(FakeRequest(client_host="198.51.100.44")) == "198.51.100.44"


def test_client_ip_is_unknown_when_there_is_no_peer():
    # Lockout keys off this string, so it must never be None.
    assert auth.client_ip(FakeRequest()) == "unknown"
