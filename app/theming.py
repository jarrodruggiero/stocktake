"""Per-account colour overrides — the theme designer.

Six colours: the accent, the two directions money can go, and the three chart
series. Surfaces, lines and ink stay with the theme, so a bad choice makes
something ugly rather than unusable (decisions.md #89).

A person picks ONE value and it applies to both light and dark, so contrast is
reported against both surfaces and the weaker one is shown — advice, not a veto.

Storage is `user.theme_colors`, token -> `#rrggbb`. Absent keys fall through to
the stylesheet, so a partial choice is a partial override.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# The surfaces a chosen colour has to be legible on: the light panel and the
# dark one. Both are `--panel-solid` from style.css.
SURFACES = {"light": "#f7f8fc", "dark": "#1b2030"}

# 3:1 is the floor for a graphical object (a chart line, a swatch); text-sized
# uses of these colours are all bold or large, which is the same floor.
MIN_CONTRAST = 3.0


@dataclass(frozen=True)
class Swatch:
    token: str          # the CSS custom property, without the leading --
    label: str
    blurb: str


# Order matters: this is the order they render in, and it goes from the colour
# people will change first to the ones they may never touch.
SWATCHES: tuple[Swatch, ...] = (
    Swatch("accent", "Accent",
           "Buttons, links, and the highlight on the page you are looking at."),
    Swatch("up", "Gain", "Money made — totals, table cells, the up arrow."),
    Swatch("down", "Loss", "Money lost."),
    Swatch("viz-1", "Chart series 1", "The first line or slice in every chart."),
    Swatch("viz-2", "Chart series 2", "The second."),
    Swatch("viz-3", "Chart series 3", "The third."),
)

BY_TOKEN = {s.token: s for s in SWATCHES}

# The built-in LIGHT value for each, shown as the input's placeholder: the
# stylesheet declares light in `:root` and dark as an override, so this is the
# base a person is choosing to replace. A test keeps it in step with style.css.
DEFAULTS = {
    "accent": "#4338ca",
    "up": "#1a7f37",
    "down": "#c62828",
    "viz-1": "#8959ba",
    "viz-2": "#1a7f37",
    "viz-3": "#aa5f04",
}


def _channels(value: str) -> tuple[int, int, int]:
    h = value.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _luminance(value: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _channels(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(value: str, surface: str) -> float:
    """WCAG contrast ratio. Same arithmetic the palette checks use."""
    a, b = sorted((_luminance(value), _luminance(surface)), reverse=True)
    return round((a + 0.05) / (b + 0.05), 2)


def normalise(value: str) -> str | None:
    """`#abc` and `#AABBCC` in, `#aabbcc` out. None if it is not a colour.

    Deliberately strict: a named colour or an `rgb()` string would work in a
    browser and then not round-trip through the `<input type="color">` beside
    it, which is a worse experience than being told no.
    """
    value = (value or "").strip()
    if not HEX.match(value):
        return None
    h = value.lstrip("#").lower()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h


def clean(submitted: dict[str, str]) -> dict[str, str]:
    """The stored form of what a person submitted.

    Unknown tokens are dropped rather than stored — the set of colours the app
    understands is `SWATCHES`, and a row naming something else would be dead
    weight that a future version might misread. A blank value means "back to
    the default", which is an absent key, not an empty string.
    """
    out: dict[str, str] = {}
    for token, raw in submitted.items():
        if token not in BY_TOKEN:
            continue
        value = normalise(raw)
        if value:
            out[token] = value
    return out


def css_variables(colors: dict | None) -> str:
    """The overrides as a `style` attribute value for <body>.

    An inline style beats both `:root` and `body[data-theme="dark"]`, which is
    exactly the precedence a personal override should have — it wins over the
    theme without the theme needing to know it exists.
    """
    if not colors:
        return ""
    parts = []
    for swatch in SWATCHES:                     # fixed order, so it is stable
        value = colors.get(swatch.token)
        if value:
            parts.append(f"--{swatch.token}:{value}")
            # The diverging poles follow their series, or a chart drawn from
            # --viz-pos keeps a stale hue when series 1 changes.
            if swatch.token == "viz-1":
                parts.append(f"--viz-pos:{value}")
    return ";".join(parts)


def warnings(colors: dict | None) -> dict[str, str]:
    """Per-token advice about a choice that will be hard to see.

    Returned rather than enforced. The check is against BOTH surfaces because
    one value serves both modes, and the weaker result is the one that matters.
    """
    out: dict[str, str] = {}
    for token, value in (colors or {}).items():
        if token not in BY_TOKEN:
            continue
        worst_mode, worst = min(
            ((mode, contrast(value, surface)) for mode, surface in SURFACES.items()),
            key=lambda pair: pair[1],
        )
        if worst < MIN_CONTRAST:
            out[token] = (
                f"Hard to see in {worst_mode} mode — {worst}:1 against the "
                f"card behind it, where {MIN_CONTRAST}:1 is the readable floor."
            )
    return out
