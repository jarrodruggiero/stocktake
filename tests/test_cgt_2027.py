"""Capital gains from 1 July 2027 (decisions.md #150).

Treasury Laws Amendment (Tax Reform No. 1) Act 2026, No. 49 of 2026:

- An asset held through 30 June 2027 is treated as sold just before 1 July 2027
  for its market value, and bought again on 1 July 2027 for the same (ITAA 1997
  s 112-155). The gain to then is deferred to the year of the real sale, and is
  discounted by 50% if the asset was held at least 12 months by that sale
  (s 112-160).
- From 1 July 2027 a cost base is indexed by the CPI instead (s 110-36(1A)),
  if the asset was acquired at least 12 months before the sale, counted from
  the real acquisition (s 114-10(9)). Indexing cannot make or grow a loss: a
  loss is worked from the cost base as it was.
- The year's losses reduce the deferred gains first and the others after;
  losses carried in do the same; the discount comes last (s 102-5(1)).

Every figure below is worked out by hand. The CPI numbers are made up.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import pytest
from freezegun import freeze_time

import factories as fac
from app import exports, fyreport, queries, simulate
from app.models import CpiQuarter

D = dt.date
CPI = {D(2027, 7, 1): "100.00", D(2027, 10, 1): "101.00", D(2028, 1, 1): "102.00",
       D(2028, 4, 1): "103.00", D(2028, 7, 1): "104.00"}


def _cpi(pf, quarters=None):
    for quarter, number in (quarters or CPI).items():
        pf.add(CpiQuarter(quarter=quarter, index_number=Decimal(number),
                          reference_period="Sep 2025 = 100.0"))
    pf.flush()


def _sale(pf, inst) -> fyreport.Disposal:
    [sale] = fyreport.instrument_disposals(inst, queries.FxBook(pf))
    return sale


@pytest.fixture
def alpha(pf):
    """100 bought in 2025 for 1,010, worth 1,500 at the close of 30 June 2027."""
    _cpi(pf)
    inst = fac.make_instrument(pf, "ALPHA", asset_class="share")
    fac.add_trade(pf, inst, "2025-01-06", "buy", 100, "10.00", brokerage="10")
    fac.add_prices(pf, inst, [("2027-06-30", "15.00")])
    return inst


@freeze_time("2029-01-15")
def test_a_parcel_held_through_30_june_2027_is_split_at_its_value_then(pf, alpha):
    """Sold for 1,990 net in August 2028. Deferred: 1,500 − 1,010 = 490,
    discountable. After: 1,500 indexed by 104/100 is 1,560, so 430."""
    fac.add_trade(pf, alpha, "2028-08-01", "sell", 100, "20.00", brokerage="10")

    [use] = _sale(pf, alpha).parcels
    cgt = fyreport.fy_cgt(pf, 2029)

    assert (use.deferred_proceeds, use.deferred_cost, use.deferred) == (
        Decimal("1500.00"), Decimal("1010.00"), Decimal("490.00"))
    assert (use.cost_base, use.factor, use.indexed_cost, use.proceeds) == (
        Decimal("1500.00"), Decimal("1.040"), Decimal("1560.00"), Decimal("1990.00"))
    assert (use.indexed_gain, use.gain, use.discountable) == (
        Decimal("430.00"), Decimal("920.00"), True)
    # What was paid and what indexing took off: 1,990 − 1,010 − 60 is the 920.
    assert (use.cost, use.indexation, use.discount) == (
        Decimal("1010.00"), Decimal("60.00"), Decimal("245.00"))
    assert (cgt.gains_discountable, cgt.gains_from_july_2027, cgt.discount, cgt.net_capital_gain) == (
        Decimal("490.00"), Decimal("430.00"), Decimal("245.00"), Decimal("675.00"))


@freeze_time("2029-01-15")
def test_a_parcel_bought_from_1_july_2027_is_indexed_from_its_own_quarter(pf):
    _cpi(pf)
    beta = fac.make_instrument(pf, "BETAX", asset_class="share")
    fac.add_trade(pf, beta, "2027-08-02", "buy", 100, "10.00", brokerage="10")
    fac.add_trade(pf, beta, "2028-09-01", "sell", 100, "12.00", brokerage="10")

    [use] = _sale(pf, beta).parcels

    assert (use.deferred, use.factor, use.indexed_cost, use.gain) == (
        None, Decimal("1.040"), Decimal("1050.40"), Decimal("139.60"))
    assert fyreport.fy_cgt(pf, 2029).net_capital_gain == Decimal("139.60")


@freeze_time("2029-01-15")
def test_held_under_a_year_there_is_no_discount_and_no_indexing(pf):
    """Bought 3 May 2027, sold 1 November 2027: the year is counted from the
    real purchase, not the deemed one on 1 July (s 114-10(9))."""
    _cpi(pf)
    gamma = fac.make_instrument(pf, "GAMMA", asset_class="share")
    fac.add_trade(pf, gamma, "2027-05-03", "buy", 100, "10.00", brokerage="10")
    fac.add_prices(pf, gamma, [("2027-06-30", "15.00")])
    fac.add_trade(pf, gamma, "2027-11-01", "sell", 100, "16.00", brokerage="10")

    [use] = _sale(pf, gamma).parcels
    cgt = fyreport.fy_cgt(pf, 2028)

    assert (use.deferred, use.discountable, use.factor, use.indexed_gain) == (
        Decimal("490.00"), False, None, Decimal("90.00"))
    assert (cgt.discount, cgt.net_capital_gain) == (0, Decimal("580.00"))
    # Not indexed, but a gain from 1 July 2027 all the same: the minimum tax's.
    assert (cgt.gains_other, cgt.gains_from_july_2027, cgt.minimum_tax_gain) == (
        Decimal("490.00"), Decimal("90.00"), Decimal("90.00"))


@freeze_time("2029-01-15")
def test_the_year_is_counted_from_the_real_purchase(pf):
    """Bought 1 March 2027 and sold 3 April 2028: over a year since it was
    bought, under one since 1 July 2027. So the gain to then is discounted,
    and the cost base after is indexed, 103/100: 1,545, and 445 after."""
    _cpi(pf)
    delta = fac.make_instrument(pf, "DELTA", asset_class="share")
    fac.add_trade(pf, delta, "2027-03-01", "buy", 100, "10.00", brokerage="10")
    fac.add_prices(pf, delta, [("2027-06-30", "15.00")])
    fac.add_trade(pf, delta, "2028-04-03", "sell", 100, "20.00", brokerage="10")

    [use] = _sale(pf, delta).parcels

    assert (use.discountable, use.factor, use.indexed_gain, use.discount) == (
        True, Decimal("1.030"), Decimal("445.00"), Decimal("245.00"))


@freeze_time("2029-01-15")
def test_an_increase_after_1_july_2027_is_indexed_from_its_own_quarter(pf, alpha):
    """An AMIT increase of 104 in February 2028 is indexed by 104/102, 1.020,
    to 106.08, beside the 1,500 by 1.040 to 1,560: 1,666.08, so 323.92 after.
    What it cost is the 1,010 paid and the 104."""
    fac.add_action(pf, alpha, "2028-02-01", "cost_base", amount="104.00")
    fac.add_trade(pf, alpha, "2028-08-01", "sell", 100, "20.00", brokerage="10")

    [use] = _sale(pf, alpha).parcels

    assert (use.cost_base, use.indexed_cost, use.indexed_gain) == (
        Decimal("1604.00"), Decimal("1666.08"), Decimal("323.92"))
    assert (use.cost, use.indexation) == (Decimal("1114.00"), Decimal("62.08"))


@freeze_time("2029-01-15")
@pytest.mark.parametrize("price, gain, indexation", [
    ("10.30", Decimal("0"), Decimal("10.00")), ("9.10", Decimal("-110.00"), Decimal("0")),
], ids=["between-the-two-cost-bases", "below-the-cost-base"])
def test_indexing_cannot_make_a_loss(pf, price, gain, indexation):
    """1,010 indexed is 1,050.40. Sold for 1,020: no gain, and no loss either,
    so only 10 of the 40.40 is used. Sold for 900: a loss of 110, from the cost
    base as it was, and none of it."""
    _cpi(pf)
    beta = fac.make_instrument(pf, "BETAX", asset_class="share")
    fac.add_trade(pf, beta, "2027-08-02", "buy", 100, "10.00", brokerage="10")
    fac.add_trade(pf, beta, "2028-09-01", "sell", 100, price, brokerage="10")

    [use] = _sale(pf, beta).parcels

    assert (use.gain, use.indexation) == (gain, indexation)


@freeze_time("2029-01-15")
def test_the_years_losses_reduce_the_deferred_gains_first(pf, alpha):
    """s 102-5(1) step 1: a 100 loss comes off the 490 deferred gain, not the
    430 indexed one, so 390 is halved: 195 + 430 = 625, where taking it off
    the indexed gain would have given 575."""
    fac.add_trade(pf, alpha, "2028-08-01", "sell", 100, "20.00", brokerage="10")
    beta = fac.make_instrument(pf, "BETAX", asset_class="share")
    fac.add_trade(pf, beta, "2028-07-03", "buy", 10, "20.00")
    fac.add_trade(pf, beta, "2028-09-01", "sell", 10, "10.00")

    cgt = fyreport.fy_cgt(pf, 2029)

    assert (cgt.losses, cgt.losses_applied_discountable, cgt.losses_applied_from_july_2027) == (
        Decimal("100.00"), Decimal("100.00"), 0)
    assert cgt.net_capital_gain == Decimal("625.00")


@freeze_time("2029-01-15")
def test_a_loss_carried_in_follows_the_same_order(pf, alpha):
    """A 100 loss in FY2028 carries to FY2029 and comes off the deferred gain."""
    gamma = fac.make_instrument(pf, "GAMMA", asset_class="share")
    fac.add_trade(pf, gamma, "2027-08-02", "buy", 10, "20.00")
    fac.add_trade(pf, gamma, "2027-09-01", "sell", 10, "10.00")
    fac.add_trade(pf, alpha, "2028-08-01", "sell", 100, "20.00", brokerage="10")

    earlier, cgt = fyreport.fy_cgt(pf, 2028), fyreport.fy_cgt(pf, 2029)

    assert (earlier.net_capital_loss, earlier.losses_carried_out) == (
        Decimal("100.00"), Decimal("100.00"))
    assert (cgt.losses_carried_applied, cgt.net_capital_gain) == (
        Decimal("100.00"), Decimal("625.00"))


@freeze_time("2029-01-15")
def test_a_fall_to_30_june_2027_is_a_loss_in_the_year_of_the_sale(pf, alpha):
    """Worth 800 at the close of 30 June 2027 against 1,010: a deferred loss of
    210. After: 800 indexed is 832, so 358, less that loss: 148."""
    fac.add_prices(pf, alpha, [("2027-06-29", "8.00")])
    pf.query(fac.Price).filter_by(instrument_id=alpha.id, date=D(2027, 6, 30)).delete()
    fac.add_trade(pf, alpha, "2028-08-01", "sell", 100, "12.00", brokerage="10")

    [use] = _sale(pf, alpha).parcels
    cgt = fyreport.fy_cgt(pf, 2029)

    assert (use.deferred, use.indexed_gain) == (Decimal("-210.00"), Decimal("358.00"))
    assert (cgt.losses, cgt.losses_applied_from_july_2027, cgt.net_capital_gain) == (
        Decimal("210.00"), Decimal("210.00"), Decimal("148.00"))


@freeze_time("2029-01-15")
def test_a_foreign_parcel_is_valued_at_the_rate_of_30_june_2027(pf):
    """Cost 1,005 × 1.60 = 1,608; worth 120 × 10 × 1.50 = 1,800 then; sold for
    (1,300 − 5) × 1.40 = 1,813. Deferred 192; 1,800 indexed is 1,872, more
    than 1,813 and less than 1,800 is not, so nothing after."""
    _cpi(pf)
    nova = fac.make_instrument(pf, "NOVA", exchange="NASDAQ", currency="USD")
    fac.add_trade(pf, nova, "2025-01-06", "buy", 10, "100.00", brokerage="5", fx_rate="1.60")
    fac.add_prices(pf, nova, [("2027-06-30", "120.00")])
    # 30 June's rate, not the day before's or the day after's.
    for day, rate in (("2027-06-29", "1.55"), ("2027-06-30", "1.50"), ("2027-07-01", "1.45")):
        fac.add_fx(pf, "USDAUD", day, rate)
    fac.add_trade(pf, nova, "2028-08-01", "sell", 10, "130.00", brokerage="5", fx_rate="1.40")

    [use] = _sale(pf, nova).parcels

    assert (use.deferred_proceeds, use.deferred, use.indexed_gain, use.gain) == (
        Decimal("1800.00"), Decimal("192.00"), 0, Decimal("192.00"))


@freeze_time("2029-01-15")
def test_a_quarter_not_yet_published_leaves_the_sale_out_and_says_why(pf, alpha):
    pf.query(CpiQuarter).filter_by(quarter=D(2028, 7, 1)).delete()
    fac.add_trade(pf, alpha, "2028-08-01", "sell", 100, "20.00", brokerage="10")

    sale = _sale(pf, alpha)
    cgt = fyreport.fy_cgt(pf, 2029)

    assert (sale.known, sale.parcels[0].awaiting_cpi) == (False, D(2028, 7, 1))
    assert (cgt.awaiting_cpi, cgt.net_capital_gain) == (["ALPHA"], 0)


@freeze_time("2029-01-15")
def test_without_a_close_in_the_last_week_of_june_2027_the_value_is_unknown(pf, alpha):
    """The market value just before 1 July 2027 is not a close from months
    earlier: a suspended or unpriced holding is named instead."""
    pf.query(fac.Price).filter_by(instrument_id=alpha.id).delete()
    fac.add_prices(pf, alpha, [("2027-03-31", "14.00")])
    fac.add_trade(pf, alpha, "2028-08-01", "sell", 100, "20.00", brokerage="10")

    cgt = fyreport.fy_cgt(pf, 2029)

    assert (cgt.unvalued, cgt.net_capital_gain) == (["ALPHA"], 0)


@freeze_time("2029-01-15")
def test_a_return_of_capital_after_1_july_2027_comes_off_the_value_unindexed(pf, alpha):
    """100 back in December 2027: the tax cost base from 1 July 2027 is 1,400,
    and only the 1,500 is indexed: 1,560 − 100 = 1,460, so 530 after. What the
    holding cost, on the dashboard, is 910."""
    fac.add_action(pf, alpha, "2027-12-01", "capital_return", amount="100.00")
    with freeze_time("2028-03-01"):
        [held] = queries.all_holdings(pf)
    fac.add_trade(pf, alpha, "2028-08-01", "sell", 100, "20.00", brokerage="10")
    pf.expire_all()

    [use] = _sale(pf, alpha).parcels

    assert held.cost_aud == Decimal("910")
    assert (use.cost_base, use.indexed_cost, use.indexed_gain, use.deferred) == (
        Decimal("1400.00"), Decimal("1460.00"), Decimal("530.00"), Decimal("490.00"))
    # What it cost is the holding's 910: 1,990 − 910 − 60 is the 1,020.
    assert (use.cost, use.indexation, use.gain) == (
        Decimal("910.00"), Decimal("60.00"), Decimal("1020.00"))


@freeze_time("2028-01-15")
def test_the_dashboard_still_shows_what_was_paid(pf, alpha):
    """The value on 30 June 2027 is a tax figure, not a cost."""
    [holding] = queries.all_holdings(pf)

    assert holding.cost_aud == Decimal("1010")


@freeze_time("2029-01-15")
def test_the_realised_gains_export_shows_the_working(pf, alpha):
    fac.add_trade(pf, alpha, "2028-08-01", "sell", 100, "20.00", brokerage="10")

    headers, rows = exports.realised_cgt(pf, fy=2029)
    row = dict(zip(headers, rows[0]))

    assert (row["Value at 30 Jun 2027 (AUD)"], row["Gain to 30 Jun 2027 (AUD)"],
            row["Indexation factor"], row["Indexed cost base (AUD)"]) == (
        Decimal("1500.00"), Decimal("490.00"), Decimal("1.040"), Decimal("1560.00"))
    assert (row["Cost base (AUD)"], row["Indexation (AUD)"], row["Gross gain (AUD)"],
            row["CGT discount (AUD)"], row["Gain after discount (AUD)"]) == (
        Decimal("1010.00"), Decimal("60.00"), Decimal("920.00"), Decimal("245.00"),
        Decimal("675.00"))


@freeze_time("2029-01-15")
def test_the_unrealised_gains_export_works_a_sale_today_as_the_simulator_does(pf, alpha):
    """Worth 2,000 now, against 1,010 paid: 990. As a sale today: 490 to
    30 June 2027, halved, and 2,000 − 1,560 = 440 after it, the CPI of the
    September 2028 quarter standing in for this one's."""
    fac.add_prices(pf, alpha, [("2029-01-14", "20.00")])

    headers, [row] = exports.unrealised_cgt(pf)
    row = dict(zip(headers, row))

    assert (row["Cost base (AUD)"], row["Market value (AUD)"], row["Unrealised gain (AUD)"]) == (
        Decimal("1010.00"), Decimal("2000.00"), Decimal("990.00"))
    assert (row["Indexation (AUD)"], row["Gain after discount (AUD)"]) == (
        Decimal("60.00"), Decimal("685.00"))


