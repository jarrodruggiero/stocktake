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
    """The database at head must be exactly what `models.py` describes.

    **The test that catches drift before it becomes a problem**: eighteen revisions
    of drift — eighteen `server_default`s the models never declared, five
    foreign keys whose `ON DELETE` disagreed outright — and nothing noticed,
    because nothing looked. It also replaces `verify_squash.py`, which compared
    against a chain that no longer exists; the guarantee never needed a chain,
    only the models and the migrations agreeing.

    A failure means a revision was hand-edited or `models.py` changed without
    one. Either way the fix is a new revision, never an edit to one that has
    shipped — decisions.md #20.
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
        "the models and the migrations at head disagree:\n  "
        + "\n  ".join(repr(d) for d in differences)
        + "\n\nAdd a revision; do not edit one that has shipped."
    )


def test_deleting_a_user_does_not_delete_their_trades(seeded: Path):
    """The bug that was live for months, now a property of `0001`.

    `user_id` records who ENTERED a trade, never who owns it — tenancy scopes
    on `portfolio_id`. The database said ON DELETE CASCADE while the model said
    SET NULL, so removing an account would have taken that person's trades and
    dividends out of a portfolio belonging to several people.

    `appcore` sets `PRAGMA foreign_keys=ON` per connection, so this is live
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


def test_an_existing_key_keeps_its_portfolio_through_0010(tmp_path: Path):
    """0010 moves a key's one portfolio into a link table, so a key can reach
    several (decisions.md #128). A key in use before the upgrade must reach
    exactly what it reached before — and the downgrade must put it back."""
    path = tmp_path / "keys.db"
    cfg = _config(path)
    command.upgrade(cfg, "0009")
    conn = sqlite3.connect(path)
    conn.executescript("""
        INSERT INTO user (id, email, name, password_hash, public_id, created_at)
        VALUES (1, 'a@example.com', 'A', 'x', 'pub-1', '2026-01-01');
        INSERT INTO portfolio (id, name, created_at) VALUES (1, 'Main', '2026-01-01');
        INSERT INTO portfolio (id, name, created_at) VALUES (2, 'Other', '2026-01-01');
        INSERT INTO api_key (id, portfolio_id, name, key_hash, prefix, scopes,
                             created_by, created_at)
        VALUES (1, 2, 'budget', 'h1', 'pfk_one', 'read', 1, '2026-01-01');
    """)
    conn.commit()
    conn.close()

    command.upgrade(cfg, "head")
    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT api_key_id, portfolio_id FROM api_key_portfolio").fetchall() == [(1, 2)]
    conn.close()

    command.downgrade(cfg, "0009")
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT id, portfolio_id FROM api_key").fetchall() == [(1, 2)]
    conn.close()


def test_0013_keeps_every_row_on_the_way_up_and_down(tmp_path: Path):
    """0013 adds a column to four tables. Dropping one again, on SQLite,
    rebuilds the table, and a holding setting cascades from its instrument:
    a rebuild that dropped `instrument` with its rows would take them along."""
    path = tmp_path / "rows.db"
    cfg = _config(path)
    command.upgrade(cfg, "0012")
    conn = sqlite3.connect(path)
    conn.executescript("""
        INSERT INTO user (id, email, name, password_hash, public_id, created_at)
        VALUES (1, 'a@example.com', 'A', 'x', 'pub-1', '2026-01-01');
        INSERT INTO portfolio (id, name, created_at) VALUES (1, 'Main', '2026-01-01');
        INSERT INTO instrument (id, ticker, exchange, currency, asset_class, drp, active)
        VALUES (1, 'ALPHA', 'ASX', 'AUD', 'etf', 1, 1);
        INSERT INTO holding_pref (id, portfolio_id, instrument_id, drp, note)
        VALUES (1, 1, 1, 1, 'kept');
        INSERT INTO trade (id, instrument_id, date, type, quantity, unit_price,
                           brokerage, fx_rate, user_id, portfolio_id)
        VALUES (1, 1, '2026-01-02', 'buy', 10, 100.5, 9.5, 1.0, 1, 1);
        INSERT INTO price (instrument_id, date, close, source, provisional)
        VALUES (1, '2026-01-02', 101.25, 'test', 0);
        INSERT INTO fx_rate (pair, date, rate, source)
        VALUES ('USDAUD', '2026-01-02', 1.52, 'test');
    """)
    conn.commit()
    conn.close()
    tables = ("instrument", "holding_pref", "trade", "price", "fx_rate")

    def counts() -> list[int]:
        conn = sqlite3.connect(path)
        try:
            return [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables]
        finally:
            conn.close()

    command.upgrade(cfg, "0013")
    assert counts() == [1, 1, 1, 1, 1]
    command.downgrade(cfg, "0012")
    assert counts() == [1, 1, 1, 1, 1]


