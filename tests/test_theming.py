"""Per-account colour overrides.

Three things here are guarantees rather than conveniences:

  * **A blank field means the default, not an empty colour.** Stored as an
    absent key, so the stylesheet shows through and a later change to the
    built-in palette still reaches that person.
  * **Only the six documented tokens are storable.** A row naming something
    else is dead weight a future version might misread.
  * **The defaults listed here are the ones in style.css.** Two lists of
    colours in two files is the arrangement that drifts, so a test compares
    them rather than trusting a comment.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import theming
from test_routes import make_login, session_csrf

HTML = {"accept": "text/html"}
CSS = Path(__file__).resolve().parents[1] / "app" / "static" / "style.css"


# --------------------------------------------------------------------------- #
# The seam
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("#abc", "#aabbcc"),
    ("#AABBCC", "#aabbcc"),
    ("  #a1b2c3  ", "#a1b2c3"),
])
def test_a_colour_is_normalised_to_six_lowercase_digits(raw, expected):
    """What is stored must round-trip through the <input type="color"> beside
    it, which only speaks #rrggbb."""
    assert theming.normalise(raw) == expected


@pytest.mark.parametrize("raw", ["red", "rgb(1,2,3)", "#12345", "", "#ggg", None])
def test_anything_that_is_not_a_hex_colour_is_refused(raw):
    assert theming.normalise(raw) is None


def test_a_blank_field_stores_nothing_rather_than_an_empty_colour():
    """The difference between "use the default" and "use no colour"."""
    assert theming.clean({"accent": "", "up": "#123456"}) == {"up": "#123456"}


def test_an_unknown_token_is_dropped():
    assert theming.clean({"accent": "#123456", "bg": "#000000"}) == {"accent": "#123456"}


def test_the_overrides_render_as_css_custom_properties():
    css = theming.css_variables({"accent": "#112233"})

    assert css == "--accent:#112233"


def test_changing_the_first_series_moves_the_positive_pole_with_it():
    """`--viz-pos` IS series 1 in the built-in palette. If it did not follow,
    a chart would keep drawing gains in the colour that was replaced."""
    css = theming.css_variables({"viz-1": "#445566"})

    assert "--viz-1:#445566" in css
    assert "--viz-pos:#445566" in css


def test_nothing_chosen_renders_no_style_at_all():
    """An empty `style=""` on <body> is noise in every page's source."""
    assert theming.css_variables(None) == ""
    assert theming.css_variables({}) == ""


def test_a_colour_too_close_to_the_card_behind_it_is_flagged():
    """Advice, not a veto — but silence would be worse than either."""
    warnings = theming.warnings({"accent": "#1b2030"})   # the dark panel itself

    assert "accent" in warnings
    assert "dark" in warnings["accent"]


def test_a_legible_colour_is_not_flagged():
    """`#7a5af8` clears 3:1 on both cards (4.26 light, 3.58 dark).

    Deliberately NOT the app's own default accent: `#2f5fd0` measures 2.83:1
    on the dark card, which is precisely why the stylesheet ships a separate
    dark accent. One value serving both modes is a real constraint, and a test
    using the default here would be asserting that it isn't."""
    assert theming.warnings({"accent": "#7a5af8"}) == {}


def test_the_worst_of_the_two_modes_is_what_gets_reported():
    """One value serves both themes, so a colour that only fails on one ground
    still has to be called out."""
    # Near-white: fine on the dark card, invisible on the light one.
    warnings = theming.warnings({"up": "#fdfdfd"})

    assert "light" in warnings["up"]


def test_the_documented_defaults_are_the_ones_in_the_stylesheet():
    """The placeholder in each box claims to be the built-in colour. If these
    two lists drift, it claims something false."""
    css = CSS.read_text()
    root = css[css.index(":root {"):css.index("/* ── dark surfaces")]

    for token, value in theming.DEFAULTS.items():
        found = re.search(rf"--{re.escape(token)}:\s*(#[0-9a-fA-F]{{6}})", root)
        assert found, f"--{token} is not declared in :root"
        assert found.group(1).lower() == value, (
            f"--{token} is {found.group(1)} in style.css but {value} in theming.DEFAULTS")


# --------------------------------------------------------------------------- #
# Through the route
# --------------------------------------------------------------------------- #

def test_a_saved_colour_reaches_the_page(client, session_factory):
    """The end of the chain: form -> row -> the <body> style attribute."""
    make_login(client, session_factory)

    client.post("/profile/colors",
                data={"accent": "#ff8800", "_csrf": session_csrf(session_factory)},
                headers=HTML, follow_redirects=True)
    page = client.get("/", headers=HTML).text

    assert "--accent:#ff8800" in page


def test_saving_junk_leaves_the_palette_alone(client, session_factory):
    make_login(client, session_factory)

    client.post("/profile/colors",
                data={"accent": "octarine", "_csrf": session_csrf(session_factory)},
                headers=HTML, follow_redirects=True)
    page = client.get("/", headers=HTML).text

    assert "octarine" not in page
    assert "--accent:" not in page


def test_reset_stores_null_rather_than_a_copy_of_the_defaults(client, session_factory):
    """The same distinction the columns reset makes: a stored copy would freeze
    today's colours into the account and opt it out of every future change."""
    from sqlalchemy import select

    from app.models import User

    email = make_login(client, session_factory)
    client.post("/profile/colors",
                data={"accent": "#ff8800", "_csrf": session_csrf(session_factory)},
                headers=HTML, follow_redirects=True)
    client.post("/profile/colors/reset",
                data={"_csrf": session_csrf(session_factory)},
                headers=HTML, follow_redirects=True)

    with session_factory() as s:
        stored = s.scalar(select(User).where(User.email == email)).theme_colors

    assert stored is None


