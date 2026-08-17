"""appkit.db — SQLAlchemy engine/session helpers built from settings.database.

Backend-agnostic: settings.database decides sqlite (default) or postgres.
Use `JSONType` for JSON columns and `dialect_insert()` for upserts so app code
stays portable across both.

Apps define models on `Base`, create a session factory once at startup, and use
`session_scope()` (or a FastAPI dependency) per request. Migrations are owned by
Alembic, not by `create_all` — keep schema changes reviewable in git.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import DatabaseSettings


class Base(DeclarativeBase):
    """Declarative base every app's models inherit from."""


# Portable JSON column type: plain JSON on sqlite, JSONB on postgres (identical
# DDL to what the pre-sqlite migrations produced, so nothing drifts).
JSONType = JSON().with_variant(JSONB(), "postgresql")


def ensure_utc(value):
    """Coerce a possibly-naive datetime to aware UTC.

    SQLite has no timezone type, so `DateTime(timezone=True)` columns come back
    *naive* on sqlite (aware on postgres). Apply this to any DB-read datetime
    before comparing it with `datetime.now(timezone.utc)` so the same code works
    on both backends (naive is assumed to be UTC, which is how appkit stores it).
    """
    import datetime as _dt

    if isinstance(value, _dt.datetime) and value.tzinfo is None:
        return value.replace(tzinfo=_dt.timezone.utc)
    return value


def dialect_insert(session: Session, table):
    """INSERT construct with `on_conflict_*` support for the session's backend.

    SQLite and Postgres expose the same on_conflict_do_nothing/do_update API,
    just from different dialect modules — pick the right one at runtime.
    """
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    else:
        from sqlalchemy.dialects.sqlite import insert as _insert
    return _insert(table)


def make_engine(db: DatabaseSettings) -> Engine:
    if getattr(db, "type", "postgres") == "sqlite":
        db.ensure_dirs()
        # check_same_thread=False: FastAPI serves sync endpoints from a thread
        # pool, so connections legitimately cross threads (SQLAlchemy's pool
        # serialises access).
        engine = create_engine(db.url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
            cur.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, much faster
            cur.execute("PRAGMA foreign_keys=ON")  # sqlite defaults FKs OFF
            cur.execute("PRAGMA busy_timeout=5000")  # wait, don't throw, on write contention
            cur.close()

        return engine
    # pool_pre_ping avoids handing out connections the server has already dropped.
    return create_engine(db.url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def make_session_factory(db: DatabaseSettings) -> sessionmaker[Session]:
    return sessionmaker(bind=make_engine(db), expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error, always close."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