@freeze_time("2029-01-15")
def test_a_closed_position_is_discounted_only_to_30_june_2027(pf, alpha):
    """Its return is still what came back over what was paid: 1,990 − 1,010."""
    fac.add_trade(pf, alpha, "2028-08-01", "sell", 100, "20.00", brokerage="10")

    headers, [row] = exports.closed_positions(pf)
    row = dict(zip(headers, row))

    assert (row["Outlay incl. brokerage (AUD)"], row["Gross capital gain (AUD)"],
            row["CGT discount (AUD)"], row["Total return incl. dividends (AUD)"]) == (
        Decimal("1010.00"), Decimal("920.00"), Decimal("245.00"), Decimal("980.00"))


@freeze_time("2028-08-15")
def test_a_simulated_sale_estimates_with_the_latest_cpi_and_says_so(pf, alpha):
    """The September 2028 quarter is not out by August: the June quarter's
    103 stands in, so 1,500 × 1.030 = 1,545 and 1,990 − 1,545 = 445."""
    fac.add_prices(pf, alpha, [("2028-08-14", "20.00")])
    pf.query(CpiQuarter).filter_by(quarter=D(2028, 7, 1)).delete()

    sale = simulate.sale(pf, {alpha.id: Decimal(100)}, income_tax_rate=Decimal(30))
    [line] = sale.selling

    assert sale.cpi_estimate == D(2028, 4, 1)
    assert (line.gain, sale.discount, sale.taxable) == (
        Decimal("935.00"), Decimal("245.00"), Decimal("690.00"))
    # Its table adds up: 2,000 − 10 − 1,010 − 45 is the 935.
    assert (sale.value, sale.brokerage, sale.cost_base, sale.indexation) == (
        Decimal("2000.00"), Decimal("10.00"), Decimal("1010.00"), Decimal("45.00"))


