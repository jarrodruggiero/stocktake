"""Optional features: switching one off actually switches it off.

A feature toggle is only worth having if every place that should honour it
does. There are four — the setup wizard, the settings page, the nav, and the
routes — and the failure mode is that three of them agree and the fourth
quietly keeps working. That is what these tests are for.
"""

from __future__ import annotations

import re

import pytest

from app import configfile, features, navigation
from test_routes import make_login

HTML = {"accept": "text/html"}


@pytest.fixture
def live_settings():
    """The app's one global settings object. conftest restores it after every
    test, so a test may switch a feature off without leaking."""
    from app.main import settings

    return settings


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #

def test_every_declared_feature_has_a_settings_field():
    """A registry entry with no field is a tickbox that saves nowhere."""
    from app.settings import FeatureSettings

    missing = [f.key for f in features.FEATURES if f.key not in FeatureSettings.model_fields]
    assert not missing, f"features with no settings field: {missing}"


def test_every_settings_field_has_a_registry_entry():
    """And the reverse: a field with no entry is a setting nothing honours —
    it would not appear in the wizard, the settings page, or the guard."""
    from app.settings import FeatureSettings

    missing = [name for name in FeatureSettings.model_fields if name not in features.BY_KEY]
    assert not missing, f"settings fields with no registry entry: {missing}"


def test_features_default_to_on():
    """An install that has never heard of this setting keeps everything it had
    before the setting existed."""
    from app.settings import FeatureSettings

    assert all(getattr(FeatureSettings(), f.key) for f in features.FEATURES)


def test_every_feature_is_editable_on_the_settings_page():
    """The requirement in one assertion: "all of these can be
    enabled/disabled in settings"."""
    names = {o.name for o in configfile.OPTIONS}
    for f in features.FEATURES:
        assert f"features.{f.key}" in names, f"{f.key} cannot be changed after setup"


def test_a_feature_owns_its_own_paths_and_not_lookalikes():
    """Prefix matching with a boundary: `/schedule` owns `/schedule/skip` but must not
    own a future `/planning`, or that page would be mysteriously switched off."""
    assert features.owning("/schedule").key == "dca_schedule"
    assert features.owning("/schedule/complete").key == "dca_schedule"
    assert features.owning("/planning") is None
    assert features.owning("/") is None


def test_an_unknown_key_counts_as_on():
    """A feature nobody has declared cannot have been switched off."""
    from app.settings import PortfolioSettings

    assert features.enabled(PortfolioSettings(), "not_a_feature") is True


# --------------------------------------------------------------------------- #
# Switching one off
# --------------------------------------------------------------------------- #

def test_the_nav_drops_the_entry(live_settings):
    live_settings.features.dca_schedule = False

    keys = [item.key for item in
            navigation.nav_for(None, live_settings)]

    assert "plan" not in keys
    assert "portfolio" in keys, "only the switched-off feature's entry should go"


def test_the_nav_keeps_it_while_the_feature_is_on(live_settings):
    """The other direction, so the test above cannot pass by hiding everything."""
    keys = [item.key for item in
            navigation.nav_for(None, live_settings)]

    assert "plan" in keys


def test_the_page_is_gone_from_the_bar_and_the_route_refuses(client, session_factory,
                                                             live_settings):
    make_login(client, session_factory)
    live_settings.features.dca_schedule = False

    page = client.get("/", headers=HTML)
    assert "DCA Schedule" not in page.text, "the nav still offers it"

    refused = client.get("/schedule", headers=HTML)
    assert refused.status_code == 410


def test_it_says_what_happened_rather_than_not_found(client, session_factory,
                                                     live_settings):
    """410, not 404. The page exists and is turned off; "not found" sends
    somebody hunting for a typo they did not make."""
    make_login(client, session_factory)
    live_settings.features.dca_schedule = False

    refused = client.get("/schedule", headers=HTML)

    assert "DCA Schedule" in refused.text
    assert "Settings" in refused.text, "nothing says where to turn it back on"


def test_writes_are_refused_too_not_just_the_page(client, session_factory,
                                                  live_settings):
    """With the nav entry hidden, a stale tab is exactly how a write to a
    switched-off feature arrives — so the guard covers POSTs, which is why it
    is middleware rather than a check inside the GET handler."""
    make_login(client, session_factory)
    live_settings.features.dca_schedule = False

    refused = client.post("/schedule/skip", data={"ticker": "ALPHA", "due_date": "2026-01-05"},
                          headers=HTML)

    assert refused.status_code == 410


def test_nothing_stored_is_touched_by_switching_it_off(client, session_factory,
                                                       live_settings):
    """Somebody will turn it off to tidy the nav, not to throw away two years
    of schedule. Turning it back on must restore what was there."""
    import factories as fac

    make_login(client, session_factory)
    with session_factory() as s:
        fac.make_instrument(s, "ALPHA")
        s.commit()
    token = re.search(r'name="_csrf" value="([^"]+)"',
                      client.get("/schedule", headers=HTML).text).group(1)
    client.post("/schedule/save",
                data={"_csrf": token, "name": "Regular buys", "interval_days": "28",
                      "amount": "500", "brokerage": "9.50", "tickers": "ALPHA"},
                headers=HTML, follow_redirects=True)

    live_settings.features.dca_schedule = False
    assert client.get("/schedule", headers=HTML).status_code == 410
    live_settings.features.dca_schedule = True

    back = client.get("/schedule", headers=HTML)
    assert back.status_code == 200
    assert "Regular buys" in back.text, "the plan did not survive being switched off"


# --------------------------------------------------------------------------- #
# The wizard
# --------------------------------------------------------------------------- #

def test_the_wizard_writes_nothing_when_every_default_is_accepted():
    """The shipped config is almost all comments precisely so that every value
    in it is a decision. `dca_schedule: true` would be a line for a default
    nobody changed."""
    from app import setupwizard

    draft = setupwizard.Draft()
    draft.features = {f.key: True for f in features.FEATURES}

    assert "features" not in setupwizard.config_values(draft)


def test_the_wizard_writes_only_what_was_switched_off():
    from app import setupwizard

    draft = setupwizard.Draft()
    draft.features = {"dca_schedule": False}

    assert setupwizard.config_values(draft)["features"] == {"dca_schedule": False}


def test_the_features_step_is_in_the_wizard():
    from app import setupwizard

    assert "features" in setupwizard.STEP_KEYS
    # Between the portfolio and the environment: it is about the app being set
    # up, not the machine it runs on.
    assert (setupwizard.STEP_KEYS.index("portfolio")
            < setupwizard.STEP_KEYS.index("features")
            < setupwizard.STEP_KEYS.index("environment"))


def test_every_page_honours_the_toggle_not_just_the_ones_main_renders(
        client, session_factory, live_settings):
    """The gap this test exists for, found 2026-08-08.

    `imports_web.py` builds its own template context and called `nav_for(ctx)`
    without the feature filter — so six pages kept offering a nav entry for a
    switched-off feature. `nav_for` now requires settings, which makes omitting
    it a TypeError rather than a silent wrong answer, and this checks the
    pages themselves rather than the signature.
    """
    make_login(client, session_factory)
    live_settings.features.dca_schedule = False

    for path in ("/", "/imports-exports", "/holdings", "/charts", "/profile"):
        page = client.get(path, headers=HTML)
        assert page.status_code == 200, f"{path} did not render"
        assert "DCA Schedule" not in page.text, (
            f"{path} still offers the switched-off feature in its nav")
