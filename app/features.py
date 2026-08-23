"""Optional features: the ones an install can switch off.

A feature is one entry here — key, name, description, what it hides — and the
nav entry, route guard, wizard tickbox and settings row all read this list.
Four places that must agree on what a feature is called is how the fourth gets
missed.

Off means hidden, not deleted: `/schedule` says so rather than 404, and nothing
stored is touched, so switching it back on restores what was there
(decisions.md #87). Trades recorded through a feature are ordinary trades and
are never affected.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Feature:
    key: str            # the settings field name under `features:`
    name: str           # what the wizard and the settings page call it
    blurb: str          # what it does, for somebody deciding
    # Nav entries this feature owns. Hidden when it is off, so the bar never
    # offers a way to a page that will only tell you it is unavailable.
    nav_keys: tuple[str, ...] = ()
    # Route prefixes it owns, for the guard. Checked with `startswith`, so
    # "/schedule" covers "/schedule/complete" and "/schedule/skip" without listing them —
    # a list that has to be kept in step with the routes would fall behind.
    routes: tuple[str, ...] = ()


FEATURES: tuple[Feature, ...] = (
    Feature(
        key="dca_schedule",
        name="DCA Schedule",
        blurb=(
            "A calendar of scheduled buys built from a rotation you set — which "
            "ticker is next, when it is due, and a record-it button that advances "
            "the schedule. Turn it off if you invest in lump sums rather than on "
            "a cycle; your trades and holdings are unaffected."
        ),
        nav_keys=("plan",),
        routes=("/schedule",),
    ),
)

BY_KEY = {f.key: f for f in FEATURES}


def enabled(settings, key: str) -> bool:
    """Whether a feature is on. Unknown keys are on, because a feature nobody
    has declared cannot have been switched off."""
    return bool(getattr(settings.features, key, True))


def hidden_nav_keys(settings) -> set[str]:
    """Nav entries to leave out, because the feature that owns them is off."""
    return {
        nav_key
        for feature in FEATURES
        if not enabled(settings, feature.key)
        for nav_key in feature.nav_keys
    }


def owning(path: str) -> Feature | None:
    """The feature that owns a request path, if any.

    Prefix matching with a boundary check: `/schedule` owns `/schedule/complete` but
    must not own a future `/planning` — a bare `startswith` would swallow it
    and the bug would show up as a page that is mysteriously switched off.
    """
    for feature in FEATURES:
        for route in feature.routes:
            if path == route or path.startswith(route + "/"):
                return feature
    return None
