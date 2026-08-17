"""Performance windows: `queries.period_summary`, `PERIODS` and `chart_ranges`.

Two returns come out of `period_summary` and they are easy to confuse, so every
scenario here is built to make one of them provable on its own:

  * `on_money_in` is money-weighted — growth over the capital that was at work.
    Its denominator must be the opening value plus GROSS contributions; the
    named regression below pins that, because netting sale proceeds off the
    contributions shrinks the denominator and flatters the percentage.
  * `twr` is time-weighted — contributions removed by construction, which is
    the only measure comparable across two windows of different size.

The scenarios are deliberately tiny step-priced portfolios rather than the
reference fixture wherever an exact figure is asserted: with a handful of days
and round prices, every number below can be re-derived on paper. The reference
fixture is used only where the question is "which windows exist", which is
about the shape of its history rather than its arithmetic.
"""

from __future__ import annotations

import datetime as dt

from freezegun import freeze_time

import fixture_portfolio as ref
from app import queries
from factories import add_dividend, add_prices, add_trade, daily, make_instrument, stepped

# --------------------------------------------------------------------------- #
# Local helpers (deliberately not in factories.py — only this module needs them)
# --------------------------------------------------------------------------- #

def summary_by_label(session) -> dict[str, dict]:
    """`period_summary` keyed by window label, for readable lookups."""
    return {row["label"]: row for row in queries.period_summary(session)}


def build_sale_window(session):
    """Buy, sell half, top up — a window whose gross and net flows differ.

    Prices are a step function on consecutive days, so the daily value series
    is 1000 / 1200 / 600 / 900 / 1125 and can be checked by multiplication.
    """
    cal = daily("2026-01-01", "2026-01-05")
    acme = make_instrument(session, "ACME", asset_class="share", name="Acme Industrial")
    add_prices(session, acme, stepped(cal, [("2026-01-01", "10.00"),
                                            ("2026-01-02", "12.00"),
                                            ("2026-01-05", "15.00")]))
    add_trade(session, acme, "2026-01-01", "buy", 100, "10.00")
    add_trade(session, acme, "2026-01-03", "sell", 50, "12.00")
    add_trade(session, acme, "2026-01-04", "buy", 25, "12.00")
    session.commit()
    return acme


def build_no_flow_window(session):
    """One buy on day one, then nothing but price moves: 1000 → 1100 → 1200 → 1500."""
    cal = daily("2026-03-02", "2026-03-05")
    nova = make_instrument(session, "NOVA", asset_class="share", name="Nova Energy")
    add_prices(session, nova, stepped(cal, [("2026-03-02", "10.00"),
                                            ("2026-03-03", "11.00"),
                                            ("2026-03-04", "12.00"),
                                            ("2026-03-05", "15.00")]))
    add_trade(session, nova, "2026-03-02", "buy", 100, "10.00")
    session.commit()
    return nova


def build_mid_window_contribution(session):
    """`build_no_flow_window`'s price path, plus a buy at the day-three close.

    Buying AT the close and with no brokerage is what makes the comparison
    exact: the contribution adds value without earning any return that day, so
    a correct time-weighted chain must come out identical to the no-flow case.
    """
    cal = daily("2026-03-02", "2026-03-05")
    nova = make_instrument(session, "NOVA", asset_class="share", name="Nova Energy")
    add_prices(session, nova, stepped(cal, [("2026-03-02", "10.00"),
                                            ("2026-03-03", "11.00"),
                                            ("2026-03-04", "12.00"),
                                            ("2026-03-05", "15.00")]))
    add_trade(session, nova, "2026-03-02", "buy", 100, "10.00")
    add_trade(session, nova, "2026-03-04", "buy", 40, "12.00")
    session.commit()
    return nova


