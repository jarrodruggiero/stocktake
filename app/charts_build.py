"""Execute a chart spec: {grain, x, measures, split, bucket, filters, type} → data.

The builder hands specs here both for the live preview and for saved charts on
the charts page, so what you see while dragging is drawn by the same code that
draws it afterwards — no second implementation to drift.

Everything reads from `cached_portfolio_series` and `all_holdings`, which the
charts page has already computed, so a preview costs a bucket-and-sum rather
than another pass over six years of prices.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy.orm import Session

from . import fields, queries

# A chart nobody can read is a chart nobody should be served.
MAX_POINTS = 400


def _fy_label(d: dt.date) -> str:
    end = d.year if d.month <= 6 else d.year + 1
    return f"FY{str(end - 1)[-2:]}/{str(end)[-2:]}"


def _bucket_key(d: dt.date, bucket: str) -> str:
    if bucket == "week":
        monday = d - dt.timedelta(days=d.weekday())
        return monday.isoformat()
    if bucket == "month":
        return d.strftime("%Y-%m")
    if bucket == "quarter":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    if bucket == "fy":
        return _fy_label(d)
    return d.isoformat()


def _num(value) -> float:
    if isinstance(value, Decimal):
        return round(float(value), 6)
    return round(float(value or 0), 6)


# How each timeseries measure is derived from a bucket. `last` is the state at
# the end of the bucket (running totals), `sum` totals a flow within it, and the
# derived ones are named computations over those — deliberately a fixed set
# rather than a formula language, so every field can be explained in a sentence.
_TS_MEASURES = {
    "value": lambda cur, prev: cur["last"]["value"],
    "invested": lambda cur, prev: cur["last"]["invested"],
    "gain": lambda cur, prev: cur["last"]["gain"],
    "gain_pct": lambda cur, prev: cur["last"]["gain_pct"],
    "flow_in": lambda cur, prev: cur["sum"]["flow_in"],
    "cash_div": lambda cur, prev: cur["sum"]["cash_div"],
    "value_gain": lambda cur, prev: cur["last"]["value"] - cur["last"]["invested"],
    "invested_delta": lambda cur, prev: cur["last"]["invested"] - prev["invested"],
    "gain_delta": lambda cur, prev: cur["last"]["gain"] - prev["gain"],
    "period_return": lambda cur, prev: (
        (cur["last"]["gain"] - prev["gain"]) / cur["last"]["invested"]
        if cur["last"]["invested"]
        else 0.0
    ),
}

_TS_BASE = ("value", "invested", "gain", "gain_pct", "flow_in", "cash_div")


def _bucket_series(series: dict, spec: dict, measures: list[str], label_prefix: str = "") -> tuple[list[str], list[dict]]:
    """Bucket one daily series and evaluate the measures over it."""
    dates = series["dates"]
    bucket = spec.get("bucket") or "day"
    since = spec.get("since")

    order: list[str] = []
    agg: dict[str, dict] = {}
    for i, iso in enumerate(dates):
        if since and iso < since:
            continue
        key = _bucket_key(dt.date.fromisoformat(iso), bucket)
        slot = agg.get(key)
        if slot is None:
            slot = agg[key] = {"last": {}, "sum": {f: 0.0 for f in _TS_BASE}}
            order.append(key)
        for f in _TS_BASE:
            slot["last"][f] = series[f][i]
            slot["sum"][f] += series[f][i]

    rows: dict[str, dict[str, float]] = {}
    prev = {f: 0.0 for f in _TS_BASE}
    for key in order:
        cur = agg[key]
        rows[key] = {m: float(_TS_MEASURES[m](cur, prev)) for m in measures}
        prev = dict(cur["last"])
    return order, [
        {
            "key": m,
            "label": (label_prefix + fields.BY_KEY[m].label) if not label_prefix
            else label_prefix,
            "kind": fields.BY_KEY[m].kind,
            "rows": rows,
        }
        for m in measures
    ]


def _thin(buckets: list[str]) -> list[str]:
    if len(buckets) <= MAX_POINTS:
        return buckets
    step = len(buckets) // MAX_POINTS + 1
    kept = buckets[::step]
    if buckets[-1] not in kept:
        kept.append(buckets[-1])
    return kept


def _timeseries_split(session: Session, spec: dict) -> dict:
    """One line per group — value per ticker over time, and the like.

    Restricted to a single measure on purpose: three measures across eight
    holdings is twenty-four series, which is a colour soup rather than a chart.
    """
    grouped = queries.cached_grouped_series(session, spec["split"], spec.get("filters"))
    measure = spec["measures"][0]
    labels: list[str] = []
    datasets = []
    for name, series in grouped["groups"].items():
        series = {**series, "dates": grouped["dates"]}
        order, built = _bucket_series(series, spec, [measure], label_prefix=name)
        if not labels:
            labels = _thin(order)
        rows = built[0]["rows"]
        datasets.append(
            {
                "key": f"{measure}:{name}",
                "label": name,
                "kind": fields.BY_KEY[measure].kind,
                "data": [round(rows[b][measure], 4 if fields.BY_KEY[measure].kind == "percent" else 2)
                         for b in labels],
            }
        )
    # Biggest last value first: the legend then reads in the order the lines sit.
    datasets.sort(key=lambda d: d["data"][-1] if d["data"] else 0, reverse=True)
    return {"labels": labels, "datasets": datasets}


def _timeseries(session: Session, spec: dict) -> dict:
    if spec.get("split"):
        return _timeseries_split(session, spec)
    series = queries.cached_portfolio_series(session)
    dates = series["dates"]
    if not dates:
        return {"labels": [], "datasets": []}

    bucket = spec.get("bucket") or "day"
    measures = [m for m in spec["measures"] if m in _TS_MEASURES]
    since = spec.get("since")  # ISO date, from the filter shelf

    # One pass: for each bucket keep the closing value of every base field and
    # the running total of the flow ones.
    order: list[str] = []
    agg: dict[str, dict] = {}
    for i, iso in enumerate(dates):
        if since and iso < since:
            continue
        key = _bucket_key(dt.date.fromisoformat(iso), bucket)
        slot = agg.get(key)
        if slot is None:
            slot = agg[key] = {"last": {}, "sum": {f: 0.0 for f in _TS_BASE}}
            order.append(key)
        for f in _TS_BASE:
            slot["last"][f] = series[f][i]
            slot["sum"][f] += series[f][i]

    # Deltas measure against the previous bucket's close, starting from zero —
    # the same basis the FY chart used before these became specs.
    rows: dict[str, dict[str, float]] = {}
    prev = {f: 0.0 for f in _TS_BASE}
    for key in order:
        cur = agg[key]
        rows[key] = {m: float(_TS_MEASURES[m](cur, prev)) for m in measures}
        prev = dict(cur["last"])

    buckets = _thin(order)  # thinned, not truncated, so the shape holds

    return {
        "labels": buckets,
        "datasets": [
            {
                "key": m,
                "label": fields.BY_KEY[m].label,
                "kind": fields.BY_KEY[m].kind,
                "data": [round(rows[b][m], 4 if fields.BY_KEY[m].kind == "percent" else 2)
                         for b in buckets],
            }
            for m in measures
        ],
    }


# Column key → where it lives in a period_summary row.
_PERIOD_MEASURES = {
    "p_from": "from",
    "p_value_then": "value_then",
    "p_contributed": "contributed",
    "p_growth": "growth",
    "p_on_money_in": "on_money_in",
    "p_twr": "twr",
    "p_twr_annual": "twr_annual",
}


def _periods(session: Session, spec: dict) -> dict:
    """One row per performance window, with whichever columns were chosen.

    The rows are fixed (1 day … all time) because they're windows, not data —
    but the columns are ordinary fields, which is what makes the performance
    summary editable rather than a black box. The cached series is passed in;
    period_summary would otherwise redo the whole six-year walk for one card.
    """
    summary = queries.period_summary(session, queries.cached_portfolio_series(session))
    measures = [m for m in spec["measures"] if m in _PERIOD_MEASURES]
    return {
        "labels": [r["label"] for r in summary],
        "datasets": [
            {
                "key": m,
                "label": fields.BY_KEY[m].label,
                "kind": fields.BY_KEY[m].kind,
                "data": [r.get(_PERIOD_MEASURES[m]) for r in summary],
            }
            for m in measures
        ],
    }


def _positions(session: Session, spec: dict) -> dict:
    holdings, _closed = queries.split_positions(queries.cached_holdings(session))
    filters = spec.get("filters") or {}
    holdings = [h for h in holdings if queries._matches(h.instrument, filters)]
    x = spec["x"]
    measures = [m for m in spec["measures"] if m in fields.BY_KEY]

    def dimension(h) -> str:
        return {
            "ticker": h.instrument.ticker,
            "asset_class": h.instrument.asset_class,
            "currency": h.instrument.currency,
            "exchange": h.instrument.exchange,
        }[x]

    def measure(h, key: str):
        return {
            "pos_value": h.value_aud,
            "pos_cost": h.cost_aud,
            "pos_gain": h.gain_aud,
            "pos_gain_pct": h.gain_pct,
            "pos_dividends": h.dividends_cash,
            "pos_units": h.units,
            "pos_day_change": h.day_change_aud,
            "pos_day_pct": h.day_pct,
        }[key]

    split = spec.get("split")

    def split_of(h) -> str:
        return {
            "ticker": h.instrument.ticker,
            "asset_class": h.instrument.asset_class,
            "currency": h.instrument.currency,
            "exchange": h.instrument.exchange,
        }[split]

    grouped: dict[str, dict[str, list]] = {}
    # (x, split) -> measure -> values, when a second dimension is in play.
    split_grouped: dict[tuple[str, str], dict[str, list]] = {}
    split_labels: list[str] = []
    for h in holdings:
        key = dimension(h)
        slot = grouped.setdefault(key, {m: [] for m in measures})
        for m in measures:
            v = measure(h, m)
            if v is not None:
                slot[m].append(v)
        if split:
            sk = split_of(h)
            if sk not in split_labels:
                split_labels.append(sk)
            sub = split_grouped.setdefault((key, sk), {m: [] for m in measures})
            for m in measures:
                v = measure(h, m)
                if v is not None:
                    sub[m].append(v)

    def combine(field_key: str, values: list) -> float:
        if not values:
            return 0.0
        if fields.BY_KEY[field_key].agg == "ratio":
            # Percentages don't add up: average them, weighted equally, rather
            # than producing a number nobody can interpret.
            return _num(sum(values) / len(values))
        return _num(sum(values))

    labels = sorted(grouped)
    # Biggest first reads better for a bar or doughnut of one measure.
    if measures and spec.get("type") in ("bar", "hbar", "doughnut"):
        labels.sort(key=lambda k: combine(measures[0], grouped[k][measures[0]]), reverse=True)

    if split:
        # One series per value of the second dimension. A measure that adds
        # up stacks to the same total as the unsplit bar; a ratio does not add
        # up, so it is grouped instead — decisions.md #28.
        m = measures[0]
        return {
            "labels": labels,
            "stacked": fields.BY_KEY[m].agg != "ratio",
            "datasets": [
                {
                    "key": f"{m}:{sk}",
                    "label": sk,
                    "kind": fields.BY_KEY[m].kind,
                    "data": [
                        combine(m, split_grouped.get((k, sk), {m: []})[m]) for k in labels
                    ],
                }
                for sk in sorted(split_labels)
            ],
        }

    return {
        "labels": labels,
        "datasets": [
            {
                "key": m,
                "label": fields.BY_KEY[m].label,
                "kind": fields.BY_KEY[m].kind,
                "data": [combine(m, grouped[k][m]) for k in labels],
            }
            for m in measures
        ],
    }


def run(session: Session, spec: dict) -> dict:
    """Data for a spec. Raises ValueError with a readable reason if it can't."""
    problem = fields.validate(spec)
    if problem:
        raise ValueError(problem)
    if spec["grain"] == "periods":
        data = _periods(session, spec)
    elif spec["grain"] == "timeseries":
        data = _timeseries(session, spec)
    else:
        data = _positions(session, spec)
    data["type"] = spec.get("type", "line")
    data["grain"] = spec["grain"]
    data["x_label"] = fields.BY_KEY[spec["x"]].label
    return data


