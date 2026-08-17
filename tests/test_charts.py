"""Charts: the spec vocabulary, the bucketing, and the shipped defaults.

`fields.validate` is the gate and `charts_build.run` is the engine, so between
them they decide whether a chart can be drawn and what it ends up saying. Three
guarantees are worth freezing here:

  * every shipped template still executes — a default nobody can reproduce in
    the builder is a chart that can never be got back once somebody deletes it;
  * a bucket's *state* measures take its closing value while its *flow*
    measures total what moved inside it, which is the easiest pair to confuse
    and the easiest way to draw a plausible-looking lie;
  * a spec that cannot be drawn is refused at the door with a readable reason,
    rather than quietly producing an empty chart.

Every expected number below is worked out by hand in the comment beside it.
"""

from __future__ import annotations

import pytest
from freezegun import freeze_time

import fixture_portfolio as ref
from app import chart_templates, charts_build, fields
from factories import add_dividend, add_prices, add_trade, daily, make_instrument

# --------------------------------------------------------------------------- #
# Scenarios
#
# The reference portfolio is used wherever the point is "this works on real
# shaped data". Where the point is exact arithmetic, a purpose-made scenario is
# built instead, small enough that every figure can be derived in a comment.
# --------------------------------------------------------------------------- #

@pytest.fixture
def reference(pf):
    ref.build_reference(pf)
    return pf


@pytest.fixture
def two_day_bucket(pf):
    """Three priced days spanning two ISO weeks, with hand-computable flows.

    2026-06-12 is a Friday and 2026-06-15/16 are the Monday and Tuesday after
    it, so the last two days share a week bucket and the first sits alone. That
    split is what lets "closing value" and "total of the flows within" tell
    visibly different stories.

    Day-by-day, all AUD, brokerage included in the outlay:
      2026-06-12  buy 100 @ 4.00 + 10 brokerage  invested 410, units 100
                  close 4.00 -> value  400   gain  400 +   0 - 410 =  -10
      2026-06-15  buy 100 @ 5.00 + 10 brokerage  invested 920, units 200
                  cash distribution 20.00
                  close 5.00 -> value 1000   gain 1000 +  20 - 920 =  100
      2026-06-16  buy  50 @ 6.00, no brokerage   invested 1220, units 250
                  cash distribution 30.00
                  close 6.00 -> value 1500   gain 1500 +  50 -1220 =  330
    """
    acme = make_instrument(pf, "ACME", asset_class="share", name="Acme Industries")
    add_prices(pf, acme, [("2026-06-12", "4.00"), ("2026-06-15", "5.00"),
                          ("2026-06-16", "6.00")])
    add_trade(pf, acme, "2026-06-12", "buy", 100, "4.00", brokerage="10.00")
    add_trade(pf, acme, "2026-06-15", "buy", 100, "5.00", brokerage="10.00")
    add_trade(pf, acme, "2026-06-16", "buy", 50, "6.00")
    add_dividend(pf, acme, "2026-06-15", "20.00")
    add_dividend(pf, acme, "2026-06-16", "30.00")
    pf.commit()
    return pf


@pytest.fixture
def two_shares_same_class(pf):
    """Two shares in one asset class whose percentages divide cleanly.

      WIDGET  100 @ 10.00 = 1000 cost, close 12.00 -> 1200 value, +200 = +20%
      NOVA    100 @ 10.00 = 1000 cost, close 14.00 -> 1400 value, +400 = +40%

    Deliberately chosen so summing (0.60) and averaging (0.30) can't be
    mistaken for one another.
    """
    widget = make_instrument(pf, "WIDGET", asset_class="share", name="Widget Co")
    nova = make_instrument(pf, "NOVA", asset_class="share", name="Nova Ltd")
    add_prices(pf, widget, [("2026-06-01", "10.00"), ("2026-06-02", "12.00")])
    add_prices(pf, nova, [("2026-06-01", "10.00"), ("2026-06-02", "14.00")])
    add_trade(pf, widget, "2026-06-01", "buy", 100, "10.00")
    add_trade(pf, nova, "2026-06-01", "buy", 100, "10.00")
    pf.commit()
    return pf


