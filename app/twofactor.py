"""The second factor: TOTP codes and single-use recovery codes.

Separate from auth.py because none of this knows about sessions or requests —
it is only about proving possession of a phone.

The secret is stored as issued, the QR is rendered locally, one step of clock
drift is accepted, and recovery codes are hashed and marked used rather than
deleted. Each of those is a place this is commonly got wrong: decisions.md #79.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .models import RecoveryCode, User

# Sixteen, matching GitHub — and why sixteen single-use codes rather than eight
# or one passphrase: decisions.md #30.
RECOVERY_CODE_COUNT = 16
# Unambiguous alphabet: no O/0, no I/1/L. These get written down by hand.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
# Characters per code: 74 bits, grouped into three blocks of five so it can
# be transcribed. Why 15 and not 13, and why the throttle stays either way —
# decisions.md #42.
RECOVERY_CODE_CHARS = 15
_GROUP = 5


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _pyotp():
    """Imported on use, like yfinance — see app/memory.py for the rationale.

    Also the seam the tests patch when they need a deterministic code.
    """
    import pyotp

    return pyotp


# --------------------------------------------------------------------------- #
# Secrets and codes
# --------------------------------------------------------------------------- #

def new_secret() -> str:
    """A fresh base32 TOTP secret."""
    return _pyotp().random_base32()


def provisioning_uri(secret: str, email: str, issuer: str) -> str:
    """The `otpauth://` URI an authenticator app scans.

    `issuer` is what shows up above the code in the app. It comes from the
    configured app name so two self-hosted installs don't collide in someone's
    authenticator list.
    """
    return _pyotp().TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def verify_code(secret: str | None, code: str) -> bool:
    """Whether this six-digit code is currently valid for that secret.

    Tolerates spaces (people paste "123 456") and rejects anything falsy
    outright rather than letting pyotp decide what an empty code means.
    """
    if not secret or not code:
        return False
    cleaned = code.replace(" ", "").strip()
    if not cleaned.isdigit():
        return False
    return bool(_pyotp().TOTP(secret).verify(cleaned, valid_window=1))


def hash_code(raw: str) -> str:
    """Recovery codes are hashed like session tokens: the database never holds
    anything that could be replayed as a credential."""
    return hashlib.sha256(_normalise(raw).encode()).hexdigest()


def _normalise(raw: str) -> str:
    """Upper-cased, stripped of the dash and any whitespace, so a code typed
    back exactly as displayed still matches."""
    return "".join(raw.split()).replace("-", "").upper()


def generate_recovery_codes(db: DbSession, user: User) -> list[str]:
    """Issue a fresh set, replacing any that already exist.

    Returns the RAW codes — the only time they exist in readable form. The
    caller must show them once and never store them.
    """
    for existing in db.scalars(
        select(RecoveryCode).where(RecoveryCode.user_id == user.id)
    ).all():
        db.delete(existing)
    db.flush()

    raw_codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        body = "".join(secrets.choice(_ALPHABET) for _ in range(RECOVERY_CODE_CHARS))
        # Grouped, because these get read aloud and typed from paper.
        raw = "-".join(body[i:i + _GROUP] for i in range(0, len(body), _GROUP))
        raw_codes.append(raw)
        db.add(RecoveryCode(user_id=user.id, code_hash=hash_code(raw)))
    db.flush()
    return raw_codes


def has_recovery_codes(db: DbSession, user: User) -> bool:
    return db.scalar(
        select(RecoveryCode.id).where(RecoveryCode.user_id == user.id).limit(1)
    ) is not None


def ensure_recovery_codes(db: DbSession, user: User) -> list[str]:
    """Issue a set only if this account has none, and say what was issued.

    Returns [] when codes already exist — which the caller must read as "do not
    show a page of codes", not as a failure. Regenerating instead would
    invalidate a list somebody printed at signup, and they would find out at
    the worst possible moment.
    """
    if has_recovery_codes(db, user):
        return []
    return generate_recovery_codes(db, user)


def consume_recovery_code(db: DbSession, user: User, raw: str) -> bool:
    """Spend a recovery code. False if it is unknown or already used.

    Marked used rather than deleted, so the count shown on the account page
    stays honest and a replay of a spent code is refused rather than looking
    like a code that never existed.
    """
    if not raw:
        return False
    match = db.scalar(
        select(RecoveryCode).where(
            RecoveryCode.user_id == user.id,
            RecoveryCode.code_hash == hash_code(raw),
            RecoveryCode.used_at.is_(None),
        )
    )
    if match is None:
        return False
    match.used_at = _utcnow()
    db.flush()
    return True


def remaining_recovery_codes(db: DbSession, user: User) -> int:
    return len(
        db.scalars(
            select(RecoveryCode).where(
                RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None)
            )
        ).all()
    )


# --------------------------------------------------------------------------- #
# Enrolment
# --------------------------------------------------------------------------- #

def qr_svg(uri: str) -> str:
    """The provisioning URI as an inline SVG.

    Rendered in-process and embedded in the page: no external image service,
    because the obvious one (a chart URL) would mean posting the TOTP secret to
    a third party in a query string.
    """
    import qrcode
    import qrcode.image.svg

    image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode()


def is_enabled(user: User) -> bool:
    """Enrolled AND switched on. A secret alone is a half-finished enrolment —
    someone who scanned the QR and closed the tab must not be locked out."""
    return bool(user.totp_secret and user.totp_enabled_at)


def begin_enrolment(user: User) -> str:
    """Issue a secret without turning anything on, or resume one in progress.

    RESUMED rather than restarted, which is the whole point. A fresh secret on
    every render means a refresh — or anything else that re-fetches the page —
    silently invalidates the QR just scanned, and the code the phone shows stops
    matching for a reason nothing on screen explains. decisions.md #39.

    Storing it does not enable anything: `is_enabled` wants `totp_enabled_at`
    as well, so a secret on its own is a half-finished enrolment that signs
    nobody in.
    """
    if not (user.totp_secret and user.totp_enabled_at is None):
        user.totp_secret = new_secret()
    return user.totp_secret


def enable(db: DbSession, user: User, secret: str) -> list[str]:
    """Turn it on. Caller has already verified a code from this secret — that
    check is what proves the app was really enrolled.

    Recovery codes are NOT reissued here. They belong to the account rather
    than to this factor, and are handed out when the account is created; making
    a fresh set now would quietly kill the list the user already saved.

    The exception is an account that has none — one created before recovery
    codes existed. A migration could not issue those, because a code has to be
    shown once and a migration has nobody to show it to.
    """
    user.totp_secret = secret
    user.totp_enabled_at = _utcnow()
    codes = ensure_recovery_codes(db, user)
    db.flush()
    return codes


def disable(db: DbSession, user: User) -> None:
    """Turn off the second factor. The recovery codes are left alone.

    Destroying them here would be right if they were a 2FA-only fallback — a
    later re-enrolment would otherwise inherit escape hatches the user thought
    were gone. They are the ACCOUNT's recovery, though: the way back in when the
    password is what went missing, so they outlive any single factor. Destroying
    them here would leave somebody who simply switched authenticators with no
    recovery at all, and nothing on screen to say so.
    """
    user.totp_secret = None
    user.totp_enabled_at = None
    db.flush()
