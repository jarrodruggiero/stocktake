"""The admin settings page, and the config file it edits.

Two behaviours matter more than the form itself. A read-only configuration file
is a *normal* deployment (a ConfigMap, a read-only mount, a baked image), so the
page has to stay useful and explain itself rather than failing at the point of
saving. And a save must keep the file's comments — they are what makes the file
editable by hand, and stripping them on the first save would be a poor trade.
"""

from __future__ import annotations

import datetime as dt
import os
import stat

import pytest

from app import configfile, lifecycle
from app.settings import PortfolioSettings

DOCUMENTED = """\
# The application's configuration. Every option is documented here.
app_name: portfolio

# Every date decision is made in this zone.
timezone: Australia/Melbourne

price_feed:
  enabled: true
  hour: 18       # after the market closes
  minute: 30

auth:
  session_ttl_days: 30
  rate_limit:
    max_attempts: 8   # per email and address
"""


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """A real config file on disk, pointed at by the module under test."""
    path = tmp_path / "config.yaml"
    path.write_text(DOCUMENTED)
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(path))
    return path


# --------------------------------------------------------------------------- #
# Can we write it?
# --------------------------------------------------------------------------- #

def test_a_normal_file_is_writable(config_file):
    assert configfile.writability().writable is True


def test_a_missing_file_is_reported_rather_than_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(tmp_path / "absent.yaml"))

    state = configfile.writability()

    assert state.writable is False
    assert "No configuration file" in state.reason


def test_a_read_only_directory_is_detected(config_file, monkeypatch):
    """The ConfigMap case: saving replaces the file through a neighbouring
    temporary file, so an unwritable directory means no save is possible even
    when the file's own bits look fine."""
    directory = config_file.parent
    original = stat.S_IMODE(os.stat(directory).st_mode)
    os.chmod(directory, 0o500)   # r-x: readable, not writable
    try:
        state = configfile.writability()
    finally:
        os.chmod(directory, original)

    assert state.writable is False
    assert "read-only" in state.reason
    # The explanation has to name the shapes this actually happens in, or an
    # administrator is left guessing at their own deployment.
    assert "ConfigMap" in state.reason
    assert str(config_file) in state.reason


def test_the_read_only_explanation_says_what_to_do_instead(config_file, monkeypatch):
    directory = config_file.parent
    original = stat.S_IMODE(os.stat(directory).st_mode)
    os.chmod(directory, 0o500)
    try:
        reason = configfile.writability().reason
    finally:
        os.chmod(directory, original)

    assert "restart" in reason.lower()


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def test_saving_keeps_every_comment(config_file):
    """The whole point of a documented config file is that it survives being
    written to. A plain YAML dump would strip all of this."""
    configfile.save({"price_feed.hour": 9})

    written = config_file.read_text()
    assert "# The application's configuration. Every option is documented here." in written
    assert "# Every date decision is made in this zone." in written
    assert "# after the market closes" in written
    assert "# per email and address" in written


def test_saving_changes_only_what_was_given(config_file):
    configfile.save({"price_feed.hour": 9})

    data = configfile.load()
    assert data["price_feed"]["hour"] == 9
    assert data["price_feed"]["minute"] == 30          # untouched
    assert data["auth"]["session_ttl_days"] == 30      # untouched
    assert data["timezone"] == "Australia/Melbourne"   # untouched


def test_a_nested_value_can_be_written(config_file):
    configfile.save({"auth.rate_limit.max_attempts": 4})

    assert configfile.load()["auth"]["rate_limit"]["max_attempts"] == 4


def test_a_key_absent_from_the_file_is_created(config_file):
    # `imports` is not in this file at all.
    configfile.save({"imports.allow_new_instruments": True})

    assert configfile.load()["imports"]["allow_new_instruments"] is True


def test_a_date_is_written_as_a_plain_string(config_file):
    configfile.save({"price_feed.backfill_start": dt.date(2015, 6, 1)})

    assert "2015-06-01" in config_file.read_text()


def test_the_file_is_replaced_atomically(config_file):
    """No half-written config if the process dies mid-save, and no temporary
    file left behind afterwards."""
    configfile.save({"price_feed.hour": 7})

    leftovers = [p.name for p in config_file.parent.iterdir() if p.name != "config.yaml"]
    assert leftovers == []


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def option(name: str) -> configfile.Option:
    return configfile.BY_NAME[name]


def test_a_bad_timezone_is_refused_with_a_usable_message():
    with pytest.raises(ValueError, match="not a known timezone"):
        configfile.coerce(option("timezone"), "Australia/Nowhere")


def test_a_good_timezone_passes():
    assert configfile.coerce(option("timezone"), "Europe/Dublin") == "Europe/Dublin"


@pytest.mark.parametrize("raw", ["not-a-number", "", "9.5"])
def test_a_non_integer_is_refused(raw):
    with pytest.raises(ValueError, match="whole number"):
        configfile.coerce(option("price_feed.hour"), raw)


@pytest.mark.parametrize("raw,message", [("24", "at most"), ("-1", "at least")])
def test_an_out_of_range_integer_is_refused(raw, message):
    with pytest.raises(ValueError, match=message):
        configfile.coerce(option("price_feed.hour"), raw)


