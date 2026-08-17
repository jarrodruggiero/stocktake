"""The reference portfolio agrees with the figures worked out by hand.

This file is the foundation the rest of the suite stands on: if these fail,
either the fixture drifted or the money math changed, and nothing else in the
suite can be trusted until it's resolved.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from freezegun import freeze_time

import fixture_portfolio as ref
from app import fyreport, queries

pytestmark = pytest.mark.usefixtures("pf")


@pytest.fixture
def portfolio(pf):
    ref.build_reference(pf)
    return pf


@freeze_time(ref.TODAY)
def test_holdings_match_hand_computed_units_cost_and_value(portfolio):
    by_ticker = {h.instrument.ticker: h for h in queries.all_holdings(portfolio)}

    for ticker, units in ref.UNITS.items():
        assert by_ticker[ticker].units == units, f"{ticker} unit balance"

    for ticker, value in ref.VALUE_AUD.items():
        assert by_ticker[ticker].value_aud == value, f"{ticker} AUD value"

    for ticker, cost in ref.COST_AUD.items():
        assert by_ticker[ticker].cost_aud == cost, f"{ticker} AUD cost"


@freeze_time(ref.TODAY)
def test_open_and_closed_positions_split_correctly(portfolio):
    open_positions, closed = queries.split_positions(queries.all_holdings(portfolio))

    assert sorted(h.instrument.ticker for h in open_positions) == [
        "ALPHA", "BETAX", "GAMMA", "OMEGA",
    ]
    # ZULU sold out entirely — it belongs in closed, not open.
    assert [c.instrument.ticker for c in closed] == ["ZULU"]


@freeze_time(ref.TODAY)
def test_dashboard_totals(portfolio):
    open_positions, _ = queries.split_positions(queries.all_holdings(portfolio))
    totals = queries.totals(open_positions)

    assert totals.cost == ref.TOTAL_COST_AUD
    assert totals.value == ref.TOTAL_VALUE_AUD
    assert totals.gain == ref.TOTAL_GAIN_AUD
    # Every holding has a price and an FX rate, so nothing is excluded.
    assert totals.excluded == []


@freeze_time(ref.TODAY)
def test_daily_move_is_alpha_only(portfolio):
    open_positions, _ = queries.split_positions(queries.all_holdings(portfolio))
    totals = queries.totals(open_positions)

    assert totals.day_change == ref.TOTAL_DAY_CHANGE_AUD


@freeze_time(ref.TODAY)
def test_series_final_point_matches_hand_computed(portfolio):
    series = queries.portfolio_series(portfolio)

    assert series["dates"][-1] == ref.TODAY.isoformat()
    assert Decimal(str(series["invested"][-1])) == ref.SERIES_INVESTED
    assert Decimal(str(series["value"][-1])) == ref.SERIES_VALUE
    assert Decimal(str(series["gain"][-1])) == ref.SERIES_GAIN


@freeze_time(ref.TODAY)
def test_fy2024_capital_gains_match_hand_computed(portfolio):
    cgt = fyreport.fy_cgt(portfolio, ref.FY2024)

    assert cgt.gains_discountable == ref.FY2024_GAINS_DISCOUNTABLE
    assert cgt.gains_other == ref.FY2024_GAINS_OTHER
    assert cgt.losses == ref.FY2024_LOSSES
    assert cgt.discount == ref.FY2024_DISCOUNT
    assert cgt.net_capital_gain == ref.FY2024_NET_CAPITAL_GAIN
    assert cgt.net_capital_loss == ref.FY2024_NET_CAPITAL_LOSS
