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
    assert theming.clean({"light-accent": "", "light-up": "#123456"}) == {
        "light": {"up": "#123456"}}


def test_an_unknown_token_or_mode_is_dropped():
    assert theming.clean({"light-accent": "#123456", "light-bg": "#000000",
                          "sepia-up": "#111111", "accent": "#222222"}) == {
        "light": {"accent": "#123456"}}


def test_each_mode_keeps_its_own_colours():
    """One colour per mode: a colour that reads on the light card rarely reads
    on the dark one (decisions.md #89)."""
    assert theming.clean({"light-up": "#111111", "dark-up": "#eeeeee"}) == {
        "light": {"up": "#111111"}, "dark": {"up": "#eeeeee"}}


def _rules(css: str) -> dict[str, str]:
    """{selector, with any @media wrapped round it: declarations}."""
    found = {}
    for media, selector, body in re.findall(
            r"(?:(@media [^{]+)\{)?([^{}@]+)\{([^}]*)\}\}?", css):
        found[(media.strip() + " " if media else "") + selector.strip()] = body
    return found


def test_a_light_colour_applies_only_where_the_page_is_light():
    rules = _rules(theming.stylesheet({"light": {"up": "#112233"}}))

    assert rules == {
        "body[data-colors][data-theme=light]": "--up:#112233",
        "@media (prefers-color-scheme: light) body[data-colors][data-theme=auto]": "--up:#112233",
    }


def test_a_dark_colour_applies_only_where_the_page_is_dark():
    rules = _rules(theming.stylesheet({"dark": {"down": "#ff6666"}}))

    assert rules == {
        "body[data-colors][data-theme=dark]": "--down:#ff6666",
        "@media (prefers-color-scheme: dark) body[data-colors][data-theme=auto]": "--down:#ff6666",
    }


def test_nothing_but_a_checked_colour_reaches_the_stylesheet():
    """The rules are written into a <style> element, so a stored value is
    checked again on the way out, as a colour or nothing."""
    css = theming.stylesheet({"light": {"up": "#fff}</style><script>x()</script>",
                                        "bg": "#000000", "down": "#abc"}})

    assert css == ("body[data-colors][data-theme=light]{--down:#aabbcc}"
                   "@media (prefers-color-scheme: light)"
                   "{body[data-colors][data-theme=auto]{--down:#aabbcc}}")


@pytest.mark.parametrize("accent", ["#0f766e", "#fbbf24", "#7c3aed", "#888888", "#ff8800"])
def test_a_chosen_accent_paints_the_buttons_in_words_that_read_on_it(accent):
    """Only `--accent` was set, but buttons paint `--accent-grad`, which kept
    the theme's own colours: the swatch promised "Buttons, links…" and moved
    no button. The words on them follow the colour too."""
    [body] = set(_rules(theming.stylesheet({"dark": {"accent": accent}})).values())
    style = dict(part.split(":", 1) for part in body.split(";"))
    stops = re.findall(r"#[0-9a-f]{6}", style["--accent-grad"])
    words = style["--on-accent"]

    assert style["--accent"] == accent and stops[0] == accent and len(stops) == 2
    assert words in ("#ffffff", "#0d1018")
    # The better of the two, and the far end no harder to read than the near.
    other = "#0d1018" if words == "#ffffff" else "#ffffff"
    assert theming.contrast(words, accent) >= theming.contrast(other, accent)
    assert theming.contrast(words, stops[1]) >= theming.contrast(words, stops[0]) >= 4.5


def test_changing_the_first_series_moves_the_positive_pole_with_it():
    """`--viz-pos` IS series 1 in the built-in palette. If it did not follow,
    a chart would keep drawing gains in the colour that was replaced."""
    [body] = set(_rules(theming.stylesheet({"light": {"viz-1": "#445566"}})).values())

    assert body == "--viz-1:#445566;--viz-pos:#445566"


def test_nothing_chosen_renders_no_style_at_all():
    """An empty <style> is noise in every page's source."""
    assert theming.stylesheet(None) == ""
    assert theming.stylesheet({}) == ""
    assert theming.stylesheet({"light": {}, "dark": {}}) == ""


def test_each_built_in_colour_meets_its_floor_on_its_own_card():
    """One value had to serve both modes, and no colour is 4.5:1 on both
    cards, so the floor was 3:1 for everything. Per mode, text is held to 4.5:1
    and a chart's lines to 3:1 — the built-in colours included, or a person
    choosing one back would be warned about the app's own."""
    weak = {(mode, s.token): theming.contrast(theming.DEFAULTS[mode][s.token],
                                              theming.SURFACES[mode])
            for mode in theming.MODES for s in theming.SWATCHES
            if theming.contrast(theming.DEFAULTS[mode][s.token],
                                theming.SURFACES[mode]) < s.floor}

    assert weak == {}
    assert {s.token: s.floor for s in theming.SWATCHES} == {
        "accent": 4.5, "up": 4.5, "down": 4.5, "viz-1": 3.0, "viz-2": 3.0, "viz-3": 3.0}


