"""The chart series: `portfolio_series`, `grouped_series`, `instrument_series`.

All three are walks over the same event stream (trades + dividends, valued
against stepped daily prices), so the tests here lean on one identity:

    gain = value + cumulative proceeds + cumulative CASH income - invested

Everything else in this file — flow vs flow_in, what counts as `invested`, how
a non-AUD holding is converted — is a consequence of that identity holding at
*every* point in the series, not just at the last one. A series that is only
right on the final day still draws a wrong chart.

Every expected number below is derived by hand in the comment beside it. Two
constants are hand-listed from the reference fixture rather than read back out
of it (`SELL_PROCEEDS`, `CASH_DIVIDENDS`) precisely so the invariant check is
independent of the code under test: reconstructing cumulative proceeds from the
series' own `flow`/`flow_in` outputs would make the assertion circular, and in
particular would NOT notice a reinvested distribution being counted as income.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from freezegun import freeze_time
from sqlalchemy import select

import fixture_portfolio as ref
from app import queries
from app.models import Instrument
from factories import add_dividend, add_drp, add_fx_series, add_prices, add_trade, make_instrument

pytestmark = pytest.mark.usefixtures("pf")


@pytest.fixture
def portfolio(pf):
    ref.build_reference(pf)
    return pf


# --------------------------------------------------------------------------- #
# The reference portfolio's realised cash flows, hand-listed.
#
# Local to this file on purpose: the invariant test must know the answer
# without asking the series for it.
# --------------------------------------------------------------------------- #

# BETAX sell 150 x 8.00 - 10.00 brokerage = 1190.00
# ZULU  sell 200 x 9.00 - 10.00 brokerage = 1790.00
SELL_PROCEEDS = [
    (dt.date(2024, 6, 10), Decimal("1190.00")),
    (dt.date(2024, 6, 20), Decimal("1790.00")),
]
# ALPHA's 2022 distribution was taken as cash, so it IS income. ALPHA's 2021
# distribution was reinvested and is deliberately absent: those 5 units are
# already in `value`, and counting the $50 here as well double-counts it.
CASH_DIVIDENDS = [(dt.date(2022, 7, 1), Decimal("60.00"))]


def _cumulative_at(rows: list[tuple[dt.date, Decimal]], day: dt.date) -> float:
    """Sum of the hand-listed amounts dated on or before `day`."""
    return float(sum((amount for when, amount in rows if when <= day), Decimal(0)))


# --------------------------------------------------------------------------- #
# portfolio_series — the core invariant
# --------------------------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_gain_identity_holds_at_every_point_in_the_series(portfolio):
    """gain == value + proceeds + cash income - invested, on every single day.

    Checked at all ~130 points rather than only the last, because the charts
    draw the whole line: a mid-series drift (an event applied on the wrong day,
    a cumulative accumulator reset) is invisible in an end-of-series assertion.
    """
    series = queries.portfolio_series(portfolio)

    assert series["dates"], "the reference portfolio must produce a series"
    for i, iso in enumerate(series["dates"]):
        day = dt.date.fromisoformat(iso)
        expected = (
            series["value"][i]
            + _cumulative_at(SELL_PROCEEDS, day)
            + _cumulative_at(CASH_DIVIDENDS, day)
            - series["invested"][i]
        )
        assert series["gain"][i] == pytest.approx(expected, abs=0.01), f"gain at {iso}"


@freeze_time(ref.TODAY)
def test_series_matches_hand_computed_points_across_its_life(portfolio):
    """Four dates worked out by hand, each chosen for what it proves.

    Spot values rather than the whole line: the invariant above proves the
    parts are consistent with each other, these prove the parts are right.
    """
    series = queries.portfolio_series(portfolio)
    at = {iso: i for i, iso in enumerate(series["dates"])}

    def point(iso: str) -> tuple[float, float, float]:
        i = at[iso]
        return series["invested"][i], series["value"][i], series["gain"][i]

    # 2021-02-01 — ALPHA alone, one month after the only buy so far.
    # invested 100 x 10.00 + 10.00 brokerage = 1010.00
    # value    100 units x 10.00             = 1000.00
    # gain     1000.00 - 1010.00             =  -10.00 (the brokerage)
    assert point("2021-02-01") == (1010.00, 1000.00, -10.00)

    # 2021-08-01 — after the DRP. The 5 reinvested units add VALUE and nothing
    # else: no invested (they cost no money in), no income (they are units).
    # value 105 x 10.00 = 1050.00, gain 1050.00 - 1010.00 = 40.00
    assert point("2021-08-01") == (1010.00, 1050.00, 40.00)

    # 2022-08-01 — OMEGA bought (USD), and ALPHA's cash distribution received.
    # invested 1010.00 + (10 x 100.00 + 5.00) USD x 1.50 fx = 1010.00 + 1507.50
    #                                                       = 2517.50
    # value    ALPHA 105 x 10.00 = 1050.00
    #        + OMEGA 10 x 100.00 USD x 1.50 fx = 1500.00     = 2550.00
    # gain     2550.00 + 0 proceeds + 60.00 cash - 2517.50   =   92.50
    assert point("2022-08-01") == (2517.50, 2550.00, 92.50)

    # 2024-07-01 — both sells done, every position at its post-sale balance.
    # invested ALPHA 1010.00 + BETAX 1120.00 + GAMMA 2010.00
    #        + OMEGA 1507.50 + ZULU 2010.00                  = 7657.50
    # value    ALPHA 105 x 12.00 = 1260.00
    #        + BETAX  50 x  8.00 =  400.00
    #        + GAMMA 100 x 20.00 = 2000.00
    #        + OMEGA  10 x 100.00 x 1.50 = 1500.00           = 5160.00
    # gain     5160.00 + 2980.00 proceeds + 60.00 cash - 7657.50 = 542.50
    assert point("2024-07-01") == (7657.50, 5160.00, 542.50)


@freeze_time(ref.TODAY)
def test_series_starts_at_the_first_trade_not_the_first_price(portfolio):
    """ALPHA is priced from 2021-01-01 but was bought on the 4th; a chart that
    opened three days early would draw a flat zero run-in."""
    series = queries.portfolio_series(portfolio)

    # The first price date on or after the 2021-01-04 buy is the next monthly
    # calendar point, 2021-02-01.
    assert series["dates"][0] == "2021-02-01"
    assert series["dates"][-1] == ref.TODAY.isoformat()


@freeze_time(ref.TODAY)
def test_invested_never_decreases_and_ends_at_the_hand_computed_total(portfolio):
    """`invested` is all-time outlay ("Total Spent"), so it only ever ratchets
    up — a sale returns money but does not un-spend it."""
    series = queries.portfolio_series(portfolio)

    assert all(b >= a for a, b in zip(series["invested"], series["invested"][1:]))
    assert Decimal(str(series["invested"][-1])) == ref.SERIES_INVESTED  # 7657.50


# --------------------------------------------------------------------------- #
# The DRP double-count regression
# --------------------------------------------------------------------------- #

@freeze_time("2026-08-02")
def test_reinvested_dividends_are_not_counted_as_income(pf):
    """A reinvested distribution is units, not income.

    The double-count adds the DRP's cash to `divs` as well as its units to
    `value`, inflating gain by the distribution amount (~$9k on a DRP-heavy
    portfolio). The smallest shape that catches it: one buy, one reinvested
    distribution, nothing else — so gain must be exactly value - invested.
    """
    nova = make_instrument(pf, "NOVA", asset_class="etf", name="Nova Index Fund", drp=True)
    add_prices(pf, nova, [("2026-01-01", "10.00"), ("2026-04-01", "10.00"),
                          ("2026-07-01", "12.00")])
    add_trade(pf, nova, "2026-01-01", "buy", 100, "10.00", brokerage="10.00")
    # $50 of distribution taken as 5 more units at $10 — no cash ever arrives.
    add_drp(pf, nova, "2026-04-01", "50.00", 5, "10.00")

    series = queries.portfolio_series(pf)

    # invested = 100 x 10.00 + 10.00 brokerage             = 1010.00
    # value    = (100 + 5 DRP) units x 12.00               = 1260.00
    # gain     = 1260.00 - 1010.00                         =  250.00
    # Counting the $50 as income as well would read 300.00.
    assert series["invested"][-1] == 1010.00
    assert series["value"][-1] == 1260.00
    assert series["gain"][-1] == 250.00
    assert series["gain"][-1] == series["value"][-1] - series["invested"][-1]
    # And it must never show up as income on any day, including the DRP's own.
    assert series["cash_div"] == [0.0, 0.0, 0.0]


@freeze_time("2026-08-02")
def test_cash_distribution_on_the_same_shape_does_count_as_income(pf):
    """The mirror of the regression above: take the same $50 as cash and it
    must land in income, otherwise "don't double count" would have been
    implemented as "don't count at all"."""
    nova = make_instrument(pf, "NOVA", asset_class="etf", name="Nova Index Fund")
    add_prices(pf, nova, [("2026-01-01", "10.00"), ("2026-04-01", "10.00"),
                          ("2026-07-01", "12.00")])
    add_trade(pf, nova, "2026-01-01", "buy", 100, "10.00", brokerage="10.00")
    add_dividend(pf, nova, "2026-04-01", "50.00")

    series = queries.portfolio_series(pf)

    # value 100 x 12.00 = 1200.00, invested 1010.00, cash income 50.00
    # gain = 1200.00 + 50.00 - 1010.00 = 240.00
    assert series["value"][-1] == 1200.00
    assert series["gain"][-1] == 240.00
    assert series["cash_div"] == [0.0, 50.0, 0.0]  # only on the payment date


# --------------------------------------------------------------------------- #
# flow (net) vs flow_in (gross)
# --------------------------------------------------------------------------- #

@freeze_time("2026-08-02")
def test_flow_is_net_of_sale_proceeds_while_flow_in_is_gross_buys(pf):
    """The two flows exist because they answer different questions: a
    time-weighted return must remove the NET external flow, but "money you put
    in" means GROSS buys — netting sales off that shrinks the denominator and
    flatters the percentage."""
    acme = make_instrument(pf, "ACME", asset_class="share", name="Acme Industries")
    add_prices(pf, acme, [("2026-01-01", "10.00"), ("2026-03-01", "12.00"),
                          ("2026-05-01", "12.00")])
    add_trade(pf, acme, "2026-01-01", "buy", 100, "10.00", brokerage="10.00")
    add_trade(pf, acme, "2026-03-01", "sell", 40, "12.00", brokerage="10.00")

    series = queries.portfolio_series(pf)

    # Day 1: a 1010.00 buy and no sale, so the two agree.
    #        100 x 10.00 + 10.00 brokerage = 1010.00
    assert series["flow_in"][0] == 1010.00
    assert series["flow"][0] == 1010.00
    # Day 2: no buy, one sale of 40 x 12.00 - 10.00 brokerage = 470.00 out.
    #        gross buys 0.00; net flow 0.00 - 470.00 = -470.00
    assert series["flow_in"][1] == 0.00
    assert series["flow"][1] == -470.00
    # Over the whole series the difference is exactly the proceeds.
    assert sum(series["flow_in"]) - sum(series["flow"]) == pytest.approx(470.00)
    # flow_in is the day-by-day decomposition of `invested`.
    assert sum(series["flow_in"]) == pytest.approx(series["invested"][-1])


# --------------------------------------------------------------------------- #
# What `invested` counts
# --------------------------------------------------------------------------- #

@freeze_time("2026-08-02")
def test_invested_counts_buys_only_not_drp_units_or_sales(pf):
    """DRP units cost nothing (no money entered the portfolio) and a sale
    returns money rather than spending it, so neither moves `invested`."""
    widget = make_instrument(pf, "WIDGET", asset_class="share", name="Widget Co")
    add_prices(pf, widget, [("2026-01-01", "10.00"), ("2026-02-01", "10.00"),
                            ("2026-03-01", "12.00")])
    add_trade(pf, widget, "2026-01-01", "buy", 100, "10.00", brokerage="10.00")
    add_trade(pf, widget, "2026-02-01", "drp", 5, "10.00")
    add_trade(pf, widget, "2026-03-01", "sell", 20, "12.00", brokerage="10.00")

    series = queries.portfolio_series(pf)

    # The single buy: 100 x 10.00 + 10.00 brokerage = 1010.00, flat thereafter.
    assert series["invested"] == [1010.00, 1010.00, 1010.00]
    # Units still move: 100 -> 105 (DRP) -> 85 (sale).
    # 100 x 10.00 = 1000.00 | 105 x 10.00 = 1050.00 | 85 x 12.00 = 1020.00
    assert series["value"] == [1000.00, 1050.00, 1020.00]
    # Final gain: 1020.00 value + (20 x 12.00 - 10.00) = 230.00 proceeds
    #             - 1010.00 invested = 240.00
    assert series["gain"][-1] == 240.00


# --------------------------------------------------------------------------- #
# Non-AUD holdings
# --------------------------------------------------------------------------- #

@freeze_time("2026-08-02")
def test_foreign_cost_uses_each_trades_own_fx_while_value_uses_the_stored_rate(pf):
    """Cost is fixed in AUD the day it is paid; value floats with the rate.

    Two buys at deliberately different rates, then a third rate for valuation,
    so no single-rate shortcut can produce these numbers by accident.
    """
    vertex = make_instrument(pf, "VERTEX", exchange="NASDAQ", asset_class="share",
                             currency="USD", name="Vertex Systems")
    add_prices(pf, vertex, [("2026-01-01", "100.00"), ("2026-03-01", "100.00"),
                            ("2026-05-01", "110.00")])
    add_fx_series(pf, "USDAUD", [("2026-01-01", "1.50"), ("2026-03-01", "2.00"),
                                 ("2026-05-01", "1.80")])
    add_trade(pf, vertex, "2026-01-01", "buy", 10, "100.00", brokerage="5.00",
              fx_rate="1.50")
    add_trade(pf, vertex, "2026-03-01", "buy", 10, "100.00", brokerage="5.00",
              fx_rate="2.00")

    series = queries.portfolio_series(pf)

    # invested, each parcel at the rate it was actually paid at:
    #   (10 x 100.00 + 5.00) USD x 1.50 = 1507.50
    # + (10 x 100.00 + 5.00) USD x 2.00 = 2010.00   -> 3517.50
    assert series["invested"] == [1507.50, 3517.50, 3517.50]
    # value, at the rate stored for that day:
    #   10 x 100.00 x 1.50 = 1500.00
    #   20 x 100.00 x 2.00 = 4000.00
    #   20 x 110.00 x 1.80 = 3960.00
    assert series["value"] == [1500.00, 4000.00, 3960.00]
    # gain = 3960.00 - 3517.50 = 442.50. Revaluing the COST at 1.80 would give
    # 20 x 100.50 x 1.80 = 3618.00 invested and a 342.00 gain instead.
    assert series["gain"][-1] == 442.50


@freeze_time("2026-08-02")
def test_foreign_value_before_the_fx_history_starts_is_not_valued_at_one_to_one(pf):
    """A portfolio seeded from a workbook can predate the FX feed's backfill.

    Nothing has moved on day one — same price as the purchase, and 1.50 is the
    only rate the app has ever held for this pair — so the chart must not open
    with a loss manufactured out of a missing market-data row.

    `(rate or Decimal(1))` is the tempting shape and it is wrong: it values the
    holding unconverted while its cost is booked at the trade's real rate.
    `FxBook` falls back to the earliest rate it has, never to 1 —
    decisions.md #5.
    """
    vertex = make_instrument(pf, "VERTEX", exchange="NASDAQ", asset_class="share",
                             currency="USD", name="Vertex Systems")
    add_prices(pf, vertex, [("2026-01-01", "100.00"), ("2026-03-01", "100.00")])
    # The daily FX series only reaches back to March; the trade is from January.
    add_fx_series(pf, "USDAUD", [("2026-03-01", "1.50")])
    add_trade(pf, vertex, "2026-01-01", "buy", 10, "100.00", fx_rate="1.50")

    series = queries.portfolio_series(pf)

    # invested = 10 x 100.00 USD x 1.50 = 1500.00 on both days.
    assert series["invested"] == [1500.00, 1500.00]
    # value on day one should be 10 x 100.00 USD x 1.50 = 1500.00 (the earliest
    # rate known for the pair), so gain is 0.00. It currently reads 1000.00 —
    # the USD figure unconverted — and a -500.00 gain.
    assert series["value"][0] == 1500.00
    assert series["gain"][0] == 0.00


# --------------------------------------------------------------------------- #
# grouped_series
# --------------------------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_grouped_by_asset_class_sums_to_the_ungrouped_total(portfolio):
    """Splitting a chart must not create or destroy money."""
    total = queries.portfolio_series(portfolio)
    grouped = queries.grouped_series(portfolio, "asset_class")

    assert grouped["dates"] == total["dates"]
    assert sorted(grouped["groups"]) == ["etf", "share"]

    etf, share = grouped["groups"]["etf"], grouped["groups"]["share"]
    # etf   = ALPHA only:  105 x 15.00                        = 1575.00
    # share = BETAX  50 x  9.00 =  450.00
    #       + GAMMA 100 x  5.00 =  500.00
    #       + OMEGA  10 x 120.00 USD x 1.60 fx = 1920.00
    #       + ZULU (sold out, 0 units)         =    0.00      = 2870.00
    assert etf["value"][-1] == 1575.00
    assert share["value"][-1] == 2870.00
    assert etf["value"][-1] + share["value"][-1] == total["value"][-1]  # 4445.00

    # invested: etf 1010.00; share 1120.00 + 2010.00 + 1507.50 + 2010.00
    #                              = 6647.50
    assert etf["invested"][-1] == 1010.00
    assert share["invested"][-1] == 6647.50
    assert etf["invested"][-1] + share["invested"][-1] == total["invested"][-1]  # 7657.50

    # gain: etf   1575.00 + 0 proceeds + 60.00 cash - 1010.00     =  625.00
    #       share 2870.00 + 2980.00 proceeds + 0 cash - 6647.50   = -797.50
    assert etf["gain"][-1] == 625.00
    assert share["gain"][-1] == -797.50
    assert etf["gain"][-1] + share["gain"][-1] == pytest.approx(total["gain"][-1])  # -172.50


@freeze_time(ref.TODAY)
def test_grouped_by_ticker_sums_to_the_ungrouped_total(portfolio):
    """The finest split there is — every holding on its own line."""
    total = queries.portfolio_series(portfolio)
    grouped = queries.grouped_series(portfolio, "ticker")

    # ZULU is fully sold but keeps a line: it has invested history to draw.
    assert sorted(grouped["groups"]) == ["ALPHA", "BETAX", "GAMMA", "OMEGA", "ZULU"]

    per_ticker = {k: v["value"][-1] for k, v in grouped["groups"].items()}
    # 105 x 15.00 | 50 x 9.00 | 100 x 5.00 | 10 x 120.00 x 1.60 | 0 units
    assert per_ticker == {"ALPHA": 1575.00, "BETAX": 450.00, "GAMMA": 500.00,
                          "OMEGA": 1920.00, "ZULU": 0.00}
    assert sum(per_ticker.values()) == total["value"][-1]  # 4445.00

    invested = {k: v["invested"][-1] for k, v in grouped["groups"].items()}
    assert invested == {"ALPHA": 1010.00, "BETAX": 1120.00, "GAMMA": 2010.00,
                        "OMEGA": 1507.50, "ZULU": 2010.00}
    assert sum(invested.values()) == total["invested"][-1]  # 7657.50


@freeze_time(ref.TODAY)
def test_grouped_gain_identity_holds_per_group_at_every_point(portfolio):
    """The same identity as the portfolio series, but ALPHA on its own: value
    plus its cash distribution less its outlay, on every day."""
    grouped = queries.grouped_series(portfolio, "ticker")
    alpha = grouped["groups"]["ALPHA"]

    for i, iso in enumerate(grouped["dates"]):
        day = dt.date.fromisoformat(iso)
        # ALPHA never sold, so proceeds are zero throughout; its only cash
        # income is the 60.00 taken on 2022-07-01 (the 2021 one was reinvested).
        cash = 60.00 if day >= dt.date(2022, 7, 1) else 0.00
        expected = alpha["value"][i] + cash - alpha["invested"][i]
        assert alpha["gain"][i] == pytest.approx(expected, abs=0.01), f"ALPHA gain at {iso}"


@freeze_time("2026-08-02")
def test_groups_with_no_history_are_dropped(pf):
    """An instrument in the catalogue that this portfolio never bought would
    otherwise draw a flat zero line and a legend entry for nothing."""
    acme = make_instrument(pf, "ACME", asset_class="share", name="Acme Industries")
    orbit = make_instrument(pf, "ORBIT", asset_class="etf", name="Orbit Index Fund")
    add_prices(pf, acme, [("2026-01-01", "10.00"), ("2026-03-01", "12.00")])
    add_prices(pf, orbit, [("2026-01-01", "20.00"), ("2026-03-01", "25.00")])
    add_trade(pf, acme, "2026-01-01", "buy", 100, "10.00", brokerage="10.00")

    by_ticker = queries.grouped_series(pf, "ticker")
    by_class = queries.grouped_series(pf, "asset_class")

    # ORBIT is priced but never traded here: no invested, no units, no line.
    assert sorted(by_ticker["groups"]) == ["ACME"]
    assert sorted(by_class["groups"]) == ["share"]
    # ACME's own numbers are untouched by the empty neighbour:
    # invested 100 x 10.00 + 10.00 = 1010.00, value 100 x 12.00 = 1200.00
    assert by_ticker["groups"]["ACME"]["invested"][-1] == 1010.00
    assert by_ticker["groups"]["ACME"]["value"][-1] == 1200.00


@freeze_time(ref.TODAY)
def test_filters_restrict_which_instruments_contribute(portfolio):
    """The chart builder's filter shelf: an asset-class filter must remove the
    other holdings from the totals, not merely from the legend."""
    etf_only = queries.grouped_series(portfolio, "ticker", {"asset_class": ["etf"]})

    assert sorted(etf_only["groups"]) == ["ALPHA"]
    # ALPHA alone: 105 x 15.00 = 1575.00 value, 100 x 10.00 + 10.00 invested.
    assert etf_only["groups"]["ALPHA"]["value"][-1] == 1575.00
    assert etf_only["groups"]["ALPHA"]["invested"][-1] == 1010.00


@freeze_time(ref.TODAY)
def test_a_filtered_series_starts_at_the_filtered_holdings_first_trade(portfolio):
    """Filtering down to GAMMA must also shorten the date axis — carrying the
    portfolio's 2021 start would draw five years of flat nothing."""
    gamma_only = queries.grouped_series(portfolio, "asset_class", {"ticker": ["GAMMA"]})

    assert sorted(gamma_only["groups"]) == ["share"]
    assert gamma_only["dates"][0] == "2024-03-01"  # GAMMA's buy date
    # 100 x 20.00 + 10.00 = 2010.00 invested, 100 x 5.00 = 500.00 value today,
    # so gain = 500.00 - 2010.00 = -1510.00
    assert gamma_only["groups"]["share"]["invested"][-1] == 2010.00
    assert gamma_only["groups"]["share"]["value"][-1] == 500.00
    assert gamma_only["groups"]["share"]["gain"][-1] == -1510.00


