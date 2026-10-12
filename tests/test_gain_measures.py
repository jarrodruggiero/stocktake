"""Two measures of gain, each labelled for what it is (decisions.md #54).

A holding's gain is UNREALISED: value less what the units still held cost,
the oldest sold first. The portfolio's chart and its period figures are TOTAL
RETURN: value plus sale proceeds plus cash income, less everything put in.
Before, the Gain tile and column measured value against every buy, sold or
not, which is neither: buy 100 at $10, receive $100, sell 50 at $20, and the
price now $20 showed a gain of 0, the chart 110% and the unrealised-CGT export
500, for one position.
"""

from __future__ import annotations

import re
from decimal import Decimal

from freezegun import freeze_time

import factories as fac
from app import exports, fyreport, queries
from test_routes import bind_to_only_portfolio, make_login

HTML = {"accept": "text/html"}
TODAY = "2026-08-03"


def _review_case(s):
    """The review's case: 100 at $10, $100 paid in cash, 50 sold at $20, and
    $20 now."""
    acme = fac.make_instrument(s, "ACME")
    fac.add_trade(s, acme, "2025-01-06", "buy", 100, "10.00")
    fac.add_dividend(s, acme, "2025-03-03", "100.00")
    fac.add_trade(s, acme, "2025-06-02", "sell", 50, "20.00")
    fac.add_prices(s, acme, [("2025-01-06", "10.00"), ("2025-06-02", "20.00"),
                             ("2026-08-03", "20.00")])
    return acme


@freeze_time(TODAY)
def test_a_holding_measures_the_units_it_still_has(pf):
    _review_case(pf)

    [holding] = queries.all_holdings(pf)
    totals = queries.totals([holding])

    # 50 units held, bought at $10: cost base 500, worth 1,000.
    assert (holding.cost, holding.cost_aud, holding.value_aud) == (500, 500, 1000)
    assert (holding.gain_aud, holding.gain_pct) == (500, 1)
    assert (totals.cost, totals.gain, totals.gain_pct) == (500, 500, 1)


@freeze_time(TODAY)
def test_the_chart_measures_total_return(pf):
    """Value 1,000 + proceeds 1,000 + income 100 − invested 1,000 = 1,100."""
    _review_case(pf)

    series = queries.portfolio_series(pf)

    assert (series["gain"][-1], series["gain_pct"][-1]) == (1100.0, 1.1)


@freeze_time(TODAY)
def test_the_holding_and_the_unrealised_cgt_export_agree(pf):
    _review_case(pf)

    _headers, rows = exports.unrealised_cgt(pf)
    holdings_headers, holdings_rows = exports.holdings(pf)
    row = dict(zip(holdings_headers, holdings_rows[0]))

    assert [r[7] for r in rows] == [Decimal("500.00")]
    assert (row["Cost base (AUD)"], row["Unrealised gain (AUD)"]) == (
        Decimal("500.00"), Decimal("500.00"))


@freeze_time(TODAY)
def test_a_reinvested_unit_counts_at_what_it_cost(pf):
    """A reinvested unit was paid for with the distribution, which is its cost
    base. Counted as free, the distribution was in the gain as well as in the
    Dividends column."""
    acme = fac.make_instrument(pf, "ACME")
    fac.add_trade(pf, acme, "2025-01-06", "buy", 100, "10.00", brokerage="10.00")
    fac.add_drp(pf, acme, "2025-07-01", "60.00", 5, "12.00")
    fac.add_prices(pf, acme, [("2026-08-03", "12.00")])

    [holding] = queries.all_holdings(pf)

    assert (holding.cost_aud, holding.value_aud, holding.gain_aud) == (1070, 1260, 190)
    assert holding.invested == 1010   # every buy: what a closed position cost