@freeze_time("2028-12-15")
def test_a_simulated_sale_counts_this_years_gains_from_1_july_2027(pf, alpha):
    """ALPHA's sale in August made 490 to 30 June 2027 and 430 after it."""
    fac.add_trade(pf, alpha, "2028-08-01", "sell", 100, "20.00", brokerage="10")
    beta = fac.make_instrument(pf, "BETAX", asset_class="share")
    fac.add_trade(pf, beta, "2028-09-01", "buy", 10, "10.00")
    fac.add_prices(pf, beta, [("2028-12-14", "11.00")])

    sale = simulate.sale(pf, {beta.id: Decimal(10)}, income_tax_rate=Decimal(30))

    assert sale.year_so_far == Decimal("920.00")


def _signed_in(client, session_factory, *, cpi=True, close="15.00", sold=True):
    """Signed in, with the `alpha` fixture's ALPHA, sold in August 2028 unless
    not `sold`; returns the user's and the portfolio's ids."""
    from test_routes import bind_to_only_portfolio, make_login

    make_login(client, session_factory)
    with session_factory() as s:
        portfolio = bind_to_only_portfolio(s)
        if cpi:
            _cpi(s)
        alpha = fac.make_instrument(s, "ALPHA", asset_class="share")
        fac.add_trade(s, alpha, "2025-01-06", "buy", 100, "10.00", brokerage="10")
        fac.add_prices(s, alpha, [("2027-06-30", close)] if close else [])
        if sold:
            fac.add_trade(s, alpha, "2028-08-01", "sell", 100, "20.00", brokerage="10")
        portfolio.income_tax_rate = Decimal(30)
        s.commit()
        return portfolio.members[0].user_id, portfolio.id, alpha.id


