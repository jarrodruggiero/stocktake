"""What the schema must guarantee, checked against a database built from 0001.

Until the squash this file migrated a populated database through `0019`–`0021`
and asserted nothing was lost. Those revisions no longer exist, so those tests
cannot — but **the guarantees they were protecting still have to hold**, and
they are the ones that were wrong in the database for months without anyone
noticing. The history went; the checks stayed.

What each one is really pinning is `docs/contributing/decisions.md` #20 and #31".
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

APP_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = str(APP_ROOT / "app" / "migrations")


def _config(path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", SCRIPTS)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return cfg


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    """A database built from `0001_initial` alone, with one of everything."""
    path = tmp_path / "seeded.db"
    command.upgrade(_config(path), "head")

    conn = sqlite3.connect(path)
    conn.executescript("""
        INSERT INTO user (id, email, name, password_hash, public_id, created_at)
        VALUES (1, 'a@example.com', 'A', 'x', 'pub-1', '2026-01-01');
        INSERT INTO portfolio (id, name, created_at)
        VALUES (1, 'Main', '2026-01-01');
        INSERT INTO portfolio_member (id, portfolio_id, user_id, role, created_at)
        VALUES (1, 1, 1, 'owner', '2026-01-01');
        INSERT INTO instrument (id, ticker, exchange, currency, asset_class,
                                drp, active)
        VALUES (1, 'ALPHA', 'ASX', 'AUD', 'etf', 1, 1);
        INSERT INTO trade (id, instrument_id, date, type, quantity, unit_price,
                           brokerage, fx_rate, user_id, portfolio_id)
        VALUES (1, 1, '2026-01-02', 'buy', 10, 100.5, 9.5, 1.0, 1, 1);
        INSERT INTO dividend (id, instrument_id, date, cash_amount, fx_rate,
                              residual_carried, user_id, portfolio_id)
        VALUES (1, 1, '2026-03-01', 42.1234, 1.0, 0.5566, 1, 1);
        INSERT INTO saved_chart (id, portfolio_id, user_id, name, spec,
                                 created_at, position, width)
        VALUES (1, 1, 1, 'Value', '{"x": "date"}', '2026-01-01', 0, 'half');
    """)
    conn.commit()
    conn.close()
    return path


def test_a_fresh_install_builds_the_whole_schema_from_0001(tmp_path: Path):
    """0001 is the base and stays the base.

    It was written to say "head is 0001", to notice a chain regrowing before
    v1.0.0. That window shut the moment a database was deployed on 0001 with
    real data in it: alembic never re-runs an applied revision, so folding a
    change back into 0001 reaches new installs and silently misses every
    existing one. From here a schema change is a new revision, and this checks
    what still has to hold — 0001 is the base of the chain, and the schema it
    builds is complete.
    """
    path = tmp_path / "fresh.db"
    command.upgrade(_config(path), "0001")

    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert conn.execute(
        "SELECT version_num FROM alembic_version").fetchone()[0] == "0001"
    for expected in ("trade", "dividend", "instrument", "user", "portfolio",
                     "saved_chart", "holding_pref", "price", "fx_rate"):
        assert expected in tables
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_the_whole_chain_runs_from_nothing_to_head(tmp_path: Path):
    """A fresh install runs every revision in order, not just the base."""
    path = tmp_path / "chain.db"
    command.upgrade(_config(path), "head")

    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"trade", "dividend", "instrument", "user"} <= tables
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_the_models_and_the_migration_have_not_drifted(tmp_path: Path):
    """`0001_initial` must be exactly what `models.py` describes. Forever.

    **The test that catches drift before it becomes a problem**: eighteen revisions
    of drift — eighteen `server_default`s the models never declared, five
    foreign keys whose `ON DELETE` disagreed outright — and nothing noticed,
    because nothing looked. It also replaces `verify_squash.py`, which compared
    against a chain that no longer exists; the guarantee never needed a chain,
    only the models and the one migration agreeing.

    A failure means the migration was hand-edited or `models.py` changed without
    one. Either way the fix is a migration, never an edit to `0001` —
    decisions.md #20.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    from app.models import Base

    path = tmp_path / "drift.db"
    command.upgrade(_config(path), "head")

    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        differences = compare_metadata(context, Base.metadata)
    engine.dispose()

    assert not differences, (
        "the models and 0001_initial disagree:\n  "
        + "\n  ".join(repr(d) for d in differences)
        + "\n\nAdd a migration — do not edit 0001_initial by hand."
    )


