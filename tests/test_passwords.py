"""What the app asks of a new password, and what it says about one.

At least 10 characters, as before, and now at most 64 (NIST 800-63B-4 asks
that 64 be allowed; ASVS 6.2.9 the same). No other rule: no mix of character
types, no list of banned passwords. The cap applies where a password is set,
never at sign-in, so an account with a longer one still gets in. Creating an
account asks for it twice, as changing it already did.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest
from sqlalchemy import select

from app import auth
from app.models import User
from test_routes import PASSWORD, make_login, pre_auth_csrf, session_csrf

HTML = {"accept": "text/html"}
TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


@pytest.mark.parametrize("length, problem", [
    (9, "at least 10"), (10, None), (64, None), (65, "at most 64"),
])
def test_a_password_is_10_to_64_characters(length, problem):
    found = auth.password_problem("x" * length)
    assert (found is None) if problem is None else (problem in (found or "")), found


def test_characters_not_bytes_are_counted():
    """NIST: each code point is one character. 64 of a three-byte one is fine."""
    assert auth.password_problem("€" * 64) is None


def test_changing_to_65_characters_is_refused(client, session_factory):
    make_login(client, session_factory)
    long = "x" * 65
    page = client.post("/profile/password", data={
        "current_password": PASSWORD, "password": long, "confirm": long,
        "_csrf": session_csrf(session_factory)}, headers=HTML)
    assert page.status_code == 200 and "at most 64" in page.text


def test_a_longer_password_from_before_still_signs_in(client, session_factory):
    """The cap is for choosing one. Refusing an existing account at the door
    would lock its owner out of the only place they could change it."""
    make_login(client, session_factory)
    long = "an old passphrase that is well over sixty-four characters long, as some were"
    with session_factory() as s:
        s.scalar(select(User)).password_hash = auth.hash_password(long)
        s.commit()
    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)

    login = client.get("/login", headers=HTML).text
    token = re.search(r'name="_csrf" value="([^"]+)"', login).group(1)
    resp = client.post("/login", data={"email": "user@example.test", "password": long,
                                       "_csrf": token}, headers=HTML,
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/"


def _invite(client, session_factory) -> str:
    make_login(client, session_factory)
    resp = client.post("/members/invite", follow_redirects=False,
                       data={"_csrf": session_csrf(session_factory), "role": "member"})
    token = re.search(r"invite=([^&]+)", resp.headers["location"]).group(1)
    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)
    return token


def _joiner(session_factory):
    with session_factory() as s:
        return s.scalar(select(User).where(User.email == "joiner@example.test"))


@pytest.mark.parametrize("confirm", ["something else entirely", ""])
def test_signing_up_asks_for_the_password_twice(client, session_factory, confirm):
    token = _invite(client, session_factory)
    page = client.get(f"/invite/{token}", headers=HTML).text
    assert re.search(r'<input type="password" name="confirm"', page)

    resp = client.post(f"/invite/{token}/signup", headers=HTML, data={
        "_csrf": pre_auth_csrf(client), "name": "Joiner", "email": "joiner@example.test",
        "password": PASSWORD, "confirm": confirm})
    assert resp.status_code == 200 and "The passwords don't match." in html.unescape(resp.text)
    assert _joiner(session_factory) is None
    _kept_all_but_the_password(resp.text, "Joiner", "joiner@example.test")


def test_signing_up_refuses_65_characters(client, session_factory):
    token = _invite(client, session_factory)
    long = "x" * 65
    resp = client.post(f"/invite/{token}/signup", headers=HTML, data={
        "_csrf": pre_auth_csrf(client), "name": "Joiner", "email": "joiner@example.test",
        "password": long, "confirm": long})
    assert "at most 64" in resp.text
    assert _joiner(session_factory) is None


def test_the_setup_account_asks_for_the_password_twice(client, session_factory):
    page = client.get("/setup", headers=HTML).text
    token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    client.post("/setup", data={"_csrf": token}, headers=HTML, follow_redirects=False)
    assert re.search(r'<input type="password" name="confirm"',
                     client.get("/setup/profile", headers=HTML).text)

    resp = client.post("/setup/profile", headers=HTML, data={
        "name": "Owner", "email": "owner@example.test", "password": PASSWORD,
        "confirm": PASSWORD + "x", "_csrf": token})
    assert resp.status_code == 200 and "The passwords don't match." in html.unescape(resp.text)
    with session_factory() as s:
        assert s.scalar(select(User)) is None
    _kept_all_but_the_password(resp.text, "Owner", "owner@example.test")


def _kept_all_but_the_password(page: str, name: str, email: str) -> None:
    """A refusal sends back what was typed, so only the passwords are typed again."""
    assert re.search(rf'<input name="name" value="{name}"', page), "the name was lost"
    assert re.search(rf'<input type="email" name="email" value="{re.escape(email)}"',
                     page), "the email was lost"
    assert PASSWORD not in page, "a password was sent back"


# Every field where somebody chooses an account password: the wizard, an
# invite, changing your own, and an admin's temporary one. Not the Postgres
# password in the wizard's database step, which is somebody else's rule.
ACCOUNT_PASSWORDS = ["setup_account.html", "invite.html", "_password_form.html",
                     "users.html"]


@pytest.mark.parametrize("template", ACCOUNT_PASSWORDS)
def test_every_new_password_field_says_10_to_64(template):
    text = (TEMPLATES / template).read_text()
    inputs = re.findall(r'<input type="(?:password|text)" name="(?:password|confirm)"[^>]*>',
                        text, re.S)
    assert inputs, f"no password field in {template}"
    for tag in inputs:
        assert 'minlength="10"' in tag and 'maxlength="64"' in tag, (template, tag)


def test_the_meter_loads_zxcvbn_from_where_it_is_served(client, session_factory):
    """The browser test inlines zxcvbn; this is the real path, which the meter
    asks for on the first keystroke."""
    make_login(client, session_factory)
    page = client.get("/profile", headers=HTML).text
    src = re.search(r'class="strength"[^>]*data-zxcvbn="([^"?]+)', page)
    assert src, "no meter on the change-password form"
    served = client.get(src.group(1))
    assert served.status_code == 200 and "zxcvbn" in served.text[:200]
