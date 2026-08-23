"""The fields a custom chart can be built from.

A chart spec is four choices: a dimension for the X axis, one or more measures,
optionally a second dimension to split into series, and a filter. This declares
the vocabulary; `charts_build.py` executes a spec against it.

Three grains, which cannot be mixed because they answer different questions:

  timeseries — one row per day. X is always the date.
  positions  — one row per holding, as it stands now. X is a holding attribute,
               so these aggregate.
  periods    — one row per performance window. Fixed rows, chosen columns.

`grain` on every field is what stops the builder offering "market value by
ticker over time" and then having to explain why it cannot draw it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    grain: str        # timeseries | positions
    role: str         # dimension | measure
    kind: str = "money"   # money | percent | number | text | date
    blurb: str = ""
    # Measures only: how rows combine when several fall in one bucket.
    agg: str = "sum"


FIELDS: tuple[Field, ...] = (
    # ---- daily series -----------------------------------------------------
    Field("date", "Date", "timeseries", "dimension", "date",
          "Every day the market produced a close"),
    Field("value", "Market value", "timeseries", "measure", "money",
          "What the holdings were worth that day", agg="last"),
    Field("invested", "Invested (cumulative)", "timeseries", "measure", "money",
          "Everything paid in to that day, incl. brokerage", agg="last"),
    Field("gain", "Net gain", "timeseries", "measure", "money",
          "Value + realised proceeds + cash income − invested", agg="last"),
    Field("gain_pct", "Return %", "timeseries", "measure", "percent",
          "Net gain over what was put in", agg="last"),
    Field("flow_in", "Bought", "timeseries", "measure", "money",
          "Money put in that day", agg="sum"),
    Field("cash_div", "Cash income", "timeseries", "measure", "money",
          "Distributions taken as cash, not reinvested", agg="sum"),
    # Derived measures. Each is a named computation over the base series
    # (see charts_build._TS_MEASURES) rather than a formula language — the
    # vocabulary stays small enough that every chart in it can be explained.
    Field("value_gain", "Value gain", "timeseries", "measure", "money",
          "Market value less what was put in — unrealised, excludes income",
          agg="derived"),
    Field("invested_delta", "Invested in period", "timeseries", "measure", "money",
          "How much more went in during each bucket", agg="derived"),
    Field("gain_delta", "Gain in period", "timeseries", "measure", "money",
          "How much the net gain moved during each bucket", agg="derived"),
    Field("period_return", "Return in period %", "timeseries", "measure", "percent",
          "Gain made in the bucket over what was invested by its end",
          agg="derived"),

    # ---- holdings ---------------------------------------------------------
    Field("ticker", "Ticker", "positions", "dimension", "text"),
    Field("asset_class", "Asset class", "positions", "dimension", "text",
          "ETF, share or crypto"),
    Field("currency", "Currency", "positions", "dimension", "text"),
    Field("exchange", "Exchange", "positions", "dimension", "text"),
    Field("pos_value", "Market value", "positions", "measure", "money",
          "Current value in AUD"),
    Field("pos_cost", "Cost", "positions", "measure", "money",
          "What was paid, incl. brokerage, in AUD"),
    Field("pos_gain", "Capital gain", "positions", "measure", "money",
          "Value less cost, in AUD"),
    Field("pos_gain_pct", "Gain %", "positions", "measure", "percent",
          "Capital gain over cost", agg="ratio"),
    Field("pos_dividends", "Dividends", "positions", "measure", "money",
          "Distributions received, native currency"),
    Field("pos_units", "Units", "positions", "measure", "number"),
    Field("pos_day_change", "Change today", "positions", "measure", "money",
          "Move since the previous close, in AUD"),
    Field("pos_day_pct", "Change today %", "positions", "measure", "percent",
          agg="ratio"),

    # ---- performance windows ---------------------------------------------
    # The rows are fixed (1 day … all time); these are the columns. Making them
    # real fields is what lets the performance summary be edited like anything
    # else instead of being a black box.
    Field("period", "Period", "periods", "dimension", "text",
          "1 day, 1 month, 1 year … all time"),
    Field("p_from", "Window start", "periods", "measure", "text",
          "The date the window opens", agg="first"),
    Field("p_value_then", "Value then", "periods", "measure", "money",
          "What the portfolio was worth when the window opened", agg="first"),
    Field("p_contributed", "You invested", "periods", "measure", "money",
          "Money put in during the window", agg="first"),
    Field("p_growth", "Market + income", "periods", "measure", "money",
          "What the market and distributions made, contributions removed",
          agg="first"),
    Field("p_on_money_in", "On money in %", "periods", "measure", "percent",
          "Growth over the capital at work — the familiar figure", agg="first"),
    Field("p_twr", "Investments %", "periods", "measure", "percent",
          "Time-weighted: how the holdings performed, contributions removed",
          agg="first"),
    Field("p_twr_annual", "Per year %", "periods", "measure", "percent",
          "The time-weighted return restated per year", agg="first"),
)

BY_KEY = {f.key: f for f in FIELDS}

# Chart types the builder can draw, and which grains suit them.
CHART_TYPES = (
    {"key": "line", "label": "Line", "grains": ["timeseries"]},
    # "Area" is the same chart with the space under the line filled in, which
    # was not obvious from the word alone — the label says so.
    {"key": "area", "label": "Line, filled", "grains": ["timeseries"]},
    {"key": "bar", "label": "Bar", "grains": ["timeseries", "positions", "periods"]},
    {"key": "hbar", "label": "Horizontal bar", "grains": ["positions", "periods"]},
    {"key": "doughnut", "label": "Doughnut", "grains": ["positions"]},
    {"key": "table", "label": "Table only",
     "grains": ["timeseries", "positions", "periods"]},
)

# How a daily series is bucketed along the X axis.
BUCKETS = (
    {"key": "day", "label": "Day"},
    {"key": "week", "label": "Week"},
    {"key": "month", "label": "Month"},
    {"key": "quarter", "label": "Quarter"},
    {"key": "fy", "label": "Financial year"},
)


def fields_for(grain: str) -> list[Field]:
    return [f for f in FIELDS if f.grain == grain]


def catalogue() -> dict:
    """The vocabulary, shaped for the builder UI."""
    return {
        "grains": [
            {
                "key": "timeseries",
                "label": "Over time",
                "blurb": "One row per day — how the whole portfolio moved",
            },
            {
                "key": "positions",
                "label": "By holding",
                "blurb": "One row per holding — how they compare right now",
            },
            {
                "key": "periods",
                "label": "By period",
                "blurb": "One row per window, 1 day to all time — the performance summary",
            },
        ],
        "fields": [
            {
                "key": f.key,
                "label": f.label,
                "grain": f.grain,
                "role": f.role,
                "kind": f.kind,
                "blurb": f.blurb,
            }
            for f in FIELDS
        ],
        "chart_types": list(CHART_TYPES),
        "buckets": list(BUCKETS),
        # Dimensions a chart can be filtered on. Values come from the
        # portfolio, so the builder fills them at render time.
        "filterable": ["asset_class", "ticker", "currency", "exchange"],
    }


def validate(spec: dict) -> str | None:
    """Why this spec can't be drawn, or None if it can.

    Called before saving and before executing, so a bad spec is refused at the
    door rather than producing an empty chart nobody can explain.
    """
    grain = spec.get("grain")
    if grain not in ("timeseries", "positions", "periods"):
        return "Pick what the chart is about: over time, by holding, or by period."
    x = spec.get("x")
    if x not in BY_KEY or BY_KEY[x].grain != grain or BY_KEY[x].role != "dimension":
        return "Drop a field on the X axis."
    measures = [m for m in (spec.get("measures") or []) if m in BY_KEY]
    if not measures:
        return "Drop at least one field on Measures."
    if any(BY_KEY[m].grain != grain or BY_KEY[m].role != "measure" for m in measures):
        return "Those measures don't belong to the same kind of chart as the X axis."
    chart_type = spec.get("type", "line")
    match = next((c for c in CHART_TYPES if c["key"] == chart_type), None)
    if match is None or grain not in match["grains"]:
        return f"A {chart_type} chart can't show that kind of data."
    if chart_type == "doughnut" and len(measures) > 1:
        return "A doughnut shows one measure at a time."
    split = spec.get("split")
    if split and (split not in BY_KEY or BY_KEY[split].role != "dimension"):
        return "That split isn't a dimension."
    if split and grain == "timeseries" and BY_KEY[split].grain != "positions":
        return "Split a daily series by a holding attribute (ticker, class…)."
    if split:
        if grain == "timeseries":
            if BY_KEY[split].key == "date":
                return "Split by a holding attribute — the date is already the X axis."
            if len(measures) > 1:
                return ("Pick one measure when splitting into series — several "
                        "measures across every holding is more lines than anyone "
                        "can read.")
            if chart_type not in ("line", "area", "bar", "table"):
                return f"A {chart_type} chart can't show one series per group."
        elif grain == "positions":
            if split == x:
                return "The split and the X axis are the same field."
            if len(measures) > 1:
                return "Pick one measure when splitting into series."
            if chart_type in ("doughnut",):
                return "A doughnut can't be split — use a stacked bar."
        elif grain == "periods":
            # Not a chart this app declines to draw — not a chart. Before
            # this rule the split was accepted and silently dropped:
            # decisions.md #32.
            return "Performance windows can't be split — each row is one window."
    return None
