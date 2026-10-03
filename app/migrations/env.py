"""Alembic environment: migrates against the app's own settings-derived URL."""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from app.models import Base

config = context.config
target_metadata = Base.metadata

# When invoked via the CLI (alembic.ini) no URL is set — derive it from the
# app's settings, same as the app itself would. upgrade_to_head() sets it.
if not config.get_main_option("sqlalchemy.url"):
    from app.settings import PortfolioSettings
    from appcore import load_config

    url = load_config(PortfolioSettings).database.url
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        render_as_batch=True,  # sqlite: ALTERs become table rebuilds
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _transactional_sqlite(engine) -> None:
    """Make SQLite's DDL roll back with everything else.

    pysqlite opens a transaction itself only before INSERT, UPDATE and DELETE,
    so Alembic, knowing that, runs SQLite's DDL outside one ("Will assume
    non-transactional DDL"). A revision that failed part way therefore left its
    first steps applied, and every restart failed on them: 0010 did exactly that
    to a real install on 2026-10-03, and it took a hand repair. SQLite itself
    can roll DDL back, so the transaction is handed to it instead: the driver's
    own handling off, BEGIN emitted here, and Alembic told the DDL is
    transactional (SQLAlchemy's documented recipe for pysqlite).

    Nothing in a migration may then need to run outside a transaction:
    `PRAGMA foreign_keys` is ignored inside one, and VACUUM refuses to run.
    """

    @event.listens_for(engine, "connect")
    def _driver_hands_off(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(connection):
        connection.exec_driver_sql("BEGIN")


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    sqlite = connectable.dialect.name == "sqlite"
    if sqlite:
        _transactional_sqlite(connectable)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # sqlite: ALTERs become table rebuilds
            # Postgres already is; SQLite is once the driver lets it be.
            **({"transactional_ddl": True} if sqlite else {}),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
