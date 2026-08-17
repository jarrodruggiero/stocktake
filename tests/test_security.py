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
    from app.models import ApiKey, Portfolio

    make_login(client, session_factory)
    raw, key_hash, prefix = auth_mod.new_api_key()
    with session_factory() as s:
        portfolio = s.scalars(select(Portfolio)).first()
        s.add(ApiKey(portfolio_id=portfolio.id, name="k", key_hash=key_hash,
                     prefix=prefix, scopes="read,write"))
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