@pytest.fixture
def long_daily_history(pf):
    """546 consecutive priced days — comfortably past MAX_POINTS (400)."""
    calendar = daily("2025-01-01", "2026-06-30")  # 365 + 181 = 546 days
    acme = make_instrument(pf, "ACME", asset_class="share")
    add_prices(pf, acme, [(day, "10.00") for day in calendar])
    add_trade(pf, acme, "2025-01-01", "buy", 100, "10.00")
    pf.commit()
    return pf


# --------------------------------------------------------------------------- #
# The shipped templates
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("template", chart_templates.TEMPLATES,
                         ids=[t["key"] for t in chart_templates.TEMPLATES])
def test_every_shipped_template_is_a_valid_spec(template):
    """A template that can't pass the gate could never be saved or re-added."""
    assert fields.validate(template["spec"]) is None, template["key"]


@freeze_time(ref.TODAY)
@pytest.mark.parametrize("template", chart_templates.TEMPLATES,
                         ids=[t["key"] for t in chart_templates.TEMPLATES])
def test_every_shipped_template_executes_against_the_reference_portfolio(
    reference, template
):
    """The guarantee that a shipped default is always reproducible in the builder.

    Parametrised over TEMPLATES rather than listed, so a template added later is
    covered the moment it lands.
    """
    spec = template["spec"]
    data = charts_build.run(reference, spec)

    assert data["labels"], f"{template['key']} drew no X labels"
    assert data["datasets"], f"{template['key']} drew no series"
    for dataset in data["datasets"]:
        assert dataset["label"], f"{template['key']} has an unlabelled series"
        assert dataset["kind"] in ("money", "percent", "number", "text", "date")
        # Every series must line up with the axis, or the chart is misaligned.
        assert len(dataset["data"]) == len(data["labels"]), template["key"]

    assert data["grain"] == spec["grain"]
    assert data["type"] == spec.get("type", "line")
    assert data["x_label"] == fields.BY_KEY[spec["x"]].label


def test_default_keys_all_name_a_real_template():
    """A new account is seeded from DEFAULT_KEYS; a typo there ships nothing."""
    for key in chart_templates.DEFAULT_KEYS:
        assert key in chart_templates.BY_KEY, key


@freeze_time(ref.TODAY)
def test_allocation_doughnut_totals_value_by_asset_class(reference):
    """A hand-checked template output, not just "it ran"."""
    data = charts_build.run(reference, chart_templates.BY_KEY["alloc_value"]["spec"])

    # etf   = ALPHA                                              = 1575.00
    # share = BETAX 450.00 + GAMMA 500.00 + OMEGA 1920.00        = 2870.00
    # ZULU is fully sold, so it is not an open position at all.
    # Doughnuts sort biggest first, so share leads.
    assert data["labels"] == ["share", "etf"]
    assert data["datasets"][0]["data"] == [2870.0, 1575.0]


@freeze_time(ref.TODAY)
def test_today_template_shows_only_the_holding_that_moved(reference):
    data = charts_build.run(reference, chart_templates.BY_KEY["today"]["spec"])

    # ALPHA closed 14.00 then 15.00: (15 - 14) / 14 = 0.0714285... -> 0.071429
    # at the builder's 6dp. Every other holding's last two closes are equal,
    # because the fixture prices are step functions.
    assert data["labels"] == ["ALPHA", "BETAX", "GAMMA", "OMEGA"]
    assert data["datasets"][0]["data"] == [0.071429, 0.0, 0.0, 0.0]


# --------------------------------------------------------------------------- #
# fields.validate — what it refuses, and why
# --------------------------------------------------------------------------- #

def test_validate_rejects_an_unknown_grain():
    problem = fields.validate({"grain": "wombat", "x": "ticker",
                               "measures": ["pos_value"]})
    assert problem is not None
    assert "what the chart is about" in problem


def test_validate_rejects_a_missing_x_axis():
    problem = fields.validate({"grain": "timeseries", "measures": ["value"]})
    assert problem is not None
    assert "X axis" in problem


def test_validate_rejects_a_spec_with_no_measures():
    problem = fields.validate({"grain": "positions", "x": "ticker", "measures": []})
    assert problem is not None
    assert "Measures" in problem


