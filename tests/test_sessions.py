"""Session lifetime: idle timeout, absolute cap, and what counts as activity.

Three clocks, each stopping something the others do not: `expires_at` slides on
activity, the idle window ends an hour of nothing, and the absolute cap of
seven days cannot be extended by renewal — without it a session used daily
never expires and a stolen cookie stays valid indefinitely.

The part most likely to be broken later is what counts as activity: the page
polls itself during a feed refresh and the idle overlay polls to keep its
countdown honest, and if either slid the window a tab left open would keep its
session alive forever. `auth.SLIDING_EXEMPT` is the list; these tests are why
it cannot quietly lose an entry.

Everything time-dependent runs under `freeze_time`.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from test_auth import request_with

from app import auth
from app.models import UserSession
from app.settings import PortfolioSettings
from appkit import ensure_utc
from factories import make_portfolio, make_user

APP_ROOT = Path(__file__).resolve().parent.parent
UTC = dt.timezone.utc
START = "2026-08-02 09:00:00"


@pytest.fixture
def settings() -> PortfolioSettings:
    return PortfolioSettings()


@pytest.fixture
def owner(db):
    user = make_user(db, "owner@example.test")
    portfolio = make_portfolio(db, "Test portfolio", owner=user)
    return user, portfolio


def a_session(db, owner, settings) -> str:
    user, portfolio = owner
    return auth.create_session(db, user, settings, active_portfolio_id=portfolio.id)


# --------------------------------------------------------------------------- #
# The idle window
# --------------------------------------------------------------------------- #

def test_a_session_survives_inside_the_idle_window(db, owner, settings):
    with freeze_time(START) as clock:
        raw = a_session(db, owner, settings)
        clock.move_to("2026-08-02 09:59:00")  # 59 minutes: still inside the hour

        assert auth.load_session(db, raw, settings) is not None


def test_a_session_dies_after_the_idle_window(db, owner, settings):
    """The headline behaviour: a valid cookie is not enough once you've been
    away for an hour."""
    with freeze_time(START) as clock:
        raw = a_session(db, owner, settings)
        clock.move_to("2026-08-02 10:01:00")

        assert auth.load_session(db, raw, settings) is None


def test_the_idle_boundary_is_inclusive(db, owner, settings):
    """Exactly on the minute counts as expired, matching how `expires_at` is
    treated — one rule for all three clocks."""
    with freeze_time(START) as clock:
        raw = a_session(db, owner, settings)
        clock.move_to("2026-08-02 10:00:00")

        assert auth.load_session(db, raw, settings) is None


def test_activity_slides_the_idle_window(db, owner, settings):
    """Half an hour in, a real request resets the clock — so the session is
    still alive 90 minutes after login, which a fixed window would not allow."""
    with freeze_time(START) as clock:
        raw = a_session(db, owner, settings)
        clock.move_to("2026-08-02 09:30:00")
        assert auth.load_auth(request_with(settings, raw), db) is not None
        clock.move_to("2026-08-02 10:29:00")

        assert auth.load_session(db, raw, settings) is not None


def test_an_expired_session_row_is_deleted_when_it_is_presented(db, owner, settings):
    with freeze_time(START) as clock:
        raw = a_session(db, owner, settings)
        clock.move_to("2026-08-02 11:00:00")
        auth.load_session(db, raw, settings)

    assert db.scalar(select(UserSession)) is None


# --------------------------------------------------------------------------- #
# The absolute cap
# --------------------------------------------------------------------------- #

def test_a_continuously_used_session_still_dies_at_the_cap(db, owner, settings):
    """The reason the cap exists. Someone active every hour would otherwise
    hold a session forever — and so would anyone who stole their cookie."""
    with freeze_time(START) as clock:
        raw = a_session(db, owner, settings)
        # Stay busy: a request every half hour for eight days.
        for half_hour in range(1, 8 * 48):
            clock.move_to(
                dt.datetime(2026, 8, 2, 9, tzinfo=UTC) + dt.timedelta(minutes=30 * half_hour)
            )
            if auth.load_auth(request_with(settings, raw), db) is None:
                break

        # Seven days from creation, to the minute.
        assert dt.datetime.now(UTC) >= dt.datetime(2026, 8, 9, 9, 0, tzinfo=UTC)
        assert auth.load_session(db, raw, settings) is None


def test_sliding_never_pushes_the_expiry_past_the_cap(db, owner, settings):
    """`session_ttl_days` is 30 and the cap is 7, so an unclamped renewal would
    happily write an expiry three weeks past the point the session must die."""
    with freeze_time(START) as clock:
        raw = a_session(db, owner, settings)
        clock.move_to("2026-08-02 09:30:00")
        ctx = auth.load_auth(request_with(settings, raw), db)

    assert ensure_utc(ctx.session.expires_at) == dt.datetime(2026, 8, 9, 9, 0, tzinfo=UTC)


def test_the_cap_is_enforced_even_when_expires_at_says_otherwise(db, owner, settings):
    """The cap must be checked directly, not merely implied by the clamp.

    Sessions created BEFORE the cap existed carry an unclamped 30-day
    `expires_at`. After an upgrade those rows are still in the table, and if
    the only thing enforcing the cap were the clamp applied on the next
    request, such a session would happily live out its original thirty days.

    Caught by mutation testing: removing the `hard_deadline` term from
    `session_expired` broke nothing until this test existed, because every
    other path had already been clamped.
    """
    with freeze_time(START):
        raw = a_session(db, owner, settings)
        row = db.scalar(select(UserSession))
        # Exactly what a naive implementation would have written.
        row.expires_at = dt.datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
        db.flush()

    # Eight days on: inside that stale expiry, inside the idle window (it was
    # used a minute ago), but past the seven-day cap.
    with freeze_time("2026-08-10 09:00:00"):
        row = db.scalar(select(UserSession))
        row.last_seen_at = dt.datetime(2026, 8, 10, 8, 59, tzinfo=UTC)
        db.flush()

        assert auth.load_session(db, raw, settings) is None


def test_the_deadlines_are_reported_together(db, owner, settings):
    """Every caller — middleware, keepalive, the browser countdown — has to
    agree on which clock runs out first."""
    with freeze_time(START):
        raw = a_session(db, owner, settings)
        row = auth.load_session(db, raw, settings)

        idle, hard = auth.session_deadlines(row, settings)

    assert idle == dt.datetime(2026, 8, 2, 10, 0, tzinfo=UTC)
    assert hard == dt.datetime(2026, 8, 9, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# What counts as activity
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", ["/session/status", "/healthz", "/readyz",
                                  "/static/style.css",
                                  # Polled continuously by every open dashboard
                                  # so live prices appear without a manual
                                  # reload. It was missing from the exemption
                                  # when it was only polled during a refresh,
                                  # where it cost seconds; left out now it
                                  # would make the idle timeout decorative.
                                  "/feed/status"])
def test_background_requests_do_not_count_as_activity(path):
    assert auth.slides_window(path) is False


@pytest.mark.parametrize("path", ["/", "/charts", "/profile", "/session/keepalive",
                                  "/holding/ALPHA"])
def test_real_pages_do_count(path):
    assert auth.slides_window(path) is True


def test_watching_the_log_console_does_not_keep_a_session_alive(
    db, owner, settings
):
    """The bug, as a behaviour rather than a list entry.

    Admin → Settings tails the log on a repeating timer. While that path
    counted as activity, leaving the page open renewed the session on every
    poll and the idle timeout never fired — reported from a desktop that had
    stayed signed in for days.
    """
    with freeze_time(START) as clock:
        raw = a_session(db, owner, settings)
        for minute in range(0, 90, 5):          # the console, polling all morning
            clock.move_to(f"2026-08-02 {9 + minute // 60}:{minute % 60:02d}:00")
            auth.load_auth(request_with(settings, raw, path="/admin/logs.json"), db)

        clock.move_to("2026-08-02 10:31:00")     # 91 minutes after the last click
        assert auth.load_session(db, raw, settings) is None


# Polled by a script, but deliberately counted as activity anyway. One reason
# each, because an unexplained entry here is indistinguishable from an
# oversight — which is the bug this whole test exists to catch.
DELIBERATELY_COUNTS = {
    "/session/keepalive": "Pressing 'keep me signed in' is a person, not a poll.",
}


def _polled_paths() -> dict[str, str]:
    """Every URL fetched from a script that repeats itself, found rather than
    listed.

    The hand-written list above could not lose an entry, but nothing stopped it
    never GAINING one: the log console was added with a self-rescheduling
    `setTimeout`, its path was never added, and leaving Admin → Settings open
    renewed the session forever. Deriving the list from the scripts is what
    makes that impossible to repeat.
    """
    found: dict[str, str] = {}
    for script in sorted((APP_ROOT / "app" / "static").glob("*.js")):
        source = script.read_text()
        repeats = "setInterval(" in source or re.search(
            r"setTimeout\(\s*poll", source)
        if not repeats:
            continue
        for url in re.findall(r"""fetch\(\s*["'](/[^"'?]+)""", source):
            found.setdefault(url, script.name)
    return found


def test_every_path_a_script_polls_is_exempt_from_sliding():
    offenders = {
        url: script for url, script in _polled_paths().items()
        if auth.slides_window(url) and url not in DELIBERATELY_COUNTS
    }

    assert not offenders, (
        "These are polled on a timer and would keep a session alive forever. "
        f"Add each to auth.SLIDING_EXEMPT, or to DELIBERATELY_COUNTS with a "
        f"reason: {offenders}")


def test_the_scan_actually_finds_the_pollers():
    """A regex that matched nothing would make the test above vacuously true —
    and it is the only thing standing between a new poller and a session that
    never ends."""
    found = _polled_paths()

    assert "/session/status" in found
    assert len(found) >= 4


def test_polling_the_status_endpoint_cannot_keep_a_session_alive(db, owner, settings):
    """The trap this whole exemption exists for. A dashboard left open polls;
    if that slid the window the tab would be immortal and the idle timeout
    would protect nobody."""
    with freeze_time(START) as clock:
        raw = a_session(db, owner, settings)
        for minute in range(10, 70, 10):
            clock.move_to(f"2026-08-02 09:{minute:02d}:00" if minute < 60
                          else "2026-08-02 10:00:00")
            auth.load_auth(request_with(settings, raw, path="/session/status"), db)

        assert auth.load_session(db, raw, settings) is None


def test_the_keepalive_path_does_slide_the_window(db, owner, settings):
    """A button press is a person — the one deliberate exception."""
    with freeze_time(START) as clock:
        raw = a_session(db, owner, settings)
        clock.move_to("2026-08-02 09:50:00")
        auth.load_auth(request_with(settings, raw, path="/session/keepalive"), db)
        clock.move_to("2026-08-02 10:30:00")  # 90 min after login, 40 after the press

        assert auth.load_session(db, raw, settings) is not None


# --------------------------------------------------------------------------- #
# Where a session came from
# --------------------------------------------------------------------------- #

def test_the_origin_is_captured_at_login(db, owner, settings):
    user, portfolio = owner

    raw = auth.create_session(db, user, settings, active_portfolio_id=portfolio.id,
                              ip="203.0.113.7", user_agent="Mozilla/5.0 (Macintosh)")
    row = auth.load_session(db, raw, settings)

    assert row.ip == "203.0.113.7"
    assert row.user_agent == "Mozilla/5.0 (Macintosh)"


def test_a_giant_user_agent_is_truncated_not_rejected(db, owner, settings):
    """A UA is attacker-controlled input. It gets cut to the column width
    rather than raising — on Postgres an over-length value is a hard error, so
    an unbounded one would turn a header into a failed login."""
    user, portfolio = owner

    raw = auth.create_session(db, user, settings, active_portfolio_id=portfolio.id,
                              user_agent="x" * 5000)
    row = auth.load_session(db, raw, settings)

    assert len(row.user_agent) == 200


def test_a_session_with_no_origin_recorded_is_fine(db, owner, settings):
    """Rows created before this existed, and the CLI/tests, have neither."""
    raw = a_session(db, owner, settings)

    row = auth.load_session(db, raw, settings)

    assert row.ip is None
    assert row.user_agent is None


# --------------------------------------------------------------------------- #
# The endpoints the overlay talks to
# --------------------------------------------------------------------------- #

HTML = {"accept": "text/html"}


def test_the_status_endpoint_reports_time_remaining(client, session_factory):
    from test_routes import make_login

    make_login(client, session_factory)

    data = client.get("/session/status").json()

    assert data["authenticated"] is True
    # A fresh session: the idle window (60 min) is the binding constraint.
    assert 3500 < data["seconds_left"] <= 3600
    assert data["warn_at"] == 120


def test_the_status_endpoint_is_honest_when_signed_out(client, session_factory):
    from test_routes import make_login

    make_login(client, session_factory)
    client.cookies.clear()

    data = client.get("/session/status").json()

    assert data == {"authenticated": False, "seconds_left": 0}


def test_asking_how_long_is_left_does_not_extend_it(client, session_factory):
    """The trap, at the route level. The overlay polls this every five seconds
    while it counts down — if that slid the window the countdown would never
    reach zero and the session would be immortal."""
    from test_routes import make_login

    with freeze_time("2026-08-02 09:00:00") as clock:
        make_login(client, session_factory)
        clock.move_to("2026-08-02 09:50:00")
        first = client.get("/session/status").json()["seconds_left"]
        clock.move_to("2026-08-02 09:55:00")
        second = client.get("/session/status").json()["seconds_left"]

    # Ten minutes left, then five. Polling moved it toward zero, not away.
    assert 560 < first <= 600
    assert 260 < second <= 300


def test_keepalive_pushes_the_deadline_out(client, session_factory):
    from test_routes import make_login, session_csrf

    with freeze_time("2026-08-02 09:00:00") as clock:
        make_login(client, session_factory)
        clock.move_to("2026-08-02 09:55:00")
        assert client.get("/session/status").json()["seconds_left"] <= 300

        resp = client.post("/session/keepalive",
                           headers={"X-CSRF-Token": session_csrf(session_factory)})

    assert resp.status_code == 200
    assert resp.json()["seconds_left"] > 3500


def test_keepalive_needs_a_csrf_token(client, session_factory):
    """It is a state-changing POST, and one that an attacker would love: it
    keeps a session they have stolen alive."""
    from test_routes import make_login

    make_login(client, session_factory)

    assert client.post("/session/keepalive").status_code == 403


def test_keepalive_on_a_dead_session_says_so(client, session_factory):
    from test_routes import make_login, session_csrf

    with freeze_time("2026-08-02 09:00:00") as clock:
        make_login(client, session_factory)
        token = session_csrf(session_factory)
        clock.move_to("2026-08-02 11:00:00")  # two hours: well past the window

        resp = client.post("/session/keepalive", headers={"X-CSRF-Token": token})

    assert resp.status_code in (401, 403)


def test_logging_out_can_land_on_the_timeout_notice(client, session_factory):
    """The overlay's expiry path. A login page that appears for no visible
    reason is worse than one that explains itself."""
    from test_routes import make_login, session_csrf

    make_login(client, session_factory)

    resp = client.post("/logout?next=%2Flogin%3Ftimeout%3D1",
                       data={"_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/login?timeout=1"
    assert "Signed out after inactivity" in client.get("/login", params={"timeout": "1"},
                                                       headers=HTML).text


@pytest.mark.parametrize("target", ["//evil.test/", "https://evil.test",
                                    "javascript:alert(1)"])
def test_logout_will_not_redirect_off_site(client, session_factory, target):
    """`next` is an open-redirect waiting to happen if it is trusted. Only
    same-site paths are honoured, and "//" is not one."""
    from test_routes import make_login, session_csrf

    make_login(client, session_factory)

    resp = client.post("/logout", params={"next": target},
                       data={"_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/login"


# --------------------------------------------------------------------------- #
# Seeing and revoking sessions
# --------------------------------------------------------------------------- #

def test_the_account_page_lists_this_session(client, session_factory):
    from test_routes import make_login

    make_login(client, session_factory)

    page = client.get("/profile", headers=HTML).text

    assert "Where you&#39;re signed in" in page or "Where you're signed in" in page
    assert "this one" in page


def test_a_second_session_shows_up_and_can_be_revoked(client, session_factory):
    from app.models import User
    from test_routes import make_login, session_csrf

    make_login(client, session_factory)
    # Captured BEFORE the extra sessions exist: session_csrf() reads the NEWEST
    # session, so minting one for another device first hands back its token.
    csrf = session_csrf(session_factory)
    # A second sign-in from "another device".
    with session_factory() as s:
        from app import auth as auth_mod
        from app.settings import PortfolioSettings

        user = s.scalars(select(User)).one()
        auth_mod.create_session(s, user, PortfolioSettings(), active_portfolio_id=1,
                                ip="198.51.100.4", user_agent="Mozilla/5.0 (iPhone)")
        s.commit()

    page = client.get("/profile", headers=HTML).text
    assert "iPhone" in page
    assert "198.51.100.4" in page

    with session_factory() as s:
        other = s.scalars(
            select(UserSession).order_by(UserSession.id.desc())).first()
        other_id = other.id

    resp = client.post(f"/profile/sessions/{other_id}/revoke",
                       data={"_csrf": csrf}, headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/profile?saved=session-revoked"
    with session_factory() as s:
        assert s.get(UserSession, other_id) is None
    # ...and we are still signed in.
    assert client.get("/profile", headers=HTML).status_code == 200


def test_you_cannot_revoke_someone_elses_session(client, session_factory):
    """Without the ownership check this is a way to sign anybody out by
    guessing a small integer."""
    import factories as fac
    from app import auth as auth_mod
    from app.settings import PortfolioSettings
    from test_routes import make_login, session_csrf

    make_login(client, session_factory)
    csrf = session_csrf(session_factory)  # before the other user's session exists
    with session_factory() as s:
        other_user = fac.make_user(s, "other@example.test")
        auth_mod.create_session(s, other_user, PortfolioSettings())
        s.commit()
        theirs = s.scalars(select(UserSession).order_by(UserSession.id.desc())).first().id

    resp = client.post(f"/profile/sessions/{theirs}/revoke",
                       data={"_csrf": csrf}, headers=HTML)

    assert resp.status_code == 404
    with session_factory() as s:
        assert s.get(UserSession, theirs) is not None


def test_revoking_the_current_session_is_refused(client, session_factory):
    """It would work, but it is a confusing way to log out — and the button
    that does it properly is right there."""
    from test_routes import make_login, session_csrf

    make_login(client, session_factory)
    with session_factory() as s:
        mine = s.scalars(select(UserSession)).one().id

    resp = client.post(f"/profile/sessions/{mine}/revoke",
                       data={"_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/profile?error=current-session"
    with session_factory() as s:
        assert s.get(UserSession, mine) is not None


def test_revoke_others_keeps_this_session_alive(client, session_factory):
    """The thing you want after losing a laptop — and it must not sign you out
    of the browser you are asking from."""
    from app import auth as auth_mod
    from app.models import User
    from app.settings import PortfolioSettings
    from test_routes import make_login, session_csrf

    make_login(client, session_factory)
    csrf = session_csrf(session_factory)  # before the extra sessions exist
    with session_factory() as s:
        user = s.scalars(select(User)).one()
        for _ in range(3):
            auth_mod.create_session(s, user, PortfolioSettings(), active_portfolio_id=1)
        s.commit()

    resp = client.post("/profile/sessions/revoke-others",
                       data={"_csrf": csrf}, headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/profile?saved=sessions-revoked"
    with session_factory() as s:
        assert len(s.scalars(select(UserSession)).all()) == 1
    assert client.get("/profile", headers=HTML).status_code == 200


@pytest.mark.parametrize("ua,expected", [
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit Chrome/120 Safari/537", "Chrome on macOS"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit Version/17 Safari/604", "Safari on iPhone"),
    ("Mozilla/5.0 (Windows NT 10.0) Gecko/20100101 Firefox/121.0", "Firefox on Windows"),
    ("Mozilla/5.0 (X11; Linux x86_64) Chrome/120 Safari/537 Edg/120", "Edge on Linux"),
    ("curl/8.4.0", "Browser"),
    (None, "Unknown device"),
])
def test_user_agents_become_something_a_person_recognises(ua, expected):
    """Crude on purpose: the question is "laptop or phone?", which does not
    justify a UA-parsing dependency and a data file to keep current."""
    from app.main import _describe_agent

    assert _describe_agent(ua) == expected


# Reaching an HTTPS-configured instance over plain HTTP: the browser discards
# the cookie and the sign-in fails silently, so the app has to say so before
# somebody retries their password — decisions.md #55.

def _settings(secure: bool, proxies=()):
    from types import SimpleNamespace

    return SimpleNamespace(
        auth=SimpleNamespace(cookie_secure=secure, trusted_proxies=list(proxies))
    )


def test_plain_http_with_secure_cookies_is_refused_with_an_explanation():
    from test_auth import FakeRequest

    problem = auth.insecure_login_problem(FakeRequest(), _settings(secure=True))

    assert problem is not None
    assert "plain HTTP" in problem
    assert "cookie_secure" in problem      # tells the admin how to fix it too


def test_plain_http_is_fine_when_secure_cookies_are_off():
    """The ordinary LAN install. Nothing to warn about."""
    from test_auth import FakeRequest

    assert auth.insecure_login_problem(FakeRequest(), _settings(secure=False)) is None


def test_a_real_https_request_is_not_warned_about():
    from test_auth import FakeRequest

    request = FakeRequest(scheme="https")

    assert auth.insecure_login_problem(request, _settings(secure=True)) is None


def test_a_trusted_proxy_saying_https_is_believed():
    """Behind a proxy the app always sees plain HTTP; the header is the only
    evidence TLS happened."""
    from test_auth import FakeRequest

    request = FakeRequest(headers={"X-Forwarded-Proto": "https"}, client_host="10.0.0.1")

    assert auth.is_secure_request(request, _settings(True, ["10.0.0.0/8"])) is True


def test_an_untrusted_peer_claiming_https_is_not_believed():
    """Anyone can set that header. Believing it from an arbitrary peer would
    let a caller assert their connection is encrypted when it is not — the same
    rule X-Forwarded-For already follows."""
    from test_auth import FakeRequest

    request = FakeRequest(headers={"X-Forwarded-Proto": "https"}, client_host="203.0.113.9")

    assert auth.is_secure_request(request, _settings(True, ["10.0.0.0/8"])) is False
    assert auth.insecure_login_problem(request, _settings(True, ["10.0.0.0/8"])) is not None


def test_the_login_page_shows_it(client, session_factory, monkeypatch):
    """It has to reach the person who is stuck, not just the test suite."""
    import app.main as main_module
    from test_routes import make_login

    make_login(client, session_factory)
    client.cookies.clear()
    monkeypatch.setattr(main_module.settings.auth, "cookie_secure", True)

    page = client.get("/login", headers={"accept": "text/html"}).text

    assert "reached it over plain HTTP" in page