# --------------------------------------------------------------------------- #
# The Portfolio page's summary chart
# --------------------------------------------------------------------------- #

# How far back each range reaches. `None` means "everything", and `fy` is
# resolved against the jurisdiction's financial year rather than a fixed span —
# see `summary_ranges`.
SUMMARY_RANGES: tuple[tuple[str, str, int | None], ...] = (
    ("1d", "1D", 1),
    ("1w", "1W", 7),
    ("1mo", "1M", 30),
    ("3mo", "3M", 91),
    ("6mo", "6M", 182),
    ("fy", "FY", None),          # this financial year — special-cased below
    ("1y", "1Y", 365),
    ("3y", "3Y", 365 * 3),
    ("5y", "5Y", 365 * 5),
    ("10y", "10Y", 365 * 10),
    ("20y", "20Y", 365 * 20),
    ("30y", "30Y", 365 * 30),
    ("all", "All", None),
)


def summary_ranges(dates: list[str], today: dt.date, fy_start: dt.date) -> list[dict]:
    """The range buttons worth offering, given how much history there is.

    **Only what can actually be drawn.** A portfolio three years old showing
    10Y, 20Y and 30Y draws three identical charts and reads as broken — the
    buttons imply data that is not there. So a span is offered only when the
    history is long enough to make it different from the one before it.

    `All` is always offered, and is the answer for anything longer.
    """
    if not dates:
        return []
    first = dt.date.fromisoformat(dates[0])
    span = (today - first).days

    out = []
    for key, label, days in SUMMARY_RANGES:
        if key == "all":
            out.append({"key": key, "label": label, "from": dates[0]})
            continue
        if key == "fy":
            # Offered only once the financial year has actually started
            # producing data — otherwise it is an empty chart with a name.
            if first <= today and fy_start >= first:
                out.append({"key": key, "label": label, "from": fy_start.isoformat()})
            continue
        if days is not None and days <= span:
            out.append({"key": key, "label": label,
                        "from": (today - dt.timedelta(days=days)).isoformat()})
    return out


