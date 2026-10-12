"""Text against what it sits on, measured off the stylesheet itself.

Dark mode drew white on its accent at 3.18:1 down to 2.64:1, on the teal and
amber accents at under 2:1, and on badges at 2.60:1. Light table headers
were 4.03:1, and the light teal and amber buttons under 4.5:1 too. 4.5:1 is
the floor for text this size (WCAG 1.4.3). Read from style.css, so a colour
changed later is measured again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import theming

CSS = (Path(__file__).resolve().parent.parent / "app" / "static" / "style.css").read_text(
    encoding="utf-8")
FLOOR = 4.5
ACCENTS = ("teal", "violet", "amber")
HEX = re.compile(r"#[0-9a-fA-F]{6}\b")


def _tokens(selector: str, css: str = CSS) -> dict[str, str]:
    """The custom properties set in the first rule for exactly `selector`."""
    found = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert found, f"no rule for {selector}"
    return dict(re.findall(r"--([\w-]+)\s*:\s*([^;]+);", found.group(1)))


def _theme(mode: str, accent: str | None) -> dict[str, str]:
    """The tokens in force, in the order the cascade applies them. An accent's
    own rule matches in both modes and comes after the dark block, so what it
    sets that the dark accent rule does not is in force in dark mode too."""
    tokens = dict(_tokens(":root"))
    if mode == "dark":
        tokens.update(_tokens('body[data-theme="dark"]'))
    if accent:
        tokens.update(_tokens(f'body[data-accent="{accent}"]'))
        if mode == "dark":
            tokens.update(_tokens(f'body[data-theme="dark"][data-accent="{accent}"]'))
    return tokens


THEMES = [(mode, accent) for mode in ("light", "dark") for accent in (None, *ACCENTS)]
IDS = [f"{mode}-{accent or 'indigo'}" for mode, accent in THEMES]


@pytest.mark.parametrize("mode, accent", THEMES, ids=IDS)
def test_a_buttons_words_are_legible_on_its_gradient(mode, accent):
    t = _theme(mode, accent)
    stops = HEX.findall(t["accent-grad"])

    assert len(stops) == 2
    assert [theming.contrast(t["on-accent"], s) >= FLOOR for s in stops] == [True, True], (
        [(s, theming.contrast(t["on-accent"], s)) for s in stops])


@pytest.mark.parametrize("mode, accent", THEMES, ids=IDS)
def test_a_badges_words_are_legible_on_every_badge(mode, accent):
    t = _theme(mode, accent)

    weak = {name: theming.contrast(t["on-badge"], t[name])
            for name in ("accent", "up", "muted", "down")
            if theming.contrast(t["on-badge"], t[name]) < FLOOR}

    assert weak == {}


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_a_table_heading_is_legible_on_its_band(mode):
    t = _theme(mode, None)

    assert theming.contrast(t["muted"], t["sunken"]) >= FLOOR
    assert theming.contrast(t["muted"], t["bg"]) >= FLOOR


@pytest.mark.parametrize("selector, token", [
    (".badge", "on-badge"), (".wizrail .wiz-done .wiznum", "on-badge"),
    ("button", "on-accent"), ("button.primary", "on-accent"), ("a.button", "on-accent"),
])
def test_the_words_are_drawn_in_the_colour_measured_above(selector, token):
    # The selector alone or in a list: `button, a.button {` is a rule for both.
    rule = re.search(r"(?m)^(?:[^{}\n]*,\s*)?" + re.escape(selector)
                     + r"(?:\s*,[^{}]*)?\s*\{([^}]*)\}", CSS)

    assert rule, selector
    assert re.search(r"(?<![\w-])color:\s*var\(--" + token + r"\)", rule.group(1)), selector


def test_the_device_dark_mode_is_the_chosen_dark_mode():
    """Declared twice by design (the stylesheet's rule 1), so measured once
    and held equal."""
    auto = re.search(r"@media \(prefers-color-scheme: dark\)\s*\{(.*?)\n\}", CSS, re.S)
    assert auto
    assert _tokens('body[data-theme="auto"]', auto.group(1)) == _tokens('body[data-theme="dark"]')
    for accent in ACCENTS:
        assert (_tokens(f'body[data-theme="auto"][data-accent="{accent}"]', CSS)
                == _tokens(f'body[data-theme="dark"][data-accent="{accent}"]'))