def test_deleting_a_user_does_not_delete_their_trades(seeded: Path):
    """The bug that was live for months, now a property of `0001`.

    `user_id` records who ENTERED a trade, never who owns it — tenancy scopes
    on `portfolio_id`. The database said ON DELETE CASCADE while the model said
    SET NULL, so removing an account would have taken that person's trades and
    dividends out of a portfolio belonging to several people.

    `appkit` sets `PRAGMA foreign_keys=ON` per connection, so this is live
    behaviour rather than a dormant declaration — which is why the test turns
    it on too.
    """
    conn = sqlite3.connect(seeded)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM user WHERE id = 1")
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM trade").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM dividend").fetchone()[0] == 1
    assert conn.execute("SELECT user_id FROM trade").fetchone()[0] is None
    assert conn.execute("SELECT user_id FROM dividend").fetchone()[0] is None
    conn.close()


def test_deleting_a_user_does_delete_their_charts(seeded: Path):
    """The same disagreement pointing the other way.

    A chart IS owned by its user, and `SavedChart` is user-scoped — every query
    filters on `user_id`. A surviving row with a NULL owner would be invisible
    for ever, so it must cascade rather than linger.
    """
    conn = sqlite3.connect(seeded)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM user WHERE id = 1")
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM saved_chart").fetchone()[0] == 0
    conn.close()


def test_deleting_a_portfolio_takes_its_records_with_it(seeded: Path):
    """The other half of the cascade rules: the tenant key DOES own its rows."""
    conn = sqlite3.connect(seeded)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM portfolio WHERE id = 1")
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM trade").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dividend").fetchone()[0] == 0
    conn.close()


def test_the_schema_enforces_its_own_checks(seeded: Path):
    """CHECK constraints survived the squash.

    They are the only thing stopping a trade typed `sel` or a negative
    quantity. The schema comparison that verified the squash ignored them until
    it was taught not to — two databases can differ only by a missing CHECK and
    look identical column for column.
    """
    conn = sqlite3.connect(seeded)
    for bad in ("INSERT INTO trade (instrument_id, date, type, quantity, "
                "unit_price, brokerage, portfolio_id) "
                "VALUES (1, '2026-01-03', 'sel', 1, 1, 0, 1)",
                "INSERT INTO trade (instrument_id, date, type, quantity, "
                "unit_price, brokerage, portfolio_id) "
                "VALUES (1, '2026-01-03', 'buy', -1, 1, 0, 1)"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(bad)
    conn.close()


def test_a_residual_of_zero_stays_distinguishable_from_unknown(seeded: Path):
    """The reason `residual_carried` has no default.

    NULL means nobody has worked it out; 0 means the registry kept nothing.
    A server-side default would erase that difference silently.
    """
    conn = sqlite3.connect(seeded)
    conn.execute("INSERT INTO dividend (id, instrument_id, date, cash_amount, "
                 "portfolio_id) VALUES (2, 1, '2026-06-01', 10.0, 1)")
    conn.execute("UPDATE dividend SET residual_carried = 0 WHERE id = 1")
    conn.commit()

    assert conn.execute(
        "SELECT residual_carried FROM dividend WHERE id = 2").fetchone()[0] is None
    assert conn.execute(
        "SELECT residual_carried FROM dividend WHERE id = 1").fetchone()[0] == 0
    conn.close()
