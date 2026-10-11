"""The admin settings page, and the config file it edits.

Two behaviours matter more than the form itself. A read-only configuration file
is a *normal* deployment (a ConfigMap, a read-only mount, a baked image), so the
page has to stay useful and explain itself rather than failing at the point of
saving. And a save must keep the file's comments — they are what makes the file
editable by hand, and stripping them on the first save would be a poor trade.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import stat
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app import configfile, lifecycle
from app.settings import PortfolioSettings

HTML = {"accept": "text/html"}

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
    assert str(configfile.load()["price_feed"]["backfill_start"]) == "2015-06-01"


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


def test_a_timezone_too_long_for_the_filesystem_is_refused_the_same_way():
    """ZoneInfo looks the name up as a file, so one too long is an OSError
    rather than not-found, and it was a 500 from the settings page."""
    with pytest.raises(ValueError, match="not a known timezone"):
        configfile.coerce(option("timezone"), "x" * 5000)


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


# --------------------------------------------------------------------------- #
# Saving the page as it is drawn
# --------------------------------------------------------------------------- #
# The tests above post the one field they are about. A browser posts every
# field on the page, and the feed timezone's box showed the zone it was only
# following: the first save, of anything, wrote that zone into the file, and a
# new timezone after it left the daily run in the old one.

def _as_sent(page: str) -> dict:
    """What a browser submits for the settings form as drawn: every field,
    a checkbox only when ticked, a list as its text."""
    from html.parser import HTMLParser

    class Form(HTMLParser):
        def __init__(self):
            super().__init__()
            self.fields, self.inside, self.text_of, self.select = {}, False, None, None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "form":
                self.inside = a.get("action") == "/admin/settings"
            if not self.inside or "disabled" in a:
                return
            if tag == "input" and a.get("name"):
                if a.get("type") != "checkbox" or "checked" in a:
                    self.fields[a["name"]] = a.get("value", "")
            elif tag == "textarea":
                self.text_of = a["name"]
                self.fields[a["name"]] = ""
            elif tag == "select":
                self.select = a["name"]
            elif tag == "option" and self.select and "selected" in a:
                self.fields[self.select] = a["value"]

        def handle_endtag(self, tag):
            if tag == "form":
                self.inside = False
            elif tag == "textarea":
                self.text_of = None
            elif tag == "select":
                self.select = None

        def handle_data(self, data):
            if self.text_of:
                self.fields[self.text_of] += data

    form = Form()
    form.feed(page)
    return form.fields


@pytest.fixture
def live_settings(monkeypatch, tmp_path):
    """The running settings and the clock, put back afterwards: a save
    applies every value to both."""
    from app import clock
    from app import main as main_mod

    conf_dir = tmp_path / "config"
    conf_dir.mkdir()
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(conf_dir / "config.yaml"))
    monkeypatch.setattr(lifecycle, "request_stop", lambda reason: None)
    live = main_mod.settings
    before = live.model_copy(deep=True)
    yield live, conf_dir / "config.yaml"
    for name in type(live).model_fields:
        setattr(live, name, getattr(before, name))
    clock.configure(before.timezone)


def _save_as_drawn(client, change: dict):
    form = _as_sent(client.get("/admin/settings", headers={"accept": "text/html"}).text)
    assert "timezone" in form and "price_feed.timezone" in form, "the form was not read"
    form.update(change)
    resp = client.post("/admin/settings", data=form, headers={"accept": "text/html"})
    assert resp.status_code == 200 and "Settings saved" in resp.text


def test_a_feed_timezone_left_to_follow_still_follows_after_a_save(client, session_factory,
                                                                   live_settings):
    from test_routes import make_login

    live, path = live_settings
    path.write_text(DOCUMENTED)                          # no feed timezone of its own
    live.timezone = live.price_feed.timezone = "Australia/Melbourne"   # as loaded from it
    make_login(client, session_factory, admin=True)

    _save_as_drawn(client, {"auth.session_ttl_days": "14"})
    _save_as_drawn(client, {"timezone": "Australia/Perth"})

    assert live.price_feed.timezone == "Australia/Perth"
    assert (configfile.load().get("price_feed") or {}).get("timezone") is None


def test_a_feed_timezone_set_on_purpose_stays_set(client, session_factory, live_settings):
    from test_routes import make_login

    live, path = live_settings
    path.write_text(DOCUMENTED.replace("  minute: 30\n", "  minute: 30\n  timezone: Asia/Tokyo\n"))
    live.timezone, live.price_feed.timezone = "Australia/Melbourne", "Asia/Tokyo"
    make_login(client, session_factory, admin=True)

    _save_as_drawn(client, {"timezone": "Australia/Perth"})

    assert live.price_feed.timezone == "Asia/Tokyo"
    assert configfile.load()["price_feed"]["timezone"] == "Asia/Tokyo"


def test_a_feed_timezone_from_the_environment_shows_as_set(live_settings, monkeypatch):
    live, path = live_settings
    path.write_text(DOCUMENTED)
    monkeypatch.setenv("APP_PRICE_FEED__TIMEZONE", "Asia/Tokyo")
    live.price_feed.timezone = "Asia/Tokyo"

    assert configfile.shown(live, configfile.BY_NAME["price_feed.timezone"]) == "Asia/Tokyo"
    monkeypatch.delenv("APP_PRICE_FEED__TIMEZONE")
    assert configfile.shown(live, configfile.BY_NAME["price_feed.timezone"]) is None


def test_a_feed_section_left_empty_shows_its_timezone_following(live_settings):
    """Every line under `price_feed:` commented out reads as nothing at all,
    not as a block: the zone is not written there, so it follows."""
    live, path = live_settings
    path.write_text("timezone: Australia/Melbourne\nprice_feed:\n  # hour: 18\n")

    assert configfile.shown(live, configfile.BY_NAME["price_feed.timezone"]) is None


# --------------------------------------------------------------------------- #
# When a save reaches the background jobs
# --------------------------------------------------------------------------- #

# 13:00 in Melbourne, 22:00 the evening before in New York.
NOW = dt.datetime(2026, 10, 12, 2, 0, tzinfo=dt.timezone.utc)


class _Stop(Exception):
    """Ends a background loop at its second wait."""


def _waits(monkeypatch, job, during_first_wait) -> list[float]:
    """Run one of main's background loops to its second wait, with the work
    stubbed and the clock stopped at NOW. `during_first_wait` runs where a
    save made while the loop sleeps would land. Returns each wait, in seconds."""
    from app import main as main_mod

    waits: list[float] = []

    async def wait(seconds):
        waits.append(seconds)
        if len(waits) > 1:
            raise _Stop
        during_first_wait()

    async def ready(what):
        return None

    class Stopped(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    monkeypatch.setattr(main_mod, "asyncio", SimpleNamespace(
        sleep=wait, get_running_loop=asyncio.get_running_loop))
    monkeypatch.setattr(main_mod, "dt", SimpleNamespace(datetime=Stopped, timedelta=dt.timedelta))
    monkeypatch.setattr(main_mod, "_await_database", ready)
    monkeypatch.setattr(main_mod, "_run_feed", lambda: True)
    monkeypatch.setattr(main_mod, "_should_poll_quotes", lambda: False)
    monkeypatch.setattr(main_mod.maintenance, "run", lambda *args: None)
    with pytest.raises(_Stop):
        asyncio.run(getattr(main_mod, job)())
    return waits


@pytest.mark.parametrize("job, schedule", [("_feed_loop", "price_feed"),
                                           ("_maintenance_loop", "maintenance")])
def test_a_feed_timezone_saved_from_the_page_moves_the_next_run(job, schedule, live_settings,
                                                                monkeypatch):
    """The page applies the feed timezone without a restart, and both daily
    jobs run in it. Each loop read the zone once, before its first run, so a
    save moved neither until the app restarted."""
    live, _ = live_settings
    live.price_feed.timezone = "Australia/Melbourne"
    getattr(live, schedule).hour, getattr(live, schedule).minute = 18, 0

    waits = _waits(monkeypatch, job, lambda: configfile.apply_live(
        live, {"price_feed.timezone": "America/New_York"}))

    first, second = (NOW + dt.timedelta(seconds=w) for w in waits)
    assert first.astimezone(ZoneInfo("Australia/Melbourne")).time() == dt.time(18, 0)
    assert second.astimezone(ZoneInfo("America/New_York")).time() == dt.time(18, 0)


def test_live_quotes_wait_the_interval_saved_from_the_page(live_settings, monkeypatch):
    """The page applies the interval without a restart, but the loop read it
    once, before its first poll, and kept waiting the old one."""
    live, _ = live_settings
    live.price_feed.quote_interval_minutes = 15

    waits = _waits(monkeypatch, "_quote_loop", lambda: configfile.apply_live(
        live, {"price_feed.quote_interval_minutes": 5}))

    assert waits == [15 * 60, 5 * 60]


def test_what_startup_reads_is_said_to_wait_for_a_restart(live_settings, monkeypatch):
    """Which background jobs run is decided once, as the app starts. A
    setting read there and applied live would let the page switch a job on
    that nothing ever starts."""
    from pydantic import BaseModel

    from app import main as main_mod

    live, _ = live_settings
    live.price_feed.enabled = live.price_feed.quotes_enabled = live.maintenance.enabled = True
    read: set[str] = set()

    class Reads:
        def __init__(self, target, prefix=""):
            self.target, self.prefix = target, prefix

        def __getattr__(self, name):
            value = getattr(self.target, name)
            read.add(self.prefix + name)
            return Reads(value, f"{self.prefix}{name}.") if isinstance(value, BaseModel) else value

    async def idle():
        return None

    async def start_and_stop():
        async with main_mod.lifespan(None):
            pass

    monkeypatch.setattr(main_mod, "settings", Reads(live))
    for job in ("_feed_loop", "_quote_loop", "_maintenance_loop"):
        monkeypatch.setattr(main_mod, job, idle)
    asyncio.run(start_and_stop())

    startup = read & set(configfile.BY_NAME)
    assert {"price_feed.enabled", "maintenance.enabled"} <= startup
    # Both ways: what waits for a restart is exactly what the app reads as it
    # starts, and the log level, read when the module is imported. Everything
    # else is read when it is used: the tests below show each.
    assert {o.name for o in configfile.OPTIONS if o.restart} == startup | {"log_level"}


def test_housekeeping_moved_from_the_page_moves_the_next_run(live_settings, monkeypatch):
    live, _ = live_settings
    live.price_feed.timezone = "Australia/Melbourne"
    live.maintenance.hour, live.maintenance.minute = 3, 0

    waits = _waits(monkeypatch, "_maintenance_loop", lambda: configfile.apply_live(
        live, {"maintenance.hour": 4, "maintenance.minute": 30}))

    first, second = (NOW + dt.timedelta(seconds=w) for w in waits)
    melbourne = ZoneInfo("Australia/Melbourne")
    assert (first.astimezone(melbourne).time(), second.astimezone(melbourne).time()) == (
        dt.time(3, 0), dt.time(4, 30))


def test_metrics_switched_on_from_the_page_serve_at_once(client, live_settings):
    live, _ = live_settings
    live.metrics.enabled = False
    assert client.get("/metrics").status_code == 404

    configfile.apply_live(live, {"metrics.enabled": True})

    assert client.get("/metrics").status_code == 200


def test_single_sign_on_set_up_from_the_page_is_offered_at_once(client, session_factory,
                                                                 live_settings):
    import factories as fac

    live, _ = live_settings
    live.auth.oidc.enabled = False
    with session_factory() as s:
        fac.make_user(s, "someone@example.test")       # past the first-run wizard
        s.commit()
    assert "Sign in with Home" not in client.get("/login", headers=HTML).text

    configfile.apply_live(live, {
        "auth.oidc.enabled": True, "auth.oidc.issuer": "https://id.example.test",
        "auth.oidc.client_id": "stocktake", "auth.oidc.client_auth": "none",
        "auth.oidc.redirect_uri": "https://stocktake.example.test/auth/oidc/callback",
        "auth.oidc.button_label": "Sign in with Home"})

    assert "Sign in with Home" in client.get("/login", headers=HTML).text


def test_passkeys_switched_on_from_the_page_are_offered_at_once(client, session_factory,
                                                                live_settings):
    from test_routes import make_login

    live, _ = live_settings
    live.auth.webauthn.enabled = False
    make_login(client, session_factory)
    before = client.get("/profile", headers=HTML).text

    configfile.apply_live(live, {
        "auth.webauthn.enabled": True, "auth.webauthn.rp_id": "stocktake.example.test",
        "auth.webauthn.origins": ["https://stocktake.example.test"]})

    after = client.get("/profile", headers=HTML).text
    assert "Turn on passkeys in Admin" in before
    assert "Turn on passkeys in Admin" not in after and "available via HTTPS" in after


def test_a_proxy_trusted_from_the_page_is_believed_at_once(client, session_factory,
                                                           live_settings):
    """The next request through it is taken as HTTPS, so signing in with
    HTTPS-only cookies stops being refused. (`client` points the app at the
    test database; the requests come from the proxy's address instead.)"""
    from fastapi.testclient import TestClient

    import factories as fac
    from app import main as main_mod

    live, _ = live_settings
    live.auth.cookie_secure, live.auth.trusted_proxies = True, []
    with session_factory() as s:
        fac.make_user(s, "someone@example.test")
        s.commit()
    proxied = TestClient(main_mod.app, client=("10.0.0.5", 50000))
    headers = {**HTML, "x-forwarded-proto": "https"}
    assert "cookie_secure" in proxied.get("/login", headers=headers).text

    configfile.apply_live(live, {"auth.trusted_proxies": ["10.0.0.0/8"]})

    assert "cookie_secure" not in proxied.get("/login", headers=headers).text


def test_ocr_switched_off_from_the_page_is_off_at_once(client, session_factory, live_settings,
                                                       monkeypatch):
    from app import statements
    from test_routes import make_login, session_csrf

    live, _ = live_settings
    live.imports.ocr.enabled = True
    monkeypatch.setattr(statements, "_pdf_text", lambda data: "")       # a scan
    monkeypatch.setattr(statements.ocr, "available", lambda: False)
    make_login(client, session_factory)

    def upload() -> str:
        return client.post("/imports-exports/statement", headers=HTML,
                           files={"file": ("scan.pdf", b"%PDF-1.4", "application/pdf")},
                           data={"_csrf": session_csrf(session_factory)}).text

    assert "turned off on this install" not in upload()
    configfile.apply_live(live, {"imports.ocr.enabled": False})
    assert "turned off on this install" in upload()


# --------------------------------------------------------------------------- #
# Reading a field, and writing the file: what a mutation run found unchecked
# --------------------------------------------------------------------------- #

def test_a_pasted_value_loses_the_spaces_around_it():
    """An issuer pasted with a trailing space is not the issuer."""
    assert configfile.coerce(option("auth.oidc.issuer"),
                             "  https://id.example.com/realms/home \t") == \
        "https://id.example.com/realms/home"


@pytest.mark.parametrize("raw", ["on", "ON", "true", "True", "1", "yes", "Yes"])
def test_every_way_of_saying_yes_is_yes(raw):
    assert configfile.coerce(option("auth.cookie_secure"), raw) is True


@pytest.mark.parametrize("opt", [o for o in configfile.OPTIONS if o.kind == "int"],
                         ids=lambda o: o.name)
def test_each_whole_number_takes_its_own_limits_and_nothing_past_them(opt):
    assert configfile.coerce(opt, str(opt.minimum)) == opt.minimum
    assert configfile.coerce(opt, str(opt.maximum)) == opt.maximum
    with pytest.raises(ValueError, match="at least"):
        configfile.coerce(opt, str(opt.minimum - 1))
    with pytest.raises(ValueError, match="at most"):
        configfile.coerce(opt, str(opt.maximum + 1))


def test_a_list_is_one_entry_per_line_with_blank_lines_dropped():
    """Blank between entries, whether empty or spaces: the box is stripped as
    a whole first, so only a line inside it tests the line's own check."""
    assert configfile.coerce(option("auth.trusted_proxies"),
                             " 10.0.0.0/8 \n\n   \n  ::1\n \n") == ["10.0.0.0/8", "::1"]


@pytest.mark.parametrize("opt", [o for o in configfile.OPTIONS if o.kind == "choice"],
                         ids=lambda o: o.name)
def test_every_offered_choice_is_accepted(opt):
    for choice in opt.choices:
        assert configfile.coerce(opt, choice) == choice


def test_saving_into_an_empty_file_works(config_file):
    config_file.write_text("")

    configfile.save({"price_feed.hour": 9})

    assert configfile.load() == {"price_feed": {"hour": 9}}


def test_saving_under_a_section_left_empty_works(config_file):
    """The shipped file leaves `oidc:` with every line under it commented
    out, which YAML reads as nothing at all."""
    config_file.write_text("auth:\n  oidc:\n    # enabled: true\n")

    configfile.save({"auth.oidc.button_label": "Sign in with Home"})

    assert configfile.load()["auth"]["oidc"]["button_label"] == "Sign in with Home"


def test_a_save_changes_its_own_line_and_no_other(config_file):
    """Quotes, list indentation and comments elsewhere are left as written,
    so a save is a one-line diff."""
    before = ("timezone: 'Australia/Melbourne'   # quoted on purpose\n"
              "auth:\n"
              "  trusted_proxies:\n"
              "    - 10.0.0.0/8\n"
              "    - \"192.168.0.0/16\"\n"
              "  session_ttl_days: 30\n")
    config_file.write_text(before)

    configfile.save({"auth.session_ttl_days": 14})

    assert config_file.read_text() == before.replace("session_ttl_days: 30",
                                                     "session_ttl_days: 14")


def test_no_file_at_all_reads_as_nothing_written(tmp_path, monkeypatch):
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(tmp_path / "absent.yaml"))

    assert configfile.load() == {}


