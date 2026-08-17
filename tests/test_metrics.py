"""The Prometheus endpoint.

Two things are being tested here and only one of them is the exposition format.

The other is a **privacy boundary**. This is the app's only unauthenticated
endpoint that reads the database, and the standing rule for the whole project is
that holdings, values and trades never leave the machine. So `test_the_metric
_names_are_a_closed_set` asserts the exact list of series names: adding a metric
is a deliberate act that fails a test until someone writes the new name down,
rather than something that happens by accident while adding a feature.

The endpoint is **off by default** for the same reason. Every other route in
this app needs a session; an upgrade that silently opened an anonymous one would
be a surprise, and a surprise in the direction nobody wants.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest

from app import metrics
from factories import add_fx, add_price, add_trade, make_instrument


@pytest.fixture(autouse=True)
def _fresh_counters():
    """Counters are module state; one test's feed run is not another's."""
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture
def on(app_module, monkeypatch):
    """Turn the endpoint on for the duration of one test."""
    monkeypatch.setattr(app_module.settings.metrics, "enabled", True)
    return app_module.settings


def body(client) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    return response.text


def series(text: str) -> dict[str, float]:
    """Parse the exposition format into {name: value}, ignoring comments."""
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition(" ")
        out[name] = float(value)
    return out


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def test_it_is_off_by_default(client):
    """No config, no endpoint. 404, not 403 — a 403 confirms it exists."""
    assert client.get("/metrics").status_code == 404


def test_the_default_in_settings_is_off(app_module):
    """Belt to the route's braces: the setting itself defaults to disabled, so
    an install that never mentions metrics never serves them."""
    from app.settings import MetricsSettings

    assert MetricsSettings().enabled is False


def test_it_serves_when_enabled(client, on):
    assert client.get("/metrics").status_code == 200


def test_it_needs_no_session(client, on):
    """The whole point: Prometheus has no cookie and cannot log in.

    `client` here has never authenticated, so a 200 proves the login
    middleware lets this path through.
    """
    assert "pf_session" not in client.cookies
    assert client.get("/metrics").status_code == 200


def test_it_is_served_as_prometheus_text(client, on):
    content_type = client.get("/metrics").headers["content-type"]
    assert content_type.startswith("text/plain")
    assert "version=0.0.4" in content_type


# --------------------------------------------------------------------------- #
# The signal the staleness alert is built on
# --------------------------------------------------------------------------- #

def test_last_price_date_is_the_newest_close_held(client, on, db, session_factory):
    inst = make_instrument(db, "ALPHA")
    add_price(db, inst, "2026-07-30", "100.00")
    add_price(db, inst, "2026-08-01", "101.00")
    db.commit()

    value = series(body(client))["stocktake_last_price_date_seconds"]
    expected = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc).timestamp()
    assert value == expected


def test_last_price_date_is_absent_when_there_are_no_prices(client, on):
    """A fresh install has no prices, and saying so with a `0` would claim they
    were last updated in 1970 — which would fire the staleness alert on every
    new install. Absent is the honest answer, and Prometheus has `absent()`
    for callers who want to alert on it."""
    text = body(client)
    assert "stocktake_last_price_date_seconds" not in text


def test_last_fx_date_is_the_newest_rate_held(client, on, db):
    add_fx(db, "USDAUD", "2026-07-29", "1.52")
    add_fx(db, "USDAUD", "2026-07-31", "1.53")
    db.commit()

    value = series(body(client))["stocktake_last_fx_date_seconds"]
    assert value == dt.datetime(2026, 7, 31, tzinfo=dt.timezone.utc).timestamp()


def test_database_ready_is_one_when_the_database_answers(client, on):
    assert series(body(client))["stocktake_database_ready"] == 1


# --------------------------------------------------------------------------- #
# Feed counters
# --------------------------------------------------------------------------- #

def test_feed_counters_start_at_zero(client, on):
    values = series(body(client))
    assert values["stocktake_feed_runs_total"] == 0
    assert values["stocktake_feed_failures_total"] == 0


def test_a_successful_run_counts_and_stamps(client, on):
    metrics.record_feed_run(ok=True)

    values = series(body(client))
    assert values["stocktake_feed_runs_total"] == 1
    assert values["stocktake_feed_failures_total"] == 0
    assert values["stocktake_feed_last_success_seconds"] > 0


def test_a_failed_run_counts_as_both_a_run_and_a_failure(client, on):
    metrics.record_feed_run(ok=False)

    values = series(body(client))
    assert values["stocktake_feed_runs_total"] == 1
    assert values["stocktake_feed_failures_total"] == 1


def test_a_failure_does_not_stamp_the_success_time(client, on):
    """The alert asks 'how long since this last WORKED'. A failed run that
    refreshed that timestamp would answer 'just now' forever."""
    metrics.record_feed_run(ok=False)

    assert "stocktake_feed_last_success_seconds" not in body(client)


def test_the_success_time_survives_a_later_failure(client, on):
    metrics.record_feed_run(ok=True)
    first = series(body(client))["stocktake_feed_last_success_seconds"]
    metrics.record_feed_run(ok=False)

    assert series(body(client))["stocktake_feed_last_success_seconds"] == first


def test_the_real_feed_records_a_run(client, on, app_module, monkeypatch, session_factory):
    """The counter is wired into `_run_feed`, not just callable in isolation.

    A metric nobody increments is worse than no metric: it reads as a healthy
    zero forever.
    """
    monkeypatch.setattr(app_module.pricefeed, "run_feed", lambda s, settings: {"prices": 0})
    app_module._run_feed()

    assert series(body(client))["stocktake_feed_runs_total"] == 1


def test_the_real_feed_records_a_failure(client, on, app_module, monkeypatch):
    def boom(session, settings):
        raise RuntimeError("yahoo said no")

    monkeypatch.setattr(app_module.pricefeed, "run_feed", boom)
    app_module._run_feed()

    values = series(body(client))
    assert values["stocktake_feed_runs_total"] == 1
    assert values["stocktake_feed_failures_total"] == 1


# --------------------------------------------------------------------------- #
# The privacy boundary
# --------------------------------------------------------------------------- #

ALLOWED = {
    "stocktake_database_ready",
    "stocktake_last_price_date_seconds",
    "stocktake_last_fx_date_seconds",
    "stocktake_feed_runs_total",
    "stocktake_feed_failures_total",
    "stocktake_feed_last_success_seconds",
}


def test_the_metric_names_are_a_closed_set(client, on, db):
    """**Read the module docstring before changing this list.**

    This endpoint is unauthenticated. Every name here has been checked to
    describe the *machine* — when it last fetched, whether it worked — and
    never the portfolio. Adding "number of holdings" or "total value" would
    publish someone's finances to anyone who can reach the port.
    """
    inst = make_instrument(db, "ALPHA")
    add_price(db, inst, "2026-08-01", "101.00")
    add_fx(db, "USDAUD", "2026-08-01", "1.53")
    db.commit()
    metrics.record_feed_run(ok=True)

    assert set(series(body(client))) <= ALLOWED


def test_it_says_nothing_about_the_portfolio(client, on, pf, db, session_factory):
    """Values chosen to be unmistakable if they ever appeared."""
    inst = make_instrument(db, "ALPHA")
    add_price(db, inst, "2026-08-01", "424242.00")
    db.commit()
    add_trade(pf, inst, "2026-07-01", "buy", "1313", "424242.00")
    pf.commit()

    text = body(client)
    for leak in ("424242", "1313", "ALPHA"):
        assert leak not in text


# --------------------------------------------------------------------------- #
# Well-formedness — a broken exposition breaks the whole scrape, not one series
# --------------------------------------------------------------------------- #

def test_every_series_is_documented_and_typed(client, on, db):
    inst = make_instrument(db, "ALPHA")
    add_price(db, inst, "2026-08-01", "101.00")
    add_fx(db, "USDAUD", "2026-08-01", "1.53")
    db.commit()
    metrics.record_feed_run(ok=True)

    text = body(client)
    for name in series(text):
        assert f"# HELP {name} " in text
        assert f"# TYPE {name} " in text


def test_counters_are_typed_as_counters_and_the_rest_as_gauges(client, on):
    text = body(client)
    assert "# TYPE stocktake_feed_runs_total counter" in text
    assert "# TYPE stocktake_feed_failures_total counter" in text
    assert "# TYPE stocktake_database_ready gauge" in text


def test_the_body_ends_with_a_newline(client, on):
    """Required by the exposition format; some parsers reject the last line
    without it."""
    assert body(client).endswith("\n")


def test_no_value_is_rendered_in_scientific_notation(client, on, db):
    """`1.7543e+09` is legal Prometheus, but these are timestamps people read
    off a scrape by hand when an alert fires."""
    inst = make_instrument(db, "ALPHA")
    add_price(db, inst, "2026-08-01", "101.00")
    db.commit()

    for line in body(client).splitlines():
        if line and not line.startswith("#"):
            assert not re.search(r"[eE][+-]", line), line
