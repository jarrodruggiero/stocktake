"""Daily housekeeping, and the upload cap.

The purges are not tidiness for its own sake. An expired session row is only
deleted when somebody happens to present that exact cookie again, so without a
sweep the table keeps every session nobody returned to. Login attempts are a
record of which address tried which email, kept only to drive lockout. A staged
CSV holds somebody's trades, waiting for a commit that may never come. All
three accumulate personal data with nothing watching them.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest
from sqlalchemy import select

from app import maintenance
from app.models import LoginAttempt, UserSession
from app.settings import PortfolioSettings


@pytest.fixture
def settings():
    return PortfolioSettings()


def add_session(db, user, *, expires_in_days: float) -> UserSession:
    now = dt.datetime.now(dt.timezone.utc)
    row = UserSession(
        token_hash=f"hash-{expires_in_days}-{time.time_ns()}",
        user_id=user.id,
        csrf_token="csrf",
        created_at=now,
        last_seen_at=now,
        expires_at=now + dt.timedelta(days=expires_in_days),
    )
    db.add(row)
    db.flush()
    return row


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

def test_expired_sessions_are_removed_and_live_ones_kept(db, owner, settings):
    user, _ = owner
    add_session(db, user, expires_in_days=-1)     # expired yesterday
    add_session(db, user, expires_in_days=-30)    # long gone
    add_session(db, user, expires_in_days=7)      # still valid
    db.commit()

    removed = maintenance.purge_expired_sessions(db, settings)

    assert removed == 2
    assert len(db.scalars(select(UserSession)).all()) == 1


def test_a_session_expiring_in_a_moment_is_left_alone(db, owner, settings):
    """The sweep must not race the boundary — only what has actually expired."""
    user, _ = owner
    add_session(db, user, expires_in_days=0.001)
    db.commit()

    assert maintenance.purge_expired_sessions(db, settings) == 0


# --------------------------------------------------------------------------- #
# Login attempts
# --------------------------------------------------------------------------- #

def test_login_attempts_past_the_retention_window_are_forgotten(db, settings):
    now = dt.datetime.now(dt.timezone.utc)
    for age_days in (1, 15, 29, 31, 400):
        db.add(LoginAttempt(email="someone@example.test", ip="192.0.2.1", success=False,
                            created_at=now - dt.timedelta(days=age_days)))
    db.commit()

    # Default retention is 30 days, so the 31- and 400-day rows go.
    removed = maintenance.purge_old_login_attempts(db, settings)

    assert removed == 2
    assert len(db.scalars(select(LoginAttempt)).all()) == 3


def test_the_retention_window_is_configurable(db, monkeypatch, settings):
    now = dt.datetime.now(dt.timezone.utc)
    for age_days in (2, 10):
        db.add(LoginAttempt(email="someone@example.test", ip="192.0.2.1", success=False,
                            created_at=now - dt.timedelta(days=age_days)))
    db.commit()
    settings.maintenance.attempt_retention_days = 5

    assert maintenance.purge_old_login_attempts(db, settings) == 1


def test_purging_attempts_does_not_disturb_lockout_inside_the_window(db, settings):
    """Lockout counts recent failures, so the sweep must never touch those."""
    now = dt.datetime.now(dt.timezone.utc)
    for _ in range(5):
        db.add(LoginAttempt(email="locked@example.test", ip="192.0.2.1", success=False,
                            created_at=now - dt.timedelta(minutes=2)))
    db.commit()

    maintenance.purge_old_login_attempts(db, settings)

    assert len(db.scalars(select(LoginAttempt)).all()) == 5


# --------------------------------------------------------------------------- #
# Staged uploads
# --------------------------------------------------------------------------- #

def test_stale_staged_uploads_are_swept(db, settings, monkeypatch, tmp_path):
    staging = tmp_path / "staged"
    staging.mkdir()
    fresh = staging / "fresh.json"
    stale = staging / "stale.json"
    fresh.write_text("{}")
    stale.write_text("{}")
    # 48 hours old, against a 24-hour window.
    old = time.time() - 48 * 3600
    import os
    os.utime(stale, (old, old))
    monkeypatch.setattr("app.imports_web.STAGING", staging)

    removed = maintenance.sweep_staged_uploads(db, settings)

    assert removed == 1
    assert fresh.exists()
    assert not stale.exists()


def test_a_missing_staging_directory_is_not_an_error(db, settings, monkeypatch, tmp_path):
    monkeypatch.setattr("app.imports_web.STAGING", tmp_path / "never-created")

    assert maintenance.sweep_staged_uploads(db, settings) == 0


# --------------------------------------------------------------------------- #
# The run as a whole
# --------------------------------------------------------------------------- #

def test_a_run_reports_what_each_step_did(session_factory, settings, monkeypatch,
                                          tmp_path):
    monkeypatch.setattr("app.imports_web.STAGING", tmp_path / "staged")
    monkeypatch.setattr(settings.imports, "visual_dir", str(tmp_path / "visual"))

    counts = maintenance.run(session_factory, settings)

    # Every declared step reports, and reports a number. Compared against
    # STEPS rather than a hand-written list, so adding a job to the sweeper
    # does not silently leave it unreported here.
    assert set(counts) == {name for name, _ in maintenance.STEPS}
    assert all(isinstance(v, int) for v in counts.values())


def test_one_failing_step_does_not_stop_the_others(session_factory, settings,
                                                   monkeypatch, tmp_path):
    """A stuck staging directory is no reason to stop expiring sessions."""
    monkeypatch.setattr("app.imports_web.STAGING", tmp_path / "staged")

    def explode(session, settings):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(maintenance, "STEPS", (
        ("broken", explode),
        ("expired sessions", maintenance.purge_expired_sessions),
    ))

    counts = maintenance.run(session_factory, settings)

    assert counts["broken"].startswith("failed: RuntimeError")
    assert counts["expired sessions"] == 0     # ran anyway


def test_the_schedule_lands_on_the_next_occurrence():
    from app.main import _next_run

    now = dt.datetime(2026, 8, 2, 10, 0)
    # Later today.
    assert _next_run(now, 14, 30) == dt.datetime(2026, 8, 2, 14, 30)
    # Already passed, so tomorrow.
    assert _next_run(now, 3, 30) == dt.datetime(2026, 8, 3, 3, 30)


# --------------------------------------------------------------------------- #
# Upload cap
# --------------------------------------------------------------------------- #

def test_an_oversized_csv_is_refused(client, session_factory, monkeypatch):
    from test_routes import make_login, session_csrf

    make_login(client, session_factory)
    # 1 MB cap, and a file comfortably past it.
    from app import main as main_mod
    monkeypatch.setattr(main_mod.settings.imports, "max_upload_mb", 1)
    oversized = b"a" * (2 * 1024 * 1024)

    resp = client.post("/imports-exports/csv",
                       files={"file": ("big.csv", oversized, "text/csv")},
                       data={"broker": "testbroker", "_csrf": session_csrf(session_factory)})

    assert resp.status_code == 413
    assert "larger than the 1 MB limit" in resp.text


def test_a_normal_sized_csv_still_imports(client, session_factory):
    from test_routes import make_login, session_csrf

    make_login(client, session_factory)
    csv_text = ("Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
                "06/01/2025,Buy,ACME,100,5.00,9.50\n").encode()

    resp = client.post("/imports-exports/csv",
                       files={"file": ("trades.csv", csv_text, "text/csv")},
                       data={"broker": "testbroker", "_csrf": session_csrf(session_factory)})

    assert resp.status_code == 200
    assert "ACME" in resp.text


def test_an_oversized_statement_is_refused(client, session_factory, monkeypatch):
    from test_routes import make_login, session_csrf

    make_login(client, session_factory)
    from app import main as main_mod
    monkeypatch.setattr(main_mod.settings.imports, "max_upload_mb", 1)

    resp = client.post("/imports-exports/statement",
                       files={"file": ("big.pdf", b"a" * (2 * 1024 * 1024),
                                       "application/pdf")},
                       data={"_csrf": session_csrf(session_factory)})

    assert resp.status_code == 413


def test_the_cap_is_configurable(client, session_factory, monkeypatch):
    """Raising the limit has to actually raise it — a hard-coded cap dressed up
    as a setting would be worse than no setting."""
    from test_routes import make_login, session_csrf

    make_login(client, session_factory)
    from app import main as main_mod
    monkeypatch.setattr(main_mod.settings.imports, "max_upload_mb", 5)
    payload = ("Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
               + "06/01/2025,Buy,ACME,1,5.00,9.50\n" * 40000).encode()
    assert 1024 * 1024 < len(payload) < 5 * 1024 * 1024

    resp = client.post("/imports-exports/csv",
                       files={"file": ("trades.csv", payload, "text/csv")},
                       data={"broker": "testbroker", "_csrf": session_csrf(session_factory)})

    assert resp.status_code == 200
