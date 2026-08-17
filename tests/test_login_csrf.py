"""Logging in after being timed out.

The bug: "if I'm logged in and I'm logged out from
inactivity, if I try to log in again I get an 'Invalid or missing CSRF token'
error; going back to the login page and logging in again works."

Two causes, and the second is the one that matters.

  1. **The cookie lifetime collided with the idle window.** `pf_csrf` lived
     3600 seconds and the idle window is 60 minutes, so the pre-auth cookie
     lapsed at almost exactly the moment somebody was timed out and sent back
     to the login page.
  2. **A stale token on the login form produced a 403 error page.** The login
     page is the one form in the app that routinely sits open long enough to go
     stale, and the failure told the user they had done something wrong.

Fixing only (1) would move the window rather than close it: a second tab, a
page restored from history, or a laptop shut overnight all reach the same
place.
"""

from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import select

from app.models import UserSession
from test_routes import PASSWORD, make_login

HTML = {"accept": "text/html"}


def _walk_away(session_factory, minutes: int = 999) -> None:
    """Age every session past the idle window — the real thing that happens
    when somebody closes the laptop. No /logout is ever posted."""
    now = dt.datetime.now(dt.timezone.utc)
    with session_factory() as s:
        for row in s.scalars(select(UserSession)):
            row.last_seen_at = now - dt.timedelta(minutes=minutes)
            row.created_at = now - dt.timedelta(minutes=minutes)
        s.commit()


def _token(page: str) -> str:
    return re.search(r'name="_csrf" value="([^"]+)"', page).group(1)


def test_the_pre_auth_cookie_outlives_the_idle_window():
    """The collision, asserted as a relationship rather than a number: whatever
    the idle window becomes, the cookie must still be there afterwards."""
    from app.main import PRE_AUTH_CSRF_SECONDS, settings

    idle = settings.auth.session_idle_minutes * 60
    assert PRE_AUTH_CSRF_SECONDS > idle * 2, (
        f"the pre-auth cookie ({PRE_AUTH_CSRF_SECONDS}s) does not comfortably "
        f"outlive the idle window ({idle}s), so it will lapse at the moment "
        "somebody is timed out and sent to log in again")


def test_logging_in_after_being_timed_out(client, session_factory):
    """The reported flow, end to end."""
    make_login(client, session_factory)
    _walk_away(session_factory)

    page = client.get("/login", headers=HTML)
    resp = client.post("/login",
                       data={"email": "user@example.test", "password": PASSWORD,
                             "_csrf": _token(page.text)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303, "signing back in after a timeout was refused"


def test_a_login_page_whose_cookie_lapsed_gives_a_form_not_an_error(
        client, session_factory):
    """The general case, which the longer cookie alone would not fix: the page
    sat open until its cookie went. It must come back as a login form."""
    make_login(client, session_factory)
    _walk_away(session_factory)
    page = client.get("/login", headers=HTML)
    token = _token(page.text)
    client.cookies.delete("pf_csrf")

    resp = client.post("/login",
                       data={"email": "user@example.test", "password": PASSWORD,
                             "_csrf": token},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 200, "still a 403 error page"
    assert "Invalid or missing CSRF token" not in resp.text
    assert 'name="password"' in resp.text, "no form to try again with"


def test_that_retry_actually_works(client, session_factory):
    """The re-issued form must carry a token that matches the cookie it set —
    otherwise this is a loop that looks friendlier and still cannot be used."""
    make_login(client, session_factory)
    _walk_away(session_factory)
    client.cookies.delete("pf_csrf")
    refused = client.post("/login",
                          data={"email": "user@example.test",
                                "password": PASSWORD, "_csrf": "stale"},
                          headers=HTML)

    resp = client.post("/login",
                       data={"email": "user@example.test", "password": PASSWORD,
                             "_csrf": _token(refused.text)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303


def test_a_wrong_token_still_never_signs_anybody_in(client, session_factory):
    """The check is not weakened — only the refusal's presentation changed.
    A cross-site POST carries no SameSite=Lax cookie, so it lands here, and it
    must get a login page rather than a session."""
    make_login(client, session_factory)
    client.cookies.delete("pf_session")
    client.cookies.delete("pf_csrf")

    resp = client.post("/login",
                       data={"email": "user@example.test", "password": PASSWORD,
                             "_csrf": "forged"},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 200, "a forged token must not redirect anywhere"
    assert "pf_session" not in resp.cookies, "a session was issued without a valid token"


def test_a_genuinely_wrong_password_still_says_so(client, session_factory):
    """The friendlier CSRF message must not swallow the real failure."""
    make_login(client, session_factory)
    page = client.get("/login", headers=HTML)

    resp = client.post("/login",
                       data={"email": "user@example.test", "password": "nope",
                             "_csrf": _token(page.text)},
                       headers=HTML)

    assert "Invalid email or password." in resp.text
