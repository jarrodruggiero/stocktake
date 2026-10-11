"""Output encoding, input validation and response headers.

The escaping tests use a ticker containing `</script>`. That string is the
whole point: `json.dumps` does not escape it, so embedding chart data with
`json.dumps(...) | safe` let a ticker close the script block early and start a
new one — a stored cross-site scripting hole reachable by anyone with write
access to a shared portfolio. Jinja's `|tojson` escapes `<`, `>` and `&`, which
is why the templates use it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import factories as fac
from app.models import Instrument, exchange_problem, ticker_problem
from test_routes import bind_to_only_portfolio, make_login, reading, session_csrf

BREAKOUT = "</script><script>alert(1)</script>"


# --------------------------------------------------------------------------- #
# Response headers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("header,expected", [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "same-origin"),
])
def test_every_response_carries_the_hardening_headers(client, session_factory,
                                                      header, expected):
    make_login(client, session_factory)

    assert client.get("/", headers={"accept": "text/html"}).headers[header] == expected


def _cookie(response, name: str) -> dict:
    """One Set-Cookie header, as {attribute (lower case): value}."""
    for header in response.headers.get_list("set-cookie"):
        first, *attrs = [part.strip() for part in header.split(";")]
        if first.startswith(name + "="):
            return {a.split("=", 1)[0].lower(): (a.split("=", 1)[1] if "=" in a else True)
                    for a in attrs}
    raise AssertionError(f"no {name} cookie set")


@pytest.mark.parametrize("secure", [False, True])
def test_the_session_cookie_cannot_be_read_by_a_script(client, session_factory, app_module,
                                                       monkeypatch, secure):
    """HttpOnly is what keeps a script that slips past the escaping from
    reading the session; nothing checked it. Secure follows the setting."""
    from fastapi.testclient import TestClient

    from app.auth import hash_password
    from test_routes import PASSWORD, pre_auth_csrf

    monkeypatch.setattr(app_module.settings.auth, "cookie_secure", secure)
    with session_factory() as s:
        fac.make_user(s, "user@example.test", password_hash=hash_password(PASSWORD))
        s.commit()
    # A secure cookie is set only over HTTPS (decisions.md #55); `client` has
    # already pointed the app at this test's database.
    browser = TestClient(app_module.app, base_url="https://testserver") if secure else client
    resp = browser.post("/login", follow_redirects=False, headers={"accept": "text/html"},
                        data={"email": "user@example.test", "password": PASSWORD,
                              "_csrf": pre_auth_csrf(browser)})

    cookie = _cookie(resp, app_module.settings.auth.cookie_name)
    assert cookie.get("httponly") is True
    assert cookie.get("samesite", "").lower() == "lax"
    assert cookie.get("path") == "/"
    assert int(cookie["max-age"]) == app_module.settings.auth.session_ttl_days * 86400
    assert (cookie.get("secure") is True) is secure


def test_the_headers_are_on_unauthenticated_responses_too(client):
    """The login page is the one an attacker reaches first."""
    assert client.get("/login", headers={"accept": "text/html"}
                      ).headers["X-Frame-Options"] == "DENY"


def test_the_content_security_policy_locks_down_the_dangerous_directives(client,
                                                                        session_factory):
    make_login(client, session_factory)

    policy = client.get("/", headers={"accept": "text/html"}).headers[
        "Content-Security-Policy"]

    # Nothing may frame this app, no injected form may post elsewhere, and no
    # injected <base> may re-point every relative URL.
    assert "frame-ancestors 'none'" in policy
    assert "form-action 'self'" in policy
    assert "base-uri 'self'" in policy
    assert "default-src 'self'" in policy


def test_the_policy_is_honest_about_inline_scripts(client, session_factory):
    """Recorded, not hidden: the templates still carry inline handlers, so the
    policy cannot be the XSS backstop it superficially resembles. If someone
    extracts those fragments, this test is the reminder to tighten it."""
    make_login(client, session_factory)

    policy = client.get("/", headers={"accept": "text/html"}).headers[
        "Content-Security-Policy"]

    assert "script-src 'self' 'unsafe-inline'" in policy


# --------------------------------------------------------------------------- #
# Ticker validation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ticker", ["ACME", "ZULU", "BRK.B", "RDS-A", "X", "ABC123"])
def test_ordinary_tickers_are_accepted(ticker):
    assert ticker_problem(ticker) is None


@pytest.mark.parametrize("ticker", [
    "", "   ",
    "A" * 13,                       # over the 12-character column
    "</script>",                    # the breakout attempt
    "AC ME",                        # whitespace inside
    "acme;DROP",                    # punctuation
    ".LEADING",                     # must start alphanumeric
])
def test_malformed_tickers_are_refused(ticker):
    assert ticker_problem(ticker) is not None


def test_a_too_long_ticker_is_refused_before_it_reaches_either_backend():
    """SQLite ignores a VARCHAR length and Postgres raises a raw database
    error, so the two disagree unless the check happens in the app."""
    assert "12 characters" in ticker_problem("A" * 40)


@pytest.mark.parametrize("exchange", ["ASX", "NASDAQ", "NYSE", "CRYPTO"])
def test_ordinary_exchanges_are_accepted(exchange):
    assert exchange_problem(exchange) is None


@pytest.mark.parametrize("exchange", ["", "1ASX", "A" * 13, "AS X"])
def test_malformed_exchanges_are_refused(exchange):
    assert exchange_problem(exchange) is not None


def test_the_add_instrument_form_refuses_a_malformed_ticker(client, session_factory):
    make_login(client, session_factory)

    client.post("/holdings/add",
                data={"ticker": BREAKOUT, "exchange": "ASX", "asset_class": "etf",
                      "currency": "AUD", "_csrf": session_csrf(session_factory)},
                headers={"accept": "text/html"})

    with reading(session_factory) as s:
        assert s.scalars(select(Instrument)).all() == []


def test_the_api_refuses_a_malformed_ticker(client, session_factory):
    from app import auth as auth_mod
    from app.models import ApiKey, Portfolio, User

    make_login(client, session_factory)
    raw, key_hash, prefix = auth_mod.new_api_key()
    with session_factory() as s:
        portfolio = s.scalars(select(Portfolio)).first()
        owner = s.scalars(select(User)).first()
        s.add(ApiKey(portfolios=[portfolio], name="k", key_hash=key_hash,
                     prefix=prefix, scopes="read,write", created_by=owner.id))
        s.commit()

    resp = client.post("/api/v1/trades",
                       json={"ticker": BREAKOUT, "type": "buy", "date": "2026-01-05",
                             "units": "1", "unit_price": "1.00"},
                       headers={"Authorization": f"Bearer {raw}"})

    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Escaping — the data blocks the templates embed
# --------------------------------------------------------------------------- #

def _plant_hostile_instrument(session_factory) -> None:
    """A holding whose *name* carries a script breakout.

    The ticker is validated now, so the name is the field a hostile member of a
    shared portfolio could still use — and names are rendered into the chart
    payloads.
    """
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "ACME", name=BREAKOUT)
        fac.add_trade(s, inst, "2026-01-05", "buy", 10, "5.00")
        fac.add_prices(s, inst, [("2026-08-01", "6.00"), ("2026-08-02", "6.50")])
        s.commit()


@pytest.mark.parametrize("path", ["/charts", "/charts/build", "/holding/ACME"])
def test_a_script_breakout_in_stored_data_is_escaped(client, session_factory, path):
    make_login(client, session_factory)
    _plant_hostile_instrument(session_factory)

    body = client.get(path, headers={"accept": "text/html"}).text

    # The literal closing tag must never appear — that is what ends the block.
    assert "</script><script>alert(1)</script>" not in body
    # Anywhere the value is rendered, its angle brackets are encoded.
    assert "alert(1)" not in body or "\\u003c" in body or "&lt;" in body


def test_the_chart_payload_is_still_valid_json(client, session_factory):
    """Escaping must not corrupt the data — the page has to keep working."""
    import json
    import re

    make_login(client, session_factory)
    _plant_hostile_instrument(session_factory)

    body = client.get("/charts", headers={"accept": "text/html"}).text
    block = re.search(r'<script id="chart-data" type="application/json">(.*?)</script>',
                      body, re.S)

    assert block is not None
    json.loads(block.group(1))   # parses, breakout and all


# --------------------------------------------------------------------------- #
# Secrets out of URLs
# --------------------------------------------------------------------------- #

def test_a_new_accounts_password_never_appears_in_a_url(client, session_factory):
    """A query string is written to the access log, the browser history and
    every proxy in between, so a temporary password must not travel in one."""
    make_login(client, session_factory, admin=True)

    resp = client.post("/users/add",
                       data={"name": "New Person", "email": "new@example.test",
                             "password": "temporary-secret-1", "is_admin": "",
                             "_csrf": session_csrf(session_factory)},
                       headers={"accept": "text/html"}, follow_redirects=False)

    assert resp.status_code == 200          # rendered, not redirected
    assert "temporary-secret-1" in resp.text  # …and shown exactly once, in the body
    assert "password=" not in str(resp.url)
    assert "location" not in resp.headers


def test_a_reset_password_never_appears_in_a_url(client, session_factory):
    from app.models import User

    make_login(client, session_factory, admin=True)
    with session_factory() as s:
        other = fac.make_user(s, "other@example.test")
        s.commit()
        other_id = other.id

    resp = client.post(f"/users/{other_id}/reset",
                       data={"password": "temporary-secret-2",
                             "_csrf": session_csrf(session_factory)},
                       headers={"accept": "text/html"}, follow_redirects=False)

    assert resp.status_code == 200
    assert "temporary-secret-2" in resp.text
    assert "location" not in resp.headers
    with session_factory() as s:
        assert s.scalar(select(User).where(User.id == other_id)).must_change_password


# --------------------------------------------------------------------------- #
# Chart ownership and robustness
# --------------------------------------------------------------------------- #

def test_reordering_charts_survives_rubbish_ids(client, session_factory):
    """The order is posted as JSON from the browser; a malformed id should be
    ignored rather than returning a 500."""
    make_login(client, session_factory)
    client.get("/charts", headers={"accept": "text/html"})

    resp = client.post("/charts/order",
                       json={"order": ["not-a-number", None, 999999]},
                       headers={"X-CSRF-Token": session_csrf(session_factory)})

    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Signing in with a password: what a mutation run found unchecked
# --------------------------------------------------------------------------- #

def _seed_failures(session_factory, email: str, count: int = 8) -> None:
    """The configured allowance of failures, from a different address each."""
    import datetime as dt

    from app.models import LoginAttempt

    with session_factory() as s:
        for n in range(count):
            s.add(LoginAttempt(email=email, ip=f"192.0.2.{n + 1}", success=False,
                               created_at=dt.datetime.now(dt.timezone.utc)))
        s.commit()


def test_a_locked_account_refuses_even_the_right_password(client, session_factory, app_module):
    """`auth.is_locked` has its own tests; nothing signed in while locked, so
    the check in the route could go and passwords be guessed without limit."""
    from test_routes import PASSWORD, make_login, pre_auth_csrf

    email = make_login(client, session_factory)
    client.cookies.clear()
    _seed_failures(session_factory, email)

    resp = client.post("/login", follow_redirects=False, headers={"accept": "text/html"},
                       data={"email": email, "password": PASSWORD,
                             "_csrf": pre_auth_csrf(client)})

    assert resp.status_code == 200 and "Too many attempts" in resp.text
    assert app_module.settings.auth.cookie_name not in resp.cookies


def test_an_email_is_matched_however_it_is_typed(client, session_factory, app_module):
    from test_routes import PASSWORD, make_login, pre_auth_csrf

    make_login(client, session_factory, email="user@example.test")
    client.cookies.clear()

    resp = client.post("/login", follow_redirects=False, headers={"accept": "text/html"},
                       data={"email": "  User@Example.TEST ", "password": PASSWORD,
                             "_csrf": pre_auth_csrf(client)})

    assert resp.status_code == 303
    assert app_module.settings.auth.cookie_name in resp.cookies


def test_a_password_hashed_to_an_older_standard_is_rehashed_at_sign_in(client, session_factory):
    from argon2 import PasswordHasher

    from app import auth
    from app.models import User
    from test_routes import PASSWORD, pre_auth_csrf

    old = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    with session_factory() as s:
        fac.make_user(s, "user@example.test", password_hash=old)
        s.commit()
    assert auth.needs_rehash(old)

    client.post("/login", follow_redirects=False, headers={"accept": "text/html"},
                data={"email": "user@example.test", "password": PASSWORD,
                      "_csrf": pre_auth_csrf(client)})

    with session_factory() as s:
        stored = s.query(User).one().password_hash
    assert stored != old and not auth.needs_rehash(stored)
    assert auth.verify_password(stored, PASSWORD)


def test_a_password_hash_that_is_current_is_left_alone_at_sign_in(client, session_factory):
    """Rehashing is for an older standard; doing it every time would rewrite
    the stored hash on every sign-in for nothing."""
    from app import auth
    from app.models import User
    from test_routes import PASSWORD, pre_auth_csrf

    current = auth.hash_password(PASSWORD)
    with session_factory() as s:
        fac.make_user(s, "user@example.test", password_hash=current)
        s.commit()

    resp = client.post("/login", follow_redirects=False, headers={"accept": "text/html"},
                       data={"email": "user@example.test", "password": PASSWORD,
                             "_csrf": pre_auth_csrf(client)})

    assert resp.status_code == 303, "the premise: the sign-in went through"
    with session_factory() as s:
        assert s.query(User).one().password_hash == current


@pytest.mark.parametrize("secure", [False, True])
def test_the_remembered_portfolio_cookie_is_httponly_and_kept_a_year(client, session_factory,
                                                                     app_module, monkeypatch,
                                                                     secure):
    from fastapi.testclient import TestClient

    from app.auth import hash_password
    from test_routes import PASSWORD, pre_auth_csrf

    monkeypatch.setattr(app_module.settings.auth, "cookie_secure", secure)
    with session_factory() as s:
        user = fac.make_user(s, "user@example.test", password_hash=hash_password(PASSWORD))
        fac.make_portfolio(s, "Mine", owner=user)
        s.commit()
    browser = TestClient(app_module.app, base_url="https://testserver") if secure else client
    resp = browser.post("/login", follow_redirects=False, headers={"accept": "text/html"},
                        data={"email": "user@example.test", "password": PASSWORD,
                              "_csrf": pre_auth_csrf(browser)})

    cookie = _cookie(resp, app_module.settings.auth.cookie_name + "_portfolio")
    assert cookie.get("httponly") is True
    assert cookie.get("samesite", "").lower() == "lax"
    assert cookie.get("path") == "/"
    assert int(cookie["max-age"]) == 365 * 86400
    assert (cookie.get("secure") is True) is secure


def test_a_sign_in_with_no_portfolio_to_open_remembers_none(client, session_factory,
                                                            app_module):
    """Nothing to remember is no cookie, not one holding the word "None"."""
    from app.auth import hash_password
    from test_routes import PASSWORD, pre_auth_csrf

    with session_factory() as s:
        fac.make_user(s, "user@example.test", password_hash=hash_password(PASSWORD))
        s.commit()
    resp = client.post("/login", follow_redirects=False, headers={"accept": "text/html"},
                       data={"email": "user@example.test", "password": PASSWORD,
                             "_csrf": pre_auth_csrf(client)})

    assert resp.status_code == 303, "the premise: the sign-in went through"
    remembered = app_module.settings.auth.cookie_name + "_portfolio="
    assert not [c for c in resp.headers.get_list("set-cookie") if c.startswith(remembered)]


def test_signing_in_asks_for_live_prices(client, session_factory, app_module, monkeypatch):
    """The one line every way in shares: without it the first page shows
    yesterday's close for up to a whole polling interval."""
    from test_routes import make_login

    asked = []
    monkeypatch.setattr(app_module, "refresh_quotes_soon", lambda: asked.append(True))

    make_login(client, session_factory)

    assert asked == [True]


@pytest.mark.parametrize("enabled", [True, False])
def test_live_prices_are_fetched_only_while_they_are_on(app_module, monkeypatch, enabled):
    """Turned off, an install makes no quote requests at all."""
    import asyncio
    import threading

    ran = threading.Event()
    monkeypatch.setattr(app_module.settings.price_feed, "quotes_enabled", enabled)
    monkeypatch.setattr(app_module, "_run_quotes", lambda: ran.set() or 0)

    async def sign_in_moment():
        app_module.refresh_quotes_soon()
        await asyncio.sleep(0)

    asyncio.run(sign_in_moment())

    assert ran.wait(2 if enabled else 0.3) is enabled


def test_a_reason_sent_back_to_the_sign_in_page_is_shown(client, session_factory):
    """A provider sign-in happens off-site; when it fails, the sign-in page
    is the only place the reason can be said."""
    import re

    with session_factory() as s:
        fac.make_user(s, "user@example.test")            # past the first-run wizard
        s.commit()

    page = client.get("/login", params={"error": "The provider refused the sign-in."},
                      headers={"accept": "text/html"}).text

    assert re.search(r'class="panel error">\s*The provider refused the sign-in\.', page)