def build_flat_then_jump(session):
    """Two years flat at 10.00, then +21% on the very last day.

    Every intermediate daily return is exactly zero, so the chained factor is
    1.21 over any window that contains the last day — which makes the
    annualisation arithmetic checkable by hand at 365 and 730 days.
    """
    cal = daily("2024-08-02", "2026-08-02")
    orbit = make_instrument(session, "ORBIT", asset_class="etf", name="Orbit Growth Fund")
    add_prices(session, orbit, stepped(cal, [("2024-08-02", "10.00"),
                                             ("2026-08-02", "12.10")]))
    add_trade(session, orbit, "2024-08-02", "buy", 100, "10.00")
    session.commit()
    return orbit


def weekdays(start, end) -> list[dt.date]:
    """`factories.daily` with weekends removed — the shape a real price feed
    produces, and the reason a window's start date can only ever snap forward."""
    return [day for day in daily(start, end) if day.weekday() < 5]


def build_flat_then_jump_weekdays_only(session):
    """`build_flat_then_jump` on a weekday-only calendar ending on a Monday.

    Frozen "today" is Monday 2026-08-03, so the 1-year cutoff is Sunday
    2025-08-03 — a day with no price row, exactly as happens on roughly two
    days in seven with any real feed.
    """
    cal = weekdays("2024-08-01", "2026-08-03")
    orbit = make_instrument(session, "ORBIT", asset_class="etf", name="Orbit Growth Fund")
    add_prices(session, orbit, stepped(cal, [("2024-08-01", "10.00"),
                                             ("2026-08-03", "12.10")]))
    add_trade(session, orbit, "2024-08-01", "buy", 100, "10.00")
    session.commit()
    return orbit


def build_sale_and_dividend(session):
    """A window with all three growth ingredients: price move, cash income, sale."""
    cal = daily("2026-01-01", "2026-01-05")
    vertex = make_instrument(session, "VERTEX", asset_class="share", name="Vertex Metals")
    add_prices(session, vertex, stepped(cal, [("2026-01-01", "10.00"),
                                              ("2026-01-03", "12.00")]))
    add_trade(session, vertex, "2026-01-01", "buy", 100, "10.00", brokerage="20.00")
    add_dividend(session, vertex, "2026-01-03", "50.00")
    add_trade(session, vertex, "2026-01-04", "sell", 40, "12.00", brokerage="10.00")
    session.commit()
    return vertex


# --------------------------------------------------------------------------- #
# on_money_in — the denominator regression
# --------------------------------------------------------------------------- #

@freeze_time("2026-01-05")
def test_on_money_in_uses_gross_contributions(pf):
    """Money in is the opening value plus GROSS buys, never buys less sales.

    Netting the sale proceeds off contributions shrinks the denominator and
    inflates the return — 47.81% on the all-time row against the
    44.73% on the yearly-performance chart. A window with a sale in it is the
    only place the two definitions diverge, so that is what this builds.
    """
    build_sale_window(pf)
    row = summary_by_label(pf)["All time"]

    # Daily series, by hand:
    #   01-01 buy 100 @ 10.00 → invested 1000, units 100, value 100 × 10 = 1000
    #   01-02 price 12.00                        value 100 × 12 = 1200
    #   01-03 sell 50 @ 12.00 → proceeds 600,    value  50 × 12 =  600
    #   01-04 buy  25 @ 12.00 → invested 1300,   value  75 × 12 =  900
    #   01-05 price 15.00                        value  75 × 15 = 1125
    #
    # Flows after the opening day: gross buys 300, sale proceeds 600,
    # so net flow = 300 − 600 = −300.
    assert row["contributed"] == 300.00      # the 01-04 buy only; the sale is not a contribution
    assert row["growth"] == 425.00           # 1125 − 1000 value change − (−300) net flow = 425

    # money_in = opening value 1000 + gross contributions 300 = 1300
    #          → 425 / 1300 = 0.326923… = 32.69%
    assert row["on_money_in"] == 0.3269

    # Using the NET flow instead gives money_in = 1000 + (−300) = 700, i.e.
    # 425 / 700 = 0.607142… = 60.71% — the same growth over a denominator the
    # sale hollowed out. Nearly double the honest figure.
    assert row["on_money_in"] != 0.6071


