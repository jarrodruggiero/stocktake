"""Two ways the Performance chart came to disagree with the Gain tile.

Reported from a live portfolio: the tile read **+214%** and the chart showed a
cliff to **−83%** that never recovered. Both numbers came from the same
`portfolio_series`, which is supposed to make that impossible — so there were
two faults, and each is enough on its own.

**The cache could not see a backfill.** `_series_fingerprint` keyed prices on
`max(Price.date)`. Adding an instrument and refreshing writes its history
*behind* a date some other holding had already reached, so the max does not
move, the row count is not consulted, and the stale series is served for good.
The FX half of that same function already carried the comment explaining this
failure — "a feed run that fetches only FX, or backfills an old pair, moves no
price date at all" — and prices were left with the hole it describes.

**A held instrument with no price yet was counted on one side only.** Its cost
entered `invested` from the trade date while its value was withheld for want of
a close, so the percentage collapsed by roughly the whole position. Withholding
the value is right (decisions.md #5 — never fabricate a number); counting the
cost anyway is not. It has to come out of both sides for that day, the same way
an instrument with no FX rate at all comes out of every figure.
"""

from __future__ import annotations

import datetime as dt

import pytest
from freezegun import freeze_time

import factories as fac
from app import charts_build, queries

TODAY = "2026-09-23"


def _weekly(start: str, end: str, close: str) -> list[tuple[str, str]]:
    day, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    rows = []
    while day <= last:
        rows.append((day.isoformat(), close))
        day += dt.timedelta(days=7)
    return rows


# --------------------------------------------------------------------------- #
# The cache could not see a backfill
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_backfilling_an_instruments_history_invalidates_the_series(pf):
    """The live sequence, in order.

    ABB is already priced to today. ANZ is added and its trade recorded, so the
    first render values ABB only. The feed then backfills ANZ *behind* today —
    which moves no maximum — and the chart has to notice.
    """
    abb = fac.make_instrument(pf, "ABB", asset_class="share")
    anz = fac.make_instrument(pf, "ANZ", asset_class="share")
    fac.add_prices(pf, abb, _weekly("2026-01-07", "2026-09-23", "4.17"))
    fac.add_trade(pf, abb, "2026-01-07", "buy", 10, "2.50", brokerage="9.50")
    fac.add_trade(pf, anz, "2026-02-04", "buy", 20, "10.50", brokerage="9.50")
    pf.commit()

    before = queries.cached_portfolio_series(pf)
    assert before["value"][-1] == pytest.approx(41.70)   # ANZ not valued yet

    # The backfill: every row lands on or before a date ABB already had, so
    # `max(Price.date)` is unchanged and only the row COUNT moves.
    highest = pf.execute(
        queries.select(queries.Price.date).order_by(queries.Price.date.desc()).limit(1)
    ).scalar()
    fac.add_prices(pf, anz, _weekly("2026-02-04", "2026-09-23", "37.83"))
    pf.commit()
    assert pf.execute(
        queries.select(queries.Price.date).order_by(queries.Price.date.desc()).limit(1)
    ).scalar() == highest

    after = queries.cached_portfolio_series(pf)

    assert after["value"][-1] == pytest.approx(798.30)


@freeze_time(TODAY)
def test_the_chart_and_the_tile_agree_after_a_backfill(pf):
    """What was actually reported: two numbers for one portfolio."""
    abb = fac.make_instrument(pf, "ABB", asset_class="share")
    anz = fac.make_instrument(pf, "ANZ", asset_class="share")
    fac.add_prices(pf, abb, _weekly("2026-01-07", "2026-09-23", "4.17"))
    fac.add_trade(pf, abb, "2026-01-07", "buy", 10, "2.50", brokerage="9.50")
    fac.add_trade(pf, anz, "2026-02-04", "buy", 20, "10.50", brokerage="9.50")
    pf.commit()
    queries.cached_portfolio_series(pf)          # warm the stale answer
    fac.add_prices(pf, anz, _weekly("2026-02-04", "2026-09-23", "37.83"))
    pf.commit()

    chart = charts_build.gain_series(queries.cached_portfolio_series(pf))
    tile = queries.totals([h for h in queries.cached_holdings(pf) if h.units > 0])

    assert chart[-1] == pytest.approx(float(tile.gain_pct * 100), abs=0.01)
    assert chart[-1] > 200        # and it is the +214% the tile shows