def test_validate_rejects_a_measure_from_a_different_grain_than_the_x_axis():
    """`value` is a daily-series measure; `ticker` is a holdings dimension.

    Joining the two would need a per-holding daily series the positions grain
    doesn't have, so it is refused rather than silently drawn from one of them.
    """
    problem = fields.validate({"grain": "positions", "x": "ticker",
                               "measures": ["value"], "type": "bar"})
    assert problem is not None
    assert "same kind of chart" in problem


def test_validate_rejects_a_chart_type_that_does_not_suit_the_grain():
    # A doughnut is a share-of-a-whole; a daily series has no whole to share.
    problem = fields.validate({"grain": "timeseries", "x": "date",
                               "measures": ["value"], "type": "doughnut"})
    assert problem is not None
    assert "doughnut chart can't show that kind of data" in problem


def test_validate_rejects_a_doughnut_with_two_measures():
    problem = fields.validate({"grain": "positions", "x": "asset_class",
                               "measures": ["pos_value", "pos_cost"],
                               "type": "doughnut"})
    assert problem is not None
    assert "one measure at a time" in problem


def test_validate_rejects_a_positions_split_equal_to_the_x_axis():
    # Splitting ticker by ticker gives one bar per series and one series per
    # bar — a stack that can only ever be one segment tall.
    problem = fields.validate({"grain": "positions", "x": "ticker",
                               "measures": ["pos_value"], "split": "ticker",
                               "type": "bar"})
    assert problem is not None
    assert "same field" in problem


def test_validate_rejects_a_doughnut_with_a_split():
    problem = fields.validate({"grain": "positions", "x": "asset_class",
                               "measures": ["pos_value"], "split": "ticker",
                               "type": "doughnut"})
    assert problem is not None
    assert "doughnut can't be split" in problem


def test_validate_rejects_a_timeseries_split_by_a_non_positions_dimension():
    # `period` is a performance-window dimension: there is no daily series per
    # window to split into.
    problem = fields.validate({"grain": "timeseries", "x": "date",
                               "measures": ["value"], "split": "period",
                               "type": "line"})
    assert problem is not None
    assert "holding attribute" in problem


def test_validate_rejects_more_than_one_measure_when_splitting():
    problem = fields.validate({"grain": "timeseries", "x": "date",
                               "measures": ["value", "invested"],
                               "split": "ticker", "type": "line"})
    assert problem is not None
    assert "one measure when splitting" in problem


def test_validate_accepts_a_well_formed_split_spec():
    """The counterpart to the rejections: the shape they are guarding is legal."""
    assert fields.validate({"grain": "timeseries", "x": "date",
                            "measures": ["value"], "split": "ticker",
                            "type": "line", "bucket": "month"}) is None


def test_validate_rejects_a_split_on_the_periods_grain():
    """There is nothing to split a fixed set of performance windows by.

    `_periods` ignores `split` entirely, so accepting it means the chart drawn
    is not the chart that was asked for and nothing says so.
    """
    problem = fields.validate({"grain": "periods", "x": "period",
                               "measures": ["p_twr"], "split": "period",
                               "type": "bar"})
    assert problem is not None


# --------------------------------------------------------------------------- #
# Bucketing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(("day", "bucket", "expected"), [
    # 2026-06-15 is a Monday (2026-06-06 is a Saturday, +9 days).
    ("2026-06-15", "day", "2026-06-15"),
    ("2026-06-15", "week", "2026-06-15"),   # already the Monday
    ("2026-06-18", "week", "2026-06-15"),   # Thursday -> back to Monday
    ("2026-06-21", "week", "2026-06-15"),   # Sunday closes the same ISO week
    ("2026-07-02", "week", "2026-06-29"),   # a week may straddle a month end
    ("2026-06-15", "month", "2026-06"),
    ("2026-01-05", "quarter", "2026-Q1"),   # (1 - 1) // 3 + 1 = 1
    ("2026-06-15", "quarter", "2026-Q2"),   # (6 - 1) // 3 + 1 = 2
    ("2026-07-01", "quarter", "2026-Q3"),   # (7 - 1) // 3 + 1 = 3
    ("2026-12-31", "quarter", "2026-Q4"),   # (12 - 1) // 3 + 1 = 4
    # The Australian year runs 1 Jul - 30 Jun, so June 2026 closes FY25/26 and
    # July 2026 opens FY26/27.
    ("2026-06-15", "fy", "FY25/26"),
    ("2026-06-30", "fy", "FY25/26"),
    ("2026-07-01", "fy", "FY26/27"),
])
def test_bucket_key(day, bucket, expected):
    from datetime import date

    assert charts_build._bucket_key(date.fromisoformat(day), bucket) == expected