# --------------------------------------------------------------------------- #
# Time-weighted return
# --------------------------------------------------------------------------- #

@freeze_time("2026-03-05")
def test_twr_equals_plain_return_when_nothing_is_added_or_withdrawn(pf):
    """With no flows the chain must collapse to value_end / value_start − 1.

    This is the cleanest possible check that the daily returns are chained
    (multiplied) rather than summed: the intermediate steps are deliberately
    unequal, so an additive implementation would land somewhere else.
    """
    build_no_flow_window(pf)
    row = summary_by_label(pf)["All time"]

    # value 1000 → 1100 → 1200 → 1500, no buys or sells after day one.
    #   day 2: 1100/1000 = 1.10
    #   day 3: 1200/1100 = 1.0909…
    #   day 4: 1500/1200 = 1.25
    #   chained: 1.10 × 1.0909… × 1.25 = 1.50
    # which is just 1500 / 1000 = 1.5, i.e. a 50% return.
    assert row["twr"] == 0.5
    assert row["twr"] == round(1500.0 / 1000.0 - 1.0, 4)
    # Summing the daily returns instead would give 0.10 + 0.0909 + 0.25 ≈ 0.4409.
    assert row["twr"] != 0.4409


@freeze_time("2026-03-05")
def test_a_mid_window_contribution_does_not_change_twr(pf):
    """Adding money must not move the time-weighted return.

    That invariance is the entire reason TWR is quoted alongside on_money_in:
    it measures the investments, not the savings rate, so two windows with very
    different contribution patterns stay comparable.
    """
    build_mid_window_contribution(pf)
    row = summary_by_label(pf)["All time"]

    # Same price path as the no-flow case, with 40 more units bought at the
    # 12.00 close on day three:
    #   day 1  value 100 × 10 = 1000
    #   day 2  value 100 × 11 = 1100                     → 1100/1000     = 1.10
    #   day 3  value 140 × 12 = 1680, flow in 480        → (1680 − 480)/1100 = 1.0909…
    #   day 4  value 140 × 15 = 2100                     → 2100/1680     = 1.25
    #   chained 1.10 × 1.0909… × 1.25 = 1.50
    assert row["twr"] == 0.5  # identical to the same price path with no contribution

    # The money-weighted figure DOES move, and should: growth is
    # 2100 − 1000 − 480 = 620 over money_in 1000 + 480 = 1480 → 0.418918…
    # A contribution that has only been invested for one day drags it below the
    # 50% the earlier money actually earned.
    assert row["on_money_in"] == 0.4189


# --------------------------------------------------------------------------- #
# growth
# --------------------------------------------------------------------------- #

@freeze_time("2026-01-05")
def test_growth_is_value_change_less_net_flow_plus_income(pf):
    """Growth separates "the portfolio got bigger because I fed it" from
    "the portfolio got bigger by itself"."""
    build_sale_and_dividend(pf)
    row = summary_by_label(pf)["All time"]

    # Daily series, by hand:
    #   01-01 buy 100 @ 10.00 + 20 brokerage → value 100 × 10 = 1000
    #   01-03 price 12.00, cash dividend 50  → value 100 × 12 = 1200
    #   01-04 sell 40 @ 12.00 − 10 brokerage → proceeds 470, value 60 × 12 = 720
    #   01-05 price unchanged                → value 720
    #
    # growth = value_end − value_start − net_flow + income
    #        = 720 − 1000 − (−470) + 50 = 240
    # Sanity check from the other direction: wealth went from 1000 to
    # 720 in units + 470 in sale cash + 50 in dividend cash = 1240, and nothing
    # was added, so the market and income made 240. The sale's 10 of brokerage
    # is correctly a drag (200 price gain + 50 income − 10 = 240).
    assert row["growth"] == 240.00


