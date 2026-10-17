"""Simulating a sale: what selling units today would bring home after tax.

The ATO's method (decisions.md #148): each sale's capital gain is its proceeds
less its cost base, brokerage included; the year's gains are reduced by its
losses and then by losses carried in from earlier years, non-discountable gains
first; the 50% discount applies to what is left of gains on parcels held over
twelve months; and the resulting net capital gain is taxed at the marginal
rate, and the Medicare levy on top. So the tax a sale costs is what it ADDS to
the year's net capital gain, times each. Every figure below is worked out by
hand.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from freezegun import freeze_time
from sqlalchemy import func, select

import factories as fac
from app import queries, simulate, tenancy
from app.models import Portfolio, Trade

TODAY = "2026-10-17"          # FY2027: 1 July 2026 to 30 June 2027
RATES = {"income_tax_rate": Decimal(30), "medicare_levy": Decimal(2)}


@pytest.fixture
def alpha(pf):
    """Two parcels: 100 at $10 held over a year by today, 100 at $12 not."""
    inst = fac.make_instrument(pf, "ALPHA", asset_class="share")
    fac.add_trade(pf, inst, "2025-01-06", "buy", 100, "10.00", brokerage="10")
    fac.add_trade(pf, inst, "2026-03-02", "buy", 100, "12.00", brokerage="10")
    fac.add_prices(pf, inst, [("2026-10-16", "15.00")])
    return inst


def _sell(pf, inst, units, *, rates=RATES, **kwargs):
    return simulate.sale(pf, {inst.id: Decimal(units)}, **rates, **kwargs)


@freeze_time(TODAY)
def test_a_sale_is_matched_fifo_and_taxed_on_what_it_adds(pf, alpha):
    """150 units at $15, less the last trade's $10 brokerage: 2,240. FIFO takes
    all of the first parcel (1,010, held over a year) and half the second
    (605). Gains 483.33 discountable and 141.67 not; the discount halves the
    first, so 383.335 is added to the year, taxed at 32%."""
    sale = _sell(pf, alpha, 150)
    [line] = sale.lines

    assert (line.units, line.value, line.brokerage_aud, line.cost_base, line.gain) == (
        150, Decimal("2250.00"), Decimal("10.00"), Decimal("1615.00"), Decimal("625.00"))
    assert (sale.gain, sale.discount, sale.losses_used, sale.taxable) == (
        Decimal("625.00"), Decimal("241.665"), 0, Decimal("383.335"))
    assert (sale.income_tax, sale.levy, sale.tax, sale.take_home) == (
        Decimal("115.00"), Decimal("7.67"), Decimal("122.67"), Decimal("2117.33"))


@freeze_time(TODAY)
def test_a_loss_carried_in_comes_off_before_the_discount(pf, alpha):
    """BETAX lost 270 in FY2025, carried to now. It takes the 141.67 that has
    no discount first, then 128.33 of the discountable gain: 355.00 left,
    halved to 177.50."""
    beta = fac.make_instrument(pf, "BETAX", asset_class="share")
    fac.add_trade(pf, beta, "2025-02-03", "buy", 50, "20.00", brokerage="10")
    fac.add_trade(pf, beta, "2025-06-02", "sell", 50, "15.00", brokerage="10")

    sale = _sell(pf, alpha, 150)

    assert (sale.carried_in, sale.losses_used, sale.discount, sale.taxable) == (
        Decimal("270.00"), Decimal("270.00"), Decimal("177.50"), Decimal("177.50"))
    assert sale.tax == Decimal("56.80")


@freeze_time(TODAY)
def test_this_years_sales_so_far_are_counted(pf, alpha):
    """GAMMA lost 120 in August. It comes off the gain with no discount."""
    gamma = fac.make_instrument(pf, "GAMMA", asset_class="share")
    fac.add_trade(pf, gamma, "2026-07-01", "buy", 10, "100.00", brokerage="10")
    fac.add_trade(pf, gamma, "2026-08-03", "sell", 10, "90.00", brokerage="10")

    sale = _sell(pf, alpha, 150)

    assert (sale.year_so_far, sale.losses_used, sale.taxable) == (
        Decimal("-120.00"), Decimal("120.00"), Decimal("263.335"))


@freeze_time(TODAY)
def test_a_loss_on_the_sale_lowers_the_years_tax(pf, alpha):
    """GAMMA made 480 in August. Selling 100 ALPHA at $8 loses 220, which comes
    off it: 220 less is taxed, 70.40 saved, and that is part of what the sale
    is worth."""
    gamma = fac.make_instrument(pf, "GAMMA", asset_class="share")
    fac.add_trade(pf, gamma, "2026-07-01", "buy", 10, "100.00", brokerage="10")
    fac.add_trade(pf, gamma, "2026-08-03", "sell", 10, "150.00", brokerage="10")
    fac.add_prices(pf, alpha, [("2026-10-17", "8.00")])

    sale = _sell(pf, alpha, 100)

    assert (sale.gain, sale.taxable, sale.tax) == (
        Decimal("-220.00"), Decimal("-220.00"), Decimal("-70.40"))
    assert sale.take_home == Decimal("860.40")       # 800 − 10 + 70.40


@freeze_time(TODAY)
def test_a_loss_with_nothing_to_offset_carries_forward(pf, alpha):
    fac.add_prices(pf, alpha, [("2026-10-17", "8.00")])

    sale = _sell(pf, alpha, 100)

    assert (sale.taxable, sale.tax, sale.take_home) == (0, 0, Decimal("790.00"))
    assert sale.carried_forward == Decimal("220.00")


@freeze_time(TODAY)
def test_brokerage_is_what_the_trade_form_would_suggest(pf, alpha):
    """The portfolio's fee when it has one: $9.50 plus 0.1%, at least $15, so
    15.00 on 2,250. A foreign holding takes the other-currencies fee, in its
    own currency. A brokerage typed in is used instead."""
    portfolio = pf.get(Portfolio, tenancy.current_portfolio_id(pf))
    portfolio.brokerage_flat, portfolio.brokerage_percent = Decimal("9.50"), Decimal("0.1")
    portfolio.brokerage_minimum = Decimal(15)
    portfolio.foreign_brokerage_flat = Decimal(5)
    nova = fac.make_instrument(pf, "NOVA", exchange="NASDAQ", currency="USD")
    fac.add_trade(pf, nova, "2025-01-06", "buy", 10, "100.00", brokerage="5", fx_rate="1.60")
    fac.add_prices(pf, nova, [("2026-10-16", "120.00")])
    fac.add_fx(pf, "USDAUD", "2026-10-16", "1.50")

    sale = simulate.sale(pf, {alpha.id: Decimal(150), nova.id: Decimal(10)}, **RATES)
    typed = simulate.sale(pf, {alpha.id: Decimal(150)}, brokerage={alpha.id: Decimal(0)},
                          **RATES)

    lines = {line.holding.instrument.ticker: line for line in sale.lines}
    assert (lines["ALPHA"].brokerage, lines["NOVA"].brokerage) == (Decimal("15.00"),
                                                                  Decimal("5.00"))
    assert lines["NOVA"].brokerage_aud == Decimal("7.50")
    assert [line.brokerage for line in typed.lines if line.units] == [0]


@freeze_time(TODAY)
def test_a_foreign_holding_is_sold_at_todays_rate_against_its_own(pf):
    """Bought at 1.60, sold at 1.50: (1,200 − 5) × 1.50 = 1,792.50 against a
    cost base of 1,005 × 1.60 = 1,608.00."""
    nova = fac.make_instrument(pf, "NOVA", exchange="NASDAQ", currency="USD")
    fac.add_trade(pf, nova, "2025-01-06", "buy", 10, "100.00", brokerage="5", fx_rate="1.60")
    fac.add_prices(pf, nova, [("2026-10-16", "120.00")])
    fac.add_fx(pf, "USDAUD", "2026-10-16", "1.50")

    [line] = _sell(pf, nova, 10).lines

    assert (line.value, line.cost_base, line.gain) == (
        Decimal("1800.00"), Decimal("1608.00"), Decimal("184.50"))


@freeze_time(TODAY)
def test_a_holding_that_cannot_be_valued_is_not_offered(pf, alpha):
    """No price, or no rate to convert one: the sale's figures would be
    guesses (decisions.md #5)."""
    omega = fac.make_instrument(pf, "OMEGA", asset_class="share")
    fac.add_trade(pf, omega, "2025-01-06", "buy", 10, "5.00")
    nova = fac.make_instrument(pf, "NOVA", exchange="NASDAQ", currency="USD")
    fac.add_trade(pf, nova, "2025-01-06", "buy", 10, "100.00", fx_rate="1.60")
    fac.add_prices(pf, nova, [("2026-10-16", "120.00")])

    sale = simulate.sale(pf, {}, **RATES)

    assert {line.holding.instrument.ticker: line.why_not for line in sale.lines} == {
        "ALPHA": None, "NOVA": "no exchange rate", "OMEGA": "no price"}


@freeze_time(TODAY)
def test_units_bought_today_can_be_sold_today(pf, alpha):
    """The sale happens now, after anything already recorded for today."""
    fac.add_trade(pf, alpha, TODAY, "buy", 50, "15.00", brokerage="10")

    [line] = _sell(pf, alpha, 250).lines

    assert (line.units, line.cost_base) == (250, Decimal("2980.00"))   # 1,010 + 1,210 + 760


@freeze_time(TODAY)
def test_a_holding_with_a_sale_of_more_than_was_held_is_not_offered(pf, alpha):
    """It has no FIFO cost to sell from (decisions.md #54)."""
    beta = fac.make_instrument(pf, "BETAX", asset_class="share")
    fac.add_trade(pf, beta, "2025-02-03", "buy", 10, "20.00")
    fac.add_trade(pf, beta, "2025-03-03", "sell", 15, "21.00")
    fac.add_trade(pf, beta, "2025-04-01", "buy", 20, "22.00")
    fac.add_prices(pf, beta, [("2026-10-16", "23.00")])

    sale = simulate.sale(pf, {beta.id: Decimal(5)}, **RATES)

    assert {line.holding.instrument.ticker: (line.why_not, line.units)
            for line in sale.lines} == {
        "ALPHA": (None, 0), "BETAX": ("a sale of more than was held", 0)}


@freeze_time(TODAY)
def test_without_a_rate_there_is_no_tax_figure(pf, alpha):
    sale = _sell(pf, alpha, 150, rates={})

    assert (sale.taxable, sale.tax, sale.take_home) == (Decimal("383.335"), None, None)


@freeze_time(TODAY)
def test_without_a_levy_none_is_added(pf, alpha):
    sale = _sell(pf, alpha, 150, rates={"income_tax_rate": Decimal(30)})

    assert (sale.income_tax, sale.levy, sale.tax) == (Decimal("115.00"), None,
                                                      Decimal("115.00"))


@freeze_time(TODAY)
def test_selling_everything_costs_what_the_holding_says_it_cost(pf, alpha):
    """The dashboard's cost base and the sale's come from one FIFO walk, a
    split and a return of capital included."""
    fac.add_action(pf, alpha, "2026-04-01", "split", ratio=(2, 1))
    fac.add_action(pf, alpha, "2026-05-01", "capital_return", amount="40.00")
    [holding] = queries.all_holdings(pf)

    [line] = _sell(pf, alpha, holding.units).lines

    assert (line.units, line.cost_base) == (400, holding.cost_aud)


@freeze_time(TODAY)
def test_nothing_is_recorded(pf, alpha):
    before = pf.scalar(select(func.count()).select_from(Trade))

    _sell(pf, alpha, 150)

    assert pf.scalar(select(func.count()).select_from(Trade)) == before
    assert not pf.new and not pf.dirty


@freeze_time(TODAY)
def test_a_percentage_of_the_portfolio_is_taken_from_every_holding(pf, alpha):
    """Half of each: whole units of a share, rounded down so the sale is never
    more than asked; a coin to its eighth place; all of it at 100%. One that
    cannot be valued is not offered, so it is not filled either."""
    beta = fac.make_instrument(pf, "BETAX", asset_class="share")
    fac.add_trade(pf, beta, "2025-02-03", "buy", 33, "20.00")
    coin = fac.make_instrument(pf, "ZULU", exchange="CRYPTO", asset_class="crypto")
    fac.add_trade(pf, coin, "2025-02-03", "buy", "0.12345679", "100.00")
    for inst in (beta, coin):
        fac.add_prices(pf, inst, [("2026-10-16", "30.00")])
    omega = fac.make_instrument(pf, "OMEGA", asset_class="share")
    fac.add_trade(pf, omega, "2025-01-06", "buy", 10, "5.00")
    # A reinvestment can leave part of a unit, which 100% still sells.
    fac.add_trade(pf, alpha, "2026-06-30", "drp", "0.5", "14.00")
    holdings = queries.all_holdings(pf)

    half = simulate.fill(holdings, Decimal(50))
    everything = simulate.fill(holdings, Decimal(100))

    assert half == {alpha.id: 100, beta.id: 16, coin.id: Decimal("0.06172839")}
    assert everything == {alpha.id: Decimal("200.5"), beta.id: 33,
                          coin.id: Decimal("0.12345679")}
