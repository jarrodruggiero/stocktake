"""Shared fixtures. Three things here are load-bearing.

1. **Environment before imports.** `appkit.config` reads `APP_CONFIG_FILE` at
   module import time, so the env vars are set at the top of this file.
2. **Schema from the real migration chain**, built once per session and copied
   per test. `Base.metadata.create_all` would test a schema production never
   has.
3. **Cache clearing.** `queries` caches by portfolio id against a fingerprint,
   every test gets id 1, and two tests can share a fingerprint — so
   `_clear_caches` is autouse. Do not remove it. `_discard_wizard_draft` is
   autouse for the same reason: the draft is process-global, and the app
   refuses pages while one is in progress.

SQLite by default; Postgres is an untested claim otherwise:

    STOCKTAKE_TEST_DB=postgres PGHOST=localhost PGPORT=5432 \
    PGDATABASE=stocktake_test PGUSER=postgres PGPASSWORD=postgres \
    pytest tests/

Isolation differs by backend, so assert on answers, never on a backend's
representation — `appkit.ensure_utc` is what papers over the difference.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# --- environment, before any appkit/app import (see note 1 above) ----------- #
TESTS_DIR = Path(__file__).parent
APP_ROOT = TESTS_DIR.parent
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="portfolio-tests-"))

BACKEND = os.environ.get("STOCKTAKE_TEST_DB", "sqlite").lower()
if BACKEND not in ("sqlite", "postgres"):
    raise RuntimeError(f"STOCKTAKE_TEST_DB must be sqlite or postgres, got {BACKEND!r}")

os.environ["APP_CONFIG_FILE"] = str(TESTS_DIR / "config.test.yaml")
if BACKEND == "sqlite":
    os.environ["APP_DATABASE__PATH"] = str(_TMP_ROOT / "app.db")
    os.environ.pop("APP_DATABASE__TYPE", None)
else:
    # app.main reads these at import time for its own session factory.
    os.environ["APP_DATABASE__TYPE"] = "postgres"
    os.environ["APP_DATABASE__HOST"] = os.environ.get("PGHOST", "localhost")
    os.environ["APP_DATABASE__PORT"] = os.environ.get("PGPORT", "5432")
    os.environ["APP_DATABASE__NAME"] = os.environ.get("PGDATABASE", "stocktake_test")
    os.environ["APP_DATABASE__USER"] = os.environ.get("PGUSER", "postgres")
    os.environ["APP_DATABASE__PASSWORD"] = os.environ.get("PGPASSWORD", "postgres")

# `app` is a package under apps/portfolio; tests may be invoked from anywhere.
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(TESTS_DIR))

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import providers, queries, tenancy  # noqa: E402
from app.models import Portfolio, PortfolioMember, User  # noqa: E402
from appkit import make_session_factory, upgrade_to_head  # noqa: E402
from appkit.config import DatabaseSettings  # noqa: E402
from appkit.db import make_engine  # noqa: E402

# A frozen "today" for every test that depends on the current date. Chosen so
# the reference portfolio (which starts 2019) fills the 1d/1m/6m/1y/3y/5y
# performance windows but NOT 10y/20y — the "record doesn't reach back that
# far" path gets exercised too.
FROZEN_TODAY = "2026-08-02"


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

def _pg_settings() -> DatabaseSettings:
    return DatabaseSettings(
        type="postgres",
        host=os.environ["APP_DATABASE__HOST"],
        port=int(os.environ["APP_DATABASE__PORT"]),
        name=os.environ["APP_DATABASE__NAME"],
        user=os.environ["APP_DATABASE__USER"],
        password=os.environ["APP_DATABASE__PASSWORD"],
    )


@pytest.fixture(scope="session")
def template_db():
    """The migrated schema, built once per session.

    SQLite gets a template file to copy; Postgres gets its one database
    migrated in place (there is no file to copy, so isolation is by truncation
    instead — see `db_path`).
    """
    if BACKEND == "postgres":
        upgrade_to_head(_pg_settings(), str(APP_ROOT / "app" / "migrations"))
        return None
    path = _TMP_ROOT / "template.db"
    upgrade_to_head(
        DatabaseSettings(type="sqlite", path=str(path)), str(APP_ROOT / "app" / "migrations")
    )
    return path


@pytest.fixture
def db_path(template_db, tmp_path: Path):
    """A clean database for one test."""
    if BACKEND == "postgres":
        # Empty every table but keep the schema (and alembic's own bookkeeping).
        engine = make_engine(_pg_settings())
        with engine.begin() as conn:
            names = [r[0] for r in conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'alembic_version'"))]
            if names:
                conn.execute(text(
                    "TRUNCATE " + ", ".join(f'"{n}"' for n in names) + " RESTART IDENTITY CASCADE"))
        engine.dispose()
        return None
    path = tmp_path / "portfolio.db"
    shutil.copy(template_db, path)
    return path


@pytest.fixture
def session_factory(db_path):
    settings = _pg_settings() if BACKEND == "postgres" else DatabaseSettings(
        type="sqlite", path=str(db_path))
    factory = make_session_factory(settings)
    tenancy.install(factory)
    yield factory
    # Every test builds its own engine, and an engine holds a pool of up to ten
    # connections. Undisposed, they come back only when CPython collects the
    # engine — so the suite's connection use depended on garbage-collection
    # timing rather than on anything it does, and a machine slow enough to hold
    # more engines at once exhausted Postgres's 100 and errored. Reported from
    # a machine with tesseract installed, where the OCR tests run.
    factory.kw["bind"].dispose()


@pytest.fixture(autouse=True)
def _clear_caches():
    """Stop one test's cached series being served to the next (see note 3)."""
    for cache in (queries._series_cache, queries._holdings_cache, queries._grouped_cache):
        cache.clear()
    yield
    for cache in (queries._series_cache, queries._holdings_cache, queries._grouped_cache):
        cache.clear()