@freeze_time(ref.TODAY)
def test_a_filter_matching_nothing_yields_an_empty_series(portfolio):
    """A filter combination with no instruments behind it is a legitimate state
    of the chart builder, not an error."""
    none = queries.grouped_series(portfolio, "ticker", {"asset_class": ["crypto"]})

    assert none == {"dates": [], "groups": {}, "excluded": []}


# --------------------------------------------------------------------------- #
# Empty portfolios
# --------------------------------------------------------------------------- #

@freeze_time("2026-08-02")
def test_empty_portfolio_returns_empty_series_rather_than_raising(pf):
    """A brand-new account loads the charts page before entering anything."""
    series = queries.portfolio_series(pf)

    assert series == {"dates": [], "invested": [], "value": [], "gain": [],
                      "gain_pct": [], "flow": [], "flow_in": [], "cash_div": [],
                      "excluded": []}
    assert queries.grouped_series(pf, "ticker") == {"dates": [], "groups": {}, "excluded": []}


@freeze_time("2026-08-02")
def test_catalogue_with_prices_but_no_trades_returns_an_empty_series(pf):
    """Market data is shared, so a fresh portfolio can see priced instruments
    it has never held. That is still an empty series, not a zero-value line."""
    orbit = make_instrument(pf, "ORBIT", asset_class="etf", name="Orbit Index Fund")
    add_prices(pf, orbit, [("2026-01-01", "20.00"), ("2026-03-01", "25.00")])

    assert queries.portfolio_series(pf)["dates"] == []
    assert queries.grouped_series(pf, "asset_class") == {"dates": [], "groups": {}, "excluded": []}