@pytest.mark.parametrize(("bucket", "expected_labels"), [
    ("day", ["2026-06-12", "2026-06-15", "2026-06-16"]),
    # Friday sits in the week beginning Monday 2026-06-08; the Monday and
    # Tuesday after it share the next.
    ("week", ["2026-06-08", "2026-06-15"]),
    ("month", ["2026-06"]),
    ("quarter", ["2026-Q2"]),
    ("fy", ["FY25/26"]),
])
def test_builder_labels_are_the_bucket_keys(two_day_bucket, bucket, expected_labels):
    data = charts_build.run(two_day_bucket, {
        "grain": "timeseries", "x": "date", "measures": ["value"],
        "type": "table", "bucket": bucket,
    })
    assert data["labels"] == expected_labels


# --------------------------------------------------------------------------- #
# What a bucket means: closing state vs total flow
# --------------------------------------------------------------------------- #

def test_last_style_measures_take_the_bucket_closing_value(two_day_bucket):
    """Market value and cumulative invested are running totals, not flows.

    Adding them across the days inside a bucket would double-count the balance
    that was already there on the first of them.
    """
    data = charts_build.run(two_day_bucket, {
        "grain": "timeseries", "x": "date", "measures": ["value", "invested"],
        "type": "table", "bucket": "week",
    })
    by_key = {d["key"]: d["data"] for d in data["datasets"]}

    assert data["labels"] == ["2026-06-08", "2026-06-15"]
    # Week of the 15th closes on Tuesday the 16th: 250 units x 6.00 = 1500.00.
    # Summing the two days inside it would have given 1000 + 1500 = 2500.00.
    assert by_key["value"] == [400.00, 1500.00]
    # Cumulative outlay at each week's close: 410.00 then 410 + 510 + 300 = 1220.00
    assert by_key["invested"] == [410.00, 1220.00]


def test_sum_style_measures_total_the_flows_inside_the_bucket(two_day_bucket):
    data = charts_build.run(two_day_bucket, {
        "grain": "timeseries", "x": "date", "measures": ["flow_in", "cash_div"],
        "type": "table", "bucket": "week",
    })
    by_key = {d["key"]: d["data"] for d in data["datasets"]}

    assert data["labels"] == ["2026-06-08", "2026-06-15"]
    # Money in: 410.00 in the first week; 510.00 + 300.00 = 810.00 in the second.
    # Taking only the closing day would have reported 300.00.
    assert by_key["flow_in"] == [410.00, 810.00]
    # Cash distributions: none, then 20.00 + 30.00 = 50.00
    assert by_key["cash_div"] == [0.00, 50.00]


# --------------------------------------------------------------------------- #
# Derived measures
# --------------------------------------------------------------------------- #

def test_derived_measures_compute_against_the_previous_bucket_close(two_day_bucket):
    """Deltas are bucket-over-bucket, and the first bucket measures from zero.

    Measuring from zero is what makes the first bar of a yearly chart show the
    whole of the first year rather than nothing.
    """
    data = charts_build.run(two_day_bucket, {
        "grain": "timeseries", "x": "date",
        "measures": ["value_gain", "invested_delta", "gain_delta", "period_return"],
        "type": "table", "bucket": "week",
    })
    by_key = {d["key"]: d["data"] for d in data["datasets"]}

    # value_gain is a within-bucket figure: closing value less closing invested.
    #   week 1:  400.00 -  410.00 =  -10.00
    #   week 2: 1500.00 - 1220.00 =  280.00
    assert by_key["value_gain"] == [-10.00, 280.00]

    # invested_delta: closing invested less the PREVIOUS bucket's close, from 0.
    #   week 1:  410.00 -    0.00 =  410.00
    #   week 2: 1220.00 -  410.00 =  810.00
    assert by_key["invested_delta"] == [410.00, 810.00]

    # gain_delta: closing net gain less the previous close, from 0.
    #   week 1:  -10.00 -    0.00 =  -10.00
    #   week 2:  330.00 - (-10.00) = 340.00
    assert by_key["gain_delta"] == [-10.00, 340.00]

    # period_return: the bucket's gain over what was invested by its end.
    #   week 1:  -10.00 /  410.00 = -0.024390... -> -0.0244
    #   week 2:  340.00 / 1220.00 =  0.278688... ->  0.2787
    assert by_key["period_return"] == [-0.0244, 0.2787]