# --------------------------------------------------------------------------- #
# A held instrument with no price yet
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_a_holding_with_no_price_yet_is_out_of_both_sides(pf):
    """Its cost must not sit in the denominator while its value is withheld.

    ANZ is bought in February and priced only from August. Through those months
    the honest answer is ABB's performance — not ABB's value measured against
    both positions' cost, which is what produced the −83% cliff.
    """
    abb = fac.make_instrument(pf, "ABB", asset_class="share")
    anz = fac.make_instrument(pf, "ANZ", asset_class="share")
    fac.add_prices(pf, abb, _weekly("2026-01-07", "2026-09-23", "4.17"))
    fac.add_trade(pf, abb, "2026-01-07", "buy", 10, "2.50", brokerage="9.50")
    fac.add_trade(pf, anz, "2026-02-04", "buy", 20, "10.50", brokerage="9.50")
    fac.add_prices(pf, anz, _weekly("2026-08-05", "2026-09-23", "37.83"))
    pf.commit()

    series = queries.portfolio_series(pf)
    gain = charts_build.gain_series(series)
    march = series["dates"].index("2026-03-04")

    # ABB alone: 41.70 against 34.50 of cost.
    assert series["invested"][march] == pytest.approx(34.50)
    assert series["value"][march] == pytest.approx(41.70)
    assert gain[march] == pytest.approx(20.87, abs=0.01)
    assert min(gain) > 0          # no cliff anywhere


@freeze_time(TODAY)
def test_the_cost_comes_back_when_the_price_does(pf):
    """The exclusion is a statement about that day's data, not a verdict."""
    abb = fac.make_instrument(pf, "ABB", asset_class="share")
    anz = fac.make_instrument(pf, "ANZ", asset_class="share")
    fac.add_prices(pf, abb, _weekly("2026-01-07", "2026-09-23", "4.17"))
    fac.add_trade(pf, abb, "2026-01-07", "buy", 10, "2.50", brokerage="9.50")
    fac.add_trade(pf, anz, "2026-02-04", "buy", 20, "10.50", brokerage="9.50")
    fac.add_prices(pf, anz, _weekly("2026-08-05", "2026-09-23", "37.83"))
    pf.commit()

    series = queries.portfolio_series(pf)

    assert series["invested"][-1] == pytest.approx(254.00)
    assert series["value"][-1] == pytest.approx(798.30)


@freeze_time(TODAY)
def test_a_sold_out_holding_with_no_price_does_not_remove_its_cost(pf):
    """Only what is still HELD can be unpriceable.

    A closed position contributes `proceeds`, not `value`, so its cost belongs
    in the denominator whatever the price series does — dropping it would
    rewrite a realised result.
    """
    abb = fac.make_instrument(pf, "ABB", asset_class="share")
    fac.add_prices(pf, abb, _weekly("2026-01-07", "2026-09-23", "4.17"))
    fac.add_trade(pf, abb, "2026-01-07", "buy", 10, "2.50", brokerage="0")
    fac.add_trade(pf, abb, "2026-02-04", "sell", 10, "5.00", brokerage="0")
    pf.commit()

    series = queries.portfolio_series(pf)

    assert series["invested"][-1] == pytest.approx(25.00)
    assert series["value"][-1] == pytest.approx(0.0)
    assert series["gain"][-1] == pytest.approx(25.00)   # 50.00 proceeds - 25.00