# --------------------------------------------------------------------------- #
# Which windows exist
# --------------------------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_windows_the_history_cannot_fill_are_skipped(pf):
    """A "10 years" row over five years of records would be a lie with a label."""
    ref.build_reference(pf)

    labels = [row["label"] for row in queries.period_summary(pf)]

    # The reference portfolio's first price is 2021-01-01 and its first trade
    # 2021-01-04, so the series opens 2021-02-01 — 5.5 years before the frozen
    # today. The 10y cutoff (2016-08-05) and 20y cutoff (2006-08-08) both fall
    # before that, so neither window can be reported.
    assert labels == ["1 day", "1 month", "6 months", "1 year", "3 years",
                      "5 years", "All time"]
    assert "10 years" not in labels
    assert "20 years" not in labels
    # PERIODS is ordered short → long and "All time" is appended last, so the
    # table reads in increasing window size.
    assert labels[-1] == "All time"


@freeze_time("2026-01-05")
def test_all_time_is_reported_even_for_a_five_day_history(pf):
    """"All time" is not a window with a cutoff — it is the whole record, so it
    is present whenever there is any record at all."""
    build_sale_window(pf)

    labels = [row["label"] for row in queries.period_summary(pf)]

    # Five days of history: only the 1-day cutoff (2026-01-04) lands inside it.
    assert labels == ["1 day", "All time"]


def test_no_windows_at_all_when_there_is_no_history(pf):
    """An empty portfolio gets an empty table rather than a row of zeroes."""
    assert queries.period_summary(pf) == []


# --------------------------------------------------------------------------- #
# twr_annual
# --------------------------------------------------------------------------- #

@freeze_time("2026-08-02")
def test_twr_annual_is_none_for_windows_shorter_than_a_year(pf):
    """Annualising a one-month return would extrapolate noise into a headline."""
    build_flat_then_jump(pf)
    rows = summary_by_label(pf)

    # Prices are flat at 10.00 until the final day's jump to 12.10, so EVERY
    # window that contains 2026-08-02 has the same raw return: 1210/1000 − 1.
    for label in ("1 day", "1 month", "6 months"):
        assert rows[label]["twr"] == 0.21, label
        assert rows[label]["twr_annual"] is None, label


@freeze_time("2026-08-02")
def test_twr_annual_restates_longer_windows_per_year(pf):
    """Windows of a year or more are annualised so they stay comparable."""
    build_flat_then_jump(pf)
    rows = summary_by_label(pf)

    # The 1-year window spans exactly 365 days (2025-08-02 → 2026-08-02), so
    # annualising is the identity: 1.21 ** (365/365) − 1 = 0.21.
    assert rows["1 year"]["twr"] == 0.21
    assert rows["1 year"]["twr_annual"] == 0.21

    # All time spans exactly 730 days (2024-08-02 → 2026-08-02), i.e. two
    # years: 1.21 ** (365/730) − 1 = sqrt(1.21) − 1 = 1.10 − 1 = 0.10.
    assert rows["All time"]["twr"] == 0.21
    assert rows["All time"]["twr_annual"] == 0.10


@freeze_time("2026-08-03")
def test_twr_annual_is_reported_for_the_one_year_window_when_prices_skip_weekends(pf):
    build_flat_then_jump_weekdays_only(pf)
    rows = summary_by_label(pf)

    # today − 365 days is Sunday 2025-08-03, so the window opens on Monday
    # 2025-08-04 and spans 364 days. The window is still a year, and the
    # annualised return of a 364-day window is within 0.1% of its raw return
    # (1.21 ** (365/364) ≈ 1.2106) — there is nothing here worth suppressing.
    assert rows["1 year"]["twr"] == 0.21
    assert rows["1 year"]["twr_annual"] is not None


# --------------------------------------------------------------------------- #
# chart_ranges
# --------------------------------------------------------------------------- #

@freeze_time("2026-08-02")
def test_chart_range_buttons_appear_only_once_the_record_reaches_back():
    # 2022-08-02 → 2026-08-02 is 1461 days (one leap day, in FY2024).
    # 3y needs 3 × 365 = 1095 → offered. 5y needs 1825 → not offered.
    ranges = queries.chart_ranges(dt.date(2022, 8, 2))

    assert [r["key"] for r in ranges] == ["all", "y3", "y1", "fy"]


