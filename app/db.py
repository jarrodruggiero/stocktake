"""The database connection, made when it is known rather than at import.

A wizard cannot offer a choice the process has already made, so connecting is
deferred. Three states: **not configured** (serve the wizard, refuse the rest),
**connected**, and **failed** — which is deliberately distinct, because the
answer is "fix this" rather than "choose one" (decisions.md #75).

`connect()` is re-entrant; the wizard calls it after choosing. `source()` reads
the two places a value can come from, because `DatabaseSettings` defaults to
SQLite and so cannot express "not configured" on its own.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from appcore import make_session_factory, upgrade_to_head
from appcore.config import DatabaseSettings

from . import tenancy

log = logging.getLogger(__name__)

ENV_PREFIX = "APP_DATABASE__"

NOT_CONFIGURED = "not configured"
FROM_ENVIRONMENT = "environment"
FROM_FILE = "config file"


# --------------------------------------------------------------------------- #
# Where did the setting come from?
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Source:
    """Whether a database was chosen, and by whom."""

    where: str                  # NOT_CONFIGURED | FROM_ENVIRONMENT | FROM_FILE
    detail: str = ""            # the sentence the wizard shows

    @property
    def configured(self) -> bool:
        return self.where != NOT_CONFIGURED


def source(settings) -> Source:
    """Report whether the database was set, and where it was set.

    The wizard uses this to decide between offering a choice and stating a
    fact. Stating the fact matters: someone whose Kubernetes deployment sets
    `APP_DATABASE__HOST` should be told that, not quietly handed a form whose
    every value would be ignored.
    """
    env_names = sorted(name for name in os.environ if name.startswith(ENV_PREFIX))
    if env_names:
        return Source(FROM_ENVIRONMENT,
                      f"set by the {', '.join(env_names)} environment "
                      f"variable{'s' if len(env_names) > 1 else ''}")

    from . import configfile

    try:
        written = configfile.load()
    except Exception:            # a malformed file is the admin page's problem
        written = {}
    if isinstance(written, dict) and written.get("database"):
        return Source(FROM_FILE, f"set in {configfile.config_path()}")

    return Source(NOT_CONFIGURED)


def describe(db: DatabaseSettings) -> str:
    """One line naming what a settings object points at, without its password."""
    if db.type == "sqlite":
        return f"SQLite at {db.path}"
    return f"Postgres — {db.name} on {db.host}:{db.port} as {db.user}"


# --------------------------------------------------------------------------- #
# Trying it before committing to it
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Probe:
    ok: bool
    detail: str


def probe(db: DatabaseSettings) -> Probe:
    """Connect, and say what happened — in the words of somebody who has to fix
    it, not in the words of a driver traceback.

    This runs before anything is written down. The wizard's "Test connection"
    button is the visible half; the invisible half is that choosing a database
    that does not work would otherwise be discovered at the next restart, with
    the config file already saved and the app no longer able to boot.
    """
    from sqlalchemy import create_engine, text

    if db.type == "sqlite":
        problem = sqlite_path_problem(db.path)
        if problem:
            return Probe(False, problem)

    engine = None
    try:
        engine = create_engine(db.url, pool_pre_ping=True)
        with engine.connect() as conn:
            if db.type == "sqlite":
                conn.execute(text("select 1"))
                return Probe(True, f"SQLite is usable at {db.path}.")
            version = conn.execute(text("show server_version")).scalar()
            return Probe(True, f"Connected to PostgreSQL {version}.")
    except Exception as exc:
        return Probe(False, _explain(db, exc))
    finally:
        if engine is not None:
            engine.dispose()


def sqlite_path_problem(path: str | None) -> str | None:
    """Whether a SQLite file could be created there, checked before connecting.

    SQLite is happy to *open* a path it cannot write and only fails later, on
    the first write — which in a container means the wizard appears to succeed
    and the app dies on the next request. The directory is the real question,
    and on a container it is usually an unmounted volume.
    """
    if not path:
        return "No database file path was given."
    target = Path(path)
    if target.is_dir():
        return f"{path} is a directory, not a database file."
    parent = target.parent
    if not parent.exists():
        return (f"The directory {parent} does not exist. In a container this "
                f"usually means no volume is mounted there — check the volume "
                f"mapping before continuing, or the database will not survive "
                f"a restart.")
    if not os.access(parent, os.W_OK):
        return (f"The directory {parent} is not writable by this process "
                f"(running as uid {os.getuid()}). Fix the ownership of the "
                f"mounted volume, or choose a path the app can write.")
    if target.exists() and not os.access(target, os.W_OK):
        return f"{path} exists but is not writable by this process."
    return None


# Driver wordings, per thing that is actually wrong. Two libraries and several
# versions say each of these differently — psycopg3 says "failed to resolve
# host" where libpq says "could not translate host name" — so match on a set of
# phrases rather than on one, and keep the raw text as the fallback.
_UNRESOLVED = ("could not translate host name", "name or service not known",
               "failed to resolve host", "nodename nor servname",
               "temporary failure in name resolution")
_REFUSED = ("connection refused", "could not connect to server")
_BAD_PASSWORD = ("password authentication failed", "no password supplied",
                 "authentication failed for user")


def _explain(db: DatabaseSettings, exc: Exception) -> str:
    """Turn a driver error into something actionable.

    The raw text is the fallback rather than the answer: "(psycopg.
    OperationalError) connection failed: :1 (::1), port 5432 failed" tells
    somebody setting this up for the first time nothing they can act on.
    """
    raw = str(exc).strip()
    # SQLAlchemy prefixes the driver's exception class and appends a link to
    # its own error docs. Neither helps here.
    raw = re.sub(r"^\([\w.]+\)\s*", "", raw.splitlines()[0]) if raw else type(exc).__name__
    if db.type != "postgres":
        return raw

    lowered = raw.lower()
    if any(phrase in lowered for phrase in _UNRESOLVED):
        return (f"The hostname {db.host!r} could not be resolved from inside this "
                f"container. Check the spelling — and if the database is another "
                f"container, that both are on the same network.")
    if any(phrase in lowered for phrase in _BAD_PASSWORD):
        return f"The server refused the credentials for user {db.user!r}."
    if any(phrase in lowered for phrase in _REFUSED):
        return (f"Nothing accepted a connection on {db.host}:{db.port}. Check the "
                f"port, that the server is running, and that it allows "
                f"connections from this address.")
    if "does not exist" in lowered and db.name in raw:
        return (f"Connected to the server, but it has no database called "
                f"{db.name!r}. Create it first — the app builds its own tables, "
                f"but it will not create the database itself.")
    if "timeout" in lowered or "timed out" in lowered:
        return (f"The connection to {db.host}:{db.port} timed out. That usually "
                f"means a firewall is dropping it rather than refusing it.")
    return raw


# --------------------------------------------------------------------------- #
# The connection itself
# --------------------------------------------------------------------------- #

class NotConfigured(RuntimeError):
    """Raised when something asks for a session before there is a database.

    Every route except the wizard is gated behind `is_ready()`, so this is a
    programming error rather than a user-facing state — which is why it says
    what to do about it.
    """


_factory = None
_current: DatabaseSettings | None = None
_migrations: str | None = None


def set_migrations(path: str) -> None:
    """Where the Alembic scripts live. Set once, at import of the app."""
    global _migrations
    _migrations = path


def connect(db: DatabaseSettings, *, migrate: bool = True) -> None:
    """Run migrations and build the session factory, replacing any current one.

    Called at startup when a database was already configured, and again by the
    wizard when someone chooses one. Migrating here rather than at import is
    the point: a database that does not exist yet cannot be migrated, and one
    chosen thirty seconds ago has to be.
    """
    global _factory, _current

    if migrate:
        if _migrations is None:
            raise RuntimeError("set_migrations() must be called before connect()")
        upgrade_to_head(db, _migrations)
    factory = make_session_factory(db)
    # The tenancy filter is attached per sessionmaker, so a replacement factory
    # needs its own listeners — without this, a database chosen in the wizard
    # would serve sessions with no portfolio scoping at all.
    tenancy.install(factory)
    _factory = factory
    _current = db
    log.info("database connected: %s", describe(db))


def session():
    """A new session, or a refusal that says why."""
    if _factory is None:
        raise NotConfigured(
            "No database is configured. Complete the setup wizard, or set "
            "APP_DATABASE__* / a database: block in config.yaml."
        )
    return _factory()


def factory():
    """The sessionmaker itself, for the places that need to pass one around."""
    if _factory is None:
        raise NotConfigured("No database is configured.")
    return _factory


def is_ready() -> bool:
    return _factory is not None


def current() -> DatabaseSettings | None:
    """What we are connected to, for the pages that report it."""
    return _current


def reset() -> None:
    """Forget the connection. For tests, and for a wizard step that failed
    after connecting."""
    global _factory, _current
    _factory = None
    _current = None