def test_a_checkbox_is_read_as_a_boolean():
    assert configfile.coerce(option("auth.cookie_secure"), "on") is True
    assert configfile.coerce(option("auth.cookie_secure"), "") is False


def test_an_unknown_choice_is_refused():
    with pytest.raises(ValueError, match="must be one of"):
        configfile.coerce(option("log_level"), "CHATTY")


def test_a_malformed_date_is_refused():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        configfile.coerce(option("price_feed.backfill_start"), "01/06/2015")


# --------------------------------------------------------------------------- #
# What the page shows
# --------------------------------------------------------------------------- #

def test_an_environment_override_is_detected(monkeypatch):
    """Saving the file would change nothing an environment variable already
    decides, and the page says so rather than implying a save took effect."""
    monkeypatch.setenv("APP_AUTH__RATE_LIMIT__MAX_ATTEMPTS", "9")

    assert configfile.overridden_by_environment(option("auth.rate_limit.max_attempts"))
    assert not configfile.overridden_by_environment(option("price_feed.hour"))


def test_effective_values_come_from_the_running_settings():
    settings = PortfolioSettings()

    # tests/config.test.yaml sets 3; the field default is 8.
    assert configfile.effective(settings, option("auth.rate_limit.max_attempts")) == 3


def test_applying_live_updates_the_running_settings_and_the_clock():
    from app import clock

    settings = PortfolioSettings()
    try:
        pending = configfile.apply_live(settings, {
            "timezone": "Europe/Dublin",
            "auth.rate_limit.max_attempts": 7,
        })

        assert settings.timezone == "Europe/Dublin"
        assert settings.auth.rate_limit.max_attempts == 7
        assert str(clock.zone()) == "Europe/Dublin"
        assert pending == []   # neither of those needs a restart
    finally:
        clock.configure("Australia/Melbourne")


def test_settings_needing_a_restart_are_reported_as_such():
    settings = PortfolioSettings()
    try:
        pending = configfile.apply_live(settings, {"log_level": "DEBUG"})

        # Logging is configured once at startup, so the value is stored but
        # will not change what is written until the app restarts. Saying
        # "saved" without saying that would be a lie.
        assert pending == ["Log level"]
    finally:
        clock_reset = __import__("app.clock", fromlist=["clock"])
        clock_reset.configure("Australia/Melbourne")


def test_every_option_resolves_against_the_real_settings_object():
    """A typo in an option's path would render an empty field and silently
    write to the wrong key — so every declared path is walked."""
    settings = PortfolioSettings()

    for opt in configfile.OPTIONS:
        configfile.effective(settings, opt)   # raises AttributeError if wrong


# --------------------------------------------------------------------------- #
# The page itself
# --------------------------------------------------------------------------- #

def test_an_admin_sees_the_settings_page(client, session_factory):
    from test_routes import make_login

    make_login(client, session_factory, admin=True)

    page = client.get("/admin/settings", headers={"accept": "text/html"})

    assert page.status_code == 200
    assert "Timezone" in page.text
    assert "Failed sign-ins allowed" in page.text


def test_a_non_admin_cannot_reach_it(client, session_factory):
    from test_routes import make_login

    make_login(client, session_factory, admin=False)

    assert client.get("/admin/settings",
                      headers={"accept": "text/html"}).status_code == 403


def test_a_read_only_deployment_shows_the_values_but_no_save_button(client, session_factory,
                                                                   monkeypatch, tmp_path):
    """Read-only is normal, so the page must still be worth opening: every
    value visible, the reason stated, and nothing offering to save."""
    from test_routes import make_login

    # Its own directory: tmp_path also holds this test's database, and making
    # that read-only would break the app rather than just the config file.
    conf_dir = tmp_path / "config"
    conf_dir.mkdir()
    path = conf_dir / "config.yaml"
    path.write_text(DOCUMENTED)
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(path))
    make_login(client, session_factory, admin=True)
    original = stat.S_IMODE(os.stat(conf_dir).st_mode)
    os.chmod(conf_dir, 0o500)
    try:
        page = client.get("/admin/settings", headers={"accept": "text/html"})
    finally:
        os.chmod(conf_dir, original)

    assert page.status_code == 200
    assert "read here but not changed" in page.text
    assert "ConfigMap" in page.text
    assert "disabled" in page.text          # the controls are greyed out
    assert "Save settings" not in page.text  # and there is nothing to press
    assert "Australia/Melbourne" in page.text  # …but the values are all there


def test_saving_through_a_read_only_file_is_refused(client, session_factory,
                                                    monkeypatch, tmp_path):
    """The page hides the controls, so a POST arriving anyway is refused rather
    than raising an unhandled write error."""
    from test_routes import make_login, session_csrf

    conf_dir = tmp_path / "config"
    conf_dir.mkdir()
    path = conf_dir / "config.yaml"
    path.write_text(DOCUMENTED)
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(path))
    make_login(client, session_factory, admin=True)
    csrf = session_csrf(session_factory)
    original = stat.S_IMODE(os.stat(conf_dir).st_mode)
    os.chmod(conf_dir, 0o500)
    try:
        resp = client.post("/admin/settings",
                           data={"timezone": "Europe/Dublin", "_csrf": csrf},
                           headers={"accept": "text/html"})
    finally:
        os.chmod(conf_dir, original)

    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# Restarting