def _fy_page(client, session_factory, *, cpi=True, fy=2029) -> str:
    _signed_in(client, session_factory, cpi=cpi)
    return client.get("/", params={"fy": fy}, headers={"accept": "text/html"}).text


@freeze_time("2029-01-15")
def test_the_fy_page_shows_the_2027_working(client, session_factory):
    """The value on 30 June 2027, the indexed cost base and the factor, and
    the gains to 30 June 2027 apart from the indexed ones."""
    page = _fy_page(client, session_factory)
    sales = page.split("<h2>Positions closed this year", 1)[1]
    row = re.search(r"<tr>\s*<td>2028-08-01</td>.*?</tr>", sales, re.S).group(0)

    # Units, cost, value then, indexation, proceeds, gain: 1,990 − 1,010 − 60.
    assert re.findall(r">([\d,]+(?:\.\d\d)?)<", row) == [
        "100", "1,010.00", "1,500.00", "60.00", "1,990.00", "920.00"]
    for shown in ("Value 30 Jun 2027", "Indexation (", "2025-01-06 ✓ ×1.040",
                  "Gains to 30 June 2027 on parcels held", "Gains from 1 July 2027", "675.00"):
        assert shown in sales, shown


@freeze_time("2029-01-15")
def test_the_fy_page_names_a_sale_waiting_on_the_cpi(client, session_factory):
    page = _fy_page(client, session_factory, cpi=False)

    assert "Totals exclude ALPHA — no CPI yet for the quarter of the sale." in page