def gain_series(series: dict) -> list[float]:
    """Cumulative gain as a percentage of what went in, per day.

    The money you put in is not something you made, so it comes off the top:
    `(value + proceeds + cash income − paid in) / paid in`. Paying $1,000 in
    adds $1,000 to both sides and creates no gain. Not a time-weighted return —
    decisions.md #54.

    `queries.portfolio_series` already computes it, so this is a unit change and
    not a second definition: the chart must not be able to disagree with the
    Gain tile.
    """
    return [round(v * 100, 4) for v in (series.get("gain_pct") or [])]


def summary_chart(session: Session, today: dt.date, fy_start: dt.date) -> dict | None:
    """The Portfolio page's performance chart: value against what went in.

    Reuses `cached_portfolio_series` — the same daily numbers every other chart
    is built from, so this cannot disagree with the charts page about how the
    portfolio has done.

    None when there is nothing to plot, which the template renders as an
    invitation rather than an empty axis.
    """
    series = queries.cached_portfolio_series(session)
    dates = series.get("dates") or []
    if len(dates) < 2:
        return None
    return {
        "dates": dates,
        # Cumulative gain %, already in percent — see `gain_series`. A LEVEL,
        # not an index: selecting a range slices the window rather than
        # rebasing it, so every range ends on the portfolio's actual gain and
        # "All" is the same number the Gain tile shows.
        "gain": gain_series(series),
        "ranges": summary_ranges(dates, today, fy_start),
    }
