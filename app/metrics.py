"""Prometheus metrics — about the machine, never about the money.

This is the only endpoint in the app that reads the database without a session,
which makes the choice of *what* to publish the important part and the
exposition format the easy part.

**The rule: every series here describes the app's own health.** When prices were
last fetched, whether the last fetch worked, whether the database answers. There
is deliberately nothing about holdings, values, trade counts or even how many
instruments are tracked — those describe the person using it, and this app's
whole promise is that they stay on the machine. `tests/test_metrics.py` pins the
name list closed so that adding one is a decision rather than an accident.

**Why it is off by default.** Every other route needs a session. An upgrade that
quietly started answering an anonymous caller would be a surprise, and the
answer being harmless is not the same as the change being expected. Turn it on
with `metrics.enabled: true` when something is there to scrape it.

No `prometheus_client` dependency. Six series in a fixed format is less code
than wiring up a registry, and the app's memory budget is the reason
to avoid a library that pulls in more than it saves.
"""

from __future__ import annotations

import datetime as dt
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import FxRate, Price

# The version matters: Prometheus content-negotiates, and a bare `text/plain`
# is accepted but leaves the parser guessing. 0.0.4 is the text exposition
# format everything speaks.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Process-lifetime counters. Prometheus expects counters to reset when a process
# restarts and handles it (`increase()`/`rate()` detect the drop), so there is
# nothing to persist here — and persisting them would mean a write on every feed
# run for a number nobody reads except a graph.
_runs = 0
_failures = 0
_last_success: float | None = None


def reset() -> None:
    """Back to a fresh process's state. For tests."""
    global _runs, _failures, _last_success
    _runs = 0
    _failures = 0
    _last_success = None


def record_feed_run(*, ok: bool) -> None:
    """Called once per price-feed run, whatever the outcome.

    `_last_success` is only stamped on success on purpose: the question the
    alert asks is "how long since this last *worked*", and refreshing the
    timestamp on a failure would answer "just now" forever while every fetch
    was failing.
    """
    global _runs, _failures, _last_success
    _runs += 1
    if ok:
        _last_success = time.time()
    else:
        _failures += 1


def _midnight_utc(day: dt.date) -> float:
    """A date as unix seconds.

    Prices are dated, not timestamped — a close belongs to a trading day, not a
    moment. UTC midnight is the conventional way to say that in a gauge, and it
    means an alert's arithmetic is the same everywhere regardless of the
    install's timezone.
    """
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp()


def _render(name: str, help_text: str, kind: str, value: float) -> str:
    # `:.0f` rather than repr: these are whole seconds, and the default float
    # formatting turns them into 1.7543e+09, which is legal and unreadable at
    # 3am when an alert has just fired.
    return (f"# HELP {name} {help_text}\n"
            f"# TYPE {name} {kind}\n"
            f"{name} {value:.0f}\n")


def render(session: Session | None) -> str:
    """The exposition body.

    `session` is None when the app has no database yet (the first-run wizard has
    not been finished). That is a legitimate state, not an error: report
    `stocktake_database_ready 0` and skip the series that would need a query.
    """
    ready = session is not None
    out = [_render(
        "stocktake_database_ready",
        "Whether the app can reach its database (1) or not (0).",
        "gauge", 1 if ready else 0,
    )]

    if session is not None:
        # Market data is shared across every portfolio, so these read unscoped
        # by nature — there is no tenancy question to answer here, which is
        # also why they are safe to publish.
        last_price = session.execute(select(func.max(Price.date))).scalar()
        last_fx = session.execute(select(func.max(FxRate.date))).scalar()
        # Absent, not zero, when there is nothing. A zero would claim prices
        # were last updated in 1970 and fire the staleness alert on every fresh
        # install; absent lets a rule use `absent()` if it wants to say
        # something about that case, and say nothing if it does not.
        if last_price is not None:
            out.append(_render(
                "stocktake_last_price_date_seconds",
                "Trading day of the most recent close held, as unix seconds (UTC midnight).",
                "gauge", _midnight_utc(last_price),
            ))
        if last_fx is not None:
            out.append(_render(
                "stocktake_last_fx_date_seconds",
                "Date of the most recent FX rate held, as unix seconds (UTC midnight).",
                "gauge", _midnight_utc(last_fx),
            ))

    out.append(_render(
        "stocktake_feed_runs_total",
        "Price feed runs since this process started.",
        "counter", _runs,
    ))
    out.append(_render(
        "stocktake_feed_failures_total",
        "Price feed runs that raised, since this process started.",
        "counter", _failures,
    ))
    if _last_success is not None:
        out.append(_render(
            "stocktake_feed_last_success_seconds",
            "When the price feed last completed without raising, as unix seconds.",
            "gauge", _last_success,
        ))

    return "".join(out)