@freeze_time("2027-06-15")
def test_a_year_before_1_july_2027_shows_none_of_it(client, session_factory):
    from test_routes import bind_to_only_portfolio, make_login

    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        alpha = fac.make_instrument(s, "ALPHA", asset_class="share")
        fac.add_trade(s, alpha, "2025-01-06", "buy", 100, "10.00")
        fac.add_trade(s, alpha, "2026-09-01", "sell", 100, "20.00")
        s.commit()
    page = client.get("/", params={"fy": 2027}, headers={"accept": "text/html"}).text

    assert "Value 30 Jun 2027" not in page and "Gains from 1 July 2027" not in page


def _simulator(client, session_factory, **kwargs) -> str:
    """The simulator, selling all of an unsold ALPHA, at 20.00 yesterday."""
    _user, _portfolio, alpha_id = _signed_in(client, session_factory, sold=False, **kwargs)
    yesterday = dt.date.today() - dt.timedelta(days=1)
    with session_factory() as s:
        fac.add_prices(s, s.get(fac.Instrument, alpha_id), [(yesterday, "20.00")])
        s.commit()
    return client.get("/simulate", params={f"units_{alpha_id}": "100"},
                      headers={"accept": "text/html"}).text


@freeze_time("2028-11-15")
def test_the_simulator_names_the_quarter_whose_cpi_stood_in(client, session_factory):
    page = _simulator(client, session_factory)

    assert "Indexation uses the CPI for the September 2028 quarter: this quarter" in page
    # 2,000 less 10 brokerage, 1,010 paid and 60 of indexation: 920.
    assert re.search(r'<td>Indexation</td>\s*<td class="num">−<span class="m">60\.00</span>',
                     page)


