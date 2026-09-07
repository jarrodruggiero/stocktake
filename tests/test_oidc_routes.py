"""Signing in through a provider, from the button to the session cookie.

The whole round trip is exercised: the redirect out carries what it should, the
callback is refused unless `state` matches the attempt that started it, and a
verified identity ends up holding a real session.

The provider is `test_oidc.FakeIdp`, which really signs. Nothing reaches the
network.
"""

from __future__ import annotations

import urllib.parse

import pytest
from sqlalchemy import select
from test_oidc import CLIENT_ID, ISSUER, FakeIdp

from app import federation
from app.models import ExternalIdentity, PortfolioMember, User
from appkit import oidc
from test_routes import make_login, session_csrf

HTML = {"accept": "text/html"}
REDIRECT = "https://stocktake.example.test/login/oidc/callback"


@pytest.fixture
def idp(monkeypatch):
    """The provider, with its signing key wired into PyJWT's key lookup."""
    fake = FakeIdp()
    monkeypatch.setattr(oidc, "_fetch", fake.fetch)

    from jwt import PyJWK, PyJWKClient

    def signing_key(self, token):  # noqa: ARG001 - matches what it replaces
        import base64

        numbers = fake.key.public_key().public_numbers()

        def b64(value: int) -> str:
            raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        return PyJWK.from_dict({"kty": "RSA", "kid": fake.kid, "alg": "RS256",
                                "use": "sig", "n": b64(numbers.n),
                                "e": b64(numbers.e)})

    monkeypatch.setattr(PyJWKClient, "get_signing_key_from_jwt", signing_key)
    return fake


@pytest.fixture
def oidc_on(app_module, idp):
    """Configured as a deployment would be, and put back afterwards."""
    conf = app_module.settings.auth.oidc
    before = (conf.enabled, conf.issuer, conf.client_id, conf.client_secret,
              conf.redirect_uri, conf.provisioning)
    conf.enabled, conf.issuer, conf.client_id = True, ISSUER, CLIENT_ID
    conf.client_secret, conf.redirect_uri = "a-secret", REDIRECT
    conf.provisioning = "off"
    try:
        yield conf
    finally:
        (conf.enabled, conf.issuer, conf.client_id, conf.client_secret,
         conf.redirect_uri, conf.provisioning) = before


def _begin(client, path: str = "/login/oidc") -> str:
    """Follow the button and return the `state` the app sent to the provider."""
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    query = urllib.parse.parse_qs(
        urllib.parse.urlparse(resp.headers["location"]).query)
    return query["state"][0]


def _callback(client, idp, state: str, nonce: str | None = None):
    idp.claims = {"nonce": nonce if nonce is not None else _nonce_for(client, idp)}
    return client.get(f"/login/oidc/callback?code=abc&state={state}",
                      follow_redirects=False)


def _nonce_for(client, idp) -> str:
    """The nonce the app stored for this attempt.

    Read from the row rather than guessed: it never reaches the browser, which
    is the point of it.
    """
    from app.models import OidcState

    factory = client.app.state.session_factory
    with factory() as db:
        return db.scalars(select(OidcState).order_by(OidcState.id.desc())).first().nonce


# --------------------------------------------------------------------------- #
# The round trip
# --------------------------------------------------------------------------- #

def test_a_linked_account_signs_in(client, session_factory, oidc_on, idp):
    make_login(client, session_factory)
    with session_factory() as db:
        user = db.scalar(select(User))
        federation.link(db, user, oidc.Identity(
            subject="idp-subject-1", issuer=ISSUER, email="user@example.test",
            email_verified=True, name="User"))
        db.commit()
    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)

    state = _begin(client)
    resp = _callback(client, idp, state)

    assert resp.headers["location"] == "/"
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 200


def test_the_redirect_out_names_this_app_and_its_callback(client, oidc_on, idp):
    resp = client.get("/login/oidc", follow_redirects=False)
    query = urllib.parse.parse_qs(
        urllib.parse.urlparse(resp.headers["location"]).query)

    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [REDIRECT]
    assert query["code_challenge_method"] == ["S256"]


# --------------------------------------------------------------------------- #
# What must be refused
# --------------------------------------------------------------------------- #

def test_a_callback_with_the_wrong_state_is_refused(
    client, session_factory, oidc_on, idp
):
    """`state` is the whole defence on this route: it is a GET the provider
    triggers, so there is no CSRF token to check.

    The account is LINKED first, so everything else about this callback would
    succeed. Without that the test passes whether or not the check exists —
    provisioning refuses the identity anyway, and the refusal looks identical.
    """
    make_login(client, session_factory)
    with session_factory() as db:
        federation.link(db, db.scalar(select(User)), oidc.Identity(
            subject="idp-subject-1", issuer=ISSUER, email="user@example.test",
            email_verified=True, name="User"))
        db.commit()
    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)
    state = _begin(client)
    idp.claims = {"nonce": _nonce_for(client, idp)}

    # The same request that would have signed them in, with one value changed.
    resp = client.get("/login/oidc/callback?code=abc&state=not-the-one",
                      follow_redirects=False)

    assert resp.headers["location"].startswith("/login?error=")
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 303
    # And the right one still works, so the refusal was about `state` alone.
    assert _callback(client, idp, state).headers["location"] == "/"


