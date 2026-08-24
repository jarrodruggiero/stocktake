"""The two WebAuthn ceremonies, driven by a real software authenticator.

`SoftWebauthnDevice` performs the same signing a phone or a YubiKey does, so
these exercise the actual cryptography rather than a mock of it — a stub would
happily agree that an unsigned response was valid, which is the one thing worth
checking here.

The base64url handling is deliberately not shared with `app/passkeys.py`: these
convert with the library's own helpers, so a mistake in ours shows up as a
disagreement rather than being applied to both sides.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives.asymmetric import ec as _ec
from soft_webauthn import SoftWebauthnDevice
from sqlalchemy import select
from webauthn.helpers import base64url_to_bytes

from app import passkeys
from app.models import User, WebauthnChallenge, WebauthnCredential
from app.settings import PortfolioSettings


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


RP_ID = "stocktake.example.test"
ORIGIN = f"https://{RP_ID}"


@pytest.fixture
def settings() -> PortfolioSettings:
    s = PortfolioSettings()
    s.auth.webauthn.enabled = True
    s.auth.webauthn.rp_id = RP_ID
    s.auth.webauthn.rp_name = "Stocktake"
    s.auth.webauthn.origins = [ORIGIN]
    return s


@pytest.fixture
def user(session_factory) -> User:
    with session_factory() as db:
        u = User(email="owner@example.test", name="Owner", password_hash="x")
        db.add(u)
        db.commit()
        return u


class VerifyingDevice(SoftWebauthnDevice):
    """A device that performs user verification, as a real passkey does.

    `SoftWebauthnDevice` hardcodes the authenticator-data flags to `0x01` —
    User Present only, never User Verified. A phone or a security key asked for
    a biometric sets `0x04` as well, and the app requires it: that bit is what
    makes an assertion two factors rather than mere possession, which is why a
    passkey may sign in without a password or a code.

    The flag has to be set BEFORE signing, since the signature covers the
    authenticator data — so this re-signs rather than editing the result.
    """

    UP_AND_UV = b"\x05"

    def get(self, options, origin):
        assertion = super().get(options, origin)
        response = assertion["response"]
        data = bytearray(response["authenticatorData"])
        data[32:33] = self.UP_AND_UV
        response["authenticatorData"] = bytes(data)
        response["signature"] = self.private_key.sign(
            bytes(data) + _sha256(response["clientDataJSON"]),
            _ec.ECDSA(_hashes.SHA256()),
        )
        return assertion


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _for_device(raw_options: str) -> dict:
    """The options as a browser hands them to an authenticator.

    `options_to_json` base64url-encodes every buffer for the wire; the device
    wants the bytes back. Decoding here rather than in the app is deliberate —
    it is the browser's job, and doing it with the library's own helper keeps
    our encoding honest.
    """
    options = json.loads(raw_options)
    options["challenge"] = base64url_to_bytes(options["challenge"])
    if "user" in options:
        options["user"]["id"] = base64url_to_bytes(options["user"]["id"])
    for key in ("excludeCredentials", "allowCredentials"):
        for descriptor in options.get(key) or []:
            descriptor["id"] = base64url_to_bytes(descriptor["id"])
    return {"publicKey": options}


def _as_json(credential: dict) -> str:
    """What the browser posts: buffers base64url'd, `id` left as it is.

    `id` is ALREADY the base64url text of `rawId` — the device returns it as
    ASCII bytes. Encoding it like a buffer double-encodes it, and the library
    rejects the result with "id and raw_id were not equivalent".

    The padding is stripped because this device keeps the `=` and a browser
    does not. That is a fidelity gap in the double, not a case the app has to
    handle.
    """
    def enc(value):
        if isinstance(value, bytes):
            return _b64(value)
        if isinstance(value, dict):
            return {k: (v.decode().rstrip("=") if k == "id" and isinstance(v, bytes)
                        else enc(v))
                    for k, v in value.items()}
        return value

    return json.dumps(enc(credential))


def _enrol(db, user, settings, device=None):
    """Run a full registration and return the device that now holds the key."""
    device = device or VerifyingDevice()
    raw_options, token = passkeys.registration_options(db, user, settings)
    attestation = device.create(_for_device(raw_options), ORIGIN)
    passkeys.verify_registration(
        db, user, _as_json(attestation), token, settings, name="Test key")
    return device


def _assert(db, device, settings):
    raw_options, token = passkeys.authentication_options(db, settings)
    assertion = device.get(_for_device(raw_options), ORIGIN)
    return _as_json(assertion), token


# --------------------------------------------------------------------------- #
# Enrolment
# --------------------------------------------------------------------------- #

def test_a_real_authenticator_enrols(session_factory, user, settings):
    with session_factory() as db:
        u = db.get(User, user.id)
        _enrol(db, u, settings)
        row = db.scalar(select(WebauthnCredential))

    assert row.user_id == user.id
    assert row.rp_id == RP_ID
    assert row.name == "Test key"
    # Stored as base64url of the raw id, which is what the assertion arrives
    # holding — if these disagree, sign-in cannot find the credential.
    assert base64url_to_bytes(row.credential_id)


def test_a_tampered_registration_is_refused(session_factory, user, settings):
    """The signature is what makes enrolment mean anything. A response whose
    client data was edited after signing must not produce a credential."""
    with session_factory() as db:
        u = db.get(User, user.id)
        raw_options, token = passkeys.registration_options(db, u, settings)
        device = SoftWebauthnDevice()
        attestation = device.create(_for_device(raw_options), ORIGIN)
        # Same shape, different challenge: exactly what a replayed or forged
        # response looks like.
        client_data = json.loads(attestation["response"]["clientDataJSON"])
        client_data["challenge"] = _b64(b"a different challenge entirely")
        attestation["response"]["clientDataJSON"] = json.dumps(client_data).encode()

        with pytest.raises(passkeys.PasskeyError):
            passkeys.verify_registration(
                db, u, _as_json(attestation), token, settings)
        assert db.scalar(select(WebauthnCredential)) is None


def test_enrolling_excludes_the_keys_already_held(session_factory, user, settings):
    """Otherwise the same authenticator registers itself twice and the account
    page lists one key as two."""
    with session_factory() as db:
        u = db.get(User, user.id)
        _enrol(db, u, settings)
        raw_options, _ = passkeys.registration_options(db, u, settings)

    excluded = json.loads(raw_options)["excludeCredentials"]
    assert len(excluded) == 1


# --------------------------------------------------------------------------- #
# Signing in
# --------------------------------------------------------------------------- #

def test_an_enrolled_key_proves_who_it_belongs_to(session_factory, user, settings):
    with session_factory() as db:
        u = db.get(User, user.id)
        device = _enrol(db, u, settings)
        credential, token = _assert(db, device, settings)
        signed_in = passkeys.verify_authentication(db, credential, token, settings)

        assert signed_in.id == user.id


def test_a_key_this_app_has_never_seen_is_refused(session_factory, user, settings):
    with session_factory() as db:
        _enrol(db, db.get(User, user.id), settings)
        stranger = VerifyingDevice()
        stranger.cred_init(RP_ID, b"stranger")
        credential, token = _assert(db, stranger, settings)

        with pytest.raises(passkeys.PasskeyError):
            passkeys.verify_authentication(db, credential, token, settings)


def test_a_challenge_is_consumed_by_reading_it(session_factory, user, settings):
    """Directly, rather than through a ceremony.

    Asserting it end-to-end proves less than it looks: replaying a whole
    assertion is ALSO refused by the sign counter, so that test stays green
    with the deletion removed. This one fails.
    """
    with session_factory() as db:
        u = db.get(User, user.id)
        _, token = passkeys.registration_options(db, u, settings)

        assert passkeys.take_challenge(db, token)
        with pytest.raises(passkeys.PasskeyError):
            passkeys.take_challenge(db, token)


def test_replaying_a_whole_assertion_is_refused(session_factory, user, settings):
    with session_factory() as db:
        u = db.get(User, user.id)
        device = _enrol(db, u, settings)
        credential, token = _assert(db, device, settings)
        passkeys.verify_authentication(db, credential, token, settings)

        with pytest.raises(passkeys.PasskeyError):
            passkeys.verify_authentication(db, credential, token, settings)


def test_a_registration_challenge_cannot_be_spent_by_another_account(
    session_factory, user, settings
):
    """The challenge remembers who it was issued to. Without that check, a
    credential enrolled in one ceremony could be attached to a different
    account by whoever could post the response.
    """
    with session_factory() as db:
        other = User(email="other@example.test", name="Other", password_hash="x")
        db.add(other)
        db.flush()
        u = db.get(User, user.id)
        raw_options, token = passkeys.registration_options(db, u, settings)
        attestation = SoftWebauthnDevice().create(_for_device(raw_options), ORIGIN)

        with pytest.raises(passkeys.PasskeyError, match="different account"):
            passkeys.verify_registration(
                db, other, _as_json(attestation), token, settings)


def test_an_expired_challenge_is_refused(session_factory, user, settings):
    import datetime as dt

    with session_factory() as db:
        u = db.get(User, user.id)
        device = _enrol(db, u, settings)
        credential, token = _assert(db, device, settings)
        row = db.scalar(select(WebauthnChallenge))
        row.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
        db.flush()

        with pytest.raises(passkeys.PasskeyError):
            passkeys.verify_authentication(db, credential, token, settings)


def test_signing_in_asks_for_no_account(session_factory, settings):
    """The ceremony is discoverable. Naming the account's keys up front would
    answer "does this email have an account here" to anybody who asked."""
    with session_factory() as db:
        raw_options, _ = passkeys.authentication_options(db, settings)

    assert not json.loads(raw_options).get("allowCredentials")


def test_a_stale_challenge_is_swept(session_factory, user, settings):
    import datetime as dt

    with session_factory() as db:
        u = db.get(User, user.id)
        passkeys.registration_options(db, u, settings)
        db.scalar(select(WebauthnChallenge)).expires_at = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
        db.flush()

        assert passkeys.purge_expired_challenges(db) == 1
        assert db.scalar(select(WebauthnChallenge)) is None


# --------------------------------------------------------------------------- #
# Whether they can be offered at all
# --------------------------------------------------------------------------- #

def test_enabled_without_a_domain_is_not_configured(settings):
    """A true `enabled` with no rp_id would offer a button that fails inside
    the browser, where nobody can see why."""
    settings.auth.webauthn.rp_id = None

    assert not passkeys.configured(settings)
    assert "rp_id" in passkeys.unavailable_reason(settings, secure=True)


def test_plain_http_is_named_as_the_reason(settings):
    assert passkeys.configured(settings)
    assert "HTTPS" in passkeys.unavailable_reason(settings, secure=False)
    assert passkeys.unavailable_reason(settings, secure=True) is None


def test_an_authenticator_that_did_not_verify_the_user_is_refused(
    session_factory, user, settings
):
    """This is what earns a passkey the right to sign in alone.

    Without user verification an assertion proves only that the device was
    present — one factor. The app would then be accepting a single factor from
    somebody holding a stolen phone, while asking a password user for two.
    `SoftWebauthnDevice` sets User Present only, so it stands in for exactly
    that authenticator.
    """
    with session_factory() as db:
        u = db.get(User, user.id)
        device = _enrol(db, u, settings)
        present_only = SoftWebauthnDevice()
        present_only.credential_id = device.credential_id
        present_only.private_key = device.private_key
        present_only.rp_id = device.rp_id
        present_only.user_handle = device.user_handle
        present_only.sign_count = device.sign_count
        credential, token = _assert(db, present_only, settings)

        with pytest.raises(passkeys.PasskeyError):
            passkeys.verify_authentication(db, credential, token, settings)
