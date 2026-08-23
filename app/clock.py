"""The app's idea of "today".

A container runs in UTC, and this app is full of date-boundary decisions —
whether a trade is in the future, which financial year is current, what the
calendar highlights. Read from UTC every one is wrong for the first hours of an
Australian morning.

So no date decision calls `datetime.date.today()`; they call `today()` here,
which answers in the configured zone. The zone is process-wide rather than an
argument: decisions.md #82.

Timestamps stay in UTC — `created_at`, session expiry and audit fields are
instants, not dates. Only calendar decisions belong here.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from .settings import DEFAULT_TIMEZONE

_zone = ZoneInfo(DEFAULT_TIMEZONE)


def configure(name: str) -> None:
    """Point the clock at a timezone. Called once, at startup, from settings.

    Raises if the zone is unknown, so a typo in config.yaml fails at boot
    rather than silently serving dates from somewhere else.
    """
    global _zone
    _zone = ZoneInfo(name)


def zone() -> ZoneInfo:
    return _zone


def today() -> dt.date:
    """The current date where the portfolio lives."""
    return dt.datetime.now(_zone).date()


def now() -> dt.datetime:
    """The current instant, as an aware datetime in the configured zone."""
    return dt.datetime.now(_zone)