# --------------------------------------------------------------------------- #
# Probing and writing the file, and the page's limits: the last pass's survivors
# --------------------------------------------------------------------------- #

def test_probing_leaves_nothing_behind(config_file, tmp_path, monkeypatch):
    """The settings page probes on every load, and the wizard probes a folder
    that may not exist yet. A probe left behind is litter beside somebody's
    configuration."""
    configfile.writability()
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(tmp_path / "fresh" / "config.yaml"))

    assert configfile.creatable().writable is True
    assert sorted(p.name for p in tmp_path.rglob("*")) == ["config.yaml", "fresh"]


def test_a_read_only_file_in_a_writable_folder_cannot_be_saved(config_file):
    """Its folder takes the probe, so only the file's own permission says no.
    The wizard asks the same of a file that is already there."""
    os.chmod(config_file, 0o400)
    try:
        saved, created = configfile.writability(), configfile.creatable()
    finally:
        os.chmod(config_file, 0o600)

    assert (saved.writable, created.writable) == (False, False)
    assert "read-only, which is normal" in saved.reason


@pytest.mark.parametrize("error, shown", [
    (OSError(30, "Read-only file system"), "read-only (Read-only file system), which"),
    (OSError(), "read-only, which"),
], ids=["with-words", "without"])
def test_the_reason_carries_the_systems_own_words_when_it_has_some(config_file, monkeypatch,
                                                                    error, shown):
    def refuse(*args, **kwargs):
        raise error
    monkeypatch.setattr(configfile.tempfile, "NamedTemporaryFile", refuse)

    assert shown in configfile.writability().reason