@freeze_time("2026-08-02")
def test_chart_range_button_appears_on_the_exact_day_it_becomes_fillable():
    # A "year" is 365 days here, and neither 2026 nor 2025 is a leap year, so
    # exactly one year of history is 2025-08-02.
    on_the_day = queries.chart_ranges(dt.date(2025, 8, 2))    # span 365 → qualifies
    day_short = queries.chart_ranges(dt.date(2025, 8, 3))     # span 364 → does not

    assert [r["key"] for r in on_the_day] == ["all", "y1", "fy"]
    assert [r["key"] for r in day_short] == ["all", "fy"]


@freeze_time("2026-08-02")
def test_chart_ranges_run_all_then_longest_to_shortest_then_this_fy():
    # 2000-01-01 is over 26 years back, so every window qualifies and the full
    # ordering is visible.
    ranges = queries.chart_ranges(dt.date(2000, 1, 1))

    assert [r["key"] for r in ranges] == ["all", "y20", "y10", "y5", "y3", "y1", "fy"]
    assert [r["label"] for r in ranges] == ["All", "20y", "10y", "5y", "3y", "1y", "This FY"]


@freeze_time("2026-08-02")
def test_chart_ranges_with_no_history_offers_only_all_and_this_fy():
    """No trades yet: the two windows that need no history still make sense."""
    assert [r["key"] for r in queries.chart_ranges(None)] == ["all", "fy"]


# --------------------------------------------------------------------------- #
# Gain % on the FY snapshot
# --------------------------------------------------------------------------- #

def test_the_snapshot_row_carries_a_gain_percentage():
    """Derived from the two numbers beside it rather than stored, so it cannot
    drift from them."""
    from decimal import Decimal

    from app.fyreport import SnapshotRow

    row = SnapshotRow(instrument=None, units=Decimal("10"),
                      invested_cum=Decimal("1000"), price=Decimal("120"),
                      price_date=None, value_aud=Decimal("1200"),
                      gain_aud=Decimal("200"))

    assert row.gain_pct == Decimal("0.2")


def test_a_holding_with_no_outlay_has_no_percentage():
    """Not 0%. Nothing was put in, so there is nothing to be a percentage OF —
    and 0% would read as "broke even", which is a different claim."""
    from decimal import Decimal

    from app.fyreport import SnapshotRow

    row = SnapshotRow(instrument=None, units=Decimal("10"),
                      invested_cum=Decimal("0"), price=None, price_date=None,
                      value_aud=None, gain_aud=Decimal("50"))

    assert row.gain_pct is None
    # …and it is the "cannot exist" kind of none, which renders N/A.
    assert row.percentage_applies is False


def test_an_unpriced_holding_is_unknown_rather_than_not_applicable():
    """The distinction that matters: an em dash says "we do not know", N/A
    says "there is nothing to know". A holding with an outlay and no price is
    the first kind — the percentage is computable, just not yet."""
    from decimal import Decimal

    from app.fyreport import SnapshotRow

    row = SnapshotRow(instrument=None, units=Decimal("10"),
                      invested_cum=Decimal("1000"), price=None, price_date=None,
                      value_aud=None, gain_aud=None)

    assert row.gain_pct is None
    assert row.percentage_applies is True


def test_an_unpriced_holding_has_no_percentage():
    from decimal import Decimal

    from app.fyreport import SnapshotRow

    row = SnapshotRow(instrument=None, units=Decimal("10"),
                      invested_cum=Decimal("1000"), price=None, price_date=None,
                      value_aud=None, gain_aud=None)

    assert row.gain_pct is None


def test_a_loss_reads_as_a_negative_percentage():
    from decimal import Decimal

    from app.fyreport import SnapshotRow

    row = SnapshotRow(instrument=None, units=Decimal("10"),
                      invested_cum=Decimal("1000"), price=Decimal("80"),
                      price_date=None, value_aud=Decimal("800"),
                      gain_aud=Decimal("-200"))

    assert row.gain_pct == Decimal("-0.2")
