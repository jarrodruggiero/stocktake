"""Passkeys and security keys: the two WebAuthn ceremonies.

Separate from auth.py for the same reason twofactor.py is — none of this knows
about sessions or requests. It turns an authenticator's answer into either a
stored credential or a verified user id, and refuses everything else.

Three things this file exists to get right:

* **`rp_id` comes from config, never from a request.** Host and
  X-Forwarded-Host are client-controlled, and the rp_id is the anchor every
  credential is permanently bound to — decisions.md #96.
* **A challenge is single-use.** It is deleted as it is read, so a replayed
  ceremony finds nothing to verify against rather than a still-valid nonce.
* **Signing in is discoverable.** The authenticator names the account, so no
  email is asked for and none is confirmed to exist.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from appcore import ensure_utc

from .models import User, WebauthnChallenge, WebauthnCredential
from .settings import PortfolioSettings

# How long a browser has to finish a ceremony. The prompt itself times out at
# 60s; this is the outer bound on the row, not the user's patience.
CHALLENGE_TTL = dt.timedelta(minutes=5)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _webauthn():
    """Imported on use, like pyotp — see app/memory.py for the rationale."""
    import webauthn

    return webauthn


class PasskeyError(Exception):
    """A ceremony that did not verify. The message is safe to show."""


# --------------------------------------------------------------------------- #
# Whether this deployment can offer them at all
# --------------------------------------------------------------------------- #

def configured(settings: PortfolioSettings) -> bool:
    """Enabled, with the two things a credential cannot be created without.

    Deliberately not "enabled", singular: a true `enabled` with no rp_id would
    offer a button that fails inside the browser, where the reason is invisible.
    """
    wa = settings.auth.webauthn
    return bool(wa.enabled and wa.rp_id and wa.origins)


def unavailable_reason(settings: PortfolioSettings, *, secure: bool) -> str | None:
    """Why passkeys cannot be offered here, in a sentence, or None.

    Said out loud rather than hiding the control, because every reason is
    something the person reading it can fix, and a missing button teaches
    nobody anything.
    """
    wa = settings.auth.webauthn
    if not wa.enabled:
        return "Turn on passkeys in Admin → Settings."
    if not wa.rp_id or not wa.origins:
        return "Set the passkey domain in Admin → Settings."
    if not secure:
        return "Requires Stocktake to be available via HTTPS."
    return None


# --------------------------------------------------------------------------- #
# Challenges
# --------------------------------------------------------------------------- #

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_challenge(db: DbSession, challenge: bytes, user: User | None) -> str:
    """Remember a challenge, returning the token the browser carries back."""
    raw = secrets.token_urlsafe(32)
    db.add(WebauthnChallenge(
        token_hash=_hash_token(raw),
        challenge=_b64(challenge),
        user_id=user.id if user else None,
        expires_at=_utcnow() + CHALLENGE_TTL,
    ))
    db.flush()
    return raw


def take_challenge(db: DbSession, raw: str | None) -> tuple[bytes, int | None]:
    """The challenge behind this token, consumed as it is read.

    Deleting on read is what makes a ceremony single-use: replaying a captured
    response finds no row, rather than a nonce that is still good.
    """
    if not raw:
        raise PasskeyError("This sign-in attempt has expired. Try again.")
    row = db.scalar(
        select(WebauthnChallenge).where(
            WebauthnChallenge.token_hash == _hash_token(raw))
    )
    if row is None or ensure_utc(row.expires_at) < _utcnow():
        if row is not None:
            db.delete(row)
            db.flush()
        raise PasskeyError("This sign-in attempt has expired. Try again.")
    challenge, user_id = _unb64(row.challenge), row.user_id
    db.delete(row)
    db.flush()
    return challenge, user_id


def purge_expired_challenges(db: DbSession) -> int:
    """Half-finished ceremonies nobody came back to. Runs with maintenance."""
    result = db.execute(
        delete(WebauthnChallenge).where(WebauthnChallenge.expires_at < _utcnow()))
    return result.rowcount or 0


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


# --------------------------------------------------------------------------- #
# Enrolment
# --------------------------------------------------------------------------- #

def registration_options(
    db: DbSession, user: User, settings: PortfolioSettings
) -> tuple[str, str]:
    """Options for `navigator.credentials.create`, and the challenge token.

    Existing credentials are excluded so a key already enrolled says so in the
    browser's own words instead of registering itself twice.
    """
    wa = settings.auth.webauthn
    w = _webauthn()
    from webauthn.helpers.structs import (  # noqa: PLC0415 - optional dependency
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    options = w.generate_registration_options(
        rp_id=wa.rp_id,
        rp_name=wa.rp_name or wa.rp_id,
        # `public_id` rather than the row id: it is what the account is known
        # by outside the database, and it is what a credential binds to.
        user_id=str(user.public_id).encode(),
        user_name=user.email,
        user_display_name=user.name or user.email,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=_unb64(c.credential_id))
            for c in _credentials(db, user)
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            # Discoverable, so signing in needs no email typed first.
            resident_key=ResidentKeyRequirement.PREFERRED,
            # Enrolled under the same requirement sign-in enforces, so a key
            # cannot be registered here and then refused every time it is used.
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    return w.options_to_json(options), issue_challenge(db, options.challenge, user)


def verify_registration(
    db: DbSession, user: User, credential: str, token: str | None,
    settings: PortfolioSettings, *, name: str = "",
) -> WebauthnCredential:
    """Store the credential the browser just created, or raise."""
    wa = settings.auth.webauthn
    challenge, owner = take_challenge(db, token)
    if owner != user.id:
        raise PasskeyError("That request belongs to a different account.")
    try:
        verified = _webauthn().verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=wa.rp_id,
            expected_origin=list(wa.origins),
        )
    except Exception as exc:  # noqa: BLE001 - the library raises many types
        raise PasskeyError(f"That passkey could not be registered: {exc}") from exc

    row = WebauthnCredential(
        user_id=user.id,
        credential_id=_b64(verified.credential_id),
        public_key=_b64(verified.credential_public_key),
        sign_count=verified.sign_count,
        name=(name or "").strip()[:80] or "Passkey",
        rp_id=wa.rp_id,
    )
    db.add(row)
    db.flush()
    return row


# --------------------------------------------------------------------------- #
# Signing in
# --------------------------------------------------------------------------- #

def authentication_options(
    db: DbSession, settings: PortfolioSettings
) -> tuple[str, str]:
    """Options for `navigator.credentials.get`, and the challenge token.

    No `allow_credentials`: the ceremony is discoverable, and naming the
    account's keys would mean asking who is signing in before they have proved
    anything — which answers "does this email have an account" to anyone who
    asks.
    """
    w = _webauthn()
    from webauthn.helpers.structs import (  # noqa: PLC0415 - optional dependency
        UserVerificationRequirement,
    )

    # REQUIRED is what lets a passkey stand in for a password AND a code: it
    # makes the authenticator prove a biometric or PIN, so the assertion is
    # two factors rather than mere possession. decisions.md #113.
    options = w.generate_authentication_options(
        rp_id=settings.auth.webauthn.rp_id,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return w.options_to_json(options), issue_challenge(db, options.challenge, None)


def verify_authentication(
    db: DbSession, credential: str, token: str | None, settings: PortfolioSettings
) -> User:
    """The user this assertion proves, or raise.

    The sign counter is the replay defence: an authenticator that implements it
    must never send one that has gone backwards. Most passkeys always send 0,
    which means "no counter" rather than "replayed" — so 0 is not compared.
    """
    wa = settings.auth.webauthn
    challenge, _ = take_challenge(db, token)
    import json  # noqa: PLC0415 - only this path needs it

    try:
        raw_id = json.loads(credential)["rawId"]
    except (ValueError, KeyError, TypeError) as exc:
        raise PasskeyError("That passkey could not be read.") from exc

    row = db.scalar(
        select(WebauthnCredential).where(WebauthnCredential.credential_id == raw_id))
    if row is None:
        raise PasskeyError("That passkey is not registered here.")

    try:
        verified = _webauthn().verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=wa.rp_id,
            expected_origin=list(wa.origins),
            credential_public_key=_unb64(row.public_key),
            credential_current_sign_count=row.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001 - the library raises many types
        raise PasskeyError("That passkey could not be verified.") from exc

    if verified.new_sign_count:
        row.sign_count = verified.new_sign_count
    row.last_used_at = _utcnow()
    db.flush()
    user = db.get(User, row.user_id)
    if user is None:
        raise PasskeyError("That passkey is not registered here.")
    return user


# --------------------------------------------------------------------------- #
# The list on the account page
# --------------------------------------------------------------------------- #

def _credentials(db: DbSession, user: User) -> list[WebauthnCredential]:
    return list(db.scalars(
        select(WebauthnCredential)
        .where(WebauthnCredential.user_id == user.id)
        .order_by(WebauthnCredential.created_at)
    ))


def for_user(db: DbSession, user: User) -> list[WebauthnCredential]:
    return _credentials(db, user)


def count_for(db: DbSession, user: User) -> int:
    return len(_credentials(db, user))


def remove(db: DbSession, user: User, credential_id: int) -> bool:
    """Delete one of this user's passkeys. Scoped to the user on purpose —
    an id from a form must never be able to reach somebody else's row."""
    row = db.scalar(
        select(WebauthnCredential).where(
            WebauthnCredential.id == credential_id,
            WebauthnCredential.user_id == user.id,
        )
    )
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True
