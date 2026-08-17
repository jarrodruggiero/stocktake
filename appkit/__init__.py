"""appkit — the scaffold this app is built on.

Config loading, the database connection and the FastAPI app factory, kept
separate from `app/` because none of it is about portfolios. Vendored rather
than published: it is small, it has exactly one consumer, and a separate
release cycle would buy nothing.
"""

from .app import create_app
from .config import BaseAppSettings, DatabaseSettings, load_config
from .db import (
    Base,
    JSONType,
    dialect_insert,
    ensure_utc,
    make_engine,
    make_session_factory,
    session_scope,
)
from .migrate import upgrade_to_head

__all__ = [
    "create_app",
    "BaseAppSettings",
    "DatabaseSettings",
    "load_config",
    "Base",
    "JSONType",
    "dialect_insert",
    "ensure_utc",
    "make_engine",
    "make_session_factory",
    "session_scope",
    "upgrade_to_head",
]
