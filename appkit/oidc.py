"""OpenID Connect, as a relying party: hand sign-in to somebody else's IdP.

Here rather than in `app/` because none of it is about portfolios — it turns an
authorisation code into a verified identity and knows nothing about what the
application then does with it. Storage, account linking and provisioning are
the application's business and are deliberately absent.

**Nothing here runs unless it is configured.** No discovery on import, no
background refresh: the first outbound request happens when somebody presses
the sign-in button. An installation that leaves this off makes no requests at
all, which is the same promise the price feed makes.

Three things this file exists to get right:

* **The ID token is verified, not decoded.** Signature against the issuer's
  published keys, then issuer, audience, expiry and nonce. A decoded-but-
  unverified token is a value the caller chose.
* **`state` and `nonce` are the caller's to store.** They are returned for the
  application to keep somewhere single-use; this module only checks what comes
  back against what it is given.
* **PKCE always.** The spec makes it optional for confidential clients. It
  costs one hash and removes a whole class of code-interception bug. It is
  also what carries a public client, where `client_auth` is `none` and the
  verifier is the only thing proving the redemption.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# The same shape as providers.TIMEOUT_SECONDS: an IdP that has gone away must
# not hold a web worker open.
TIMEOUT_SECONDS = 10
USER_AGENT = "stocktake"


class OidcError(Exception):
    """A sign-in that could not be completed. The message is safe to show."""


@dataclass
class Provider:
    """A configured identity provider, and what discovery told us about it."""

    issuer: str
    client_id: str
    client_secret: str | None
    redirect_uri: str
    scopes: tuple[str, ...] = ("openid", "email", "profile")
    # `basic` sends the secret as client_secret_basic; `none` is a public
    # client. Explicit rather than inferred from a missing secret, so a secret
    # dropped from the environment fails instead of downgrading — decisions.md #121.
    client_auth: str = "basic"
    # Filled by `discover()`. Cached on the instance rather than globally so a
    # settings change takes effect without a process restart.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Identity:
    """Who the provider says this is. Claims beyond these stay unread — a
    relying party that maps groups to roles is a bigger promise than
    authenticating somebody."""

    subject: str
    issuer: str
    email: str | None
    email_verified: bool
    name: str | None


# --------------------------------------------------------------------------- #
# Talking to the provider
# --------------------------------------------------------------------------- #

def _fetch(url: str, data: dict[str, str] | None = None,
           auth: tuple[str, str] | None = None) -> dict:
    """One request to the provider. Separate so tests can stub exactly this.

    A test that reaches a real IdP would pass or fail on somebody else's
    uptime, so the suite blocks this the way it blocks the price feed.
    """
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if auth is not None:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # The body carries the provider's own `error_description`, which says
        # far more than "HTTP 400" — a wrong client secret reads as exactly
        # that, and guessing is what makes this hard to set up.
        detail = ""
        try:
            detail = json.loads(exc.read().decode()).get("error_description", "")
        except Exception:  # noqa: BLE001 - the body may be anything at all
            pass
        raise OidcError(f"The identity provider refused: {detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise OidcError(f"Could not reach the identity provider: {exc}") from exc


def discover(provider: Provider) -> dict:
    """The provider's published configuration, fetched once per Provider.

    The issuer in the document must match the one configured. A document that
    names a different issuer is either a misconfiguration or somebody else's
    provider answering, and both should stop here.
    """
    if provider.metadata:
        return provider.metadata
    url = provider.issuer.rstrip("/") + "/.well-known/openid-configuration"
    document = _fetch(url)
    if document.get("issuer", "").rstrip("/") != provider.issuer.rstrip("/"):
        raise OidcError(
            "The provider's configuration names a different issuer "
            f"({document.get('issuer')!r}) than the one configured.")
    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not document.get(required):
            raise OidcError(f"The provider did not publish a {required}.")
    provider.metadata = document
    return document


# --------------------------------------------------------------------------- #
# The redirect out
# --------------------------------------------------------------------------- #

def _verifier() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def begin(provider: Provider) -> tuple[str, dict[str, str]]:
    """Where to send the browser, and what the caller must remember.

    The second value is opaque to the caller beyond needing to survive until
    the callback: `state` defends the callback against forgery, `nonce` ties
    the ID token to this attempt, and the PKCE verifier proves the code is
    being redeemed by whoever asked for it.
    """
    document = discover(provider)
    verifier, challenge = _verifier()
    pending = {
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "verifier": verifier,
    }
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": provider.redirect_uri,
        "scope": " ".join(provider.scopes),
        "state": pending["state"],
        "nonce": pending["nonce"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{document['authorization_endpoint']}?{query}", pending


# --------------------------------------------------------------------------- #
# The callback
# --------------------------------------------------------------------------- #

def _basic_auth(provider: Provider) -> tuple[str, str] | None:
    """The token request's credentials, or None for a public client.

    Only the exact value `none` drops them: an unrecognised setting has to
    authenticate, or a typo would be a silent downgrade — decisions.md #121.
    """
    if provider.client_auth == "none":
        return None
    if not provider.client_secret:
        raise OidcError(
            "This client is set to authenticate with a secret but none is "
            "configured. Set auth.oidc.client_secret, or set "
            "auth.oidc.client_auth to 'none' if the provider issued a public "
            "client.")
    return provider.client_id, provider.client_secret


def complete(provider: Provider, *, code: str, pending: dict[str, str]) -> Identity:
    """Redeem the code and return the identity the ID token proves.

    `pending` is what `begin` handed out. The caller is responsible for having
    looked it up by the `state` the browser came back with, and for making that
    lookup single-use — a state that can be spent twice is not a defence.
    """
    document = discover(provider)
    tokens = _fetch(
        document["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": provider.redirect_uri,
            "client_id": provider.client_id,
            "code_verifier": pending["verifier"],
        },
        auth=_basic_auth(provider),
    )
    raw = tokens.get("id_token")
    if not raw:
        raise OidcError("The provider returned no ID token.")
    return _verify(provider, raw, nonce=pending["nonce"])


def _verify(provider: Provider, raw: str, *, nonce: str) -> Identity:
    """Signature, issuer, audience, expiry, nonce — in that order, all of them.

    Decoding without verifying would make every claim below a value the caller
    chose, which is the difference between "signed in" and "said so".
    """
    import jwt  # noqa: PLC0415 - imported on use, like the other optional deps
    from jwt import PyJWKClient  # noqa: PLC0415

    document = discover(provider)
    try:
        key = PyJWKClient(document["jwks_uri"]).get_signing_key_from_jwt(raw)
        claims = jwt.decode(
            raw,
            key.key,
            algorithms=document.get("id_token_signing_alg_values_supported")
            or ["RS256"],
            audience=provider.client_id,
            issuer=provider.issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except Exception as exc:  # noqa: BLE001 - PyJWT raises a family of these
        raise OidcError(f"That sign-in could not be verified: {exc}") from exc

    # Checked here rather than left to the library: `nonce` is what ties this
    # token to the redirect this browser started, and PyJWT does not know it.
    if claims.get("nonce") != nonce:
        raise OidcError("That sign-in did not match the request that started it.")

    return Identity(
        subject=str(claims["sub"]),
        issuer=str(claims["iss"]),
        email=claims.get("email"),
        # Absent means NOT verified. Treating a missing claim as true is how an
        # unverified address ends up trusted enough to match an account.
        email_verified=bool(claims.get("email_verified")),
        name=claims.get("name") or claims.get("preferred_username"),
    )
