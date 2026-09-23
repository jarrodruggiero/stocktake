"""The FX leg asks only for days that have finished.

A live pod logged this on every startup:

    ERROR yfinance: $USDAUD=X: possibly delisted; no price data found
    (1d 2026-09-24 -> 2026-09-25)

Nothing was delisted and nothing was missing — the stored series was current to
the 23rd, and the feed was asking for the 24th, a day that had not closed. Yahoo
has no answer for an unfinished day, says so in the only vocabulary it has, and
the app logged it at ERROR.

The price leg has never done this: it stops at `settled_through(exchange)`, the
last date that cannot still move. The FX leg ran to `today` instead, so the
first run of any day asked for that day. Same bug for the same reason on every
Monday, too, where the day before today is a Sunday with no rate at all.

Why it matters more than one noisy line: "possibly delisted" at ERROR is
indistinguishable, at a glance, from a pair the app has genuinely lost. A log
that cries wolf daily is one nobody reads on the day it is right.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest
from freezegun import freeze_time
from sqlalchemy import func, select
from test_pricefeed import stub_yf

import factories as fac
from app import pricefeed, tenancy
from app.models import FxRate
from app.settings import PriceFeedSettings

BACKFILL = dt.date(2026, 7, 1)
SETTINGS = SimpleNamespace(price_feed=PriceFeedSettings(backfill_start=BACKFILL))


@pytest.fixture
def feed_session(session_factory):
    """An unscoped session — what the feed runs on.

    Declared here rather than imported from `test_pricefeed`: importing a
    fixture shadows the parameter of the same name, which ruff reports as a
    redefinition and pytest resolves by luck.
    """
    with tenancy.unscoped_session(session_factory) as session:
        yield session


# --------------------------------------------------------------------------- #
# What counts as settled for a rate
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "today, expected, why",
    [
        ("2026-09-24", "2026-09-23", "a Thursday: yesterday"),
        ("2026-09-21", "2026-09-18", "a Monday: back to Friday, not Sunday"),
        ("2026-09-20", "2026-09-18", "a Sunday: back to Friday"),
        ("2026-09-19", "2026-09-18", "a Saturday: Friday"),
    ],
)
def test_the_last_settled_rate_date(today, expected, why):
    """Yesterday, rolled back over a weekend.

    The weekend half is the part `settled_through`'s fallback does not do: it
    answers `today - 1` for a market with no known bell, which on a Monday is a
    Sunday. FX has no Sunday rate, so asking for one produces the same
    "possibly delisted" error this is meant to stop.
    """
    with freeze_time(today):
        assert pricefeed.fx_settled_through() == dt.date.fromisoformat(expected), why


# --------------------------------------------------------------------------- #
# What the feed asks for
# --------------------------------------------------------------------------- #

@freeze_time("2026-09-24 08:00:00")
def test_a_current_series_is_not_asked_for_today(feed_session, monkeypatch):
    """The reported case. Rates stored through yesterday, so there is nothing
    settled left to fetch and the provider is not called at all."""
    fac.make_instrument(feed_session, "NOVA", exchange="NASDAQ", currency="USD",
                        name="Nova")
    fac.add_fx_series(feed_session, "USDAUD", [
        (d.isoformat(), "1.50") for d in _weekdays("2026-07-01", "2026-09-23")
    ])
    feed_session.commit()
    asked: list[tuple] = []

    def _closes(symbol, start, end):
        asked.append((symbol, start, end))
        return []

    monkeypatch.setattr(pricefeed, "_closes", _closes)
    stub_yf(monkeypatch)

    pricefeed.run_feed(feed_session, SETTINGS)

    assert [a for a in asked if a[0] == "USDAUD=X"] == []


@freeze_time("2026-09-24 08:00:00")
def test_a_real_gap_is_fetched_but_only_up_to_the_settled_day(
    feed_session, monkeypatch
):
    """The other side: a genuine hole still gets filled, and the window stops
    at the last finished day rather than running into an open one."""
    fac.make_instrument(feed_session, "NOVA", exchange="NASDAQ", currency="USD",
                        name="Nova")
    fac.add_fx_series(feed_session, "USDAUD", [
        (d.isoformat(), "1.50") for d in _weekdays("2026-07-01", "2026-09-18")
    ])
    feed_session.commit()
    asked: list[tuple] = []

    def _closes(symbol, start, end):
        asked.append((symbol, start, end))
        return [(dt.date(2026, 9, 21), Decimal("1.55"))]

    monkeypatch.setattr(pricefeed, "_closes", _closes)
    stub_yf(monkeypatch)

    pricefeed.run_feed(feed_session, SETTINGS)

    (_symbol, start, end), = [a for a in asked if a[0] == "USDAUD=X"]
    assert start == dt.date(2026, 9, 19)     # the day after what is stored
    assert end == dt.date(2026, 9, 23)       # settled, NOT today
    assert feed_session.scalar(
        select(func.count()).select_from(FxRate).where(FxRate.date == dt.date(2026, 9, 21))
    ) == 1


@freeze_time("2026-09-21 08:00:00")   # a Monday
def test_on_a_monday_the_weekend_is_not_requested(feed_session, monkeypatch):
    """Stored through Friday, so a Monday has nothing settled to ask for. The
    naive `today - 1` would ask for Saturday to Sunday and get the error this
    whole file is about."""
    fac.make_instrument(feed_session, "NOVA", exchange="NASDAQ", currency="USD",
                        name="Nova")
    fac.add_fx_series(feed_session, "USDAUD", [
        (d.isoformat(), "1.50") for d in _weekdays("2026-07-01", "2026-09-18")
    ])
    feed_session.commit()
    asked: list[tuple] = []
    monkeypatch.setattr(pricefeed, "_closes",
                        lambda s, a, b: asked.append((s, a, b)) or [])
    stub_yf(monkeypatch)

    pricefeed.run_feed(feed_session, SETTINGS)

    assert [a for a in asked if a[0] == "USDAUD=X"] == []


def _weekdays(start: str, end: str) -> list[dt.date]:
    """Every weekday in the range — a dense series, so `_fetch_start` resumes
    incrementally rather than deciding it is sparse and refetching the lot."""
    day, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    out = []
    while day <= last:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out
