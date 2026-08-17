"""Verify a migration against a POPULATED predecessor, on either backend.

    ./tools/verify_migration.py 0014            # sqlite
    PG=1 ./tools/verify_migration.py 0014       # postgres

Every migration in this project gets this before it ships, and the reason is
that a green test suite does not cover it: the suite builds every schema from
nothing at head, so it passes even for a migration that would destroy existing
rows. This walks the path a real deployment takes instead — stop at the
predecessor, put data in, upgrade, check the data survived and the new columns
behave, downgrade, re-upgrade.

The generic checks below catch the usual failures. **Anything specific to the
migration under test still needs adding by hand** — 0014's backfill, for
instance, had to be checked for giving every row a *distinct* UUID, because the
tempting one-line version gives them all the same one and that would make every
account the same person to any app federating with this one. Generic checks
would have called that a pass.

Add those in `extra_checks()`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402

SCRIPTS = str(APP_ROOT / "app" / "migrations")

# A user row that satisfies every NOT NULL as at 0014. Extend it when a
# migration adds one — that is the usual reason this script suddenly fails on
# the INSERT rather than on a check.
USER_INSERT = (
    'insert into "user" (id, email, name, password_hash, is_admin,'
    " must_change_password, created_at, theme, accent, nav_style,"
    " charts_seeded, is_active, public_id)"
    " values (:id, :email, :name, 'x', :admin, false,"
    " '2026-01-01 00:00:00', 'auto', 'blue', 'both', false, true, :public_id)"
)


def cfg(url: str) -> Config:
    c = Config()
    c.set_main_option("script_location", SCRIPTS)
    c.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return c


def check(label: str, ok: bool) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        raise SystemExit(f"FAILED: {label}")


def columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def version_of(engine) -> str:
    with engine.begin() as conn:
        return conn.execute(text("select version_num from alembic_version")).scalar()


def predecessor(target: str) -> str:
    script = ScriptDirectory.from_config(cfg("sqlite://"))
    rev = script.get_revision(target)
    if not rev.down_revision:
        raise SystemExit(f"{target} has no predecessor — it is the base revision.")
    return rev.down_revision


def new_columns(engine_before: set[str], engine_after: set[str]) -> set[str]:
    return engine_after - engine_before


def extra_checks(engine, target: str) -> None:
    """Per-migration assertions. Add to this rather than trusting the generic
    ones — they cannot know what the migration was *for*."""
    if target == "0014":
        import uuid
        with engine.begin() as conn:
            ids = [r[0] for r in conn.execute(text('select public_id from "user"'))]
        check("every public_id is distinct (not one value copied across)",
              len(set(ids)) == len(ids))
        check("all v4 UUIDs", all(uuid.UUID(i).version == 4 for i in ids))


def run(url: str, dialect: str, target: str) -> None:
    previous = predecessor(target)
    print(f"\n=== {dialect}: {previous} -> {target} ===")
    c = cfg(url)
    engine = create_engine(url)

    # 1. A database at the predecessor, as a live deployment would be.
    command.upgrade(c, previous)
    check(f"stopped at {previous}", version_of(engine) == previous)
    before_user = columns(engine, "user")
    before_session = columns(engine, "user_session")

    # 2. Populate it. Several rows, because "gave every row the same value" is
    #    a failure one row cannot show.
    with engine.begin() as conn:
        import uuid as _u
        for i in range(1, 4):
            params = {"id": i, "email": f"u{i}@example.test", "name": f"u{i}",
                      "admin": i == 1, "public_id": str(_u.uuid4())}
            statement = USER_INSERT
            if "public_id" not in before_user:
                statement = (statement.replace(", public_id)", ")")
                             .replace(", :public_id)", ")"))
                params.pop("public_id")
            conn.execute(text(statement), params)
    with engine.begin() as conn:
        check("rows inserted at the predecessor",
              conn.execute(text('select count(*) from "user"')).scalar() == 3)

    # 3. Upgrade.
    command.upgrade(c, target)
    check(f"at {target}", version_of(engine) == target)
    with engine.begin() as conn:
        emails = [r[0] for r in conn.execute(text('select email from "user" order by id'))]
    check("every pre-existing row survived",
          emails == ["u1@example.test", "u2@example.test", "u3@example.test"])

    added = (new_columns(before_user, columns(engine, "user"))
             | new_columns(before_session, columns(engine, "user_session")))
    print(f"  ...  columns added: {sorted(added) or 'none'}")
    extra_checks(engine, target)

    # 4. Downgrade. The change goes; the rows must not.
    command.downgrade(c, previous)
    check(f"back at {previous}", version_of(engine) == previous)
    with engine.begin() as conn:
        after = [r[0] for r in conn.execute(text('select email from "user" order by id'))]
    check("rows survived the downgrade too", after == emails)
    # SQLite has no DROP COLUMN before 3.35, so alembic rebuilds the table —
    # the neighbours are worth checking, not just the row count.
    with engine.begin() as conn:
        row = conn.execute(text(
            'select name, is_admin, theme from "user" where id = 1')).one()
    check("neighbouring columns intact after the table rebuild",
          row[0] == "u1" and bool(row[1]) is True and row[2] == "auto")

    # 5. Repeatable, not one-way.
    command.upgrade(c, target)
    check(f"re-upgraded to {target}", version_of(engine) == target)
    command.upgrade(c, "head")
    print(f"  ...  head is {version_of(engine)}")
    engine.dispose()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    target = sys.argv[1]
    if os.environ.get("PG"):
        base = "postgresql+psycopg://postgres:postgres@localhost:55432/"
        admin = create_engine(base + "postgres", isolation_level="AUTOCOMMIT")
        name = f"verify_{target}"
        with admin.begin() as conn:
            conn.execute(text(f"drop database if exists {name}"))
            conn.execute(text(f"create database {name}"))
        admin.dispose()
        run(base + name, "postgres", target)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            run(f"sqlite:///{tmp}/verify.db", "sqlite", target)
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
