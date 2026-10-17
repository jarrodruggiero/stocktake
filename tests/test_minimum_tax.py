"""The 30% minimum tax on capital gains from 1 July 2027 (decisions.md #151).

ITAA 1997 Div 119, as inserted by Act No. 49 of 2026: an individual pays extra
tax of 30% of the year's minimum tax capital gain, its gains from 1 July 2027
left after losses, less the basic income tax that gain adds, rounded down to a
whole dollar, when that is more than nil (s 119-10). Basic income tax leaves out
the Medicare levy. Nobody who gets an income support payment in the year pays
it (s 119-15). With one income tax rate (#147), the extra is (30% − the rate)
of the gain, worked out for the year, so a sale adds the difference it makes.

Every figure below is worked out by hand.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import pytest
from freezegun import freeze_time
from test_api import bearer, issue_key
from test_cgt_2027 import _cpi, _fy_page

import factories as fac
from app import fyreport, simulate
from app.models import CpiQuarter, Portfolio
from test_routes import bind_to_only_portfolio, make_login, session_csrf

D = dt.date
HTML = {"accept": "text/html"}
RATES = "/api/v1/portfolio/tax-rates"


@pytest.fixture
def alpha(pf):
    """100 bought in 2025 for 1,010, worth 1,500 at the close of 30 June 2027,
    and 2,000 now, in August 2028, before the September quarter's CPI is out.
    Sold whole: 490 to 30 June 2027, discounted, and 1,990 − 1,500 × 1.030 =
    445 after it, which is the minimum tax capital gain. (As test_cgt_2027's
    alpha, copied rather than imported: testing.md.)"""
    _cpi(pf)
    pf.query(CpiQuarter).filter_by(quarter=D(2028, 7, 1)).delete()
    inst = fac.make_instrument(pf, "ALPHA", asset_class="share")
    fac.add_trade(pf, inst, "2025-01-06", "buy", 100, "10.00", brokerage="10")
    fac.add_prices(pf, inst, [("2027-06-30", "15.00"), ("2028-08-14", "20.00")])
    return inst


def _sell(pf, inst, **kwargs):
    return simulate.sale(pf, {inst.id: Decimal(100)}, **kwargs)


@freeze_time("2028-08-15")
def test_below_30_percent_the_gains_from_1_july_2027_are_topped_up(pf, alpha):
    """At 15%: income tax on the 690 taxable is 103.50 and the levy 13.80; the
    445 is topped up by 15% of it, 66.75, rounded down to 66."""
    sale = _sell(pf, alpha, income_tax_rate=Decimal(15), medicare_levy=Decimal(2))

    assert (sale.taxable, sale.income_tax, sale.levy, sale.minimum_tax) == (
        Decimal("690.00"), Decimal("103.50"), Decimal("13.80"), Decimal("66"))
    assert sale.tax == Decimal("183.30")
    assert sale.take_home == Decimal("1806.70")    # 2,000 − 10 brokerage − 183.30


@freeze_time("2028-08-15")
def test_with_no_income_tax_at_all_it_is_the_whole_30_percent(pf, alpha):
    """Under the tax-free threshold: 30% of 445 is 133.50, so 133."""
    sale = _sell(pf, alpha, income_tax_rate=Decimal(0))

    assert (sale.income_tax, sale.minimum_tax, sale.tax) == (0, Decimal("133"), Decimal("133"))


@freeze_time("2028-08-15")
@pytest.mark.parametrize("rate", [Decimal(30), Decimal(45)])
def test_at_30_percent_or_more_there_is_nothing_to_top_up(pf, alpha, rate):
    assert _sell(pf, alpha, income_tax_rate=rate).minimum_tax == 0


@freeze_time("2028-08-15")
def test_it_is_the_years_figure_that_is_rounded_down(pf, alpha):
    """BETAX made 22.20 in July, held under a year so not indexed: the year's
    top-up was 15% of it, 3.33, so 3. With the sale it is 15% of 467.20, 70.08,
    so 70: the sale adds 67, where its own 66.75 rounded down would be 66."""
    beta = fac.make_instrument(pf, "BETAX", asset_class="share")
    fac.add_trade(pf, beta, "2028-01-10", "buy", 10, "100.00", brokerage="0")
    fac.add_trade(pf, beta, "2028-07-20", "sell", 10, "102.22", brokerage="0")

    # BETAX's is the last brokerage now, so ALPHA's 10 is typed.
    sale = _sell(pf, alpha, income_tax_rate=Decimal(15), brokerage={alpha.id: Decimal(10)})

    assert sale.minimum_tax == Decimal("67")


@freeze_time("2028-08-15")
def test_nobody_on_income_support_pays_it(pf, alpha):
    pf.query(Portfolio).one().income_support = True

    sale = _sell(pf, alpha, income_tax_rate=Decimal(15))

    assert (sale.minimum_tax, sale.tax) == (0, Decimal("103.50"))


@freeze_time("2028-08-15")
def test_without_an_income_tax_rate_there_is_no_figure(pf, alpha):
    assert _sell(pf, alpha).minimum_tax is None


@freeze_time("2027-06-15")
def test_before_1_july_2027_there_is_none(pf):
    """A sale today is under the old rules: there are no gains from 1 July
    2027 for the tax to be worked on."""
    gamma = fac.make_instrument(pf, "GAMMA", asset_class="share")
    fac.add_trade(pf, gamma, "2025-01-06", "buy", 100, "10.00", brokerage="10")
    fac.add_prices(pf, gamma, [("2027-06-14", "20.00")])

    sale = simulate.sale(pf, {gamma.id: Decimal(100)}, income_tax_rate=Decimal(15))

    assert (sale.minimum_tax, fyreport.fy_cgt(pf, 2027).minimum_tax_gain) == (0, 0)


def _owner(client, session_factory, **saved) -> dict:
    make_login(client, session_factory)
    with session_factory() as s:
        portfolio = bind_to_only_portfolio(s)
        for column, value in saved.items():
            setattr(portfolio, column, value)
        s.commit()
        return {"portfolio": portfolio.id, "user": portfolio.members[0].user_id}


@freeze_time("2028-08-15")
def test_the_simulator_shows_the_top_up_with_the_other_tax(client, session_factory):
    _owner(client, session_factory, income_tax_rate=Decimal(15), medicare_levy=Decimal(2))
    with session_factory() as s:
        bind_to_only_portfolio(s)
        _cpi(s)
        s.query(CpiQuarter).filter_by(quarter=D(2028, 7, 1)).delete()
        inst = fac.make_instrument(s, "ALPHA", asset_class="share")
        fac.add_trade(s, inst, "2025-01-06", "buy", 100, "10.00", brokerage="10")
        fac.add_prices(s, inst, [("2027-06-30", "15.00"), ("2028-08-14", "20.00")])
        s.commit()
        alpha_id = inst.id

    page = client.get("/simulate", params={f"units_{alpha_id}": "100"}, headers=HTML).text

    row = re.search(r"<tr><td>Minimum tax top-up.*?</tr>", page, re.S)
    assert row and re.search(r'−<span class="m">66\.00</span>', row.group(0))
    assert "About the minimum tax" in row.group(0)
    assert "183.30" in page    # the Tax tile counts it


@freeze_time("2029-01-15")
def test_the_fy_page_gives_the_minimum_tax_capital_gain(client, session_factory):
    """test_cgt_2027's year: 430 from 1 July 2027 and no losses."""
    page = _fy_page(client, session_factory)

    assert re.search(r'<td>Minimum tax capital gain.*?</td><td class="num">'
                     r'<span class="m">430\.00</span>', page, re.S)