def test_0014_gives_each_mode_the_colours_that_served_both(tmp_path: Path):
    """A stored colour applied to light and dark alike, so it goes into both,
    and the page looks as it did. Going down keeps the light set."""
    import json

    path = tmp_path / "colours.db"
    cfg = _config(path)
    command.upgrade(cfg, "0013")
    conn = sqlite3.connect(path)
    for user_id, colours in ((1, {"accent": "#ff8800", "up": "#112233"}), (2, None), (3, {})):
        conn.execute("INSERT INTO user (id, email, name, password_hash, public_id, created_at,"
                     " theme_colors) VALUES (?, ?, 'A', 'x', ?, '2026-01-01', ?)",
                     (user_id, f"{user_id}@example.com", f"pub-{user_id}",
                      None if colours is None else json.dumps(colours)))
    conn.commit()
    conn.close()

    def stored() -> dict:
        conn = sqlite3.connect(path)
        try:
            return {i: None if c is None else json.loads(c)
                    for i, c in conn.execute("SELECT id, theme_colors FROM user ORDER BY id")}
        finally:
            conn.close()

    command.upgrade(cfg, "0014")
    chosen = {"accent": "#ff8800", "up": "#112233"}
    assert stored() == {1: {"light": chosen, "dark": chosen}, 2: None, 3: {}}

    conn = sqlite3.connect(path)
    conn.execute("UPDATE user SET theme_colors = ? WHERE id = 1",
                 (json.dumps({"light": {"up": "#111111"}, "dark": {"up": "#eeeeee"}}),))
    conn.commit()
    conn.close()
    command.downgrade(cfg, "0013")
    assert stored()[1] == {"up": "#111111"}


def test_0015_and_0016_keep_every_distribution_on_the_way_up_and_down(tmp_path: Path):
    """0015 and 0016 add columns to `dividend`, which SQLite rebuilds to drop again:
    a rebuild that lost the rows, or the trade a DRP points at, would lose
    income."""
    path = tmp_path / "period.db"
    cfg = _config(path)
    command.upgrade(cfg, "0014")
    conn = sqlite3.connect(path)
    conn.executescript("""
        INSERT INTO portfolio (id, name, created_at) VALUES (1, 'Main', '2026-01-01');
        INSERT INTO instrument (id, ticker, exchange, currency, asset_class, drp, active)
        VALUES (1, 'ALPHA', 'ASX', 'AUD', 'etf', 1, 1);
        INSERT INTO trade (id, instrument_id, date, type, quantity, unit_price,
                           brokerage, fx_rate, portfolio_id)
        VALUES (1, 1, '2026-07-16', 'drp', 3, 50.0, 0, 1.0, 1);
        INSERT INTO dividend (id, instrument_id, date, cash_amount, fx_rate,
                              reinvest_trade_id, portfolio_id)
        VALUES (1, 1, '2026-07-16', 150.0, 1.0, 1, 1);
    """)
    conn.commit()
    conn.close()

    def rows() -> list[tuple]:
        conn = sqlite3.connect(path)
        try:
            return conn.execute("SELECT id, date, reinvest_trade_id FROM dividend").fetchall()
        finally:
            conn.close()

    command.upgrade(cfg, "0015")
    assert rows() == [(1, "2026-07-16", 1)]
    command.upgrade(cfg, "0016")                # the tax withheld: the same rebuild
    assert rows() == [(1, "2026-07-16", 1)]
    command.downgrade(cfg, "0014")
    assert rows() == [(1, "2026-07-16", 1)]


