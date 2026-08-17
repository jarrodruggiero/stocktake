"""The app's idea of "today".

A container runs in UTC unless told otherwise, and this app is full of
date-boundary decisions: whether a trade is in the future, which financial year
is current, where a performance window starts, what the calendar highlights.
Read from UTC, all of those are wrong for the first hours of every Australian
morning — a trade entered at 8am is "tomorrow", and the new financial year
arrives a day late.

So no date decision may call `datetime.date.today()`. They call `today()` here,
which answers in the configured zone.

The zone is process-wide configuration rather than an argument, deliberately.
Threading a timezone through `queries`, `fyreport`, `plans` and `calendarview`
would put a parameter on two dozen functions that have nothing else to say
about it, and every one of those signatures would be a chance to forget. It is
set once, at startup, from `settings.timezone`.

Timestamps are a different question and stay in UTC: `created_at`, session
expiry and audit fields are instants, not dates, and are compared with
`datetime.now(timezone.utc)`. Only *calendar* decisions belong here.
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