def test_a_callback_with_no_attempt_at_all_is_refused(client, oidc_on, idp):
    resp = client.get("/login/oidc/callback?code=abc&state=invented",
                      follow_redirects=False)

    assert resp.headers["location"].startswith("/login?error=")


def test_an_unknown_identity_is_refused_when_provisioning_is_off(
    client, session_factory, oidc_on, idp
):
    make_login(client, session_factory)
    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)

    state = _begin(client)
    resp = _callback(client, idp, state)

    assert resp.headers["location"].startswith("/login?error=")
    with session_factory() as db:
        assert db.scalar(select(User).where(
            User.email == "member@example.test")) is None


def test_a_provider_error_comes_back_as_a_message(client, oidc_on, idp):
    resp = client.get("/login/oidc/callback?error=access_denied",
                      follow_redirects=False)

    assert "refused" in urllib.parse.unquote(resp.headers["location"])


def test_the_attempt_survives_a_failure_so_it_can_be_retried(
    client, session_factory, oidc_on, idp
):
    """A refusal must not burn the attempt: the state row is spent only once
    everything has succeeded, so a stumble does not force a second trip."""
    from app.models import OidcState

    state = _begin(client)
    client.get("/login/oidc/callback?code=abc&state=wrong", follow_redirects=False)

    with session_factory() as db:
        assert db.scalar(select(OidcState)) is not None
    assert _callback(client, idp, state).headers["location"].startswith("/login")


# --------------------------------------------------------------------------- #
# Provisioning
# --------------------------------------------------------------------------- #

def test_open_provisioning_creates_the_account(
    client, session_factory, oidc_on, idp
):
    make_login(client, session_factory)
    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)
    oidc_on.provisioning = "open"

    state = _begin(client)
    resp = _callback(client, idp, state)

    assert resp.headers["location"] == "/"
    with session_factory() as db:
        made = db.scalar(select(User).where(User.email == "member@example.test"))
        assert made is not None
        # No password at all, rather than one nobody can use.
        assert made.password_hash is None


def test_an_invite_carries_across_the_round_trip(
    client, session_factory, oidc_on, idp
):
    """The reason `provisioning: invite` is usable: the link somebody was sent
    has to survive being bounced through the provider and back."""
    make_login(client, session_factory)
    issued = client.post("/members/invite", follow_redirects=False,
                         data={"_csrf": session_csrf(session_factory),
                               "role": "viewer"})
    token = issued.headers["location"].partition("invite=")[2]
    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)
    oidc_on.provisioning = "invite"

    state = _begin(client, f"/login/oidc?invite={token}")
    resp = _callback(client, idp, state)

    assert resp.headers["location"] == "/"
    with session_factory() as db:
        made = db.scalar(select(User).where(User.email == "member@example.test"))
        member = db.scalar(select(PortfolioMember).where(
            PortfolioMember.user_id == made.id))
        assert member.role == "viewer"


def test_invite_provisioning_refuses_somebody_without_one(
    client, session_factory, oidc_on, idp
):
    make_login(client, session_factory)
    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)
    oidc_on.provisioning = "invite"

    state = _begin(client)
    resp = _callback(client, idp, state)

    assert "invitation" in urllib.parse.unquote(resp.headers["location"])


# --------------------------------------------------------------------------- #
# Off, and linking
# --------------------------------------------------------------------------- #

def test_nothing_is_offered_when_it_is_not_configured(client, session_factory):
    make_login(client, session_factory)
    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)

    assert "/login/oidc" not in client.get("/login", headers=HTML).text
    assert client.get("/login/oidc", follow_redirects=False
                      ).headers["location"].startswith("/login?error=")


def test_the_login_page_offers_it_when_it_is(client, session_factory, oidc_on, idp):
    make_login(client, session_factory)
    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)

    assert "/login/oidc" in client.get("/login", headers=HTML).text


def test_a_signed_in_account_can_link_a_provider(
    client, session_factory, oidc_on, idp
):
    """How an existing local account gains a provider — deliberately, by
    somebody already signed in, so email matching is never involved."""
    make_login(client, session_factory)

    resp = client.post("/profile/oidc/link", follow_redirects=False,
                       data={"_csrf": session_csrf(session_factory)})
    state = urllib.parse.parse_qs(
        urllib.parse.urlparse(resp.headers["location"]).query)["state"][0]
    _callback(client, idp, state)

    with session_factory() as db:
        link = db.scalar(select(ExternalIdentity))
        assert link is not None and link.issuer == ISSUER
    assert "Single sign-on" in client.get("/profile", headers=HTML).text
