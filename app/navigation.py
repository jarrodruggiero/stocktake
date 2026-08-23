"""Navigation and the customisable chart layout.

Both are lists of small declarations rather than markup scattered through the
templates, so the order the user asked for lives in one place and their own
preferences are a permutation of it.

Icons are inline SVG paths (24x24, stroked): no icon font, no external request,
and they inherit the accent colour. Anyone adding a nav entry adds a `path`
here — that is the whole job.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    href: str
    path: str  # SVG path data, drawn in a 24x24 viewBox
    admin_only: bool = False


# The three things that are genuinely *places*: your portfolio, your schedule,
# and getting data in and out. Everything else moved beside what it acts on —
# decisions.md #27.
NAV: tuple[NavItem, ...] = (
    NavItem("portfolio", "Portfolio", "/", "M3 13h4v8H3zM10 3h4v18h-4zM17 9h4v12h-4z"),
    NavItem("plan", "DCA Schedule", "/schedule",
            "M3 5h18v16H3zM3 9h18M8 3v4M16 3v4M8 14h3M8 17h6"),
    NavItem("imports", "Imports & Exports", "/imports-exports",
            "M12 3v10M8 9l4 4 4-4M4 17v3h16v-3"),
)

# The admin cog. Not in NAV — it renders in the top bar's right-hand group with
# the profile menu, because it is app-level and must sit in the same place on
# every page rather than move with the page list.
ADMIN_ITEM = NavItem(
    "admin", "Admin", "/admin",
    "M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6zM9 12l2 2 4-4")


# Entries a person may hide. Everything in NAV, deliberately — including
# Portfolio: the brand mark already goes home, so hiding it loses nothing, and
# somebody who has finished importing should be able to clear that entry away.
HIDEABLE = tuple(item.key for item in NAV)


def hideable_for(ctx, settings) -> list[NavItem]:
    """The entries the appearance setting should offer.

    An entry belonging to a switched-off feature is not offered: it is already
    gone, and listing it would let somebody "show" something that cannot appear.
    """
    from . import features

    hidden_by_feature = features.hidden_nav_keys(settings)
    return [item for item in NAV if item.key not in hidden_by_feature]


def nav_for(ctx, settings) -> list[NavItem]:
    """The links this person can use.

    Admin covers portfolio sharing too, so every member sees it — the page
    itself hides what they can't do. Entries owned by a switched-off optional
    feature are left out: the bar must never offer a way to a page whose only
    content is "this is turned off".

    **`settings` is required rather than optional on purpose.** It began as an
    optional `hidden` set, and six of the seven call sites — every page
    rendered by `imports_web` — silently kept showing the entry for a feature
    that was off. That is precisely the failure `features.py` exists to
    prevent, and an argument you can forget is how it happened. Now omitting it
    is a TypeError.
    """
    from . import features

    hidden = features.hidden_nav_keys(settings)
    # …and whatever this person chose to hide. Their own preference, not the
    # portfolio's — see `User.nav_hidden`.
    if ctx is not None and getattr(ctx.user, "nav_hidden", None):
        hidden = hidden | set(ctx.user.nav_hidden)
    return [
        item for item in NAV
        if item.key not in hidden
        and (not item.admin_only or (ctx and ctx.is_admin))
    ]


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ChartDef:
    key: str
    title: str
    blurb: str
    wide: bool = False


# Every chart the page can draw, in the default order. A user's saved layout is
# a permutation of these keys plus a hidden list; unknown keys are ignored, so
# removing one here can't break a saved layout.
CHARTS: tuple[ChartDef, ...] = (
    ChartDef("today", "Percentage difference today",
             "Last close vs previous close, per open holding (native currency)", True),
    ChartDef("portfolio", "Portfolio over time",
             "Daily — invested (cumulative buys), market value, and net gain "
             "incl. realised sales and cash dividends", True),
    ChartDef("alloc_invested", "Total invested %", "All-time outlay by asset class"),
    ChartDef("alloc_value", "Total position %", "Current value by asset class"),
    ChartDef("yrpct", "Yearly performance", "Gain in each FY as % of what was invested"),
    ChartDef("gainpct", "Overall performance by year", "Cumulative gain % at each FY end"),
    ChartDef("cum", "Year-end growth", "Cumulative invested vs cumulative value gain"),
    ChartDef("yearly", "Yearly invested & gain", "Per-FY deltas"),
)

CHART_BY_KEY = {c.key: c for c in CHARTS}
DEFAULT_ORDER = [c.key for c in CHARTS]


def resolve_layout(layout: dict | None) -> tuple[list[ChartDef], list[ChartDef]]:
    """(visible charts in order, hidden charts) for a saved preference.

    Tolerant by design: unknown keys are dropped and charts added to the app
    since the layout was saved appear at the end rather than vanishing.
    """
    layout = layout or {}
    hidden = set(layout.get("hidden") or [])
    saved = [k for k in (layout.get("order") or []) if k in CHART_BY_KEY]
    order = saved + [k for k in DEFAULT_ORDER if k not in saved]
    return (
        [CHART_BY_KEY[k] for k in order if k not in hidden],
        [CHART_BY_KEY[k] for k in order if k in hidden],
    )


# --------------------------------------------------------------------------- #
# Getting back to where you came from
# --------------------------------------------------------------------------- #

# Where a `?return=` can lead, and what to call it. The NAME is decided with
# the path rather than in a template: a `?return=` is an arbitrary path, and a
# template turning one into a name is guessing.
BACK_LABELS = {"/": "Portfolio", "/holdings": "Holdings",
               "/imports-exports": "Imports", "/charts": "Charts",
               "/schedule": "DCA Schedule"}


def safe_path(where: str | None, fallback: str) -> str:
    """A path on this site, or the fallback. The open-redirect guard.

    "Starts with a slash" is not enough: `//evil.test` is a protocol-relative
    URL a browser follows straight off this site, and `/\\evil.test` is treated
    the same way by some. Both look like paths and neither is one.
    """
    if not where or not where.startswith("/"):
        return fallback
    if where.startswith("//") or where.startswith("/\\"):
        return fallback
    return where


def back_label(path: str, fallback: str) -> str:
    """The name of the page `path` leads to."""
    return BACK_LABELS.get(path, fallback)