# --------------------------------------------------------------------------- #

def test_kubernetes_is_recognised_as_certain_to_restart(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")

    state = lifecycle.supervision()

    assert (state.restarts, state.certain) == (True, True)
    assert state.platform == "Kubernetes"


def test_a_container_is_recognised_but_only_probably(monkeypatch, tmp_path):
    """Nothing inside a container can see whether it was given a restart
    policy, so the wording must not promise one."""
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(lifecycle, "_in_container", lambda: True)

    state = lifecycle.supervision()

    assert state.restarts is True
    assert state.certain is False          # inferred, not known
    assert "restart policy" in state.detail


def test_a_plain_process_is_told_it_will_not_come_back(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(lifecycle, "_in_container", lambda: False)

    state = lifecycle.supervision()

    assert state.restarts is False
    assert "start it yourself" in state.detail


def test_the_restart_button_asks_the_process_to_stop(client, session_factory, monkeypatch):
    from test_routes import make_login, session_csrf

    asked = []
    monkeypatch.setattr(lifecycle, "request_stop", lambda reason: asked.append(reason))
    make_login(client, session_factory, admin=True)

    resp = client.post("/admin/settings/restart",
                       data={"_csrf": session_csrf(session_factory)},
                       headers={"accept": "text/html"})

    assert resp.status_code == 200
    assert len(asked) == 1
    assert "Restarting" in resp.text


def test_a_non_admin_cannot_restart_the_application(client, session_factory, monkeypatch):
    from test_routes import make_login, session_csrf

    asked = []
    monkeypatch.setattr(lifecycle, "request_stop", lambda reason: asked.append(reason))
    make_login(client, session_factory, admin=False)

    resp = client.post("/admin/settings/restart",
                       data={"_csrf": session_csrf(session_factory)},
                       headers={"accept": "text/html"})

    assert resp.status_code == 403
    assert asked == []


def test_restarting_needs_the_csrf_token(client, session_factory, monkeypatch):
    from test_routes import make_login

    asked = []
    monkeypatch.setattr(lifecycle, "request_stop", lambda reason: asked.append(reason))
    make_login(client, session_factory, admin=True)

    assert client.post("/admin/settings/restart").status_code == 403
    assert asked == []


def test_save_and_restart_does_both(client, session_factory, monkeypatch, tmp_path):
    """The point of the combined button: a setting that only applies at startup
    should not need two visits to take effect."""
    from test_routes import make_login, session_csrf

    conf_dir = tmp_path / "config"
    conf_dir.mkdir()
    path = conf_dir / "config.yaml"
    path.write_text(DOCUMENTED)
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(path))
    asked = []
    monkeypatch.setattr(lifecycle, "request_stop", lambda reason: asked.append(reason))
    make_login(client, session_factory, admin=True)

    resp = client.post("/admin/settings",
                       data={"price_feed.hour": "7", "restart": "1",
                             "_csrf": session_csrf(session_factory)},
                       headers={"accept": "text/html"})

    assert resp.status_code == 200
    assert "Settings saved" in resp.text
    assert len(asked) == 1                              # …and it restarted
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(path))
    assert configfile.load()["price_feed"]["hour"] == 7  # …having saved first


def test_saving_without_the_restart_flag_does_not_restart(client, session_factory,
                                                          monkeypatch, tmp_path):
    from test_routes import make_login, session_csrf

    conf_dir = tmp_path / "config"
    conf_dir.mkdir()
    path = conf_dir / "config.yaml"
    path.write_text(DOCUMENTED)
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(path))
    asked = []
    monkeypatch.setattr(lifecycle, "request_stop", lambda reason: asked.append(reason))
    make_login(client, session_factory, admin=True)

    client.post("/admin/settings",
                data={"price_feed.hour": "7", "_csrf": session_csrf(session_factory)},
                headers={"accept": "text/html"})

    assert asked == []


def test_restart_is_offered_even_when_the_config_is_read_only(client, session_factory,
                                                              monkeypatch, tmp_path):
    """A read-only config file is exactly the deployment where changes arrive
    from outside — so that is when a restart is most useful, not least."""
    from test_routes import make_login

    conf_dir = tmp_path / "config"
    conf_dir.mkdir()
    path = conf_dir / "config.yaml"
    path.write_text(DOCUMENTED)
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(path))
    make_login(client, session_factory, admin=True)
    original = stat.S_IMODE(os.stat(conf_dir).st_mode)
    os.chmod(conf_dir, 0o500)
    try:
        page = client.get("/admin/settings", headers={"accept": "text/html"})
    finally:
        os.chmod(conf_dir, original)

    assert "Save settings" not in page.text   # nothing to save
    assert "Restart now" in page.text         # but restarting still makes sense
