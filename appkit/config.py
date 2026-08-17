"""appkit.config — load a YAML file into a validated, typed settings object.

The pattern every app follows:
  * functional config lives in `config.yaml` (a ConfigMap in k8s, git in the repo)
  * secrets arrive as env vars, never from the file, and OVERRIDE the YAML
  * everything is validated by pydantic at boot — bad/missing config fails loudly

Nested values use the `APP_` prefix and `__` delimiter, so the DB password comes
from `APP_DATABASE__PASSWORD` and fills/overrides `database.password` in the YAML.
"""

from __future__ import annotations

import os
from typing import Literal, Type, TypeVar
from urllib.parse import quote

from pydantic import BaseModel, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

# Path is overridable for local dev (e.g. APP_CONFIG_FILE=./config.yaml).
CONFIG_FILE = os.environ.get("APP_CONFIG_FILE", "/config/config.yaml")


class DatabaseSettings(BaseModel):
    """This app's own database.

    Default is a per-pod SQLite file (the *arr model): zero external moving
    parts, entirely adequate for single-user internal apps. Set
    `type: postgres` to opt back into an external/shared server — everything
    downstream (SQLAlchemy, Alembic) works off `url`, so nothing else changes.
    """

    type: Literal["sqlite", "postgres"] = "sqlite"

    # --- sqlite ---
    # Defaults to /data/<app_name>.db (filled in by BaseAppSettings, which knows
    # the app name). /data must be a PersistentVolumeClaim — /config holds the
    # read-only ConfigMap over an otherwise ephemeral FS, so a DB file there
    # would be lost on every pod restart. Override to move the file.
    path: str | None = None

    # --- postgres (only read when type: postgres) ---
    host: str = "postgres.databases.svc.cluster.local"
    port: int = 5432
    name: str = ""
    user: str = ""
    password: str = ""  # injected via APP_DATABASE__PASSWORD (never in YAML)
    sslmode: str = "disable"  # in-cluster traffic; tighten if that changes

    @model_validator(mode="after")
    def _require_postgres_identity(self) -> "DatabaseSettings":
        if self.type == "postgres" and (not self.name or not self.user):
            raise ValueError("database.type=postgres requires database.name and database.user")
        return self

    @model_validator(mode="after")
    def _guard_ambiguous_sqlite(self) -> "DatabaseSettings":
        # Fail fast on the footgun: a config with Postgres-only fields (name/user)
        # but no explicit type defaults to sqlite and silently ignores them,
        # booting on an empty file. Force the intent to be explicit.
        if self.type == "sqlite" and (self.name or self.user):
            raise ValueError(
                "database has Postgres fields (name/user) but type is sqlite "
                "(the default) — set 'type: postgres' explicitly, or remove "
                "name/user. Refusing to silently use an empty sqlite file."
            )
        return self

    @property
    def url(self) -> str:
        """SQLAlchemy URL for the configured backend.

        Postgres uses the psycopg (v3) driver. User/password are
        percent-encoded: the provisioned passwords include URL-reserved
        characters (#, ?, %, ...) that would break a raw URL.
        """
        if self.type == "sqlite":
            if not self.path:
                raise ValueError("database.path is required for sqlite (no app_name to derive it from)")
            return f"sqlite:///{self.path}"
        return (
            f"postgresql+psycopg://{quote(self.user, safe='')}:{quote(self.password, safe='')}"
            f"@{self.host}:{self.port}/{self.name}?sslmode={self.sslmode}"
        )

    def ensure_dirs(self) -> None:
        """Create the sqlite file's parent directory (no-op for postgres)."""
        if self.type == "sqlite" and self.path:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)


class BaseAppSettings(BaseSettings):
    """Base every app subclasses. Source priority: env > YAML > field defaults."""

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_nested_delimiter="__",
        yaml_file=CONFIG_FILE,
        extra="forbid",  # typo in the YAML → boot error, not a silent no-op
    )

    app_name: str
    log_level: str = "INFO"
    # Omitting the whole block gets you sqlite at /data/<app_name>.db (a PVC).
    database: DatabaseSettings = DatabaseSettings()

    @model_validator(mode="after")
    def _default_sqlite_path(self) -> "BaseAppSettings":
        if self.database.type == "sqlite" and self.database.path is None:
            self.database.path = f"/data/{self.app_name}.db"
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # First source wins on conflict → env overrides YAML overrides defaults.
        return (
            env_settings,
            YamlConfigSettingsSource(settings_cls),
            init_settings,
        )


T = TypeVar("T", bound=BaseAppSettings)


def load_config(model: Type[T]) -> T:
    """Instantiate an app's settings model (reads YAML + env per the sources above)."""
    return model()