@pytest.mark.parametrize("error, detail", [
    (OSError(13, "Permission denied"), " (Permission denied)"), (OSError(), ""),
], ids=["with-words", "without"])
def test_the_wizards_reason_carries_the_systems_own_words_when_it_has_some(
        tmp_path, monkeypatch, error, detail):
    import pathlib

    folder = tmp_path / "absent"
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(folder / "config.yaml"))

    def refuse(self, *args, **kwargs):
        raise error
    monkeypatch.setattr(pathlib.Path, "mkdir", refuse)

    assert f"in {folder}{detail}. That is normal" in configfile.creatable().reason


@pytest.mark.parametrize("write", ["save", "write_tree"])
def test_a_write_that_fails_leaves_no_half_written_file(config_file, monkeypatch, write):
    """The new file is written beside the old and swapped in. A failure part
    way must not leave the half-written one, which holds the configuration."""
    from ruamel.yaml import YAML

    def full_disk(self, data, stream):
        stream.write("half")
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(YAML, "dump", full_disk)

    with pytest.raises(OSError):
        if write == "save":
            configfile.save({"auth.session_ttl_days": 14})
        else:
            configfile.write_tree({"timezone": "Asia/Tokyo"})

    assert [p.name for p in config_file.parent.iterdir()] == ["config.yaml"]
    assert config_file.read_text() == DOCUMENTED


