"""The provider telling us a session ended — issue #28.

appcore verifies the `logout_token` and stops. Its own suite covers the
protocol: the signature, the `events` claim, and the two checks that stop an ID
token being accepted as an instruction to log somebody out. This file covers
what a *verified* notice does here, which is the half appcore cannot know.

## It is session propagation, not revocation

The first thing worth stating, because the feature invites the stronger
reading. A logout notice ends **sessions** and never sets `is_active = False`:
letting a provider disable an account is a much larger authority to hand over,
and it would break the promise that local accounts keep working when the
provider is down. So somebody holding both a password and a provider identity
is signed out and can sign straight back in locally a moment later. That is
the behaviour, not a gap in it.

What the app already does is stronger and separate: deactivating a user calls
`destroy_sessions_for()` immediately, and `load_auth` deletes an inactive
user's session on their next request. Back-channel logout does not reach that
switch and is not meant to.

## `sid` first, `sub` as the blunt instrument

A token names a session (`sid`) or an identity (`sub`). `sid` ends exactly the
session the provider meant. `sub` ends every session for that identity — more
than it asked for, and all that can be done with what it sent.

## Unauthenticated on purpose

No cookie, no CSRF token, no API key. The signature on the token is the whole
authentication, which is what the spec intends and what lets a **public
client** use it — the reporter's deployment is one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

# Plain functions only. `idp` and `oidc_on` are FIXTURES and are declared below
# rather than imported: importing a fixture shadows the parameter of the same
# name, which ruff reports as a redefinition and pytest resolves by luck. Third
# time I have made that mistake, so it is written down here as well.
from test_oidc_routes import CLIENT_ID, ISSUER, REDIRECT, _begin, _nonce_for

import factories as fac
from app import auth as auth_mod
from app import federation
from app.models import ExternalIdentity, User, UserSession
from appcore import oidc
from appcore.testing import FakeIdp
from test_routes import make_login, session_csrf

HTML = {"accept": "text/html"}
ENDPOINT = "/oidc/backchannel-logout"


@pytest.fixture
def idp(monkeypatch):
    """A provider that really signs — see `test_oidc_routes` for the original.

    `install` redirects both the discovery fetch and PyJWT's key lookup, so the
    verification under test is the real one and nothing reaches the network.
    """
    return FakeIdp(issuer=ISSUER, client_id=CLIENT_ID).install(monkeypatch)


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


def _signed_in_through_the_provider(client, session_factory, idp, *, sid="session-1"):
    """Sign in for real, so the session carries whatever the callback stored.

    Built by driving the callback rather than by hand: what is under test is
    that the `sid` from the ID token reaches the session row, and a fixture
    that set it directly would pass with that wiring removed.
    """
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
    # Both claims together. `test_oidc_routes._callback` ASSIGNS `idp.claims`
    # rather than merging, so setting `sid` before calling it is silently
    # discarded — and nothing fails except the one assertion about `oidc_sid`.
    idp.claims = {"nonce": _nonce_for(client, idp), "sid": sid}
    resp = client.get(f"/login/oidc/callback?code=abc&state={state}",
                      follow_redirects=False)
    assert resp.headers["location"] == "/", resp.text
    return resp


def _sessions(session_factory):
    with session_factory() as db:
        return db.scalars(select(UserSession)).all()


# --------------------------------------------------------------------------- #
# Capturing the session id at sign-in
# --------------------------------------------------------------------------- #

def test_the_providers_session_id_is_stored(client, session_factory, oidc_on, idp):
    """Without this nothing else here can work, and nothing else would fail:
    the endpoint would simply never match a session."""
    _signed_in_through_the_provider(client, session_factory, idp, sid="session-7")

    with session_factory() as db:
        row = db.scalars(select(UserSession)).one()
        assert row.oidc_sid == "session-7"
        assert row.via_oidc is True


def test_a_provider_that_sends_no_session_id_still_signs_in(
    client, session_factory, oidc_on, idp
):
    """`sid` is optional in the spec. Its absence must not break sign-in —
    a `sub`-keyed logout still reaches that session."""
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

    idp.claims = {"nonce": _nonce_for(client, idp)}
    resp = client.get(f"/login/oidc/callback?code=abc&state={state}",
                      follow_redirects=False)

    assert resp.headers["location"] == "/"
    with session_factory() as db:
        assert db.scalars(select(UserSession)).one().oidc_sid is None


# --------------------------------------------------------------------------- #
# What a notice ends
# --------------------------------------------------------------------------- #

def test_a_session_keyed_logout_ends_that_session(
    client, session_factory, oidc_on, idp
):
    _signed_in_through_the_provider(client, session_factory, idp, sid="session-1")
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 200

    resp = client.post(ENDPOINT,
                       data={"logout_token": idp.logout_token(sid="session-1")})

    assert resp.status_code == 200
    assert _sessions(session_factory) == []
    assert client.get("/", headers=HTML,
                      follow_redirects=False).status_code == 303


def test_a_logout_for_another_session_leaves_this_one_alone(
    client, session_factory, oidc_on, idp
):
    """The reason `sid` is preferred over `sub`: one browser signing out at the
    provider must not sign the person out of every other one."""
    _signed_in_through_the_provider(client, session_factory, idp, sid="session-1")

    resp = client.post(ENDPOINT,
                       data={"logout_token": idp.logout_token(sid="somebody-else")})

    assert resp.status_code == 200
    assert len(_sessions(session_factory)) == 1
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 200


def test_a_subject_keyed_logout_ends_every_session_for_that_identity(
    client, session_factory, oidc_on, idp
):
    """Blunter, and all that can be done with a token carrying only `sub`."""
    _signed_in_through_the_provider(client, session_factory, idp, sid="session-1")

    resp = client.post(
        ENDPOINT, data={"logout_token": idp.logout_token(sid=None,
                                                         sub="idp-subject-1")})

    assert resp.status_code == 200
    assert _sessions(session_factory) == []


def test_a_subject_nobody_here_is_linked_to_ends_nothing(
    client, session_factory, oidc_on, idp
):
    _signed_in_through_the_provider(client, session_factory, idp, sid="session-1")

    resp = client.post(
        ENDPOINT, data={"logout_token": idp.logout_token(sid=None,
                                                         sub="a-stranger")})

    assert resp.status_code == 200
    assert len(_sessions(session_factory)) == 1


def test_a_subject_from_another_issuer_does_not_match(
    client, session_factory, oidc_on, idp, app_module
):
    """Matched on `(issuer, subject)`, never on subject alone — the rule
    `resolve()` follows, for the same reason.

    Subjects are only unique WITHIN a provider: `1`, `admin` and a bare uuid
    are all plausible in two directories at once. An installation that changed
    provider keeps the old `external_identity` rows, so a subject collision
    would end the wrong person's sessions. Nothing proved this until the
    mutation dropping the issuer clause passed.
    """
    _signed_in_through_the_provider(client, session_factory, idp, sid="session-1")
    with session_factory() as db:
        stranger = fac.make_user(db, "elsewhere@example.test")
        db.flush()
        db.add(ExternalIdentity(user_id=stranger.id,
                                issuer="https://old-idp.example.test",
                                subject="idp-subject-1"))
        db.flush()
        # A PROVIDER session of their own, which is the thing that would be
        # wrongly ended. Without one there is nothing for the bug to destroy,
        # and the first version of this test passed with the issuer clause
        # removed for exactly that reason.
        auth_mod.create_session(db, stranger, app_module.settings)
        db.scalars(select(UserSession).where(
            UserSession.user_id == stranger.id)).one().via_oidc = True
        db.commit()
        stranger_id = stranger.id

    # Signed by OUR provider, so `notice.issuer` is ours. The stranger holds
    # the same subject under a DIFFERENT issuer and must be untouched.
    resp = client.post(
        ENDPOINT, data={"logout_token": idp.logout_token(sid=None,
                                                         sub="idp-subject-1")})

    assert resp.status_code == 200
    with session_factory() as db:
        survivors = db.scalars(select(UserSession).where(
            UserSession.user_id == stranger_id)).all()
        assert len(survivors) == 1


def test_only_a_provider_session_ever_carries_a_sid(client, session_factory,
                                                    oidc_on, idp, app_module):
    """The invariant that lets the `sid` lookup skip a `via_oidc` filter.

    `oidc_sid` is written in exactly one place and always with `via_oidc`, so
    a local session holds NULL and cannot match a sid. If that ever stops being
    true, the sid path needs the filter back — and this fails first.
    """
    _signed_in_through_the_provider(client, session_factory, idp, sid="session-1")
    # A second session made the ordinary way, so there is one of each to
    # compare. Built through `create_session` rather than a second login: what
    # is being pinned is what that function writes, not what a login route does.
    with session_factory() as db:
        local = fac.make_user(db, "local@example.test")
        db.flush()
        auth_mod.create_session(db, local, app_module.settings)
        db.commit()

    with session_factory() as db:
        rows = db.scalars(select(UserSession)).all()
        assert len(rows) == 2
        for row in rows:
            assert (row.oidc_sid is None) is (row.via_oidc is False)


# --------------------------------------------------------------------------- #
# What it must NOT do
# --------------------------------------------------------------------------- #

def test_the_account_is_not_deactivated(client, session_factory, oidc_on, idp):
    """The line this feature must not cross.

    Letting a provider disable an account is a much larger authority than
    ending a session, and it would break the promise that local accounts keep
    working when the provider is down. So the person signs straight back in
    locally — which is the intended behaviour, not a gap.
    """
    _signed_in_through_the_provider(client, session_factory, idp, sid="session-1")

    client.post(ENDPOINT, data={"logout_token": idp.logout_token(sid="session-1")})

    with session_factory() as db:
        assert db.scalar(select(User)).is_active is True


def test_a_local_session_is_never_touched(client, session_factory, oidc_on, idp):
    """A password sign-in has no provider session, so nothing the provider says
    about one can reach it."""
    make_login(client, session_factory)
    with session_factory() as db:
        user = db.scalar(select(User))
        federation.link(db, user, oidc.Identity(
            subject="idp-subject-1", issuer=ISSUER, email="user@example.test",
            email_verified=True, name="User"))
        db.commit()

    resp = client.post(
        ENDPOINT, data={"logout_token": idp.logout_token(sid=None,
                                                         sub="idp-subject-1")})

    assert resp.status_code == 200
    # Still signed in: the session did not begin at the provider.
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 200


# --------------------------------------------------------------------------- #
# What it refuses
# --------------------------------------------------------------------------- #

def test_an_unverifiable_token_is_refused(client, session_factory, oidc_on, idp):
    _signed_in_through_the_provider(client, session_factory, idp, sid="session-1")

    resp = client.post(ENDPOINT, data={"logout_token": "not-a-token"})

    assert resp.status_code == 400
    assert len(_sessions(session_factory)) == 1


def test_an_id_token_is_not_a_logout_instruction(
    client, session_factory, oidc_on, idp
):
    """appcore refuses it; this is the route proving it does not swallow the
    refusal and end the session anyway."""
    _signed_in_through_the_provider(client, session_factory, idp, sid="session-1")

    resp = client.post(ENDPOINT,
                       data={"logout_token": idp.id_token(sid="session-1")})

    assert resp.status_code == 400
    assert len(_sessions(session_factory)) == 1


def test_a_missing_token_is_refused(client, oidc_on, idp):
    assert client.post(ENDPOINT, data={}).status_code == 400


def test_the_endpoint_is_closed_when_oidc_is_off(client, session_factory, idp):
    """No provider configured means nothing may claim to be one. 404 rather
    than 400: there is no such endpoint on this installation."""
    make_login(client, session_factory)

    resp = client.post(ENDPOINT,
                       data={"logout_token": idp.logout_token(sid="session-1")})

    assert resp.status_code == 404


def test_it_needs_no_cookie_or_csrf_token(client, session_factory, oidc_on, idp):
    """The point of the endpoint. A provider has neither, and the signature is
    what stands in for both — so this must work from a client with no session
    at all."""
    _signed_in_through_the_provider(client, session_factory, idp, sid="session-1")
    token = idp.logout_token(sid="session-1")
    client.cookies.clear()

    resp = client.post(ENDPOINT, data={"logout_token": token})

    assert resp.status_code == 200
    assert _sessions(session_factory) == []