def test_0017_adds_corporate_actions_beside_what_is_there(tmp_path: Path):
    """A new table, so the trades it changes come through both ways untouched.
    Its rows are their portfolio's, as trades are: deleting a portfolio takes
    its own actions and leaves another's."""
    path = tmp_path / "actions.db"
    cfg = _config(path)
    command.upgrade(cfg, "0016")
    conn = sqlite3.connect(path)
    conn.executescript("""
        INSERT INTO portfolio (id, name, created_at) VALUES (1, 'Main', '2026-01-01'),
                                                            (2, 'Other', '2026-01-01');
        INSERT INTO instrument (id, ticker, exchange, currency, asset_class, drp, active)
        VALUES (1, 'ALPHA', 'ASX', 'AUD', 'share', 0, 1);
        INSERT INTO trade (id, instrument_id, date, type, quantity, unit_price,
                           brokerage, fx_rate, portfolio_id)
        VALUES (1, 1, '2025-01-06', 'buy', 100, 10.0, 0, 1.0, 1);
    """)
    conn.commit()
    conn.close()

    def trades() -> list[tuple]:
        conn = sqlite3.connect(path)
        try:
            return conn.execute("SELECT id, date, quantity, portfolio_id FROM trade").fetchall()
        finally:
            conn.close()

    before = trades()
    command.upgrade(cfg, "0017")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        INSERT INTO corporate_action (id, portfolio_id, instrument_id, date, kind,
                                      ratio_new, ratio_old)
        VALUES (1, 1, 1, '2025-03-03', 'split', 2, 1), (2, 2, 1, '2025-03-03', 'split', 2, 1);
        DELETE FROM portfolio WHERE id = 2;
    """)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO corporate_action (portfolio_id, instrument_id, date, kind) "
                     "VALUES (1, 1, '2025-04-01', 'bonus')")
    kept = conn.execute("SELECT id FROM corporate_action").fetchall()
    conn.close()

    assert (kept, trades()) == ([(1,)], before)
    command.downgrade(cfg, "0016")
    assert "corporate_action" not in _tables(path)
    assert trades() == before


def test_0018_adds_the_tax_rate_beside_a_portfolios_settings(tmp_path: Path):
    """A column on `portfolio`, which SQLite rebuilds to drop it again: a rebuild
    that lost a portfolio or its fees would lose what its owner set."""
    path = tmp_path / "rate.db"
    cfg = _config(path)
    command.upgrade(cfg, "0017")
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO portfolio (id, name, created_at, brokerage_flat) "
                 "VALUES (1, 'Main', '2026-01-01', 9.5)")
    conn.commit()
    conn.close()

    def portfolios(column: str = "brokerage_flat") -> list[tuple]:
        conn = sqlite3.connect(path)
        try:
            return conn.execute(f"SELECT id, name, {column} FROM portfolio").fetchall()
        finally:
            conn.close()

    before = portfolios()
    command.upgrade(cfg, "0018")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE portfolio SET marginal_tax_rate = 32.5")
    conn.commit()
    conn.close()

    assert (portfolios(), portfolios("marginal_tax_rate")) == (before, [(1, "Main", 32.5)])
    command.downgrade(cfg, "0017")
    assert portfolios() == before
    with pytest.raises(sqlite3.OperationalError):
        portfolios("marginal_tax_rate")


def test_0019_takes_the_levy_out_of_the_one_rate(tmp_path: Path):
    """0018's rate was asked for with the 2% Medicare levy in it. 0019 keeps
    the levy apart, so 32 becomes 30 and 2; a rate too small to hold the levy
    is left as it was. Down again, they add back up."""
    path = tmp_path / "levy.db"
    cfg = _config(path)
    command.upgrade(cfg, "0018")
    conn = sqlite3.connect(path)
    conn.executescript("""
        INSERT INTO portfolio (id, name, created_at, marginal_tax_rate)
        VALUES (1, 'Mine', '2026-01-01', 32), (2, 'Unset', '2026-01-01', NULL),
               (3, 'Small', '2026-01-01', 1.5);
    """)
    conn.commit()
    conn.close()

    def rates(columns: str) -> list[tuple]:
        conn = sqlite3.connect(path)
        try:
            return conn.execute(f"SELECT id, {columns} FROM portfolio ORDER BY id").fetchall()
        finally:
            conn.close()

    command.upgrade(cfg, "0019")
    assert rates("income_tax_rate, medicare_levy") == [(1, 30, 2), (2, None, None),
                                                       (3, 1.5, None)]
    command.downgrade(cfg, "0018")
    assert rates("marginal_tax_rate") == [(1, 32), (2, None), (3, 1.5)]


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _revision(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        conn.close()


def test_a_migration_that_fails_part_way_leaves_nothing_behind(tmp_path: Path):
    """What 0010 did to a real install on 2026-10-03, replayed.

    That database came from an older app and named `api_key`'s index
    `ix_api_key_portfolio`. 0010 created `api_key_portfolio`, then failed to
    drop `ix_api_key_portfolio_id`, and Alembic ran SQLite's DDL outside any
    transaction ("Will assume non-transactional DDL"), so the new table stayed.
    Every restart then failed on "table already exists", and it took a hand
    repair. As one transaction the failure leaves the database at 0009, and
    putting the cause right is all it takes.
    """
    path = tmp_path / "older-names.db"
    cfg = _config(path)
    command.upgrade(cfg, "0009")
    conn = sqlite3.connect(path)
    conn.executescript("DROP INDEX ix_api_key_portfolio_id;"
                       "CREATE INDEX ix_api_key_portfolio ON api_key (portfolio_id);")
    conn.close()

    with pytest.raises(Exception, match="ix_api_key_portfolio_id"):
        command.upgrade(cfg, "head")

    assert "api_key_portfolio" not in _tables(path)
    assert _revision(path) == [("0009",)]

    conn = sqlite3.connect(path)
    conn.executescript("DROP INDEX ix_api_key_portfolio;"
                       "CREATE INDEX ix_api_key_portfolio_id ON api_key (portfolio_id);")
    conn.close()
    command.upgrade(cfg, "head")

    assert "api_key_portfolio" in _tables(path)
    assert _revision(path) != [("0009",)]
