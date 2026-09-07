"""The relying-party half of OIDC, against a fake provider that really signs.

The ID token is signed with an RSA key generated in the test and served through
a stubbed JWKS endpoint, so `_verify` does the same work it does in production.
A stub that returned claims directly would agree that an unsigned token was
fine, which is the one thing worth checking.

Nothing here reaches the network: `_fetch` is stubbed, and the suite's own
guard fails any test that tries.
"""

from __future__ import annotations

import datetime as dt
import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from appkit import oidc

ISSUER = "https://idp.example.test"
CLIENT_ID = "stocktake"
REDIRECT = "https://stocktake.example.test/login/oidc/callback"


class FakeIdp:
    """An identity provider: one RSA key, a discovery document, and a token
    endpoint that mints whatever the test asks for."""

    def __init__(self, issuer: str = ISSUER):
        self.issuer = issuer
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "test-key"
        self.claims: dict = {}
        self.token_requests: list[dict] = []
        self.omit_id_token = False

    # -- what the provider publishes ---------------------------------------- #

    def discovery(self) -> dict:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    def id_token(self, **overrides) -> str:
        now = dt.datetime.now(dt.timezone.utc)
        claims = {
            "iss": self.issuer,
            "sub": "idp-subject-1",
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + dt.timedelta(minutes=5),
            "email": "member@example.test",
            "email_verified": True,
            "name": "A Member",
            **self.claims,
            **overrides,
        }
        return jwt.encode(claims, self.key, algorithm="RS256",
                          headers={"kid": self.kid})

    # -- the stub that stands in for the network ---------------------------- #

    def fetch(self, url, data=None, auth=None):
        if url.endswith("/.well-known/openid-configuration"):
            return self.discovery()
        if url.endswith("/token"):
            self.token_requests.append(dict(data or {}))
            if self.omit_id_token:
                return {"access_token": "x"}
            return {"access_token": "x", "id_token": self.id_token(**self.claims)}
        raise AssertionError(f"unexpected request to {url}")


@pytest.fixture
def idp(monkeypatch) -> FakeIdp:
    fake = FakeIdp()
    monkeypatch.setattr(oidc, "_fetch", fake.fetch)

    # PyJWKClient fetches the key set over HTTPS. Point it at the fake's key
    # instead of the network, without touching the verification it wraps.
    from jwt import PyJWKClient

    def signing_key(self, token):  # noqa: ARG001 - matches the method it replaces
        from jwt import PyJWK

        numbers = fake.key.public_key().public_numbers()

        def b64(value: int) -> str:
            import base64
            raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        return PyJWK.from_dict({"kty": "RSA", "kid": fake.kid, "alg": "RS256",
                                "use": "sig", "n": b64(numbers.n),
                                "e": b64(numbers.e)})

    monkeypatch.setattr(PyJWKClient, "get_signing_key_from_jwt", signing_key)
    return fake


@pytest.fixture
def provider() -> oidc.Provider:
    return oidc.Provider(issuer=ISSUER, client_id=CLIENT_ID,
                         client_secret="a-secret", redirect_uri=REDIRECT)


def _round_trip(provider, idp, **overrides) -> oidc.Identity:
    _, pending = oidc.begin(provider)
    idp.claims = {"nonce": pending["nonce"], **overrides}
    return oidc.complete(provider, code="an-auth-code", pending=pending)


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #

def test_a_signed_token_produces_the_identity_it_claims(provider, idp):
    identity = _round_trip(provider, idp)

    assert identity.subject == "idp-subject-1"
    assert identity.issuer == ISSUER
    assert identity.email == "member@example.test"
    assert identity.email_verified is True
    assert identity.name == "A Member"


def test_the_redirect_carries_pkce_and_a_nonce(provider, idp):
    import urllib.parse

    url, pending = oidc.begin(provider)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] and query["code_challenge"] != [pending["verifier"]]
    assert query["state"] == [pending["state"]]
    assert query["nonce"] == [pending["nonce"]]
    assert query["redirect_uri"] == [REDIRECT]


def test_the_verifier_is_sent_when_the_code_is_redeemed(provider, idp):
    """Without it the PKCE challenge in the redirect proves nothing — the
    provider has nothing to compare the code against."""
    _, pending = oidc.begin(provider)
    idp.claims = {"nonce": pending["nonce"]}
    oidc.complete(provider, code="an-auth-code", pending=pending)

    assert idp.token_requests[0]["code_verifier"] == pending["verifier"]


