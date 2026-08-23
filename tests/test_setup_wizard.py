"""The first-run wizard: what it asks, what it writes, and when it closes.

Two guarantees run through everything here, both the kind a green suite can
hide:

  * **Nothing is written until it has been shown to work** (decisions.md #65).
    A half-written config file is worse than none: the app boots into it and the
    wizard never runs again.
  * **The wizard closes behind itself.** Its early steps are public, so the only
    thing stopping `/setup/environment` being an open endpoint for setting
    trusted proxies is the per-step gate (decisions.md #92). Tested from both
    sides.

The state the wizard exists for — no config and no database — cannot be reached
from this suite, because conftest builds a configured database before
`app.main` is importable. One test reaches it in a fresh interpreter.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import select

from app import configfile, setupwizard
from app import db as database
from app.models import Portfolio, User
from appkit.config import DatabaseSettings

APP_ROOT = Path(__file__).resolve().parent.parent
HTML = {"accept": "text/html"}
PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def _fresh_draft():
    """A wizard draft per test.

    It is module-level state by design (see setupwizard's docstring), which
    means one test's answers would otherwise be the next test's starting point.
    """
    setupwizard.discard()
    yield
    setupwizard.discard()


# --------------------------------------------------------------------------- #
# Where a database setting came from
# --------------------------------------------------------------------------- #

def test_an_environment_variable_is_reported_as_the_source(monkeypatch, app_module):
    settings = app_module.settings
    monkeypatch.setenv("APP_DATABASE__HOST", "pg.internal")

    source = database.source(settings)

    assert source.configured is True
    assert source.where == database.FROM_ENVIRONMENT
    # Naming the variable matters: "set by your deployment" leaves someone
    # hunting for which of four places it came from.
    assert "APP_DATABASE__HOST" in source.detail


def test_a_config_file_entry_is_reported_as_the_source(monkeypatch, tmp_path, app_module):
    settings = app_module.settings
    for name in [k for k in __import__("os").environ if k.startswith("APP_DATABASE__")]:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("app_name: portfolio\ndatabase:\n  type: sqlite\n  path: /data/x.db\n")
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(path))

    source = database.source(settings)

    assert source.where == database.FROM_FILE
    assert str(path) in source.detail


def test_nothing_anywhere_reads_as_not_configured(monkeypatch, tmp_path, app_module):
    """The state the wizard is for. `DatabaseSettings` defaults to SQLite, so
    the settings object alone cannot tell you this — only the two places a
    human could have written it down can."""
    settings = app_module.settings
    for name in [k for k in __import__("os").environ if k.startswith("APP_DATABASE__")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(tmp_path / "absent.yaml"))

    source = database.source(settings)

    assert source.configured is False
    assert source.where == database.NOT_CONFIGURED


def test_a_config_file_without_a_database_block_is_not_configured(
        monkeypatch, tmp_path, app_module):
    """A file can exist and still not choose a database — the shipped default
    is exactly that, with every line commented out."""
    settings = app_module.settings
    for name in [k for k in __import__("os").environ if k.startswith("APP_DATABASE__")]:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("app_name: portfolio\ntimezone: Australia/Perth\n")
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(path))

    assert database.source(settings).configured is False


def test_describe_never_includes_the_password():
    """It is rendered on a page and written to the log."""
    db = DatabaseSettings(type="postgres", host="pg", port=5432, name="portfolio",
                          user="portfolio", password="hunter2")

    assert "hunter2" not in database.describe(db)


# --------------------------------------------------------------------------- #
# Trying a database before committing to it
# --------------------------------------------------------------------------- #

def test_a_writable_sqlite_path_passes(tmp_path):
    result = database.probe(DatabaseSettings(type="sqlite",
                                             path=str(tmp_path / "portfolio.db")))

    assert result.ok is True


def test_a_sqlite_path_whose_directory_is_missing_is_refused(tmp_path):
    """The container case, and the reason this is checked before connecting:
    SQLite opens a path it cannot write and only fails on the first write, so
    without this the wizard would appear to succeed and the app would die on
    the next request."""
    result = database.probe(DatabaseSettings(type="sqlite",
                                             path=str(tmp_path / "nope" / "p.db")))

    assert result.ok is False
    assert "does not exist" in result.detail
    # It has to name the likely cause, or the reader is left guessing.
    assert "volume" in result.detail


def test_a_sqlite_path_in_a_read_only_directory_is_refused(tmp_path):
    import os
    import stat

    directory = tmp_path / "ro"
    directory.mkdir()
    original = stat.S_IMODE(os.stat(directory).st_mode)
    os.chmod(directory, 0o500)
    try:
        result = database.probe(DatabaseSettings(type="sqlite",
                                                 path=str(directory / "p.db")))
    finally:
        os.chmod(directory, original)

    assert result.ok is False
    assert "not writable" in result.detail


def test_a_directory_given_as_the_database_file_is_refused(tmp_path):
    result = database.probe(DatabaseSettings(type="sqlite", path=str(tmp_path)))

    assert result.ok is False
    assert "is a directory" in result.detail


def test_an_unresolvable_postgres_host_is_explained_not_dumped():
    """The driver says "failed to resolve host 'x': [Errno 8] nodename nor
    servname provided". That is not an error message for somebody typing a
    hostname into a setup wizard for the first time."""
    result = database.probe(DatabaseSettings(
        type="postgres", host="no-such-host.invalid", port=5432,
        name="portfolio", user="portfolio", password="x"))

    assert result.ok is False
    assert "could not be resolved" in result.detail
    assert "Errno" not in result.detail
    assert "psycopg" not in result.detail


def test_a_refused_postgres_connection_is_explained():
    """Port 1 has nothing on it, on any machine that will ever run this."""
    result = database.probe(DatabaseSettings(
        type="postgres", host="127.0.0.1", port=1,
        name="portfolio", user="portfolio", password="x"))

    assert result.ok is False
    assert "Nothing accepted a connection" in result.detail


def test_a_successful_postgres_probe_reports_the_server_version():
    """Proof the Postgres path is exercised and not merely the error branches.

    Skipped on the SQLite run rather than pointed at a hardcoded port: a test
    that silently passes because nothing was listening is worse than no test.
    """
    import os

    if os.environ.get("STOCKTAKE_TEST_DB") != "postgres":
        pytest.skip("needs the Postgres backend")

    result = database.probe(DatabaseSettings(
        type="postgres",
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5432")),
        name=os.environ.get("PGDATABASE", "stocktake_test"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", "postgres")))

    assert result.ok is True
    assert "PostgreSQL" in result.detail


def test_a_bad_postgres_password_is_explained():
    """`_explain` maps the driver's phrasings, and there are several per
    problem — psycopg and libpq word the same failure differently."""
    db = DatabaseSettings(type="postgres", host="pg", port=5432, name="portfolio",
                          user="portfolio", password="x")

    assert "refused the credentials" in database._explain(
        db, Exception("password authentication failed for user \"portfolio\""))


def test_a_missing_postgres_database_says_to_create_it():
    """The app builds its own tables but will not create the database. Somebody
    who does not know that reads "does not exist" as a bug."""
    db = DatabaseSettings(type="postgres", host="pg", port=5432, name="portfolio",
                          user="portfolio", password="x")

    detail = database._explain(db, Exception('database "portfolio" does not exist'))

    assert "Create it first" in detail


def test_a_timed_out_connection_is_distinguished_from_a_refused_one():
    """Different causes: refused is nothing listening, timed out is a firewall
    dropping packets. Saying "check the port" for the second wastes an hour."""
    db = DatabaseSettings(type="postgres", host="pg", port=5432, name="portfolio",
                          user="portfolio", password="x")

    assert "firewall" in database._explain(db, Exception("connection timed out"))


def test_an_unrecognised_error_falls_back_to_the_raw_text():
    """Better a driver message than a wrong guess — but with SQLAlchemy's
    exception-class prefix stripped, because that part helps nobody."""
    db = DatabaseSettings(type="postgres", host="pg", port=5432, name="portfolio",
                          user="portfolio", password="x")

    detail = database._explain(db, Exception("(psycopg.OperationalError) something odd"))

    assert detail == "something odd"


def test_a_sqlite_error_is_not_run_through_the_postgres_explanations():
    db = DatabaseSettings(type="sqlite", path="/data/p.db")

    assert database._explain(db, Exception("disk I/O error")) == "disk I/O error"


def test_an_empty_sqlite_path_is_refused():
    assert "No database file path" in database.sqlite_path_problem("")


def test_an_unwritable_existing_sqlite_file_is_refused(tmp_path):
    """Distinct from an unwritable directory: the file is already there and the
    process cannot write it, which is the wrong-ownership case on a volume
    restored from a backup."""
    import os
    import stat

    target = tmp_path / "portfolio.db"
    target.write_text("")
    original = stat.S_IMODE(os.stat(target).st_mode)
    os.chmod(target, 0o400)
    try:
        problem = database.sqlite_path_problem(str(target))
    finally:
        os.chmod(target, original)

    assert problem is not None and "not writable" in problem


def test_a_malformed_config_file_does_not_stop_the_wizard_starting(
        monkeypatch, tmp_path, app_module):
    """A broken config is the admin page's problem to report. Here it must not
    prevent the one page that could fix it from rendering."""
    for name in [k for k in __import__("os").environ if k.startswith("APP_DATABASE__")]:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("auth: [this is not\n  valid: yaml\n")
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(path))

    assert database.source(app_module.settings).configured is False


def test_asking_for_a_session_before_there_is_one_says_what_to_do():
    """Every route but the wizard is gated on `is_ready()`, so this is a
    programming error — and one whose message should not be `NoneType is not
    callable`."""
    saved = database._factory
    database.reset()
    try:
        with pytest.raises(database.NotConfigured) as caught:
            database.session()
        with pytest.raises(database.NotConfigured):
            database.factory()
    finally:
        database._factory = saved

    assert "setup wizard" in str(caught.value)


def test_connecting_before_the_migration_path_is_known_is_a_loud_failure():
    """`set_migrations()` is called once at import. If a future refactor moved
    the connect earlier, silently skipping migrations would produce a database
    with no tables and errors nowhere near the cause."""
    saved = database._migrations
    database._migrations = None
    try:
        with pytest.raises(RuntimeError, match="set_migrations"):
            database.connect(DatabaseSettings(type="sqlite", path="/tmp/never.db"))
    finally:
        database._migrations = saved


# --------------------------------------------------------------------------- #
# The steps, as pure logic
# --------------------------------------------------------------------------- #

def test_the_first_step_is_the_welcome():
    assert setupwizard.next_step(database_configured=True, account_exists=False) == "welcome"


def test_a_configured_database_skips_that_step():
    setupwizard.draft().completed("welcome")

    assert setupwizard.next_step(database_configured=True,
                                 account_exists=False) == "account"


def test_an_unconfigured_database_comes_before_the_account():
    """The order is a real dependency, not a preference: the account is a row,
    and there is nowhere to put it yet."""
    setupwizard.draft().completed("welcome")

    assert setupwizard.next_step(database_configured=False,
                                 account_exists=False) == "database"


def test_the_settings_steps_follow_the_account():
    draft = setupwizard.draft()
    draft.completed("welcome")

    assert setupwizard.next_step(database_configured=True,
                                 account_exists=True) == "recovery"
    draft.completed("recovery")
    assert setupwizard.next_step(database_configured=True, account_exists=True) == "2fa"
    draft.completed("2fa")
    assert setupwizard.next_step(database_configured=True, account_exists=True) == "portfolio"
    draft.completed("portfolio")
    assert setupwizard.next_step(database_configured=True,
                                 account_exists=True) == "features"
    draft.completed("features")
    assert setupwizard.next_step(database_configured=True,
                                 account_exists=True) == "environment"
    draft.completed("environment")
    assert setupwizard.next_step(database_configured=True, account_exists=True) == "finish"


def test_a_step_that_does_not_apply_is_left_out_of_the_rail():
    """Removed, not greyed with "not needed": a deployment that
    supplies its own database and config file should not be looking at nine
    steps, four of which it will never visit."""
    setupwizard.discard()
    rail = setupwizard.progress("account", skip={"database"})

    assert "database" not in [row["key"] for row in rail]


def test_the_remaining_steps_are_numbered_as_they_are_shown():
    """A rail reading 1, 3, 4 is a rail that has lost something."""
    setupwizard.discard()
    rail = setupwizard.progress("account", skip={"database"})

    assert [row["number"] for row in rail] == list(range(1, len(rail) + 1))


def test_a_step_you_completed_is_never_reclassified_as_skipped():
    """The bug the reversal above depended on fixing.

    `_skipped_steps()` asks whether a database is *configured* — and after the
    database step there is one. So the step you had just finished was marked
    "not needed", and once skipped steps are hidden it would have disappeared
    from the rail the moment you completed it. That is the "changing shape
    under you" the greying was working around, and this was its cause.
    """
    setupwizard.discard()
    draft = setupwizard.draft()
    draft.completed("welcome")
    draft.completed("database")

    rail = setupwizard.progress("account", skip={"database"})
    states = {row["key"]: row["state"] for row in rail}

    assert states["database"] == "done", "a completed step was hidden as skipped"


def test_the_rail_marks_earlier_steps_done():
    rail = setupwizard.progress("portfolio")
    states = {row["key"]: row["state"] for row in rail}

    assert states["welcome"] == "done"
    assert states["portfolio"] == "active"
    assert states["finish"] == "todo"


# --------------------------------------------------------------------------- #
# Validating what was typed
# --------------------------------------------------------------------------- #

def test_proxies_are_split_on_anything_reasonable():
    assert setupwizard.parse_proxies("10.1.0.0/16\n172.18.0.5, 192.168.1.1") == [
        "10.1.0.0/16", "172.18.0.5", "192.168.1.1"]


def test_an_empty_proxy_box_is_no_proxies():
    assert setupwizard.parse_proxies("   \n  ") == []


def test_a_proxy_that_is_not_an_address_is_refused():
    """Validated rather than trusted because the failure is silent: an
    unparseable entry would simply never match, and the app would go on
    believing nobody while looking configured."""
    with pytest.raises(ValueError, match="10.1.0.0/16"):
        setupwizard.parse_proxies("my-proxy.local")


def test_an_external_url_is_parsed():
    url = setupwizard.parse_external_url("https://portfolio.example.com")

    assert url.secure is True
    assert url.host == "portfolio.example.com"


def test_an_external_url_without_a_scheme_is_refused():
    with pytest.raises(ValueError, match="scheme"):
        setupwizard.parse_external_url("portfolio.example.com")


def test_an_external_url_with_a_path_is_refused():
    """The app is served from the root of a hostname. Accepting a path would
    produce a post-restart redirect that goes nowhere."""
    with pytest.raises(ValueError, match="base URL"):
        setupwizard.parse_external_url("https://example.com/portfolio")


def test_a_non_http_scheme_is_refused():
    with pytest.raises(ValueError, match="http"):
        setupwizard.parse_external_url("ftp://example.com")


def test_a_blank_external_url_is_simply_absent():
    assert setupwizard.parse_external_url("  ") is None


def test_an_https_url_carries_the_tls_warning():
    """Said before it is chosen. Somebody who expects the app to terminate TLS
    will otherwise get a secure cookie, reach the app over HTTP, and be unable
    to sign in with nothing on screen connecting the two."""
    note = setupwizard.tls_note(setupwizard.parse_external_url("https://x.example.com"))

    assert "does not terminate TLS" in note


def test_an_http_url_carries_no_such_warning():
    assert setupwizard.tls_note(setupwizard.parse_external_url("http://x.example.com")) == ""


def test_an_unknown_timezone_is_refused():
    assert "not a timezone" in setupwizard.timezone_problem("Mars/Olympus")


def test_a_real_timezone_passes():
    assert setupwizard.timezone_problem("Europe/Dublin") is None


def test_the_timezone_list_is_offered_in_full():
    zones = setupwizard.timezones()

    assert "Australia/Melbourne" in zones and "America/New_York" in zones
    assert zones == sorted(zones)


def test_postgres_needs_a_host_a_name_and_a_user():
    for missing in ({"host": ""}, {"name": ""}, {"user": ""}):
        fields = {"host": "pg", "port": "5432", "name": "portfolio",
                  "user": "portfolio", "password": "x", **missing}
        with pytest.raises(ValueError):
            setupwizard.postgres_settings(**fields)


def test_a_non_numeric_postgres_port_is_refused():
    with pytest.raises(ValueError, match="whole number"):
        setupwizard.postgres_settings(host="pg", port="five", name="portfolio",
                                      user="portfolio", password="x")


def test_a_blank_postgres_port_defaults_to_5432():
    chosen = setupwizard.postgres_settings(host="pg", port="", name="portfolio",
                                           user="portfolio", password="x")

    assert chosen.port == 5432


# --------------------------------------------------------------------------- #
# What ends up in the file
# --------------------------------------------------------------------------- #

def test_only_what_was_chosen_is_written():
    """A config listing every value makes every default a decision somebody has
    to maintain. The shipped file is almost all comments for the same reason."""
    draft = setupwizard.Draft(timezone="Europe/Dublin", price_feed=True)

    values = setupwizard.config_values(draft)

    assert values == {"timezone": "Europe/Dublin", "price_feed": {"enabled": True}}
    assert "auth" not in values
    assert "database" not in values


def test_a_database_the_wizard_chose_is_written():
    draft = setupwizard.Draft(
        database=DatabaseSettings(type="sqlite", path="/data/p.db"))

    assert setupwizard.config_values(draft)["database"] == {
        "type": "sqlite", "path": "/data/p.db"}


def test_a_database_the_deployment_chose_is_NOT_written():
    """The wizard never writes down a database it did not pick. Doing so would
    freeze an environment variable's value into a file, and the next time that
    variable changed the file would quietly win."""
    assert "database" not in setupwizard.config_values(setupwizard.Draft())


def test_an_https_url_turns_on_the_secure_cookie():
    draft = setupwizard.Draft(external_url="https://portfolio.example.com")

    assert setupwizard.config_values(draft)["auth"]["cookie_secure"] is True


def test_an_http_url_leaves_the_cookie_alone():
    """Setting `cookie_secure` on a plain-HTTP install is how you lock everyone
    out — the browser accepts the login and discards the cookie."""
    draft = setupwizard.Draft(external_url="http://192.168.1.5:8000")

    assert "auth" not in setupwizard.config_values(draft)


def test_the_restart_list_names_things_individually():
    """"Some settings need a restart" tells nobody whether the thing they came
    for is one of them."""
    draft = setupwizard.Draft(external_url="https://x.example.com",
                              trusted_proxies=["10.0.0.0/8"])

    pending = setupwizard.needs_restart(draft)

    assert any("HTTPS-only" in item for item in pending)
    assert any("proxies" in item for item in pending)


def test_nothing_needing_a_restart_produces_an_empty_list():
    assert setupwizard.needs_restart(setupwizard.Draft()) == []


# --------------------------------------------------------------------------- #
# Writing the file
# --------------------------------------------------------------------------- #

def test_a_new_config_is_created_from_the_annotated_default(tmp_path, monkeypatch):
    """What the wizard creates has to be the *documented* config with a few
    values filled in, not a four-line stub. The file is the main documentation
    anybody reads, and one born without its comments never grows them back.
    """
    target = tmp_path / "config.yaml"
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(target))

    configfile.write_tree({"timezone": "Europe/Dublin"})

    written = target.read_text()
    assert "timezone: Europe/Dublin" in written
    assert written.count("#") > 20        # the shipped commentary came with it


def test_writing_creates_a_missing_directory(tmp_path, monkeypatch):
    """`/config` may not exist at all on a fresh container."""
    target = tmp_path / "sub" / "dir" / "config.yaml"
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(target))

    configfile.write_tree({"timezone": "Europe/Dublin"})

    assert target.is_file()


def test_writing_merges_rather_than_replacing_a_block(tmp_path, monkeypatch):
    """Setting `auth.trusted_proxies` must not take the rest of `auth:` with
    it — including its comments."""
    target = tmp_path / "config.yaml"
    target.write_text(
        "auth:\n"
        "  # how long a session lasts\n"
        "  session_ttl_days: 30\n"
        "  cookie_secure: false\n"
    )
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(target))

    configfile.write_tree({"auth": {"trusted_proxies": ["10.0.0.0/8"]}})

    written = target.read_text()
    assert "session_ttl_days: 30" in written
    assert "# how long a session lasts" in written
    assert "trusted_proxies" in written


def test_writing_replaces_a_value_that_is_already_there(tmp_path, monkeypatch):
    target = tmp_path / "config.yaml"
    target.write_text("timezone: Australia/Perth\n")
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(target))

    configfile.write_tree({"timezone": "Europe/Dublin"})

    assert "Europe/Dublin" in target.read_text()
    assert "Australia/Perth" not in target.read_text()


def test_a_writable_directory_with_no_file_is_creatable(tmp_path, monkeypatch):
    """`writability()` reports a missing file as unwritable, which is right for
    the admin page and wrong here — on a fresh container it is *supposed* to be
    missing."""
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(tmp_path / "config.yaml"))

    assert configfile.creatable().writable is True
    assert configfile.writability().writable is False


def test_a_read_only_directory_is_not_creatable(tmp_path, monkeypatch):
    import os
    import stat

    directory = tmp_path / "ro"
    directory.mkdir()
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(directory / "config.yaml"))
    original = stat.S_IMODE(os.stat(directory).st_mode)
    os.chmod(directory, 0o500)
    try:
        state = configfile.creatable()
    finally:
        os.chmod(directory, original)

    assert state.writable is False
    # It has to name the shape this actually happens in, or a Kubernetes user
    # reads it as a fault rather than as their own deployment working normally.
    assert "ConfigMap" in state.reason


def test_the_rendered_yaml_is_what_would_have_been_written():
    """The read-only case shows the YAML instead of saving it, so it has to be
    real YAML rather than a description of it."""
    from ruamel.yaml import YAML

    text = configfile.render({"timezone": "Europe/Dublin",
                              "auth": {"cookie_secure": True}})

    assert YAML(typ="safe").load(text) == {"timezone": "Europe/Dublin",
                                           "auth": {"cookie_secure": True}}


# --------------------------------------------------------------------------- #
# The flow over HTTP
#
# These run with a database already configured (conftest builds one), which is
# the deployment shape where the wizard skips its database step — Kubernetes,
# or anyone who wrote a config file first.
# --------------------------------------------------------------------------- #

def _token(client, path: str = "/setup") -> str:
    """The CSRF token the page at `path` would carry."""
    page = client.get(path, headers=HTML)
    found = re.search(r'name="_csrf" value="([^"]+)"', page.text)
    return found.group(1) if found else client.cookies.get("pf_csrf")


def _through_the_account(client) -> None:
    """Welcome, create the account, and view the recovery codes.

    The codes page is a GET that issues them, so walking past it is part of
    getting to the steps beyond — exactly as a browser would.
    """
    token = _token(client)
    client.post("/setup", data={"_csrf": token}, headers=HTML, follow_redirects=False)
    client.post("/setup/profile",
                data={"name": "Owner", "email": "owner@example.test",
                      "password": PASSWORD, "_csrf": token},
                headers=HTML, follow_redirects=False)
    client.get("/setup/recovery", headers=HTML)


def test_the_welcome_step_says_what_it_detected(client, app_module):
    """Named, not merely acknowledged — "a database is configured" leaves
    somebody wondering which one, which is the question they came with.

    Asserted against `describe()` rather than a literal, because this suite
    runs on both backends and the right answer differs.
    """
    page = client.get("/setup", headers=HTML)

    assert page.status_code == 200
    assert database.describe(app_module.settings.database) in page.text


def test_the_privacy_promise_is_made_where_the_choice_is(client):
    """It must not sit in a "What this is" section on the welcome page — the
    README sells the app, this wizard sets it up.

    The promise itself is not marketing, though: it is a claim about what this
    install does, and it belongs at the control that decides it. So it is
    asserted at the market-data checkbox rather than on a page somebody clicks
    past.
    """
    _through_the_account(client)

    page = client.get("/setup/portfolio", headers=HTML).text

    assert 'name="price_feed"' in page, "no market-data choice to explain"
    assert "sends ticker symbols and nothing else" in page


def test_the_database_step_is_skipped_when_one_is_configured(client):
    resp = client.post("/setup", data={"_csrf": _token(client)}, headers=HTML,
                       follow_redirects=False)

    assert resp.headers["location"] == "/setup/profile"


def test_the_rail_leaves_out_the_database_step_when_it_is_skipped(client):
    """It is removed, not greyed with "not needed": this
    deployment supplies its own database and will never visit that step."""
    page = client.get("/setup", headers=HTML)

    assert "not needed" not in page.text
    rail = page.text[page.text.index('class="wizrail"'):page.text.index("</ol>")]
    assert "Database" not in rail


def test_the_welcome_step_needs_its_csrf_token(client):
    resp = client.post("/setup", data={}, headers=HTML)

    assert resp.status_code == 403


def test_the_account_step_creates_an_admin_and_signs_them_in(client, session_factory):
    _through_the_account(client)

    with session_factory() as s:
        user = s.scalars(select(User)).one()
        assert user.email == "owner@example.test"
        assert user.is_admin is True
    assert client.cookies.get("pf_session")


def test_the_account_step_refuses_a_weak_password(client, session_factory):
    token = _token(client)
    client.post("/setup", data={"_csrf": token}, headers=HTML, follow_redirects=False)

    page = client.post("/setup/profile",
                       data={"name": "Owner", "email": "owner@example.test",
                             "password": "short", "_csrf": token}, headers=HTML)

    assert page.status_code == 200
    with session_factory() as s:
        assert s.scalars(select(User)).first() is None


def test_the_account_step_goes_on_to_the_recovery_codes(client):
    token = _token(client)
    client.post("/setup", data={"_csrf": token}, headers=HTML, follow_redirects=False)

    resp = client.post("/setup/profile",
                       data={"name": "Owner", "email": "owner@example.test",
                             "password": PASSWORD, "_csrf": token},
                       headers=HTML, follow_redirects=False)

    # The codes come before the second factor on purpose: they are what makes
    # skipping the next step survivable.
    assert resp.headers["location"] == "/setup/recovery"


def test_two_factor_is_not_a_wizard_step_any_more(client):
    """It moved to the account page, where it already lived — `/profile/2fa`
    is the way to turn it on and off, so a wizard step would be a
    second door to one room and a step in a flow somebody is trying to finish.

    The equivalent behaviour is covered by `test_twofactor.py` against the
    account page, which is now the only way in.
    """
    _through_the_account(client)

    assert "2fa" not in setupwizard.STEP_KEYS
    assert client.get("/setup/2fa", headers=HTML).status_code == 404


def test_the_recovery_step_continues_straight_to_the_portfolio(client):
    """The step between them is gone, so the link had to move with it — a
    "continue" pointing at a 404 is the obvious way to break this."""
    _through_the_account(client)

    page = client.get("/setup/recovery", headers=HTML)

    assert "/setup/portfolio" in page.text
    assert "/setup/2fa" not in page.text


def test_the_finish_step_still_points_at_two_factor(client):
    """Removing the step costs something real: a step is a PROMPT and an
    account-page toggle is not. This is the nudge that keeps most of it."""
    _through_the_account(client)
    token = _token(client, "/setup/portfolio")
    client.post("/setup/portfolio",
                data={"_csrf": token, "name": "Mine", "timezone": "Australia/Melbourne"},
                headers=HTML, follow_redirects=False)
    client.post("/setup/features", data={"_csrf": _token(client, "/setup/features"),
                                         "feature": "dca_schedule"},
                headers=HTML, follow_redirects=False)

    page = client.get("/setup/finish", headers=HTML)

    assert "two-factor" in page.text.lower()


def test_the_portfolio_step_renames_the_portfolio_and_records_the_zone(
        client, session_factory):
    _through_the_account(client)
    token = _token(client, "/setup/portfolio")

    resp = client.post("/setup/portfolio",
                       data={"_csrf": token, "name": "Family portfolio",
                             "timezone": "Europe/Dublin", "price_feed": "on"},
                       headers=HTML, follow_redirects=False)

    # Optional features come between the portfolio and the environment: they
    # are about the app being set up, not the machine it runs on.
    assert resp.headers["location"] == "/setup/features"
    with session_factory() as s:
        assert s.scalars(select(Portfolio)).one().name == "Family portfolio"
    assert setupwizard.draft().timezone == "Europe/Dublin"
    assert setupwizard.draft().price_feed is True


def test_an_unticked_market_data_box_is_recorded_as_off(client):
    """An unchecked checkbox is not submitted at all, so "off" has to be the
    absence of the field rather than a value — easy to get backwards, and the
    consequence is an app that phones out when told not to."""
    _through_the_account(client)

    client.post("/setup/portfolio",
                data={"_csrf": _token(client, "/setup/portfolio"), "name": "Mine",
                      "timezone": "Europe/Dublin"},
                headers=HTML, follow_redirects=False)

    assert setupwizard.draft().price_feed is False


def test_the_portfolio_step_refuses_an_unknown_timezone(client, session_factory):
    _through_the_account(client)

    resp = client.post("/setup/portfolio",
                       data={"_csrf": _token(client, "/setup/portfolio"),
                             "name": "Mine", "timezone": "Mars/Olympus"},
                       headers=HTML, follow_redirects=False)

    assert "error=" in resp.headers["location"]
    with session_factory() as s:
        assert s.scalars(select(Portfolio)).one().name != "Mine"


def test_the_environment_step_stores_proxies_and_the_url(client):
    _through_the_account(client)
    client.post("/setup/portfolio",
                data={"_csrf": _token(client, "/setup/portfolio"), "name": "Mine",
                      "timezone": "Europe/Dublin"},
                headers=HTML, follow_redirects=False)

    resp = client.post("/setup/environment",
                       data={"_csrf": _token(client, "/setup/environment"),
                             "trusted_proxies": "10.1.0.0/16",
                             "external_url": "https://portfolio.example.test"},
                       headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/setup/finish"
    assert setupwizard.draft().trusted_proxies == ["10.1.0.0/16"]


def test_the_environment_step_refuses_a_bad_proxy(client):
    _through_the_account(client)
    client.post("/setup/portfolio",
                data={"_csrf": _token(client, "/setup/portfolio"), "name": "Mine",
                      "timezone": "Europe/Dublin"},
                headers=HTML, follow_redirects=False)

    resp = client.post("/setup/environment",
                       data={"_csrf": _token(client, "/setup/environment"),
                             "trusted_proxies": "my-proxy.local", "external_url": ""},
                       headers=HTML, follow_redirects=False)

    assert "error=" in resp.headers["location"]
    assert setupwizard.draft().trusted_proxies == []


# --------------------------------------------------------------------------- #
# The wizard closes behind itself
# --------------------------------------------------------------------------- #

def test_the_early_steps_are_refused_once_an_account_exists(client):
    """`/setup/` is in PUBLIC_PREFIXES, so this gate is the only thing between
    these pages and anybody who can reach the app."""
    _through_the_account(client)
    client.cookies.clear()

    for path in ("/setup", "/setup/database", "/setup/profile"):
        resp = client.get(path, headers=HTML, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/login", path


def test_creating_a_second_account_through_the_wizard_is_refused(client, session_factory):
    _through_the_account(client)

    resp = client.post("/setup/profile",
                       data={"name": "Intruder", "email": "intruder@example.test",
                             "password": PASSWORD, "_csrf": _token(client)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    with session_factory() as s:
        assert len(s.scalars(select(User)).all()) == 1


def test_the_settings_steps_are_refused_with_no_wizard_in_progress(client):
    """What closes them after the wizard finishes: `finish` discards the draft,
    and a restarted process never had one."""
    _through_the_account(client)
    setupwizard.discard()

    for path in ("/setup/portfolio", "/setup/environment", "/setup/finish"):
        resp = client.get(path, headers=HTML, follow_redirects=False)
        assert resp.status_code == 303, path


def test_the_environment_step_cannot_be_posted_to_once_setup_is_done(client):
    """The one that would matter most: trusted proxies decide whose
    `X-Forwarded-For` is believed."""
    _through_the_account(client)
    token = _token(client, "/setup/environment")
    setupwizard.discard()

    resp = client.post("/setup/environment",
                       data={"_csrf": token, "trusted_proxies": "0.0.0.0/0",
                             "external_url": ""},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] != "/setup/finish"


def test_a_logged_out_visitor_cannot_reach_the_settings_steps(client):
    _through_the_account(client)
    client.cookies.clear()

    resp = client.get("/setup/environment", headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/login"


# --------------------------------------------------------------------------- #
# Finishing
# --------------------------------------------------------------------------- #

def _to_the_finish(client) -> None:
    _through_the_account(client)
    client.post("/setup/portfolio",
                data={"_csrf": _token(client, "/setup/portfolio"), "name": "Mine",
                      "timezone": "Europe/Dublin", "price_feed": "on"},
                headers=HTML, follow_redirects=False)
    client.post("/setup/environment",
                data={"_csrf": _token(client, "/setup/environment"),
                      "trusted_proxies": "", "external_url": ""},
                headers=HTML, follow_redirects=False)


def test_the_finish_step_shows_the_yaml_before_writing_it(client, tmp_path, monkeypatch):
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(tmp_path / "config.yaml"))
    _to_the_finish(client)

    page = client.get("/setup/finish", headers=HTML)

    assert "timezone: Europe/Dublin" in page.text
    assert not (tmp_path / "config.yaml").exists()   # shown, not saved


def test_finishing_writes_the_config_and_closes_the_wizard(client, tmp_path, monkeypatch):
    target = tmp_path / "config.yaml"
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(target))
    _to_the_finish(client)

    resp = client.post("/setup/finish",
                       data={"_csrf": _token(client, "/setup/finish")},
                       headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/"
    assert target.is_file()
    assert "Europe/Dublin" in target.read_text()
    assert setupwizard.in_progress() is False


def test_an_abandoned_wizard_leaves_no_config_file(client, tmp_path, monkeypatch):
    """What makes starting again safe. A half-written file is worse than none:
    the app boots into it and the wizard never runs."""
    target = tmp_path / "config.yaml"
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(target))

    _to_the_finish(client)          # everything except pressing the last button

    assert not target.exists()


def test_a_read_only_config_path_still_finishes(client, tmp_path, monkeypatch):
    """The Kubernetes case. Everything chosen is already applied to the running
    app; the file is what a restart would need, and the page has already shown
    the YAML to paste."""
    import os
    import stat

    directory = tmp_path / "ro"
    directory.mkdir()
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(directory / "config.yaml"))
    _through_the_account(client)
    client.post("/setup/portfolio",
                data={"_csrf": _token(client, "/setup/portfolio"), "name": "Mine",
                      "timezone": "Europe/Dublin"},
                headers=HTML, follow_redirects=False)
    token = _token(client, "/setup/finish")

    original = stat.S_IMODE(os.stat(directory).st_mode)
    os.chmod(directory, 0o500)
    try:
        resp = client.post("/setup/finish", data={"_csrf": token}, headers=HTML,
                           follow_redirects=False)
    finally:
        os.chmod(directory, original)

    assert resp.status_code == 303
    assert setupwizard.in_progress() is False


def test_the_environment_step_is_skipped_when_the_config_cannot_be_written(
        client, tmp_path, monkeypatch):
    """Nothing on it could be saved, so offering it would be a form that
    discards what you type."""
    import os
    import stat

    directory = tmp_path / "ro"
    directory.mkdir()
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(directory / "config.yaml"))
    _through_the_account(client)
    token = _token(client, "/setup/portfolio")

    original = stat.S_IMODE(os.stat(directory).st_mode)
    os.chmod(directory, 0o500)
    try:
        resp = client.post("/setup/portfolio",
                           data={"_csrf": token, "name": "Mine",
                                 "timezone": "Europe/Dublin"},
                           headers=HTML, follow_redirects=False)
    finally:
        os.chmod(directory, original)

    assert resp.headers["location"] == "/setup/finish"


def test_the_restart_page_asks_the_process_to_stop_after_rendering(
        client, monkeypatch):
    """Rendered first, THEN the SIGTERM. The other order sends the signal
    before the browser has the page telling it what is happening."""
    from app import lifecycle

    order = []
    monkeypatch.setattr(lifecycle, "request_stop", lambda reason: order.append(reason))
    _through_the_account(client)

    page = client.get("/setup/restarting?to=https%3A%2F%2Fx.example.test%2F",
                      headers=HTML)

    assert page.status_code == 200
    assert "https://x.example.test/" in page.text
    assert len(order) == 1


# The database step's routes. conftest always configures a database, so these
# make the app *report* itself unconfigured — what the routes branch on — and
# stub the connect. That the connect itself works is the subprocess test's job,
# because only there is it a real one.
# --------------------------------------------------------------------------- #

@pytest.fixture
def unconfigured(monkeypatch):
    """Make the wizard believe nothing has chosen a database yet."""
    from app import db as db_module
    from app import main

    monkeypatch.setattr(main.database, "source",
                        lambda settings: db_module.Source(db_module.NOT_CONFIGURED))
    connected = []
    monkeypatch.setattr(main.database, "connect",
                        lambda chosen, **kw: connected.append(chosen))
    return connected


def test_the_welcome_step_leads_to_the_database_step_when_nothing_is_set(
        client, unconfigured):
    resp = client.post("/setup", data={"_csrf": _token(client)}, headers=HTML,
                       follow_redirects=False)

    assert resp.headers["location"] == "/setup/database"


def test_the_database_step_offers_both_kinds(client, unconfigured):
    page = client.get("/setup/database", headers=HTML)

    assert page.status_code == 200
    assert 'value="sqlite"' in page.text and 'value="postgres"' in page.text


def test_testing_a_good_path_reports_success_and_connects_nothing(
        client, unconfigured, tmp_path):
    page = client.post("/setup/database/test",
                       data={"_csrf": _token(client), "kind": "sqlite",
                             "path": str(tmp_path / "p.db")}, headers=HTML)

    assert "is usable at" in page.text
    assert unconfigured == [], "the Test button must not connect"


def test_testing_a_bad_path_reports_the_problem(client, unconfigured, tmp_path):
    page = client.post("/setup/database/test",
                       data={"_csrf": _token(client), "kind": "sqlite",
                             "path": str(tmp_path / "missing" / "p.db")}, headers=HTML)

    assert "does not exist" in page.text
    assert unconfigured == []


def test_an_invalid_postgres_form_is_reported_without_connecting(
        client, unconfigured):
    """A validation failure, not a connection failure — it must never reach the
    probe, let alone the connect."""
    page = client.post("/setup/database/test",
                       data={"_csrf": _token(client), "kind": "postgres",
                             "host": "", "port": "5432", "name": "portfolio",
                             "user": "portfolio", "password": "x"}, headers=HTML)

    assert "hostname" in page.text
    assert unconfigured == []


def test_a_redrawn_database_form_keeps_what_was_typed_except_the_password(
        client, unconfigured):
    """Losing a password somebody just pasted, on a failed connection test, is
    its own small cruelty — and re-typing it is where the typo comes from."""
    page = client.post("/setup/database/test",
                       data={"_csrf": _token(client), "kind": "postgres",
                             "host": "no-such-host.invalid", "port": "6543",
                             "name": "mydb", "user": "myuser",
                             "password": "sup3r-s3cret"}, headers=HTML)

    assert 'value="no-such-host.invalid"' in page.text
    assert 'value="6543"' in page.text
    assert 'value="mydb"' in page.text
    # …but the password is never echoed back into the page source.
    assert "sup3r-s3cret" not in page.text
    # Two layers keep it that way, and this pins the one doing the work: the
    # password input carries no `value` at all, so it cannot echo one even if
    # the context dict grew a password key. (The route filters it as well.)
    field = re.search(r'<input type="password"[^>]*name="password"[^>]*>', page.text)
    assert field is not None
    assert "value=" not in field.group(0)


def test_choosing_a_working_database_connects_it_and_moves_on(
        client, unconfigured, tmp_path):
    target = tmp_path / "chosen.db"

    resp = client.post("/setup/database",
                       data={"_csrf": _token(client), "kind": "sqlite",
                             "path": str(target)}, headers=HTML,
                       follow_redirects=False)

    assert resp.headers["location"] == "/setup/profile"
    assert len(unconfigured) == 1
    assert unconfigured[0].path == str(target)
    # …and it is remembered, so the last step can write it to the config file.
    assert setupwizard.draft().database.path == str(target)


def test_choosing_a_broken_database_neither_connects_nor_remembers(
        client, unconfigured, tmp_path):
    resp = client.post("/setup/database",
                       data={"_csrf": _token(client), "kind": "sqlite",
                             "path": str(tmp_path / "missing" / "p.db")},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 200          # the form, redrawn with the reason
    assert unconfigured == []
    assert setupwizard.draft().database is None


def test_a_connect_that_fails_after_the_probe_is_reported_not_raised(
        client, monkeypatch, tmp_path):
    """The probe can pass and the migration still fail — a database that
    answers but is at an incompatible revision, say. That has to reach the page
    as a sentence, not as a 500."""
    from app import db as db_module
    from app import main

    monkeypatch.setattr(main.database, "source",
                        lambda settings: db_module.Source(db_module.NOT_CONFIGURED))

    def explode(chosen, **kw):
        raise RuntimeError("alembic said no")

    monkeypatch.setattr(main.database, "connect", explode)

    page = client.post("/setup/database",
                       data={"_csrf": _token(client), "kind": "sqlite",
                             "path": str(tmp_path / "p.db")}, headers=HTML)

    assert page.status_code == 200
    assert "alembic said no" in page.text
    assert setupwizard.draft().database is None


def test_the_database_step_steps_aside_once_one_is_connected(client):
    """Reachable by a back button after the step is done. It must not offer to
    choose again — the process is already using one."""
    resp = client.get("/setup/database", headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup/profile"


def test_a_configured_but_unconnected_database_sends_you_to_the_database_step(
        client, monkeypatch):
    """Configured and not connected means the connection failed at boot. Saying
    so beats an account form that cannot save anything."""
    from app import main

    monkeypatch.setattr(main.database, "is_ready", lambda: False)

    resp = client.post("/setup", data={"_csrf": _token(client)}, headers=HTML,
                       follow_redirects=False)

    assert resp.headers["location"] == "/setup/database"


def test_that_step_explains_itself_rather_than_offering_a_choice(
        client, monkeypatch, app_module):
    """It names what the deployment set and stops there. Offering a form whose
    every value would be ignored — the process is already connected — would be
    worse than showing nothing."""
    from app import main

    monkeypatch.setattr(main.database, "is_ready", lambda: False)

    page = client.get("/setup/database", headers=HTML)

    assert database.describe(app_module.settings.database) in page.text
    assert 'name="kind"' not in page.text            # no choice offered
    # …and why you are here. Any wording of "the database is unreachable" will
    # do — pinning one sentence made a tone edit look like a missing warning.
    assert re.search(r"could not (connect|be reached)|can't be reached", page.text)


# --------------------------------------------------------------------------- #
# The background loops, before there is anything for them to talk to
# --------------------------------------------------------------------------- #

def test_the_loops_wait_for_a_database_instead_of_failing_against_none(monkeypatch):
    """Found by running the real container, not by this suite.

    Both loops start with the process. On a fresh install the wizard has not
    run, so without the wait they fire immediately and fill the first page of the log
    with `NotConfigured` tracebacks — burying the one line somebody needs
    ("Uvicorn running on…") under a stack trace that looks like a crash.

    Driven with `asyncio.run` rather than a plugin: there is no pytest-asyncio
    here, and one coroutine does not justify adding one.
    """
    import asyncio

    from app import db as db_module
    from app import main

    # Not ready for the first three checks, then ready. The count matters:
    # `_await_database` asks once to decide whether to wait at all, then once
    # per loop — so three "no"s means two waits, not three.
    answers = iter([False, False, False, True])
    monkeypatch.setattr(db_module, "is_ready", lambda: next(answers, True))
    monkeypatch.setattr(main.database, "is_ready", db_module.is_ready)

    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    asyncio.run(main._await_database("price feed"))

    # It polled rather than giving up, and it stopped as soon as the database
    # appeared instead of spinning.
    assert slept == [2, 2]


def test_a_loop_with_a_database_already_there_does_not_wait_at_all(monkeypatch):
    """The normal case — a configured deployment must not pay a delay for a
    check that is only interesting on a fresh install."""
    import asyncio

    from app import main

    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(main.database, "is_ready", lambda: True)

    asyncio.run(main._await_database("maintenance"))

    assert slept == []


# --------------------------------------------------------------------------- #
# The state this whole feature exists for
# --------------------------------------------------------------------------- #

@pytest.mark.slow
def test_a_completely_unconfigured_install_boots_into_the_wizard(tmp_path):
    """No config file, no APP_DATABASE__*, no database — a fresh container.

    In a subprocess because this suite cannot reach that state: conftest has
    already configured a database by the time `app.main` is importable, and
    `app.main` can only be imported once per process.
    """
    script = textwrap.dedent(f"""
        import os, sys
        for name in [k for k in os.environ if k.startswith("APP_")]:
            del os.environ[name]
        os.environ.pop("STOCKTAKE_TEST_DB", None)
        os.environ["APP_CONFIG_FILE"] = {str(tmp_path / "config.yaml")!r}
        sys.path[:0] = [{str(APP_ROOT)!r},
                        {str(APP_ROOT.parent.parent / "libs" / "appkit")!r}]

        from fastapi.testclient import TestClient
        from app import db, main

        assert db.is_ready() is False, "should have booted with no database"
        client = TestClient(main.app)
        html = {{"accept": "text/html"}}

        # Everything goes to the wizard rather than to a 500 from the first query.
        for path in ("/", "/charts", "/admin"):
            resp = client.get(path, headers=html, follow_redirects=False)
            assert resp.status_code == 303 and resp.headers["location"] == "/setup", path

        # …and a non-HTML caller is told, rather than being redirected into a
        # page it cannot use.
        assert client.get("/", headers={{"accept": "application/json"}},
                          follow_redirects=False).status_code == 503

        # Readiness must NOT fail here, or Kubernetes keeps traffic away from
        # the one page that can fix it.
        assert client.get("/readyz").status_code == 200

        page = client.get("/setup", headers=html)
        assert page.status_code == 200
        assert "Not configured" in page.text
        print("OK")
    """)

    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, timeout=180)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "OK" in result.stdout


@pytest.mark.slow
def test_the_database_step_probes_before_it_connects_and_connects_before_it_writes(
        tmp_path):
    """The database step, walked for real — the one part of the wizard this
    suite cannot otherwise reach.

    Everything else here runs with conftest's database already configured, so
    the step is skipped and its guarantees go untested. That was not a
    theoretical gap: with these assertions absent, deleting the probe check
    from `/setup/database` broke nothing.

    Three claims, in the order they matter:

      1. a database that cannot work is refused, and nothing is connected;
      2. testing a good one still connects nothing — the button reports, it
         does not commit;
      3. choosing one connects and migrates it, and *still* writes no config
         file. That only happens at the end.
    """
    data = tmp_path / "data"
    data.mkdir()
    script = textwrap.dedent(f"""
        import os, sys
        for name in [k for k in os.environ if k.startswith("APP_")]:
            del os.environ[name]
        os.environ.pop("STOCKTAKE_TEST_DB", None)
        os.environ["APP_CONFIG_FILE"] = {str(tmp_path / "config.yaml")!r}
        sys.path[:0] = [{str(APP_ROOT)!r},
                        {str(APP_ROOT.parent.parent / "libs" / "appkit")!r}]

        from pathlib import Path
        from fastapi.testclient import TestClient
        from app import db, main

        client = TestClient(main.app)
        html = {{"accept": "text/html"}}
        config = Path({str(tmp_path / "config.yaml")!r})
        good = {str(data / "portfolio.db")!r}

        client.get("/setup", headers=html)
        token = client.cookies.get("pf_csrf")
        resp = client.post("/setup", data={{"_csrf": token}}, headers=html,
                           follow_redirects=False)
        assert resp.headers["location"] == "/setup/database", resp.headers

        # 1. A path that cannot work.
        page = client.post("/setup/database/test",
                           data={{"_csrf": token, "kind": "sqlite",
                                 "path": "/nope/nowhere/p.db"}}, headers=html)
        assert "does not exist" in page.text
        assert db.is_ready() is False

        # 2. A good one — reported, not committed.
        page = client.post("/setup/database/test",
                           data={{"_csrf": token, "kind": "sqlite", "path": good}},
                           headers=html)
        assert "is usable at" in page.text, page.text[:400]
        assert db.is_ready() is False, "testing must not connect"
        assert not config.exists()

        # 2b. Choosing a BROKEN one must not connect either, and must explain
        # itself in the probe's words. Without the probe the request still
        # fails safely — but on an Alembic traceback, which is the difference
        # between "no volume is mounted at /nope" and "unable to open database
        # file". Only the first tells anybody what to do.
        resp = client.post("/setup/database",
                           data={{"_csrf": token, "kind": "sqlite",
                                 "path": "/nope/nowhere/p.db"}}, headers=html,
                           follow_redirects=False)
        assert resp.status_code == 200, "a refusal redraws the form"
        assert db.is_ready() is False, "a database that cannot work was connected"
        assert "does not exist" in resp.text and "volume" in resp.text, resp.text[:600]
        assert "sqlalchemy" not in resp.text.lower()
        assert not config.exists()

        # 3. Choosing a good one connects, migrates — and writes nothing.
        resp = client.post("/setup/database",
                           data={{"_csrf": token, "kind": "sqlite", "path": good}},
                           headers=html, follow_redirects=False)
        assert resp.headers["location"] == "/setup/profile", resp.headers
        assert db.is_ready() is True
        assert Path(good).exists()
        assert not config.exists(), "nothing is written until the last step"

        # And the schema really is at head — a connection without migrations
        # would fail on the first query instead of here.
        from sqlalchemy import select
        from app.models import Instrument, Portfolio, Trade, User
        with db.session() as s:
            assert s.scalars(select(User)).first() is None

        # 4. The factory the wizard built must carry the tenancy filter.
        #
        # This is the one guarantee that ONLY exists on this path: the rest of
        # the suite runs against a factory conftest builds and installs itself,
        # so a `connect()` that forgot `tenancy.install` would look perfectly
        # healthy everywhere else — and would serve a wizard-chosen database
        # with no portfolio scoping at all. That is the whole access boundary.
        import datetime as dt
        from decimal import Decimal
        from app import tenancy

        with db.session() as s:
            mine = Portfolio(name="Mine"); theirs = Portfolio(name="Theirs")
            s.add_all([mine, theirs]); s.flush()
            inst = Instrument(ticker="ACME", exchange="ASX", asset_class="share",
                              currency="AUD")
            s.add(inst); s.flush()
            tenancy.bind(s, mine.id, None)
            s.add(Trade(instrument_id=inst.id, date=dt.date(2026, 1, 5), type="buy",
                        quantity=Decimal("10"), unit_price=Decimal("4"),
                        brokerage=Decimal("0"), fx_rate=Decimal("1")))
            s.commit()
            mine_id, theirs_id = mine.id, theirs.id

        with db.session() as s:
            tenancy.bind(s, mine_id, None)
            assert len(s.scalars(select(Trade)).all()) == 1, "own row missing"
        with db.session() as s:
            tenancy.bind(s, theirs_id, None)
            leaked = s.scalars(select(Trade)).all()
            assert leaked == [], "the tenancy filter is not attached to this factory"
        print("OK")
    """)

    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, timeout=180)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "OK" in result.stdout


def test_the_app_is_unreachable_until_the_wizard_finishes(client, session_factory):
    """The database and the account exist from step 3 onward — the account has
    to be written somewhere — so from there every page answers and deleting
    `/setup` from the address bar walks straight into a half-configured app: no
    timezone, no portfolio, no config file written.

    Setup is finished when the draft is discarded, so that is the test.
    """
    _through_the_account(client)

    for path in ("/", "/holdings", "/imports-exports", "/profile"):
        resp = client.get(path, headers=HTML, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"].startswith("/setup"), path

    # Not a bounce to a CLOSED step: welcome shuts once an account exists, and
    # sending somebody there loops through /login and back.
    resp = client.get("/", headers=HTML, follow_redirects=False)
    assert resp.headers["location"] != "/setup"

    # Abandoning it is legitimate, so the way out stays open. /logout is POST
    # only, and 405 from the ROUTE is the proof: the middleware let it past
    # rather than bouncing it to the wizard like everything else.
    assert client.get("/logout", headers=HTML,
                      follow_redirects=False).status_code == 405

    # And once it is finished, the app answers.
    setupwizard.discard()
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 200


def test_a_machine_caller_gets_503_rather_than_a_redirect_mid_wizard(client):
    """A redirect to an HTML page is a useless answer to a fetch, and a 200
    would be a lie about a half-configured app."""
    _through_the_account(client)
    resp = client.get("/holdings", headers={"accept": "application/json"},
                      follow_redirects=False)
    assert resp.status_code == 503


def test_every_step_after_the_first_offers_a_way_back(client):
    """Noticing on the summary that step 5 was wrong should not mean starting
    the wizard again."""
    _through_the_account(client)

    for path, expected in (("/setup/portfolio", "/setup/recovery"),
                           ("/setup/environment", "/setup/features")):
        page = client.get(path, headers=HTML)
        assert f'class="backlink" href="{expected}"' in page.text, path

    # And the recovery page, which is part of a step rather than one of its
    # own, goes back to the step it belongs to rather than to itself.
    assert 'class="backlink"' in client.get("/setup/recovery", headers=HTML).text
