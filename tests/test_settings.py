"""How configuration is resolved.

The precedence is **environment > YAML file > field default**, and the
surprising part — the one that cost a wrong test when it was first written —
is where constructor keyword arguments sit: *last*, behind the YAML file. So

    PortfolioSettings(timezone="Europe/Berlin")

does not necessarily produce a Berlin timezone. It is the `init_settings`
source, and `settings_customise_sources` deliberately puts it after the file so
that a deployment's config.yaml cannot be overridden by a stray default in
code. Anything constructing settings in a test must go through the environment
instead, or it is asserting nothing.

These tests read against `tests/config.test.yaml`, which differs from the field
defaults on purpose (log_level WARNING vs INFO, 3 login attempts vs 8, the
price feed off rather than on) — those differences are what make each layer
observable.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from app.settings import DEFAULT_TIMEZONE, PortfolioSettings
from appkit.config import DatabaseSettings


def _isolate_database_env(monkeypatch) -> None:
    """Clear every APP_DATABASE__* variable.

    conftest sets these when the suite runs against Postgres, and they are
    higher priority than anything a test can express — so a test observing the
    file or default layers has to remove them all, not just the one it happens
    to remember.
    """
    for name in [k for k in __import__("os").environ if k.startswith("APP_DATABASE__")]:
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# The three layers
# --------------------------------------------------------------------------- #

def test_a_field_default_applies_when_nothing_else_sets_it():
    # `auth.cookie_name` appears in neither the environment nor the test YAML.
    assert PortfolioSettings().auth.cookie_name == "pf_session"


def test_the_yaml_file_beats_the_field_default():
    settings = PortfolioSettings()

    assert settings.log_level == "WARNING"                    # field default INFO
    assert settings.auth.rate_limit.max_attempts == 3         # field default 8
    assert settings.price_feed.enabled is False               # field default True


def test_the_environment_beats_the_yaml_file(monkeypatch):
    monkeypatch.setenv("APP_LOG_LEVEL", "DEBUG")

    assert PortfolioSettings().log_level == "DEBUG"            # YAML says WARNING


def test_a_nested_value_is_reachable_through_the_double_underscore(monkeypatch):
    """`APP_` prefix, `__` between levels — this is how the database password
    reaches the app from a Secret without ever being written to the file."""
    monkeypatch.setenv("APP_AUTH__RATE_LIMIT__MAX_ATTEMPTS", "11")

    assert PortfolioSettings().auth.rate_limit.max_attempts == 11   # YAML says 3


def test_constructor_arguments_lose_to_the_yaml_file():
    """The trap, pinned deliberately.

    A deployment's config.yaml must win over a value passed in code, so
    `init_settings` is the lowest-priority source. The consequence is that
    building settings with kwargs in a test proves nothing — go through the
    environment.
    """
    settings = PortfolioSettings(log_level="CRITICAL")

    assert settings.log_level == "WARNING"    # the file, not the argument


def test_constructor_arguments_do_apply_where_the_file_is_silent():
    """…and they are not ignored outright: with no file entry and no
    environment variable, an argument is all there is."""
    assert PortfolioSettings(auth={"cookie_name": "custom_cookie"}).auth.cookie_name == (
        "custom_cookie"
    )


def test_the_environment_beats_a_constructor_argument(monkeypatch):
    monkeypatch.setenv("APP_AUTH__COOKIE_NAME", "from_env")

    assert PortfolioSettings(auth={"cookie_name": "from_code"}).auth.cookie_name == "from_env"


# --------------------------------------------------------------------------- #
# Failing loudly
# --------------------------------------------------------------------------- #

def test_an_unknown_key_is_refused_rather_than_ignored():
    """`extra="forbid"`: a typo in the config stops the app at boot instead of
    silently keeping the default, which is the failure nobody notices.

    Note this catches a bad key in the FILE (or in code), not a stray `APP_*`
    environment variable — the environment source only reads variables matching
    a declared field, so a misspelled one is invisible rather than fatal. That
    asymmetry is worth knowing before trusting a typo to be caught."""
    with pytest.raises(ValidationError):
        PortfolioSettings(timezoen="Australia/Perth")   # deliberate typo


def test_a_misspelled_environment_variable_is_silently_ignored(monkeypatch):
    """The other half of the asymmetry above, pinned so nobody assumes
    otherwise: this is why the admin settings page validates what it writes."""
    monkeypatch.setenv("APP_TIMEZOEN", "Australia/Perth")

    assert PortfolioSettings().timezone == DEFAULT_TIMEZONE


def test_postgres_requires_a_name_and_user():
    with pytest.raises(ValidationError):
        DatabaseSettings(type="postgres", name="", user="")


def test_postgres_fields_without_an_explicit_type_are_refused():
    """The footgun this guard exists for: a config carrying Postgres fields but
    no `type` would default to sqlite and boot on an empty file, losing every
    row silently."""
    with pytest.raises(ValidationError, match="explicitly"):
        DatabaseSettings(name="portfolio", user="portfolio")


# --------------------------------------------------------------------------- #
# Derived values
# --------------------------------------------------------------------------- #

def test_the_timezone_defaults_to_the_documented_constant():
    """One constant, used by both the settings default and app/clock.py, so
    they cannot drift apart."""
    assert PortfolioSettings(app_name="x").timezone == DEFAULT_TIMEZONE


def test_the_sqlite_path_is_derived_from_the_app_name(monkeypatch):
    """With no path configured anywhere, the database lands at
    /data/<app_name>.db — which must be a mounted volume, not the ephemeral
    container filesystem.

    Isolated from tests/config.test.yaml (which sets a path of its own) by
    pointing the settings class at a file that does not exist: the only way to
    observe the field-default layer on its own.
    """
    _isolate_database_env(monkeypatch)

    class Isolated(PortfolioSettings):
        model_config = SettingsConfigDict(
            env_prefix="APP_", env_nested_delimiter="__",
            yaml_file="/nonexistent/config.yaml", extra="forbid",
        )

    assert Isolated(app_name="demo").database.path == "/data/demo.db"


# --------------------------------------------------------------------------- #
# Surviving a tidy-up
# --------------------------------------------------------------------------- #

def test_a_block_with_every_line_commented_out_still_starts(tmp_path, monkeypatch):
    """Commenting out the last option under a heading leaves the heading
    parsing as null, not as an absent key. Refusing to start on that would mean
    a config file that breaks by being tidied — which is how the shipped file
    was written before this was caught.
    """
    path = tmp_path / "config.yaml"
    path.write_text(
        "app_name: portfolio\n"
        "auth:\n"
        "  # rate_limit:\n"
        "  #   max_attempts: 8\n"
        "price_feed:\n"
        "  # enabled: true\n"
    )
    _isolate_database_env(monkeypatch)

    class Isolated(PortfolioSettings):
        model_config = SettingsConfigDict(
            env_prefix="APP_", env_nested_delimiter="__",
            yaml_file=str(path), extra="forbid",
        )

    settings = Isolated()

    assert settings.auth.rate_limit.max_attempts == 8   # the default, not a crash
    assert settings.price_feed.enabled is True


def test_the_shipped_config_file_loads(monkeypatch):
    """The documented config.yaml that ships with the app must actually work —
    it is the first thing anyone deploying this will read and copy."""
    from pathlib import Path as _Path

    shipped = _Path(__file__).parent.parent / "config.yaml"
    _isolate_database_env(monkeypatch)

    class Shipped(PortfolioSettings):
        model_config = SettingsConfigDict(
            env_prefix="APP_", env_nested_delimiter="__",
            yaml_file=str(shipped), extra="forbid",
        )

    settings = Shipped()

    assert settings.app_name == "stocktake"
    assert settings.database.type == "sqlite"
    # Commented-out options fall through to their documented defaults.
    assert settings.log_level == "INFO"
    assert settings.auth.session_ttl_days == 30
    assert settings.auth.rate_limit.max_attempts == 8
    # The shipped file carries NO broker overrides — a config override wins
    # over the file permanently, so shipping copies would freeze them. What
    # must still be true is that both formats are available, from the files.
    assert settings.imports.brokers == {}
    assert sorted(settings.imports.brokers_available()) == ["commsec", "selfwealth"]


#


# Starting with no configuration at all. Every setting has a default,
# including `app_name`, so the first-run wizard can boot and ask —
# decisions.md #33.

class _NoFileSettings(PortfolioSettings):
    """PortfolioSettings pointed at a config file that does not exist.

    The real class bakes `yaml_file` in at import from APP_CONFIG_FILE, so
    changing the environment in a test cannot move it — a subclass is the only
    honest way to observe the no-file case.
    """

    model_config = SettingsConfigDict(yaml_file="/nonexistent/portfolio.yaml")


def test_the_app_starts_with_no_config_file(monkeypatch):
    """The premise the whole wizard rests on. If this fails, a fresh container
    dies at import and there is no page to show anybody."""
    _isolate_database_env(monkeypatch)

    settings = _NoFileSettings()

    assert settings.app_name == "stocktake"
    assert settings.timezone == DEFAULT_TIMEZONE


def test_what_it_boots_into_is_a_working_app(monkeypatch):
    """Not merely that it starts — that the defaults are a usable install, and
    a safe one: plain HTTP assumed, and no proxy believed until told."""
    _isolate_database_env(monkeypatch)

    settings = _NoFileSettings()

    assert settings.price_feed.enabled is True
    assert settings.auth.session_idle_minutes == 60
    assert settings.auth.cookie_secure is False
    assert settings.auth.trusted_proxies == []
    assert settings.maintenance.enabled is True
    assert settings.imports.max_upload_mb == 10


def test_the_helm_chart_declares_the_version_this_release_is():
    """Chart.yaml's appVersion must equal the app's version.

    values.yaml ships `tag: ""`, which the deployment resolves to
    .Chart.AppVersion — so an appVersion left behind at the previous release
    does not fail, it quietly deploys the OLD image to everybody using the
    chart. Nothing in the release workflow bumps this, so the check has to be
    here.
    """
    import tomllib
    from pathlib import Path

    from ruamel.yaml import YAML

    root = Path(__file__).resolve().parent.parent
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    chart = YAML(typ="safe").load((root / "deploy/helm/stocktake/Chart.yaml").read_text())
    assert str(chart["appVersion"]) == version, (
        f"Chart.yaml appVersion is {chart['appVersion']!r} but the app is "
        f"{version!r} — bump it with the release")


def test_the_compose_copy_of_the_config_has_not_drifted():
    """deploy/compose/config.yaml must be the same file as the shipped default.

    It is a reference copy — the compose README tells people it documents every
    option — so when it drifts it documents an app that does not exist. It had:
    example hostnames the root file did not, and it was missing the `features:`
    and `ocr:` blocks entirely, so two shipped features were undocumented for
    anyone reading the copy rather than the original.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    shipped = (root / "config.yaml").read_text()
    copy = (root / "deploy" / "compose" / "config.yaml").read_text()
    assert copy == shipped, (
        "deploy/compose/config.yaml has drifted from config.yaml — copy the "
        "root file over it rather than editing one of them")


def test_the_annotated_default_config_ships_for_the_wizard_to_write_from():
    """The wizard produces a config by editing this file, so it must exist and
    still parse. The Dockerfile copies it in as config.default.yaml."""
    from pathlib import Path

    from ruamel.yaml import YAML

    default = Path(__file__).resolve().parent.parent / "config.yaml"
    assert default.is_file(), "the annotated default config must ship"

    # Almost every line is a comment — that is the point of it — but it has to
    # be valid YAML that loads to a mapping or to nothing.
    parsed = YAML(typ="safe").load(default.read_text())
    assert parsed is None or isinstance(parsed, dict)