def test_a_file_of_nothing_but_comments_reads_as_nothing_and_takes_a_write(tmp_path,
                                                                           monkeypatch):
    """Every line commented out reads as no document at all rather than an
    empty mapping: the reader and the wizard's merge both have to see one."""
    path = tmp_path / "config.yaml"
    path.write_text("# everything here is commented out\n# timezone: Asia/Tokyo\n")
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(path))

    assert configfile.load() == {}
    configfile.write_tree({"timezone": "Asia/Tokyo"})
    assert configfile.load()["timezone"] == "Asia/Tokyo"


@pytest.mark.parametrize("name", ["price_feed.hour", "price_feed.minute",
                                  "maintenance.hour", "maintenance.minute"])
def test_a_time_of_day_takes_every_value_a_clock_has_and_no_other(name):
    """The loops schedule with exactly what `datetime.time` takes."""
    field = name.rsplit(".", 1)[-1]
    limit = {"hour": 24, "minute": 60}[field]

    accepted = []
    for value in range(-1, limit + 1):
        try:
            configfile.coerce(option(name), str(value))
        except ValueError:
            continue
        accepted.append(value)
        dt.time(**{field: value})                 # and the loop can schedule it

    assert accepted == list(range(limit))


@pytest.mark.parametrize("name", [
    "auth.session_ttl_days", "auth.session_absolute_days", "auth.session_idle_minutes",
    "auth.rate_limit.max_attempts", "auth.rate_limit.window_minutes",
    "auth.rate_limit.lockout_minutes", "maintenance.attempt_retention_days",
    "maintenance.staged_upload_hours", "price_feed.quote_interval_minutes",
    "imports.max_upload_mb"])
def test_a_length_or_a_count_takes_one_and_refuses_nothing(name):
    """Nothing would sign everyone straight back out, lock them out on no
    attempts, or refuse every upload. One is the strictest setting there is."""
    assert configfile.coerce(option(name), "1") == 1
    with pytest.raises(ValueError, match="at least 1"):
        configfile.coerce(option(name), "0")


def test_no_idle_warning_at_all_is_a_choice():
    assert configfile.coerce(option("auth.idle_warning_seconds"), "0") == 0


def test_a_blank_button_label_goes_back_to_the_default():
    assert configfile.coerce(option("auth.oidc.button_label"), "  ") is None


@pytest.mark.parametrize("name", ["auth.trusted_proxies", "auth.webauthn.origins"])
def test_a_blank_list_is_saved_as_none_at_all(name):
    assert configfile.coerce(option(name), "") == []
