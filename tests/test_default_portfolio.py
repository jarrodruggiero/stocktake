"""Signing in opens the portfolio you chose, or else the last one you had open.

Every sign-in landed on the first portfolio the person owned, so somebody who
works in a shared one switched to it every time. The profile can now name a
default; without one, a cookie holding only the last portfolio's id decides.
Either is a hint, never a credential: a portfolio the person is not a member
of is ignored, whatever names it.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pyotp
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select

import factories as fac
from app import passkeys, twofactor
from app.models import Portfolio, PortfolioMember, User, UserSession
from test_routes import PASSWORD, make_login, pre_auth_csrf, session_csrf

HTML = {"accept": "text/html"}
EMAIL = "user@example.test"
COOKIE = "pf_session_portfolio"
APP_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def three(client, session_factory):
    """Mine (made at sign-up, owned), Shared (a member), and Theirs (not mine)."""
    make_login(client, session_factory)
    with session_factory() as s:
        user = s.scalar(select(User))
        mine = s.scalar(select(Portfolio))
        shared = fac.make_portfolio(s, "Shared")
        fac.add_member(s, shared, user, role="member")
        stranger = fac.make_user(s, "stranger@example.test")
        theirs = fac.make_portfolio(s, "Theirs", owner=stranger)
        s.commit()
        return {"mine": mine.id, "shared": shared.id, "theirs": theirs.id, "user": user.id}


def _sign_out(client, session_factory):
    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)


def _sign_in(client):
    page = client.get("/login", headers=HTML).text
    token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    return client.post("/login", data={"email": EMAIL, "password": PASSWORD,
                                       "_csrf": token}, headers=HTML,
                       follow_redirects=False)


def _landed(session_factory) -> int:
    with session_factory() as s:
        return s.scalars(select(UserSession.active_portfolio_id)
                         .order_by(UserSession.id.desc())).first()


def _set_default(client, session_factory, value: str):
    return client.post("/profile/appearance", data={
        "_csrf": session_csrf(session_factory), "theme": "auto", "nav_style": "both",
        "times_in": "market", "default_portfolio": value}, headers=HTML,
        follow_redirects=False)


def test_the_default_is_where_a_sign_in_lands(client, session_factory, three):
    assert _set_default(client, session_factory, str(three["shared"])).status_code == 303
    _sign_out(client, session_factory)
    resp = _sign_in(client)
    assert _landed(session_factory) == three["shared"]
    assert resp.cookies.get(COOKIE) == str(three["shared"]), "and now it is the last opened"


def test_the_code_step_keeps_the_default(client, session_factory, three):
    secret = twofactor.new_secret()
    with session_factory() as s:
        twofactor.enable(s, s.get(User, three["user"]), secret)
        s.commit()
    _set_default(client, session_factory, str(three["shared"]))
    _sign_out(client, session_factory)
    half = _sign_in(client)
    assert half.headers["location"] == "/login/code"
    assert COOKIE not in half.cookies, "a password without its code has opened nothing"
    page = client.get("/login/code", headers=HTML).text
    token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    resp = client.post("/login/code", data={"code": pyotp.TOTP(secret).now(), "_csrf": token},
                       headers=HTML, follow_redirects=False)
    assert _landed(session_factory) == three["shared"]
    assert resp.cookies.get(COOKIE) == str(three["shared"])


def test_a_passkey_sign_in_lands_on_the_default(client, session_factory, three, monkeypatch):
    _set_default(client, session_factory, str(three["shared"]))
    _sign_out(client, session_factory)
    monkeypatch.setattr(passkeys, "verify_authentication",
                        lambda db, *_a, **_k: db.get(User, three["user"]))
    resp = client.post("/login/passkey", data={"credential": "{}", "token": "t",
                                                "_csrf": pre_auth_csrf(client)})
    assert resp.status_code == 200, resp.text
    assert _landed(session_factory) == three["shared"]
    assert resp.cookies.get(COOKIE) == str(three["shared"])


def test_a_recovery_code_sign_in_lands_on_the_default(client, session_factory, three):
    with session_factory() as s:
        codes = twofactor.generate_recovery_codes(s, s.get(User, three["user"]))
        s.commit()
    _set_default(client, session_factory, str(three["shared"]))
    _sign_out(client, session_factory)
    client.get("/login/recover", headers=HTML)
    resp = client.post("/login/recover", data={"email": EMAIL, "code": codes[0],
                                               "_csrf": pre_auth_csrf(client)},
                       headers=HTML, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    assert _landed(session_factory) == three["shared"]
    assert resp.cookies.get(COOKIE) == str(three["shared"])


def test_without_a_default_the_last_one_opened_is_where_it_lands(
        client, session_factory, three):
    resp = client.post("/portfolio/switch", data={
        "portfolio_id": str(three["shared"]), "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False)
    assert resp.cookies.get(COOKIE) == str(three["shared"]), "the id, and nothing else"
    _sign_out(client, session_factory)
    _sign_in(client)
    assert _landed(session_factory) == three["shared"]


def test_a_cookie_naming_someone_elses_portfolio_opens_nothing_of_theirs(
        client, session_factory, three):
    _sign_out(client, session_factory)
    client.cookies.set(COOKIE, str(three["theirs"]))
    _sign_in(client)
    assert _landed(session_factory) == three["mine"]


@pytest.mark.parametrize("value", ["abc", "-1", "0", "99999999999999999999", "1e9", "",
                                   "1; 2"])
def test_a_cookie_that_is_not_an_id_is_ignored(client, session_factory, three, value):
    _sign_out(client, session_factory)
    client.cookies.set(COOKIE, value)
    assert _sign_in(client).status_code == 303
    assert _landed(session_factory) == three["mine"]


def test_a_cookie_of_superscript_digits_is_ignored(app_module):
    """A Cookie header is read as latin-1, where "²" is one byte and a digit to
    isdigit(), but nothing int() can parse. The test client re-encodes headers
    as UTF-8, so the byte goes to the reader directly."""
    from starlette.requests import Request

    raw = [(b"cookie", f"{COOKIE}=".encode() + b"\xb2")]
    assert app_module._remembered_portfolio(Request({"type": "http", "headers": raw})) is None


def test_a_default_since_left_falls_back(client, session_factory, three):
    _set_default(client, session_factory, str(three["shared"]))
    with session_factory() as s:
        s.execute(delete(PortfolioMember).where(
            PortfolioMember.portfolio_id == three["shared"]))
        s.commit()
    _sign_out(client, session_factory)
    _sign_in(client)
    assert _landed(session_factory) == three["mine"]


def test_a_default_that_was_deleted_is_cleared(client, session_factory, three):
    _set_default(client, session_factory, str(three["shared"]))
    with session_factory() as s:
        s.execute(delete(PortfolioMember).where(
            PortfolioMember.portfolio_id == three["shared"]))
        s.delete(s.get(Portfolio, three["shared"]))
        s.commit()
    with session_factory() as s:
        assert s.get(User, three["user"]).default_portfolio_id is None


def test_the_profile_refuses_a_portfolio_that_is_not_yours(client, session_factory, three):
    assert _set_default(client, session_factory, str(three["theirs"])).status_code == 400
    with session_factory() as s:
        assert s.get(User, three["user"]).default_portfolio_id is None


@pytest.mark.parametrize("value", ["\u00b2", "\u0661", "9" * 5000, " 1", "1.0"])
def test_the_profile_refuses_what_is_not_an_id(client, session_factory, three, value):
    assert _set_default(client, session_factory, value).status_code == 400


def test_last_opened_is_the_empty_choice(client, session_factory, three):
    _set_default(client, session_factory, str(three["shared"]))
    _set_default(client, session_factory, "")
    with session_factory() as s:
        assert s.get(User, three["user"]).default_portfolio_id is None
    page = client.get("/profile", headers=HTML).text
    picker = re.search(r'<select name="default_portfolio">.*?</select>', page, re.S).group(0)
    assert re.findall(r"<option[^>]*>([^<]+)", picker) == ["Last opened", "Shared",
                                                           "Test portfolio"]


def test_one_portfolio_offers_no_choice(client, session_factory):
    make_login(client, session_factory)
    assert 'name="default_portfolio"' not in client.get("/profile", headers=HTML).text


def test_the_migration_adds_and_removes_the_default(tmp_path):
    """Run against a populated database, downgrade included (PR template)."""
    path = tmp_path / "default.db"
    cfg = Config()
    cfg.set_main_option("script_location", str(APP_ROOT / "app" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(cfg, "0011")
    conn = sqlite3.connect(path)
    conn.executescript("""
        INSERT INTO user (id, email, name, password_hash, public_id, created_at)
        VALUES (1, 'a@example.com', 'A', 'x', 'pub-1', '2026-01-01');
        INSERT INTO portfolio (id, name, created_at) VALUES (1, 'Main', '2026-01-01');
    """)
    conn.commit()
    conn.close()

    command.upgrade(cfg, "0012")
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT email, default_portfolio_id FROM user").fetchall() == [
        ("a@example.com", None)]
    conn.close()

    command.downgrade(cfg, "0011")
    conn = sqlite3.connect(path)
    assert "default_portfolio_id" not in {r[1] for r in conn.execute("PRAGMA table_info(user)")}
    assert conn.execute("SELECT email FROM user").fetchall() == [("a@example.com",)]
    conn.close()


def test_a_provider_sign_in_lands_on_the_default(client, session_factory, three,
                                                  app_module, monkeypatch):
    import urllib.parse

    from app import federation
    from app.models import OidcState
    from appcore import oidc
    from appcore.testing import FakeIdp

    issuer = "https://idp.example.test"
    idp = FakeIdp(issuer=issuer, client_id="stocktake").install(monkeypatch)
    conf = app_module.settings.auth.oidc
    for name, value in (("enabled", True), ("issuer", issuer), ("client_id", "stocktake"),
                        ("client_secret", "s"), ("provisioning", "off"),
                        ("redirect_uri", "https://stocktake.example.test/login/oidc/callback")):
        monkeypatch.setattr(conf, name, value)
    with session_factory() as s:
        federation.link(s, s.get(User, three["user"]), oidc.Identity(
            subject="idp-subject-1", issuer=issuer, email=EMAIL, email_verified=True, name="U"))
        s.commit()
    _set_default(client, session_factory, str(three["shared"]))
    _sign_out(client, session_factory)

    out = client.get("/login/oidc", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(out.headers["location"]).query)["state"][0]
    with session_factory() as s:
        idp.claims = {"nonce": s.scalars(select(OidcState.nonce)
                                         .order_by(OidcState.id.desc())).first()}
    back = client.get(f"/login/oidc/callback?code=abc&state={state}", follow_redirects=False)
    assert back.headers["location"] == "/", back.text
    assert _landed(session_factory) == three["shared"]
    assert back.cookies.get(COOKIE) == str(three["shared"])
