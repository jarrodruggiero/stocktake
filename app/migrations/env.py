"""Alembic environment: migrates against the app's own settings-derived URL."""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

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


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # sqlite: ALTERs become table rebuilds
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