def test_a_colour_too_close_to_the_card_behind_it_is_flagged():
    """Advice, not a veto — but silence would be worse than either."""
    warnings = theming.warnings({"dark": {"accent": "#1b2030"}})   # the dark panel itself

    assert list(warnings) == ["dark"] and list(warnings["dark"]) == ["accent"]


def test_text_is_held_to_the_text_floor_and_a_chart_to_the_graphic_one():
    """#8a7f3f measures 3.81:1 on the light card: enough for a chart's line,
    not for the words in a gain cell."""
    assert theming.contrast("#8a7f3f", theming.SURFACES["light"]) == 3.81
    warnings = theming.warnings({"light": {"up": "#8a7f3f", "viz-1": "#8a7f3f"}})

    assert list(warnings["light"]) == ["up"]
    assert "4.5:1" in warnings["light"]["up"]


def test_each_mode_is_measured_on_its_own_card():
    """Near-white is fine on the dark card and invisible on the light one."""
    assert theming.warnings({"dark": {"up": "#fdfdfd"}}) == {}
    assert list(theming.warnings({"light": {"up": "#fdfdfd"}})["light"]) == ["up"]


def test_the_documented_defaults_are_the_ones_in_the_stylesheet():
    """The placeholder in each box claims to be the built-in colour. If these
    two lists drift, it claims something false."""
    css = CSS.read_text(encoding="utf-8")
    blocks = {"light": css[css.index(":root {"):css.index("/* ── dark surfaces")],
              "dark": re.search(r'\nbody\[data-theme="dark"\] \{([^}]*)\}', css).group(1)}

    for mode, block in blocks.items():
        for token, value in theming.DEFAULTS[mode].items():
            found = re.search(rf"--{re.escape(token)}:\s*(#[0-9a-fA-F]{{6}})", block)
            assert found, f"--{token} is not declared for {mode}"
            assert found.group(1).lower() == value, (
                f"{mode} --{token} is {found.group(1)} in style.css but {value} here")


# --------------------------------------------------------------------------- #
# Through the route
# --------------------------------------------------------------------------- #

def _save(client, session_factory, **colours):
    return client.post("/profile/colors",
                       data={**colours, "_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=True)


def test_a_saved_colour_reaches_the_page(client, session_factory):
    """The end of the chain: form -> row -> the page's <style>."""
    make_login(client, session_factory)

    _save(client, session_factory, **{"dark-accent": "#ff8800"})
    page = client.get("/", headers=HTML).text

    style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
    assert "body[data-colors][data-theme=dark]{--accent:#ff8800;" in style
    assert "--accent-grad:linear-gradient(135deg, #ff8800, " in style
    assert re.search(r"<body [^>]*\bdata-colors\b", page)


def test_saving_junk_leaves_the_palette_alone(client, session_factory):
    make_login(client, session_factory)

    _save(client, session_factory, **{"light-accent": "octarine"})
    page = client.get("/", headers=HTML).text

    assert "octarine" not in page
    assert "--accent:" not in page
    assert not re.search(r"<body [^>]*\bdata-colors\b", page)


def test_reset_stores_null_rather_than_a_copy_of_the_defaults(client, session_factory):
    """The same distinction the columns reset makes: a stored copy would freeze
    today's colours into the account and opt it out of every future change."""
    from sqlalchemy import select

    from app.models import User

    email = make_login(client, session_factory)
    _save(client, session_factory, **{"light-accent": "#ff8800"})
    client.post("/profile/colors/reset",
                data={"_csrf": session_csrf(session_factory)},
                headers=HTML, follow_redirects=True)

    with session_factory() as s:
        stored = s.scalar(select(User).where(User.email == email)).theme_colors

    assert stored is None


def test_the_account_page_offers_every_colour_in_each_mode(client, session_factory):
    make_login(client, session_factory)
    _save(client, session_factory, **{"dark-up": "#ffffff"})

    page = client.get("/profile", headers=HTML).text

    for mode in theming.MODES:
        for swatch in theming.SWATCHES:
            assert f'name="{mode}-{swatch.token}"' in page, f"{mode} {swatch.token} has no field"
            assert f'placeholder="{theming.DEFAULTS[mode][swatch.token]}"' in page
            # Heard without the tab, so each names its mode: "a dark colour"
            # reads as a shade.
            assert f'aria-label="Pick the {swatch.label} colour for {mode} mode"' in page
            assert f'aria-label="{swatch.label} colour code for {mode} mode"' in page
    assert 'value="#ffffff"' in page
    assert 'role="tab"' in page and "/static/colourtabs.js" in page
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