# --------------------------------------------------------------------------- #
# Positions: splitting and aggregation
# --------------------------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_positions_split_series_sum_to_the_unsplit_totals(reference):
    """A holding lands in exactly one series, so the stack has to reach the
    same height as the plain bar. If it didn't, the split view would quietly
    disagree with the one beside it."""
    plain_spec = {"grain": "positions", "x": "ticker",
                  "measures": ["pos_value"], "type": "bar"}
    split_spec = {**plain_spec, "split": "currency"}

    plain = charts_build.run(reference, plain_spec)
    split = charts_build.run(reference, split_spec)

    # Biggest first: OMEGA 1920.00, ALPHA 1575.00, GAMMA 500.00, BETAX 450.00
    assert plain["labels"] == ["OMEGA", "ALPHA", "GAMMA", "BETAX"]
    assert plain["datasets"][0]["data"] == [1920.0, 1575.0, 500.0, 450.0]

    assert split["labels"] == plain["labels"]
    assert split["stacked"] is True
    stacked_totals = [
        sum(dataset["data"][i] for dataset in split["datasets"])
        for i in range(len(split["labels"]))
    ]
    assert stacked_totals == plain["datasets"][0]["data"]


@freeze_time(ref.TODAY)
def test_positions_split_puts_each_holding_in_exactly_one_series(reference):
    data = charts_build.run(reference, {
        "grain": "positions", "x": "ticker", "measures": ["pos_value"],
        "split": "currency", "type": "bar",
    })
    by_label = {d["label"]: d["data"] for d in data["datasets"]}

    # Series are the currencies, in sorted order. Only OMEGA trades in USD, so
    # every other column of the USD series is an explicit zero, not a gap.
    assert [d["label"] for d in data["datasets"]] == ["AUD", "USD"]
    # labels are OMEGA, ALPHA, GAMMA, BETAX
    assert by_label["AUD"] == [0.0, 1575.0, 500.0, 450.0]
    assert by_label["USD"] == [1920.0, 0.0, 0.0, 0.0]


@freeze_time(ref.TODAY)
def test_split_timeseries_series_add_up_to_the_whole_portfolio(reference):
    """One line per holding must still describe the same portfolio as the total.

    The split series come from a second walk (`grouped_series`), so this is the
    guard against the two walks drifting apart.
    """
    data = charts_build.run(
        reference, chart_templates.BY_KEY["value_over_time_by_holding"]["spec"]
    )

    assert data["labels"][-1] == "2026-08"  # month bucket, closing 2026-08-02
    # ALPHA 1575.00 + BETAX 450.00 + GAMMA 500.00 + OMEGA 1920.00 + ZULU 0.00
    final = sum(dataset["data"][-1] for dataset in data["datasets"])
    assert final == float(ref.TOTAL_VALUE_AUD)  # 4445.00


def test_percent_measures_are_averaged_not_summed(two_shares_same_class):
    """Percentages don't add up.

    WIDGET is +20% and NOVA is +40%; the asset class did not gain 60%.
    """
    data = charts_build.run(two_shares_same_class, {
        "grain": "positions", "x": "asset_class",
        "measures": ["pos_gain_pct"], "type": "bar",
    })

    assert data["labels"] == ["share"]
    # (0.20 + 0.40) / 2 holdings = 0.30 — summing would have given 0.60.
    assert data["datasets"][0]["data"] == [0.3]