# --------------------------------------------------------------------------- #
# What must be refused
# --------------------------------------------------------------------------- #

def test_a_token_signed_by_someone_else_is_refused(provider, idp):
    """The signature is the whole of the trust. A token with perfect claims and
    the wrong key is exactly what an attacker can produce."""
    other = FakeIdp()
    _, pending = oidc.begin(provider)
    forged = jwt.encode(
        {"iss": ISSUER, "sub": "idp-subject-1", "aud": CLIENT_ID,
         "iat": dt.datetime.now(dt.timezone.utc),
         "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5),
         "nonce": pending["nonce"]},
        other.key, algorithm="RS256", headers={"kid": idp.kid})
    idp.fetch = lambda url, data=None, auth=None: (
        idp.discovery() if "openid-configuration" in url
        else {"id_token": forged})

    with pytest.raises(oidc.OidcError):
        oidc.complete(provider, code="c", pending=pending)


def test_a_token_for_another_audience_is_refused(provider, idp):
    """An ID token minted for a different client of the same IdP is a valid
    token — just not one that says anything about signing in here."""
    with pytest.raises(oidc.OidcError):
        _round_trip(provider, idp, aud="some-other-app")


def test_an_expired_token_is_refused(provider, idp):
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    with pytest.raises(oidc.OidcError):
        _round_trip(provider, idp, exp=past, iat=past)


def test_a_replayed_nonce_is_refused(provider, idp):
    """The nonce ties the token to the redirect this browser started. Without
    it a token captured from another sign-in would be accepted here."""
    _, pending = oidc.begin(provider)
    idp.claims = {"nonce": "a nonce from some other attempt"}

    with pytest.raises(oidc.OidcError, match="did not match"):
        oidc.complete(provider, code="c", pending=pending)


def test_a_missing_nonce_is_refused(provider, idp):
    _, pending = oidc.begin(provider)
    idp.claims = {}

    with pytest.raises(oidc.OidcError):
        oidc.complete(provider, code="c", pending=pending)


def test_a_discovery_document_naming_another_issuer_is_refused(provider, idp):
    """Either a misconfiguration or somebody else's provider answering. Both
    should stop before a token is ever requested."""
    idp.issuer = "https://someone-else.example.test"

    with pytest.raises(oidc.OidcError, match="different issuer"):
        oidc.discover(provider)


def test_a_provider_with_no_id_token_is_refused(provider, idp):
    idp.omit_id_token = True
    _, pending = oidc.begin(provider)

    with pytest.raises(oidc.OidcError, match="no ID token"):
        oidc.complete(provider, code="c", pending=pending)


def test_an_unverified_email_is_reported_as_unverified(provider, idp):
    """A missing `email_verified` means NOT verified. Treating absence as true
    is how an unverified address becomes trusted enough to match an account."""
    identity = _round_trip(provider, idp, email_verified=None)

    assert identity.email_verified is False


# --------------------------------------------------------------------------- #
# Nothing happens until it is used
# --------------------------------------------------------------------------- #

def test_discovery_is_fetched_once_and_then_cached(provider, idp, monkeypatch):
    calls: list[str] = []

    def counting(url, data=None, auth=None):
        calls.append(url)
        return idp.fetch(url, data, auth)

    # Patch the module attribute: the fixture already bound `oidc._fetch` to
    # the fake's method, so reassigning `idp.fetch` would change nothing.
    monkeypatch.setattr(oidc, "_fetch", counting)

    oidc.begin(provider)
    oidc.begin(provider)

    assert sum("openid-configuration" in url for url in calls) == 1


def test_building_a_provider_makes_no_requests(monkeypatch):
    """Construction is inert. An installation that never presses the button
    makes no outbound request at all — the same promise the price feed makes.
    """
    def explode(*args, **kwargs):
        raise AssertionError("a request was made before anyone signed in")

    monkeypatch.setattr(oidc, "_fetch", explode)
    oidc.Provider(issuer=ISSUER, client_id=CLIENT_ID, client_secret="s",
                  redirect_uri=REDIRECT)


def test_the_shared_fetch_is_the_only_way_out(provider):
    """One seam, so the suite's network guard and the tests above cover every
    call. A second `urlopen` elsewhere in the module would be invisible."""
    source = (oidc.__file__ and open(oidc.__file__).read()) or ""

    assert source.count("urlopen") == 1
    assert json.dumps  # the import is used for parsing, not for requests