@freeze_time(TODAY)
def test_a_parcel_sold_in_full_has_no_gain_in_the_ledger(pf):
    """The ledger's per-parcel gain is on the units of it still held, so a
    parcel the FIFO sold has none, and one sold in part counts what is left."""
    acme = fac.make_instrument(pf, "ACME")
    fac.add_trade(pf, acme, "2025-01-06", "buy", 100, "10.00", brokerage="10.00")
    fac.add_trade(pf, acme, "2025-02-03", "buy", 100, "11.00", brokerage="10.00")
    fac.add_trade(pf, acme, "2025-06-02", "sell", 150, "20.00", brokerage="10.00")
    fac.add_prices(pf, acme, [("2026-08-03", "15.00")])

    _inst, holding, events = queries.ledger(pf, "ACME")
    gains = {(e.date.isoformat(), e.kind): (e.gain, e.gain_pct) for e in events}

    assert gains[("2025-01-06", "buy")] == (None, None)
    # 50 of the second parcel: cost (1,100 + 10) / 2 = 555, worth 750.
    assert gains[("2025-02-03", "buy")] == (Decimal("195"), Decimal("195") / Decimal("555"))
    assert holding.gain == Decimal("195")


@freeze_time(TODAY)
def test_a_holding_with_a_sale_of_more_than_was_held_has_no_cost(client, session_factory):
    """Written before every form refused one: there is no FIFO cost for what
    remains, so the holding is left out of the totals and named, and the page
    says what to do."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME")
        fac.add_trade(s, acme, "2025-01-06", "buy", 100, "10.00")
        fac.add_trade(s, acme, "2025-02-03", "sell", 150, "12.00")
        fac.add_trade(s, acme, "2025-03-03", "buy", 100, "10.00")
        fac.add_prices(s, acme, [("2026-08-03", "11.00")])
        s.commit()

    with session_factory() as s:
        bind_to_only_portfolio(s)
        [holding] = queries.all_holdings(s)
        totals = queries.totals([holding])
    page = client.get("/", headers=HTML).text

    assert (holding.oversold, holding.units, holding.cost, holding.gain) == (
        True, Decimal(50), None, None)
    assert (totals.oversold, totals.excluded, totals.cost) == (["ACME"], [], 0)
    assert ("Totals exclude ACME — a sale of more\nunits than were held. Correct it or "
            "record the missing buy.") in page


@freeze_time(TODAY)
def test_the_fy_snapshot_measures_the_units_held_at_its_end(pf):
    """At 30 June 2025, 50 units held, bought at $10, worth $20: 500 on 500."""
    _review_case(pf)

    [row] = fyreport.fy_report(pf, 2025)["snapshot"]

    assert (row.cost_base, row.value_aud, row.gain_aud, row.gain_pct) == (
        500, 1000, 500, 1)
    assert row.invested_cum == 1000   # every buy to date, for the API's `invested`


@freeze_time(TODAY)
def test_a_trade_with_no_rate_is_converted_as_the_fy_page_converts_it(pf):
    """The dashboard took only each trade's own rate and left the holding out
    without one, while the FY page took the stored series for its date: one
    holding, two answers (decisions.md #5)."""
    nova = fac.make_instrument(pf, "NOVA", exchange="NASDAQ", currency="USD")
    fac.add_trade(pf, nova, "2026-07-01", "buy", 10, "100.00", fx_rate=None)
    fac.add_fx(pf, "USDAUD", "2026-06-30", "1.50")
    fac.add_fx(pf, "USDAUD", "2026-07-31", "1.60")
    fac.add_prices(pf, nova, [("2026-08-03", "110.00")])

    [holding] = queries.all_holdings(pf)

    assert (holding.cost_aud, holding.value_aud) == (1500, 1760)
    assert queries.totals([holding]).excluded == []


@freeze_time(TODAY)
def test_the_pages_name_each_measure(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        _review_case(s)
        s.commit()

    dashboard = client.get("/", headers=HTML).text
    fy = client.get("/?fy=2025", headers=HTML).text
    holding = client.get("/holding/ACME", headers=HTML).text

    tiles = re.findall(r'<span class="label">([^<]+)</span>', dashboard)
    assert tiles[:3] == ["Cost base", "Total value", "Unrealised gain"]
    assert ">Unrealised gain<" in re.sub(r"<span class=\"sortarrow\".*?</span>", "",
                                         dashboard.split('class="holdings"', 1)[1])
    assert 'aria-label="Total return: ' in dashboard
    assert "Cost base (AUD)" in fy and "Unrealised gain (AUD)" in fy
    assert '<span class="label">Cost base</span>' in holding