# --------------------------------------------------------------------------- #
# instrument_series
# --------------------------------------------------------------------------- #

@freeze_time("2026-08-02")
def test_instrument_series_tracks_cumulative_invested_and_unit_value(pf):
    """One holding's own line: invested ratchets on buys only, value follows
    the unit balance times that day's close."""
    delta = make_instrument(pf, "DELTAQ", asset_class="share", name="Delta Quarry")
    add_prices(pf, delta, [("2025-12-01", "9.00"), ("2026-01-01", "10.00"),
                           ("2026-02-01", "11.00"), ("2026-03-01", "12.00"),
                           ("2026-04-01", "12.00")])
    add_trade(pf, delta, "2026-01-01", "buy", 100, "10.00", brokerage="10.00")
    add_trade(pf, delta, "2026-02-01", "drp", 5, "11.00")
    add_trade(pf, delta, "2026-03-01", "sell", 25, "12.00", brokerage="10.00")

    series = queries.instrument_series(pf, delta)

    # The 2025-12-01 close predates the first trade and is dropped.
    assert series["dates"] == ["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"]
    # invested: 100 x 10.00 + 10.00 brokerage = 1010.00, then flat — the DRP
    # units cost nothing and the sale is not a negative purchase.
    assert series["invested"] == [1010.00, 1010.00, 1010.00, 1010.00]
    # value: 100 x 10.00 | 105 x 11.00 | 80 x 12.00 | 80 x 12.00
    assert series["value"] == [1000.00, 1155.00, 960.00, 960.00]


