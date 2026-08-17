"""Dates are answered in the portfolio's timezone, not the container's.

Every test here picks an instant where the two disagree — the hours between
midnight in Melbourne and midnight in UTC — because that window is the entire
bug. A container runs in UTC, so for the first ten hours of every Australian
morning a UTC container believes it is still yesterday: a trade entered at 8am
was "in the future", and the new financial year arrived a day late.
"""

from __future__ import annotations

import datetime as dt

import pytest
from freezegun import freeze_time

import factories as fac
from app import clock, fyreport
from app.settings import DEFAULT_TIMEZONE, PortfolioSettings

# 2026-08-01 23:00 UTC is 2026-08-02 09:00 in Melbourne (AEST, +10). The two
# calendars disagree about what day it is, which is the only interesting case.
EARLY_MORNING_UTC = "2026-08-01 23:00:00"
MELBOURNE_DATE = dt.date(2026, 8, 2)
UTC_DATE = dt.date(2026, 8, 1)


@pytest.fixture(autouse=True)
def melbourne():
    """Every test in this file runs with the clock configured as production is."""
    clock.configure(DEFAULT_TIMEZONE)
    yield
    clock.configure(DEFAULT_TIMEZONE)


# --------------------------------------------------------------------------- #
# The clock itself
# --------------------------------------------------------------------------- #

@freeze_time(EARLY_MORNING_UTC)
def test_today_is_the_local_date_not_the_utc_one():
    assert dt.datetime.now(dt.timezone.utc).date() == UTC_DATE   # what the box thinks
    assert clock.today() == MELBOURNE_DATE                        # what the app must use


@freeze_time(EARLY_MORNING_UTC)
def test_now_is_an_aware_local_instant():
    moment = clock.now()

    assert moment.tzinfo is not None
    assert (moment.hour, moment.date()) == (9, MELBOURNE_DATE)


def test_configuring_an_unknown_zone_fails_loudly():
    """A typo in config.yaml must stop the app at boot rather than quietly
    serving dates from somewhere else."""
    from zoneinfo import ZoneInfoNotFoundError

    with pytest.raises(ZoneInfoNotFoundError):
        clock.configure("Australia/Nowhere")


@freeze_time(EARLY_MORNING_UTC)
def test_the_zone_is_configurable_not_hardcoded():
    """The app is Australian by default, not by assumption — someone in another
    country configures their own zone and every date follows it."""
    clock.configure("Europe/London")   # 2026-08-02 00:00 UTC+1 → still 1 Aug

    assert clock.today() == dt.date(2026, 8, 2)

    clock.configure("America/New_York")   # 2026-08-01 19:00 EDT
    assert clock.today() == dt.date(2026, 8, 1)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

# Settings priority is env > YAML > constructor defaults, so these drive the
# values through the environment — passing kwargs would be silently overridden
# by tests/config.test.yaml, and the test would prove nothing.
def test_the_price_feed_follows_the_app_timezone_by_default(monkeypatch):
    monkeypatch.setenv("APP_TIMEZONE", "Europe/Berlin")

    settings = PortfolioSettings()

    assert settings.timezone == "Europe/Berlin"
    assert settings.price_feed.timezone == "Europe/Berlin"


def test_the_price_feed_can_still_keep_its_own_hours(monkeypatch):
    """A market can trade in a different zone from the one you count days in."""
    monkeypatch.setenv("APP_TIMEZONE", "Europe/Berlin")
    monkeypatch.setenv("APP_PRICE_FEED__TIMEZONE", "Australia/Sydney")

    settings = PortfolioSettings()

    assert settings.price_feed.timezone == "Australia/Sydney"


# --------------------------------------------------------------------------- #
# What it fixes
# --------------------------------------------------------------------------- #

@freeze_time(EARLY_MORNING_UTC)
def test_a_trade_dated_today_is_not_rejected_as_being_in_the_future(client,
                                                                   session_factory):
    """The headline symptom: recording this morning's trade before ~10am was
    refused, because the container's "today" was still yesterday."""
    from sqlalchemy import select

    from app.models import Trade
    from test_routes import bind_to_only_portfolio, make_login, session_csrf

    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME", name="Acme Industries")
        s.commit()
        acme_id = acme.id

    resp = client.post(
        "/trade/new",
        data={"instrument_id": str(acme_id), "type": "buy",
              "trade_date": MELBOURNE_DATE.isoformat(),   # "today", locally
              "quantity": "10", "unit_price": "4.00", "brokerage": "0",
              "_csrf": session_csrf(session_factory)},
        headers={"accept": "text/html"}, follow_redirects=False)

    assert resp.status_code == 303
    with session_factory() as s:
        bind_to_only_portfolio(s)
        assert s.scalars(select(Trade)).one().date == MELBOURNE_DATE


@freeze_time(EARLY_MORNING_UTC)
def test_tomorrow_is_still_refused(client, session_factory):
    """The fix must move the boundary, not remove it."""
    from test_routes import bind_to_only_portfolio, make_login, session_csrf

    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME")
        s.commit()
        acme_id = acme.id

    resp = client.post(
        "/trade/new",
        data={"instrument_id": str(acme_id), "type": "buy",
              "trade_date": (MELBOURNE_DATE + dt.timedelta(days=1)).isoformat(),
              "quantity": "1", "unit_price": "1.00", "brokerage": "0",
              "_csrf": session_csrf(session_factory)},
        headers={"accept": "text/html"})

    assert "future" in resp.text.lower()


# 2026-06-30 14:00 UTC is 2026-07-01 00:00 in Melbourne: the instant the
# Australian financial year turns over.
@freeze_time("2026-06-30 14:00:00")
def test_the_new_financial_year_arrives_on_1_july_locally(pf):
    """A new FY must appear automatically on 1 July.
    Under UTC it appeared a day late, because 1 July in Melbourne is still
    30 June in London."""
    acme = fac.make_instrument(pf, "ACME")
    fac.add_trade(pf, acme, "2021-03-01", "buy", 10, "5.00")
    pf.commit()

    assert clock.today() == dt.date(2026, 7, 1)
    # July belongs to the FY *ending* 2027, and it must be offered immediately.
    assert fyreport.available_fys(pf)[0] == 2027


@freeze_time("2026-06-30 13:00:00")
def test_the_old_financial_year_is_still_current_an_hour_earlier(pf):
    """23:00 on 30 June, locally — the FY has not turned over yet."""
    acme = fac.make_instrument(pf, "ACME")
    fac.add_trade(pf, acme, "2021-03-01", "buy", 10, "5.00")
    pf.commit()

    assert clock.today() == dt.date(2026, 6, 30)
    assert fyreport.available_fys(pf)[0] == 2026
