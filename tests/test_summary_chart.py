"""The Portfolio page's performance chart.

This is the chart that made it reasonable for Charts to leave the nav: the
charts page is the workshop where custom charts are built, and this answers
"how am I going" on the page you land on.

The design rule worth pinning is the range bar. **Only ranges the history can
actually draw are offered** — a portfolio three years old showing 10Y, 20Y and
30Y renders three identical charts and reads as broken, because the buttons
imply data that is not there.
"""

from __future__ import annotations

import datetime as dt

from app import charts_build

TODAY = dt.date(2026, 8, 8)
FY_START = dt.date(2026, 7, 1)


def _dates(days: int) -> list[str]:
    start = TODAY - dt.timedelta(days=days)
    return [(start + dt.timedelta(days=i)).isoformat() for i in range(days + 1)]


def _keys(days: int) -> list[str]:
    return [r["key"] for r in
            charts_build.summary_ranges(_dates(days), TODAY, FY_START)]


def test_a_young_portfolio_is_not_offered_decades():
    """The whole point. Three months of history has nothing to say about 10
    years, and a button implying otherwise is a broken-looking chart."""
    keys = _keys(90)

    assert "10y" not in keys
    assert "20y" not in keys
    assert "30y" not in keys


def test_it_offers_what_it_can_draw():
    keys = _keys(90)

    assert "1w" in keys
    assert "1mo" in keys


def test_a_long_history_unlocks_the_long_ranges():
    keys = _keys(365 * 12)

    assert "10y" in keys
    assert "20y" not in keys, "twelve years is not twenty"


def test_all_is_always_offered():
    """Whatever the history, "everything" is a meaningful answer — and it is
    the fallback for anything longer than the longest button."""
    for days in (2, 90, 365 * 40):
        assert "all" in _keys(days)


def test_all_starts_at_the_first_day_there_is_data_for():
    ranges = charts_build.summary_ranges(_dates(30), TODAY, FY_START)
    everything = next(r for r in ranges if r["key"] == "all")

    assert everything["from"] == _dates(30)[0]


def test_the_financial_year_is_offered_from_its_own_start():
    """Not a fixed span: "this FY" means since 1 July here, and that is a
    jurisdiction question rather than a number of days."""
    ranges = charts_build.summary_ranges(_dates(365), TODAY, FY_START)
    fy = next(r for r in ranges if r["key"] == "fy")

    assert fy["from"] == FY_START.isoformat()


def test_no_history_offers_nothing():
    assert charts_build.summary_ranges([], TODAY, FY_START) == []


def test_the_ranges_come_back_shortest_first():
    """The bar reads left to right from tightest to widest, which is the order
    people expect and the order "All" belongs at the end of."""
    keys = _keys(365 * 12)

    assert keys[-1] == "all"
    assert keys.index("1w") < keys.index("1y") < keys.index("10y")


# What the line means: cumulative gain over money in, the SAME definition as
# the Gain tile, the FY pages and the exports. Not a time-weighted return —
# decisions.md #54.

def test_paying_money_in_is_not_a_gain():
    """The defect the measure exists to avoid, in its sharpest form: value
    doubles, and every cent of it was a deposit."""
    line = charts_build.gain_series({"gain_pct": [0.0, 0.0]})

    assert line == [0.0, 0.0]


def test_the_line_is_the_gain_the_rest_of_the_app_reports():
    """The whole point of taking it from `portfolio_series`: a chart that
    disagrees with the tile above it makes both untrustworthy, and there is no
    way to tell from the page which one is lying."""
    series = {"gain_pct": [0.0, 0.1234, 0.4725]}

    line = charts_build.gain_series(series)

    assert line[-1] == 47.25
    assert line == [round(v * 100, 4) for v in series["gain_pct"]]


def test_a_real_loss_still_shows_as_one():
    assert charts_build.gain_series({"gain_pct": [0.0, -0.1]}) == [0.0, -10.0]


def test_an_empty_series_produces_an_empty_line():
    """Before the first trade there is nothing to divide by, and
    `portfolio_series` says so with an empty list rather than a zero."""
    assert charts_build.gain_series({}) == []
    assert charts_build.gain_series({"gain_pct": []}) == []


def test_the_chart_hands_over_a_level_not_an_index():
    """The browser slices this series; it does not rebase it. If these were
    still index values starting at 1.0, every range would silently plot
    "1% gain" on its first day."""
    series = {"dates": ["2026-01-01", "2026-01-02"], "gain_pct": [0.0, 0.25]}

    line = charts_build.gain_series(series)

    assert line[0] == 0.0, "the first day of a portfolio has made nothing"
    assert line[-1] == 25.0


# --------------------------------------------------------------------------- #
# End to end: the chart on the page agrees with the portfolio
# --------------------------------------------------------------------------- #
#
# The complaint that caused this change was not that a formula was wrong — it
# was that the page said 125% while the portfolio had made 47%. That gap is
# invisible to a unit test of either half, so this walks the real route.

def test_the_chart_on_the_page_reports_the_portfolios_actual_gain(
        client, session_factory):
    """A buy at $10 that is now worth $15 is a 50% gain, and that is the
    number the chart's last point must carry."""
    import json
    import re

    import factories as fac
    from test_routes import bind_to_only_portfolio, make_login

    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME")
        fac.add_trade(s, acme, "2026-01-05", "buy", 100, "10.00", brokerage="0")
        fac.add_prices(s, acme, [("2026-01-05", "10.00"), ("2026-08-07", "15.00")])
        s.commit()

    page = client.get("/", headers={"accept": "text/html"}).text

    blob = re.search(r'<script type="application/json" id="summarydata">(.*?)</script>',
                     page, re.S)
    assert blob, "the dashboard rendered no summary chart"
    data = json.loads(blob.group(1))
    assert data["gain"][-1] == 50.0
    # …and it is a percentage, not dollars: $500 was made, and 500 must not be
    # what the chart plots.
    assert data["gain"][-1] != 500.0


def test_a_contribution_does_not_move_the_chart(client, session_factory):
    """Buying more at the current price is not a gain and must not read as one
    — the point of the whole chart."""
    import json
    import re

    import factories as fac
    from test_routes import bind_to_only_portfolio, make_login

    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME")
        # Two buys at the same price as the latest close: nothing has moved.
        fac.add_trade(s, acme, "2026-01-05", "buy", 100, "10.00", brokerage="0")
        fac.add_trade(s, acme, "2026-06-05", "buy", 100, "10.00", brokerage="0")
        fac.add_prices(s, acme, [("2026-01-05", "10.00"), ("2026-08-07", "10.00")])
        s.commit()

    page = client.get("/", headers={"accept": "text/html"}).text
    data = json.loads(re.search(
        r'<script type="application/json" id="summarydata">(.*?)</script>',
        page, re.S).group(1))

    assert data["gain"][-1] == 0.0
    assert max(abs(v) for v in data["gain"]) == 0.0