@freeze_time("2028-08-15")
def test_the_simulator_names_a_holding_it_cannot_value_at_30_june_2027(client,
                                                                      session_factory):
    page = _simulator(client, session_factory, close=None)

    assert "Totals exclude ALPHA — no close in the week before 1 July 2027" in page


@freeze_time("2028-08-15")
def test_the_simulator_names_a_holding_with_no_cpi_to_index_it_by(client, session_factory):
    """No CPI at all, with the feed blocked: there is nothing to stand in."""
    page = _simulator(client, session_factory, cpi=False)

    assert "Totals exclude ALPHA — no CPI yet to index it by." in page
    assert "Indexation uses" not in page


def _fy_api(client, session_factory, **kwargs) -> dict:
    from test_api import bearer, issue_key

    user_id, portfolio_id, _alpha = _signed_in(client, session_factory, **kwargs)
    key = issue_key(session_factory, portfolio_id, created_by=user_id)
    return client.get("/api/v1/fy/2029", headers=bearer(key)).json()["cgt"]


@freeze_time("2029-01-15")
def test_the_api_gives_the_gains_from_1_july_2027_and_the_minimum_tax_gain(client,
                                                                           session_factory):
    cgt = _fy_api(client, session_factory)

    assert (cgt["gains_discountable"], cgt["gains_from_july_2027"], cgt["minimum_tax_gain"],
            cgt["net_capital_gain"]) == (490, 430, 430, 675)
    assert (cgt["awaiting_cpi"], cgt["unvalued"]) == ([], [])


@freeze_time("2029-01-15")
@pytest.mark.parametrize("missing, named", [({"cpi": False}, "awaiting_cpi"),
                                            ({"close": None}, "unvalued")])
def test_the_api_names_a_sale_its_totals_leave_out(client, session_factory, missing, named):
    cgt = _fy_api(client, session_factory, **missing)

    assert (cgt[named], cgt["net_capital_gain"]) == (["ALPHA"], 0)
