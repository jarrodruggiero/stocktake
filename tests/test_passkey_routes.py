"""Passkeys through the HTTP endpoints, with a real authenticator on the end.

`test_passkeys.py` covers the ceremonies; this covers what the browser actually
talks to — the CSRF, the cookies, the redirects, and the one behaviour that
makes passkeys worth having: signing in without the password or the code.

Requests are made over `https://` because WebAuthn is refused outside a secure
context, and the app refuses to offer it there too.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from test_passkeys import ORIGIN, RP_ID, VerifyingDevice, _as_json, _for_device

from app import twofactor
from app.models import User, WebauthnCredential
from test_routes import PASSWORD, make_login, pre_auth_csrf, session_csrf

HTML = {"accept": "text/html"}


@pytest.fixture
def secure(app_module, session_factory):
    """A client on HTTPS, with passkeys configured as a deployment would."""
    from fastapi.testclient import TestClient

    wa = app_module.settings.auth.webauthn
    before = (wa.enabled, wa.rp_id, wa.rp_name, list(wa.origins))
    wa.enabled, wa.rp_id, wa.rp_name, wa.origins = True, RP_ID, "Stocktake", [ORIGIN]

    original_local = app_module.SessionLocal
    original_state = app_module.app.state.session_factory
    app_module.SessionLocal = session_factory
    app_module.app.state.session_factory = session_factory
    try:
        yield TestClient(app_module.app, base_url=ORIGIN)
    finally:
        wa.enabled, wa.rp_id, wa.rp_name, wa.origins = before
        app_module.SessionLocal = original_local
        app_module.app.state.session_factory = original_state


def _add_passkey(client, session_factory, device=None):
    """Enrol through the two real endpoints, as the script does."""
    device = device or VerifyingDevice()
    csrf = session_csrf(session_factory)
    options = client.post("/profile/passkeys/options", data={"_csrf": csrf}).json()
    attestation = device.create(_for_device(json.dumps(options["options"])), ORIGIN)
    resp = client.post("/profile/passkeys", follow_redirects=False, data={
        "_csrf": csrf, "token": options["token"],
        "credential": _as_json(attestation), "name": "My phone"})
    return device, resp


def _sign_in_with(client, device):
    csrf = pre_auth_csrf(client)
    options = client.post("/login/passkey/options", data={"_csrf": csrf}).json()
    assertion = device.get(_for_device(json.dumps(options["options"])), ORIGIN)
    return client.post("/login/passkey", data={
        "_csrf": csrf, "token": options["token"],
        "credential": _as_json(assertion)})


# --------------------------------------------------------------------------- #
# Enrolling
# --------------------------------------------------------------------------- #

def test_a_passkey_can_be_added_from_the_account(secure, session_factory):
    make_login(secure, session_factory)
    _, resp = _add_passkey(secure, session_factory)

    assert resp.headers["location"] == "/profile/passkeys?saved=added"
    with session_factory() as db:
        row = db.scalar(select(WebauthnCredential))
        assert row.name == "My phone"

    page = secure.get("/profile/passkeys", headers=HTML).text
    assert "My phone" in page


def test_the_account_page_reports_passkeys_as_a_way_in(secure, session_factory):
    """The whole point of the methods table: every way into this account and
    whether it is on, without having to infer it from which forms appear."""
    make_login(secure, session_factory)
    import re

    def row() -> str:
        page = secure.get("/profile", headers=HTML).text
        table = re.search(r'<table class="methods">.*?</table>', page, re.S).group(0)
        return re.search(r"<tr>(?:(?!</tr>).)*Passkeys.*?</tr>", table, re.S).group(0)

    assert ">Off<" in row()
    _add_passkey(secure, session_factory)
    assert ">On<" in row() and "Manage" in row()


def test_a_passkey_can_be_removed(secure, session_factory):
    make_login(secure, session_factory)
    _add_passkey(secure, session_factory)
    with session_factory() as db:
        key_id = db.scalar(select(WebauthnCredential)).id

    secure.post(f"/profile/passkeys/{key_id}/remove", follow_redirects=False,
                data={"_csrf": session_csrf(session_factory)})

    with session_factory() as db:
        assert db.scalar(select(WebauthnCredential)) is None


def test_one_person_cannot_remove_another_persons_passkey(secure, session_factory):
    """The id comes from a form, so it must never reach a row that is not
    theirs — scoping the delete to the signed-in user is the whole defence."""
    make_login(secure, session_factory)
    _add_passkey(secure, session_factory)
    with session_factory() as db:
        key_id = db.scalar(select(WebauthnCredential)).id

    secure.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)
    make_login(secure, session_factory, email="them@example.test")
    secure.post(f"/profile/passkeys/{key_id}/remove", follow_redirects=False,
                data={"_csrf": session_csrf(session_factory)})

    with session_factory() as db:
        assert db.get(WebauthnCredential, key_id) is not None


# --------------------------------------------------------------------------- #
# Signing in
# --------------------------------------------------------------------------- #

def test_a_passkey_signs_in_with_no_password(secure, session_factory):
    make_login(secure, session_factory)
    device, _ = _add_passkey(secure, session_factory)
    secure.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)
    assert secure.get("/", headers=HTML, follow_redirects=False).status_code == 303

    resp = _sign_in_with(secure, device)

    assert resp.status_code == 200, resp.text
    assert resp.json()["next"] == "/"
    assert secure.get("/", headers=HTML, follow_redirects=False).status_code == 200


def test_a_passkey_does_not_stop_at_the_code_step(secure, session_factory):
    """The behaviour that makes them worth having.

    A password sign-in with two-factor on lands on `/login/code` holding a
    session that `load_session` refuses. A passkey has already proved both
    factors — possession of the authenticator and a biometric or PIN — so
    stopping to ask for a code would be asking for a third. decisions.md #113.
    """
    make_login(secure, session_factory)
    device, _ = _add_passkey(secure, session_factory)
    with session_factory() as db:
        user = db.scalar(select(User))
        twofactor.enable(db, user, "JBSWY3DPEHPK3PXP")
        db.commit()
    secure.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)

    # The password route does stop, which is what makes the comparison mean
    # something rather than proving two-factor was simply off.
    token = pre_auth_csrf(secure)
    password_login = secure.post(
        "/login", follow_redirects=False, headers=HTML,
        data={"email": "user@example.test", "password": PASSWORD, "_csrf": token})
    assert password_login.headers["location"] == "/login/code"

    assert _sign_in_with(secure, device).json()["next"] == "/"
    assert secure.get("/", headers=HTML, follow_redirects=False).status_code == 200


def test_an_unregistered_key_is_refused_without_saying_why(secure, session_factory):
    make_login(secure, session_factory)
    _add_passkey(secure, session_factory)
    secure.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)
    stranger = VerifyingDevice()
    stranger.cred_init(RP_ID, b"stranger")

    resp = _sign_in_with(secure, stranger)

    assert resp.status_code == 400
    assert secure.get("/", headers=HTML, follow_redirects=False).status_code == 303


# --------------------------------------------------------------------------- #
# When they cannot be offered
# --------------------------------------------------------------------------- #

def test_plain_http_is_offered_nothing(client, app_module, session_factory):
    """A browser refuses WebAuthn outside a secure context, so a button here
    would fail inside the browser where the reason is invisible."""
    wa = app_module.settings.auth.webauthn
    before = (wa.enabled, wa.rp_id, list(wa.origins))
    wa.enabled, wa.rp_id, wa.origins = True, RP_ID, [ORIGIN]
    try:
        # The account comes first: with none, `/login` redirects into the setup
        # wizard, and the assertion below would pass by reading that page.
        make_login(client, session_factory)
        assert client.post("/profile/passkeys/options", data={
            "_csrf": session_csrf(session_factory)}).status_code == 400
        assert "HTTPS" in client.get("/profile/passkeys", headers=HTML).text
        client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                    follow_redirects=False)
        assert "passkeysignin" not in client.get("/login", headers=HTML).text
    finally:
        wa.enabled, wa.rp_id, wa.origins = before


def test_the_login_page_offers_a_passkey_when_it_can(secure, session_factory):
    make_login(secure, session_factory)
    secure.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)

    assert "passkeysignin" in secure.get("/login", headers=HTML).text


def test_the_options_endpoints_need_a_csrf_token(secure, session_factory):
    make_login(secure, session_factory)

    assert secure.post("/profile/passkeys/options", data={"_csrf": "wrong"}
                       ).status_code in (400, 403)
    assert secure.post("/login/passkey/options", data={"_csrf": "wrong"}
                       ).status_code in (400, 403)