def test_the_owner_says_so_in_portfolio_settings(client, session_factory):
    _owner(client, session_factory)

    def save(**ticked):
        data = {"_csrf": session_csrf(session_factory), "name": "Main",
                "reporting_currency": "AUD", "jurisdiction": "AU", **ticked}
        return client.post("/portfolio/settings", data=data, headers=HTML,
                           follow_redirects=False)

    def stored():
        with session_factory() as s:
            return s.query(Portfolio).one().income_support

    assert save(income_support="1").status_code == 303
    box = re.search(r'<input[^>]*name="income_support"[^>]*>',
                    client.get("/members", headers=HTML).text).group(0)
    assert (stored(), "checked" in box) == (True, True)
    save()
    assert stored() is False


def test_the_api_reads_and_sets_it_beside_the_rates(client, session_factory):
    ids = _owner(client, session_factory, income_tax_rate=Decimal(15))
    key = bearer(issue_key(session_factory, ids["portfolio"], created_by=ids["user"],
                           scopes="read,write"))

    before = client.get("/api/v1/portfolio", headers=key).json()["portfolio"]
    resp = client.put(RATES, headers=key, json={"income_support": True})
    refused = client.put(RATES, headers=key, json={"income_support": None})

    assert before["income_support"] is False
    assert resp.json() == {"income_tax_rate": 15.0, "medicare_levy": None,
                           "income_support": True}
    assert refused.status_code == 422