@pytest.fixture(autouse=True)
def _discard_wizard_draft():
    """A wizard draft is process-global, so one test's leaks into the next.

    It never showed until the app started REFUSING pages while a wizard is in
    progress: before that nothing outside /setup consulted it, and a stale
    draft was invisible. Now a test that walks the wizard makes every later
    test's request redirect.
    """
    from app import setupwizard

    setupwizard.discard()
    yield
    setupwizard.discard()


@pytest.fixture(autouse=True)
def _restore_settings():
    """Put the live settings object back the way the test found it.

    `/admin/settings` calls `configfile.apply_live`, which mutates the ONE
    global `settings` the whole app reads. A test that saves settings therefore
    changes the app for every test after it, in file order.

    That was harmless while every editable setting only affected background
    behaviour. It stopped being harmless when `features.dca_schedule` landed:
    the settings tests post partial forms, an absent checkbox means False, and
    28 unrelated plan tests started getting 410 from a feature they never
    touched. The fix belongs here rather than in those tests — the next global
    setting will do the same thing.
    """
    from app.main import settings as live

    before = live.model_copy(deep=True)
    yield
    for name in type(live).model_fields:
        setattr(live, name, getattr(before, name))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing in this suite may talk to the internet.

    Not a style rule — it caught a real bug. When the price feed gained fallback
    providers, a test that stubbed Yahoo into returning nothing fell straight
    through to the live Frankfurter API and passed against real market data. It
    would have failed on a plane, in CI without egress, and differently every
    day.

    So the shared HTTP helper raises here by default. A test that wants a
    provider's parsing exercised stubs `providers._get_json` itself with a
    canned payload, which is both hermetic and far clearer about what shape it
    is testing against.
    """
    def _blocked(url, params):
        raise AssertionError(
            f"a test tried to reach the network: {url} {params}. Stub "
            "`providers._get_json` with a canned payload instead."
        )

    monkeypatch.setattr(providers, "_get_json", _blocked)

    # The other way out, and a nastier one: adding an instrument kicks a real
    # feed run onto a worker thread that outlives the test — decisions.md #53.
    import app.main as main_module

    monkeypatch.setattr(main_module, "_kick_feed", lambda: None)


@pytest.fixture
def db(session_factory):
    """An *unbound* session: no portfolio on it.

    Personal-data queries raise `TenancyError` here by design — use this for
    market-data work (instruments, prices, FX) and for testing that the
    fail-closed guarantee actually fails closed.
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    finally:
        session.close()


@pytest.fixture
def owner(db):
    """A user + their portfolio + the owner membership. Returns (user, portfolio)."""
    user = User(
        email="owner@example.test",
        name="Test Owner",
        password_hash="x",  # tests that care about login set a real hash
        is_admin=True,
    )
    db.add(user)
    db.flush()
    portfolio = Portfolio(name="Test portfolio")
    db.add(portfolio)
    db.flush()
    db.add(PortfolioMember(portfolio_id=portfolio.id, user_id=user.id, role="owner"))
    db.flush()
    return user, portfolio


@pytest.fixture
def pf(db, owner):
    """A session bound to one portfolio — what a logged-in request gets.

    Yields the session; `pf.info` carries the portfolio/user ids. This is the
    fixture most tests want.
    """
    user, portfolio = owner
    tenancy.bind(db, portfolio.id, user.id)
    return db


# --------------------------------------------------------------------------- #
# Route-level testing
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def app_module(template_db):
    """The imported FastAPI app.

    `app.main` does config load + migrations + session factory at *import*
    time, so it can only be imported once per process — hence session scope.
    Per-test isolation comes from `client`, which repoints both session
    factories at a fresh database.

    The test config sets `price_feed.enabled: false`, so importing this does
    not start the feed's background task or touch the network.
    """
    from app import main

    return main


@pytest.fixture
def client(app_module, session_factory, db_path):
    """A TestClient whose app talks to this test's database.

    Two factories must be repointed, because the app reaches the database two
    ways: module-global `SessionLocal` (the pre-login/`anon()` paths) and
    `app.state.session_factory` (everything behind `auth.scoped_session`).
    Patching only one leaves half the app on the session-scoped database.

    Constructed WITHOUT a `with` block on purpose: Starlette only runs the
    lifespan for a context-managed client, and the lifespan starts the price
    feed. Belt and braces with `price_feed.enabled: false`.
    """
    from fastapi.testclient import TestClient

    original_local = app_module.SessionLocal
    original_state = app_module.app.state.session_factory
    app_module.SessionLocal = session_factory
    app_module.app.state.session_factory = session_factory
    try:
        yield TestClient(app_module.app)
    finally:
        app_module.SessionLocal = original_local
        app_module.app.state.session_factory = original_state
