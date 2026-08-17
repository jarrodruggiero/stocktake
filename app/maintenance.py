"""Daily housekeeping.

Deliberately its own scheduled job rather than a passenger on the price feed.
The two answer to different things — one to market hours, the other to how long
you want records kept — and bolting the second onto the first would mean
turning off the feed silently stopped the cleanup as well.

Each step is small, independent, and wrapped: a step that fails logs and the
rest still run, because a stuck staging directory is no reason to stop expiring
sessions. Everything runs on an unscoped session, which is correct rather than
convenient — sessions and login attempts belong to no portfolio.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path

from sqlalchemy import delete, select

from . import tenancy
from .models import LoginAttempt, UserSession

log = logging.getLogger(__name__)


def purge_expired_sessions(session, settings) -> int:
    """Drop sessions that have already expired.

    `load_session` deletes an expired row when it happens to be presented, so
    without this the table keeps every session nobody ever returned to — which
    is most of them.
    """
    now = dt.datetime.now(dt.timezone.utc)
    result = session.execute(delete(UserSession).where(UserSession.expires_at < now))
    return result.rowcount or 0


def purge_old_login_attempts(session, settings) -> int:
    """Forget failed sign-ins past the retention window.

    They exist to drive lockout, not as an audit log, so keeping them longer
    than the lockout window serves nobody and quietly accumulates a list of
    which addresses tried which email.
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=settings.maintenance.attempt_retention_days
    )
    result = session.execute(delete(LoginAttempt).where(LoginAttempt.created_at < cutoff))
    return result.rowcount or 0


def sweep_staged_uploads(session, settings) -> int:
    """Remove half-finished CSV imports left in the staging area.

    A preview writes the parsed rows to a file and waits for someone to press
    commit. When they don't, the file stays — holding the trades they were
    about to import, which is personal data with no owner watching it.
    """
    from .imports_web import STAGING

    if not STAGING.exists():
        return 0
    cutoff = time.time() - settings.maintenance.staged_upload_hours * 3600
    removed = 0
    for path in STAGING.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:               # already gone, or not ours
            log.warning("could not sweep %s: %s", path, exc)
    return removed


def sweep_visual_mapper(session, settings) -> int:
    """Rendered page images from the visual statement mapper.

    Belt to the lazy sweep's braces. `pagemap.create` clears cold sessions
    whenever somebody uploads, which covers every install where the feature is
    used more than once — this is for the one where it is used exactly once and
    the last session's images would otherwise sit on the volume forever.
    """
    from . import pagemap

    return pagemap.sweep(settings)


# Order is not significant — each step stands alone. Add one here and it runs
# with the rest, inside the same error handling.
STEPS = (
    ("expired sessions", purge_expired_sessions),
    ("old login attempts", purge_old_login_attempts),
    ("staged uploads", sweep_staged_uploads),
    ("visual mapper pages", sweep_visual_mapper),
)

# `STEPS` under the name the tests look it up by.
JOBS = STEPS


def run(session_factory, settings) -> dict[str, int | str]:
    """Run every step once. Returns what each one did, for the log."""
    counts: dict[str, int | str] = {}
    for name, step in STEPS:
        try:
            with tenancy.unscoped_session(session_factory) as session:
                counts[name] = step(session, settings)
        except Exception as exc:
            # One failing step must not take the others with it.
            log.exception("maintenance step %r failed", name)
            counts[name] = f"failed: {type(exc).__name__}"
    log.info("maintenance run: %s", counts)
    return counts


def expired_session_count(session) -> int:
    """Used by the tests, and handy in a shell when wondering if this works."""
    now = dt.datetime.now(dt.timezone.utc)
    return len(session.scalars(select(UserSession).where(UserSession.expires_at < now)).all())


def staging_dir() -> Path:
    from .imports_web import STAGING

    return STAGING
