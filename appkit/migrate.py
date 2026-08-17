"""appkit.migrate — run Alembic migrations programmatically.

The deploy pattern: apps call `upgrade_to_head()` at startup, before serving
traffic. The kube_app module's init containers don't receive Secret env vars
(so they can't reach the DB); running in-process keeps migrations working with
zero module changes. Alembic's version table makes this idempotent, and our
apps are single-replica so there's no migration race.

Each app keeps its migration scripts inside the package (app/migrations) so
they ship in the image with no extra Dockerfile steps.
"""

from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config

from .config import DatabaseSettings

log = logging.getLogger(__name__)


def upgrade_to_head(db: DatabaseSettings, script_location: str) -> None:
    """Apply any pending migrations in `script_location` to the app's database."""
    db.ensure_dirs()  # sqlite: parent dir must exist before alembic connects
    cfg = Config()
    cfg.set_main_option("script_location", script_location)
    # configparser treats % as interpolation syntax — escape it (passwords).
    cfg.set_main_option("sqlalchemy.url", db.url.replace("%", "%%"))
    log.info("running alembic upgrade head (scripts: %s)", script_location)
    command.upgrade(cfg, "head")
