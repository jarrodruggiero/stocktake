"""The default charts, written in the builder's own vocabulary.

Every chart the app ships is a spec here — nothing is drawn by hand-written
code any more. That is the point: if a default can't be expressed as a spec,
then the builder can't reproduce it, and somebody who edits or deletes it can
never get it back.

A new account is seeded with these. From that moment they are that person's
charts: editing one changes their copy, deleting one removes it, and the
template stays here to be re-added from the builder's "Start from a template"
list.
"""

from __future__ import annotations

# key → template. `key` is stable and recorded on the saved chart, so a chart
# that came from a template can still be told apart from one built by hand.
TEMPLATES: tuple[dict, ...] = (
    {
        "key": "today",
        "name": "Percentage difference today",
        "blurb": "Last close vs previous close, per open holding",
        "width": "full",
        "spec": {
            "grain": "positions",
            "x": "ticker",
            "measures": ["pos_day_pct"],
            "type": "bar",
        },
    },
    {
        "key": "portfolio",
        "name": "Portfolio over time",
        "blurb": "Invested, market value and net gain, daily",
        "width": "full",
        "spec": {
            "grain": "timeseries",
            "x": "date",
            "measures": ["value", "invested", "gain"],
            "type": "line",
            "bucket": "day",
        },
    },
    {
        "key": "periods",
        "name": "Performance summary",
        "blurb": "1 day to 20 years: what you put in, what it made, the return",
        "width": "full",
        "spec": {
            "grain": "periods",
            "x": "period",
            "measures": ["p_from", "p_value_then", "p_contributed", "p_growth",
                         "p_on_money_in", "p_twr", "p_twr_annual"],
            "type": "table",
        },
    },
    {
        "key": "alloc_invested",
        "name": "Total invested %",
        "blurb": "All-time outlay by asset class",
        "width": "half",
        "spec": {
            "grain": "positions",
            "x": "asset_class",
            "measures": ["pos_cost"],
            "type": "doughnut",
        },
    },
    {
        "key": "alloc_value",
        "name": "Total position %",
        "blurb": "Current value by asset class",
        "width": "half",
        "spec": {
            "grain": "positions",
            "x": "asset_class",
            "measures": ["pos_value"],
            "type": "doughnut",
        },
    },
    {
        "key": "yrpct",
        "name": "Yearly performance",
        "blurb": "Gain in each financial year as % of what was invested",
        "width": "half",
        "spec": {
            "grain": "timeseries",
            "x": "date",
            "measures": ["period_return"],
            "type": "bar",
            "bucket": "fy",
        },
    },
    {
        "key": "gainpct",
        "name": "Overall performance by year",
        "blurb": "Cumulative return at each financial year end",
        "width": "half",
        "spec": {
            "grain": "timeseries",
            "x": "date",
            "measures": ["gain_pct"],
            "type": "bar",
            "bucket": "fy",
        },
    },
    {
        "key": "cum",
        "name": "Year-end growth",
        "blurb": "Cumulative invested vs cumulative value gain",
        "width": "half",
        "spec": {
            "grain": "timeseries",
            "x": "date",
            "measures": ["invested", "value_gain"],
            "type": "bar",
            "bucket": "fy",
        },
    },
    {
        "key": "yearly",
        "name": "Yearly invested & gain",
        "blurb": "What went in and what it made, per financial year",
        "width": "half",
        "spec": {
            "grain": "timeseries",
            "x": "date",
            "measures": ["invested_delta", "gain_delta"],
            "type": "bar",
            "bucket": "fy",
        },
    },
    {
        "key": "month_ends",
        "name": "Month ends",
        "blurb": "The daily series as a table, one row per month",
        "width": "full",
        "spec": {
            "grain": "timeseries",
            "x": "date",
            "measures": ["invested", "value", "gain", "gain_pct"],
            "type": "table",
            "bucket": "month",
        },
    },
    # Beyond the originals — cheap to offer once everything is a spec, and the
    # obvious next questions someone asks of a portfolio.
    {
        "key": "value_by_ticker",
        "name": "Value by holding",
        "blurb": "What each position is worth right now",
        "width": "half",
        "spec": {
            "grain": "positions",
            "x": "ticker",
            "measures": ["pos_value"],
            "type": "hbar",
        },
    },
    {
        "key": "gain_by_ticker",
        "name": "Gain % by holding",
        "blurb": "How each position has performed against its cost",
        "width": "half",
        "spec": {
            "grain": "positions",
            "x": "ticker",
            "measures": ["pos_gain_pct"],
            "type": "hbar",
        },
    },
    {
        "key": "value_over_time_by_holding",
        "name": "Value over time, by holding",
        "blurb": "One line per holding — how each has grown, not just the total",
        "width": "full",
        "spec": {
            "grain": "timeseries",
            "x": "date",
            "measures": ["value"],
            "split": "ticker",
            "type": "line",
            "bucket": "month",
        },
    },
    {
        "key": "value_over_time_by_class",
        "name": "Value over time, by asset class",
        "blurb": "How the mix between ETFs, shares and crypto has shifted",
        "width": "full",
        "spec": {
            "grain": "timeseries",
            "x": "date",
            "measures": ["value"],
            "split": "asset_class",
            "type": "area",
            "bucket": "month",
        },
    },
    {
        "key": "returns_by_period",
        "name": "Return by period",
        "blurb": "The time-weighted return over each window, as bars",
        "width": "half",
        "spec": {
            "grain": "periods",
            "x": "period",
            "measures": ["p_twr"],
            "type": "bar",
        },
    },
    {
        "key": "value_by_ticker_currency",
        "name": "Value by holding and currency",
        "blurb": "Each position stacked by the currency it trades in",
        "width": "half",
        "spec": {
            "grain": "positions",
            "x": "ticker",
            "measures": ["pos_value"],
            "split": "currency",
            "type": "bar",
        },
    },
    {
        "key": "income",
        "name": "Cash income by year",
        "blurb": "Distributions taken as cash rather than reinvested",
        "width": "half",
        "spec": {
            "grain": "timeseries",
            "x": "date",
            "measures": ["cash_div"],
            "type": "bar",
            "bucket": "fy",
        },
    },
)

BY_KEY = {t["key"]: t for t in TEMPLATES}

# What a new account starts with, in this order. The extras above are offered
# in the builder but not applied unasked — a first look at the page should be
# the set we designed, not everything we could think of.
DEFAULT_KEYS = (
    "today",
    "portfolio",
    "periods",
    "alloc_invested",
    "alloc_value",
    "yrpct",
    "gainpct",
    "cum",
    "yearly",
    "month_ends",
)