@freeze_time("2026-08-02")
def test_instrument_series_is_in_native_currency_not_converted(pf):
    """The per-holding chart is drawn in the instrument's own currency, so a
    stored FX rate must not touch it — mixing the two would silently rescale
    every USD holding's line."""
    vertex = make_instrument(pf, "VERTEX", exchange="NASDAQ", asset_class="share",
                             currency="USD", name="Vertex Systems")
    add_prices(pf, vertex, [("2026-01-01", "100.00"), ("2026-03-01", "110.00")])
    add_fx_series(pf, "USDAUD", [("2026-01-01", "1.50"), ("2026-03-01", "1.80")])
    add_trade(pf, vertex, "2026-01-01", "buy", 10, "100.00", brokerage="5.00",
              fx_rate="1.50")

    series = queries.instrument_series(pf, vertex)

    # USD throughout: 10 x 100.00 + 5.00 = 1005.00 invested (not x1.50),
    # value 10 x 100.00 = 1000.00 then 10 x 110.00 = 1100.00 (not x1.80).
    assert series["invested"] == [1005.00, 1005.00]
    assert series["value"] == [1000.00, 1100.00]


@freeze_time("2026-08-02")
def test_instrument_series_is_empty_for_an_untraded_instrument(pf):
    """Opening the ledger for something in the catalogue this portfolio has
    never bought."""
    orbit = make_instrument(pf, "ORBIT", asset_class="etf", name="Orbit Index Fund")
    add_prices(pf, orbit, [("2026-01-01", "20.00"), ("2026-03-01", "25.00")])

    assert queries.instrument_series(pf, orbit) == {"dates": [], "invested": [], "value": []}


@freeze_time(ref.TODAY)
def test_instrument_series_value_reaches_zero_when_a_position_is_closed(portfolio):
    """ZULU was sold out entirely: its line must go to zero and stay there
    while `invested` keeps the outlay on the record."""
    zulu = portfolio.scalars(select(Instrument).where(Instrument.ticker == "ZULU")).one()
    series = queries.instrument_series(portfolio, zulu)

    at = {iso: i for i, iso in enumerate(series["dates"])}
    # Bought 200 @ 10.00 + 10.00 brokerage on 2023-02-01 -> 2010.00 outlay,
    # 200 x 10.00 = 2000.00 of value the day it was bought.
    assert series["invested"][at["2023-02-01"]] == 2010.00
    assert series["value"][at["2023-02-01"]] == 2000.00
    # Sold out on 2024-06-20; the next price point holds no units.
    assert series["value"][at["2024-07-01"]] == 0.00
    assert series["invested"][at["2024-07-01"]] == 2010.00  # outlay stays on record
    assert series["value"][-1] == 0.00