def test_a_split_percent_chart_is_not_stacked(two_shares_same_class):
    """`_positions` promises "the bars stack to the same total as without it",
    and that promise only holds for a measure that adds up.

    A percent measure is averaged by `combine()`, so stacking the segments
    drew +20% and +40% end to end as 0.60 where the unsplit bar of the same two
    holdings is 0.30 — twice the height, and a number nobody had computed.

    The segments themselves were never wrong; putting them end to end was. So
    the split view groups instead, which answers "each ticker's gain % within
    its class" correctly, rather than being refused outright.
    """
    plain_spec = {"grain": "positions", "x": "asset_class",
                  "measures": ["pos_gain_pct"], "type": "bar"}
    plain = charts_build.run(two_shares_same_class, plain_spec)
    split = charts_build.run(two_shares_same_class, {**plain_spec, "split": "ticker"})

    assert split["stacked"] is False
    # Each segment is the holding's own figure, and the unsplit bar remains
    # their average — the two views now agree about what they are showing.
    assert sorted(d["data"][0] for d in split["datasets"]) == [0.2, 0.4]
    assert plain["datasets"][0]["data"] == [0.3]


def test_a_split_money_chart_is_still_stacked_and_still_sums(two_shares_same_class):
    """The other half of the same rule: money adds up, so it stacks, and the
    stack still equals the unsplit bar. Pinned because the ratio fix is a
    condition on this line and could easily have turned stacking off for
    everything."""
    plain_spec = {"grain": "positions", "x": "asset_class",
                  "measures": ["pos_value"], "type": "bar"}
    plain = charts_build.run(two_shares_same_class, plain_spec)
    split = charts_build.run(two_shares_same_class, {**plain_spec, "split": "ticker"})

    assert split["stacked"] is True
    stacked_total = sum(dataset["data"][0] for dataset in split["datasets"])
    assert stacked_total == plain["datasets"][0]["data"][0] == 2600.0


def test_money_measures_are_summed_within_a_group(two_shares_same_class):
    data = charts_build.run(two_shares_same_class, {
        "grain": "positions", "x": "asset_class",
        "measures": ["pos_value"], "type": "bar",
    })

    assert data["labels"] == ["share"]
    # 100 x 12.00 = 1200.00 plus 100 x 14.00 = 1400.00 -> 2600.00
    assert data["datasets"][0]["data"] == [2600.0]


# --------------------------------------------------------------------------- #
# Thinning
# --------------------------------------------------------------------------- #

def test_thin_leaves_a_series_under_the_limit_alone():
    buckets = [str(i) for i in range(charts_build.MAX_POINTS)]
    assert charts_build._thin(buckets) == buckets


def test_thin_keeps_the_first_and_last_point():
    """The last point is the one people read first — "what is it worth now".

    Plain slicing drops it whenever the length isn't a multiple of the step, so
    it is put back explicitly.
    """
    buckets = [str(i) for i in range(1001)]
    kept = charts_build._thin(buckets)

    # step = 1001 // 400 + 1 = 3, so slicing keeps 0, 3, ... 999 (334 points)
    # and index 1000 has to be appended -> 335.
    assert kept[0] == "0"
    assert kept[-1] == "1000"
    assert len(kept) == 335


def test_a_long_daily_chart_is_thinned_but_still_spans_the_whole_history(
    long_daily_history,
):
    data = charts_build.run(long_daily_history, {
        "grain": "timeseries", "x": "date", "measures": ["value"],
        "type": "line", "bucket": "day",
    })

    # 546 priced days, step = 546 // 400 + 1 = 2 -> 273 kept by slicing, plus
    # the final day appended because 545 is odd = 274.
    assert len(data["labels"]) == 274
    assert data["labels"][0] == "2025-01-01"
    assert data["labels"][-1] == "2026-06-30"
    assert len(data["datasets"][0]["data"]) == len(data["labels"])


# --------------------------------------------------------------------------- #
# Refusing a bad spec
# --------------------------------------------------------------------------- #

def test_run_raises_a_readable_value_error_for_an_invalid_spec(pf):
    """`run` refuses at the door rather than drawing an unexplainable chart."""
    spec = {"grain": "positions", "x": "ticker", "measures": [], "type": "bar"}

    with pytest.raises(ValueError) as excinfo:
        charts_build.run(pf, spec)

    # The message a person sees is exactly the reason validate gave.
    assert str(excinfo.value) == fields.validate(spec)
    assert str(excinfo.value) == "Drop at least one field on Measures."


def test_run_refuses_a_spec_whose_measures_belong_to_another_grain(pf):
    spec = {"grain": "timeseries", "x": "date", "measures": ["pos_value"],
            "type": "line"}

    with pytest.raises(ValueError, match="same kind of chart"):
        charts_build.run(pf, spec)