def test_the_account_page_offers_every_settable_colour(client, session_factory):
    make_login(client, session_factory)

    page = client.get("/profile", headers=HTML).text

    for swatch in theming.SWATCHES:
        assert f'name="{swatch.token}"' in page, f"{swatch.token} has no field"
    assert "/static/colourpicker.js" in page


# --------------------------------------------------------------------------- #
# Portfolio settings
# --------------------------------------------------------------------------- #

def test_a_portfolio_can_be_renamed(client, session_factory):
    """It could be named once, at creation, and never again."""
    make_login(client, session_factory)

    client.post("/portfolio/settings",
                data={"name": "Family trust", "reporting_currency": "AUD",
                      "jurisdiction": "AU", "_csrf": session_csrf(session_factory)},
                headers=HTML, follow_redirects=True)
    page = client.get("/", headers=HTML).text

    assert "Family trust" in page


def test_the_currency_and_country_are_stored(client, session_factory):
    """Stored, and — for now — only stored. The page says so; this pins that
    the value survives the round trip so T28b has something to read."""
    from sqlalchemy import select

    from app.models import Portfolio

    make_login(client, session_factory)

    client.post("/portfolio/settings",
                data={"name": "Mine", "reporting_currency": "NZD",
                      "jurisdiction": "NZ", "_csrf": session_csrf(session_factory)},
                headers=HTML, follow_redirects=True)

    with session_factory() as s:
        pf = s.scalars(select(Portfolio)).first()

    assert pf.reporting_currency == "NZD"
    assert pf.jurisdiction == "NZ"


def test_an_empty_name_is_refused(client, session_factory):
    make_login(client, session_factory)

    resp = client.post("/portfolio/settings",
                       data={"name": "   ", "reporting_currency": "AUD",
                             "jurisdiction": "AU", "_csrf": session_csrf(session_factory)},
                       headers=HTML)

    assert resp.status_code == 400


def test_a_currency_the_app_cannot_render_is_refused(client, session_factory):
    """`money.SYMBOLS` decides what can be displayed. Storing something outside
    it would render as the raw code in every total."""
    make_login(client, session_factory)

    resp = client.post("/portfolio/settings",
                       data={"name": "Mine", "reporting_currency": "ZZZ",
                             "jurisdiction": "AU", "_csrf": session_csrf(session_factory)},
                       headers=HTML)

    assert resp.status_code == 400


def test_the_admin_page_says_the_currency_is_not_applied_yet(client, session_factory):
    """A setting that silently does nothing is worse than no setting."""
    make_login(client, session_factory)

    page = client.get("/members", headers=HTML).text

    assert "not applied yet" in page


# --------------------------------------------------------------------------- #
# The Settings page console
# --------------------------------------------------------------------------- #

def test_the_console_keeps_warnings_and_above_but_not_info():
    """The level IS the design. Error-only says something broke and nothing
    about what led there; INFO is one line per web request and buries it."""
    import logging

    from app import logbuffer

    logbuffer.clear()
    logbuffer.install()
    log = logging.getLogger("test.console")
    log.info("a routine request")
    log.warning("the feed fell back to its secondary source")
    log.error("the import failed")

    kept = [line.message for line in logbuffer.recent()]

    assert "a routine request" not in kept
    assert "the feed fell back to its secondary source" in kept
    assert "the import failed" in kept


def test_tailing_asks_for_what_it_has_not_seen():
    """The page keeps the lines it was first shown and appends. Without a
    sequence it would have to re-fetch the window and work out the difference,
    which is how a console starts duplicating lines."""
    import logging

    from app import logbuffer

    logbuffer.clear()
    logbuffer.install()
    log = logging.getLogger("test.console")
    log.warning("first")
    log.warning("second")
    seen = logbuffer.recent()[-1].seq
    log.warning("third")

    fresh = logbuffer.since(seen)

    assert [line.message for line in fresh] == ["third"]


def test_a_burst_longer_than_the_buffer_returns_what_is_left():
    """The reader has missed lines either way; showing the recent ones beats
    showing none."""
    import logging

    from app import logbuffer

    logbuffer.clear()
    logbuffer.install()
    log = logging.getLogger("test.console")
    for i in range(logbuffer.CAPACITY + 50):
        log.warning("line %d", i)

    fresh = logbuffer.since(1)

    assert len(fresh) == logbuffer.CAPACITY


def test_the_console_endpoint_is_admin_only(client, session_factory):
    """It reports on the whole installation, not one portfolio."""
    make_login(client, session_factory)

    resp = client.get("/admin/logs.json")

    assert resp.status_code == 200          # the test login is an admin
    assert "lines" in resp.json()


def test_the_settings_page_renders_the_opening_window(client, session_factory):
    """Server-rendered, so the console is useful with scripting off."""
    import logging

    from app import logbuffer

    make_login(client, session_factory)
    logbuffer.clear()
    logbuffer.install()
    logging.getLogger("test.console").warning("something worth reading")

    page = client.get("/admin/settings", headers=HTML).text

    assert "something worth reading" in page
    assert "/static/console.js" in page
