"""Per-account colour overrides — the theme designer.

Six colours: the accent, the two directions money can go, and the three chart
series. Surfaces, lines and ink stay with the theme, so a bad choice makes
something ugly rather than unusable (decisions.md #89).

Light and dark each have their own six, so each is measured on its own card,
against the floor for what it is drawn as — advice, not a veto.

Storage is `user.theme_colors`, mode -> token -> `#rrggbb`. Absent keys fall
through to the stylesheet, so a partial choice is a partial override.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

MODES = ("light", "dark")

# The surface a chosen colour has to be legible on in each mode: the panel.
# Both are `--panel-solid` from style.css.
SURFACES = {"light": "#f7f8fc", "dark": "#1b2030"}

# The floors a colour is held to, by what it is drawn as: 15px text needs
# 4.5:1 (WCAG 1.4.3), a chart's line or slice 3:1 (1.4.11).
TEXT, GRAPHIC = 4.5, 3.0

# Words on a chosen accent's buttons: the dark ground's own colour, or white.
INK = "#0d1018"


@dataclass(frozen=True)
class Swatch:
    token: str          # the CSS custom property, without the leading --
    label: str
    blurb: str
    floor: float        # TEXT or GRAPHIC


# Order matters: this is the order they render in, and it goes from the colour
# people will change first to the ones they may never touch.
SWATCHES: tuple[Swatch, ...] = (
    Swatch("accent", "Accent",
           "Buttons, links, and the highlight on the page you are looking at.", TEXT),
    Swatch("up", "Gain", "Money made — totals, table cells, the up arrow.", TEXT),
    Swatch("down", "Loss", "Money lost.", TEXT),
    Swatch("viz-1", "Chart series 1", "The first line or slice in every chart.", GRAPHIC),
    Swatch("viz-2", "Chart series 2", "The second.", GRAPHIC),
    Swatch("viz-3", "Chart series 3", "The third.", GRAPHIC),
)

BY_TOKEN = {s.token: s for s in SWATCHES}

# The built-in value for each in each mode, shown as the input's placeholder.
# A test keeps them in step with style.css.
DEFAULTS = {
    "light": {"accent": "#4338ca", "up": "#1a7f37", "down": "#c62828",
              "viz-1": "#8959ba", "viz-2": "#1a7f37", "viz-3": "#aa5f04"},
    "dark": {"accent": "#8b83f0", "up": "#2ea043", "down": "#f25050",
             "viz-1": "#a37ecd", "viz-2": "#2ea043", "viz-3": "#c77f3e"},
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


def clean(submitted) -> dict[str, dict[str, str]]:
    """The stored form of what a person submitted, from fields named
    `<mode>-<token>`.

    Anything else is ignored rather than stored — the colours the app
    understands are `SWATCHES` in `MODES`, and a row naming something else
    would be dead weight that a future version might misread. A blank value
    means "back to the default", which is an absent key, not an empty string.
    """
    out: dict[str, dict[str, str]] = {}
    for mode in MODES:
        chosen = {s.token: value for s in SWATCHES
                  if (value := normalise(submitted.get(f"{mode}-{s.token}", "")))}
        if chosen:
            out[mode] = chosen
    return out


def _chosen(colors, mode: str) -> dict:
    found = colors.get(mode) if isinstance(colors, dict) else None
    return found if isinstance(found, dict) else {}


def _toward(colour: str, other: str, share: float) -> str:
    """`colour` moved `share` of the way to `other`, in sRGB."""
    a = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * share):02x}" for x, y in zip(a, b))


def _accent(value: str) -> list[str]:
    """A chosen accent as buttons paint it: the gradient, and the words on
    it. White or ink, whichever reads better; the gradient runs from the
    colour away from the words, so its far end is never harder to read."""
    words = "#ffffff" if contrast("#ffffff", value) >= contrast(INK, value) else INK
    far = _toward(value, "#000000" if words == "#ffffff" else "#ffffff", 0.2)
    return [f"--accent-grad:linear-gradient(135deg, {value}, {far})", f"--on-accent:{words}"]


def _declarations(chosen: dict) -> str:
    parts = []
    for swatch in SWATCHES:                     # fixed order, so it is stable
        # Checked again on the way out: this is written into a <style>.
        value = normalise(chosen.get(swatch.token) or "")
        if value:
            parts.append(f"--{swatch.token}:{value}")
            # The diverging poles follow their series, or a chart drawn from
            # --viz-pos keeps a stale hue when series 1 changes.
            if swatch.token == "viz-1":
                parts.append(f"--viz-pos:{value}")
            # Buttons paint the gradient, which the theme otherwise fixes.
            if swatch.token == "accent":
                parts += _accent(value)
    return ";".join(parts)


# Each mode's rule applies only where the page is in that mode, an `auto` page
# following the device. `[data-colors]` puts it above the stylesheet's accent
# presets, two attributes deep, and it comes after them. Unquoted attribute
# values, so autoescaping has nothing to change.
_RULES = {
    "light": ("body[data-colors][data-theme=light]{%s}"
              "@media (prefers-color-scheme: light){body[data-colors][data-theme=auto]{%s}}"),
    "dark": ("body[data-colors][data-theme=dark]{%s}"
             "@media (prefers-color-scheme: dark){body[data-colors][data-theme=auto]{%s}}"),
}


def stylesheet(colors: dict | None) -> str:
    """The overrides as the text of a <style> element, or "" for none."""
    out = []
    for mode in MODES:
        body = _declarations(_chosen(colors, mode))
        if body:
            out.append(_RULES[mode] % (body, body))
    return "".join(out)


def warnings(colors: dict | None) -> dict[str, dict[str, str]]:
    """Advice about a choice that will be hard to see, per mode and token:
    returned rather than enforced."""
    out: dict[str, dict[str, str]] = {}
    for mode in MODES:
        for token, raw in _chosen(colors, mode).items():
            swatch, value = BY_TOKEN.get(token), normalise(raw or "")
            if swatch is None or value is None:
                continue
            ratio = contrast(value, SURFACES[mode])
            if ratio < swatch.floor:
                out.setdefault(mode, {})[token] = (
                    f"Hard to see — {ratio}:1 against the card behind it, where "
                    f"{swatch.floor:g}:1 is the readable floor.")
    return out
