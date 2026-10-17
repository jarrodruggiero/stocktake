"""The Simulate sale page: reached from the dashboard, read only, and holding
what was typed when it refuses something (decisions.md #148).

The figures are worked out in test_sale_simulator.py; these hold the page to
them, and to the controls that fill the form.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from freezegun import freeze_time
from sqlalchemy import select

import factories as fac
from app.models import Portfolio, PortfolioMember, User
from test_routes import bind_to_only_portfolio, make_login

HTML = {"accept": "text/html"}
TODAY = "2026-10-17"
PAGE = "/simulate"


@pytest.fixture
def alpha(client, session_factory) -> int:
    """Signed in, with 200 ALPHA at $15 and a 32% marginal rate saved."""
    make_login(client, session_factory)
    with session_factory() as s:
        portfolio = bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "ALPHA", asset_class="share")
        fac.add_trade(s, inst, "2025-01-06", "buy", 100, "10.00", brokerage="10")
        fac.add_trade(s, inst, "2026-03-02", "buy", 100, "12.00", brokerage="10")
        fac.add_prices(s, inst, [("2026-10-16", "15.00")])
        portfolio.marginal_tax_rate = Decimal(32)
        s.commit()
        return inst.id


def _results(page: str) -> str:
    """The results section alone: the table above it holds figures too."""
    found = re.search(r'<section class="saleresult".*?</section>', page, re.S)
    return found.group(0) if found else ""


def _input(page: str, name: str) -> str:
    found = re.search(rf'<input[^>]*name="{name}"[^>]*>', page)
    return found.group(0) if found else ""


@freeze_time(TODAY)
def test_the_dashboard_offers_it_between_refresh_and_charts(client, alpha):
    page = client.get("/", headers=HTML).text
    actions = page.split('<div class="headactions">', 1)[1].split("</div>", 1)[0]

    assert 'href="/simulate">Simulate sale</a>' in actions
    assert (actions.index("Refresh prices") < actions.index("Simulate sale")
            < actions.index("View charts"))


@freeze_time(TODAY)
def test_a_viewer_can_simulate_a_sale_too(client, session_factory, alpha):
    """It changes nothing, so it is anyone's who can see the portfolio."""
    with session_factory() as s:
        s.scalar(select(PortfolioMember)).role = "viewer"
        s.scalar(select(User)).is_admin = False
        s.commit()

    dashboard = client.get("/", headers=HTML).text
    page = client.get(f"{PAGE}?units_{alpha}=150", headers=HTML)

    assert 'href="/simulate">Simulate sale</a>' in dashboard
    assert page.status_code == 200 and "2,117.33" in _results(page.text)


@freeze_time(TODAY)
def test_the_page_says_it_is_a_calculator_before_anything_else(client, alpha):
    page = client.get(PAGE, headers=HTML).text
    content = page.split("<h1>", 1)[1]

    assert content.index("A calculator only, not financial or tax advice.") < content.index(
        "<form")


@freeze_time(TODAY)
def test_a_sale_is_worked_out_from_the_form(client, alpha):
    """Value 2,250, brokerage 10, cost base 1,615: a 625 gain, 383.34 of it
    taxable after the discount, 122.67 tax at the saved 32%."""
    page = client.get(f"{PAGE}?units_{alpha}=150", headers=HTML)
    results = _results(page.text)

    for figure in ("2,250.00", "10.00", "1,615.00", "625.00", "241.66", "383.34", "122.67",
                   "2,117.33"):
        assert figure in results, figure
    assert "Tax at 32%" in results


@freeze_time(TODAY)
def test_nothing_sold_shows_no_results(client, alpha):
    assert _results(client.get(PAGE, headers=HTML).text) == ""


@freeze_time(TODAY)
@pytest.mark.parametrize("query, units", [("fill=all", "200"),
                                          ("fill=percent&percent=50", "100")])
def test_sell_all_and_a_percentage_fill_the_units(client, alpha, query, units):
    page = client.get(f"{PAGE}?{query}", headers=HTML).text

    assert f'value="{units}"' in _input(page, f"units_{alpha}")
    assert _results(page)


@freeze_time(TODAY)
@pytest.mark.parametrize("query, message", [
    ("units_{id}=500", "ALPHA: 500 is more than the 200 units held."),
    ("units_{id}=-1", "ALPHA: units can&#39;t be negative."),
    ("units_{id}=abc", "ALPHA: &#39;abc&#39; is not a number."),
    ("units_{id}=10&brokerage_{id}=-1", "ALPHA brokerage can&#39;t be negative."),
    ("units_{id}=10&rate=150", "Marginal tax rate can&#39;t be more than 100."),
    ("fill=percent&percent=0", "Sell a percentage above 0 and up to 100."),
])
def test_what_cannot_be_worked_out_is_refused_and_comes_back(client, alpha, query, message):
    resp = client.get(f"{PAGE}?{query.format(id=alpha)}", headers=HTML)

    assert resp.status_code == 400
    assert message in resp.text
    assert _results(resp.text) == ""
    typed = re.search(rf"units_{alpha}=([^&]*)", query.format(id=alpha))
    if typed:
        assert f'value="{typed.group(1)}"' in _input(resp.text, f"units_{alpha}")


@freeze_time(TODAY)
def test_a_typed_rate_is_used_and_not_saved(client, session_factory, alpha):
    start = client.get(PAGE, headers=HTML).text
    page = client.get(f"{PAGE}?rate=40&units_{alpha}=150", headers=HTML).text

    assert 'value="32"' in _input(start, "rate")
    assert "Tax at 40%" in _results(page) and "153.33" in _results(page)
    with session_factory() as s:
        assert s.scalar(select(Portfolio.marginal_tax_rate)) == Decimal("32.00")


@freeze_time(TODAY)
def test_with_no_rate_the_tax_is_left_blank(client, alpha):
    page = client.get(f"{PAGE}?rate=&units_{alpha}=150", headers=HTML).text
    results = _results(page)

    assert "383.34" in results
    assert "Set a marginal tax rate to see the tax." in results
    assert "2,117.33" not in results


@freeze_time(TODAY)
def test_a_holding_that_cannot_be_valued_has_nothing_to_type(client, session_factory, alpha):
    with session_factory() as s:
        bind_to_only_portfolio(s)
        omega = fac.make_instrument(s, "OMEGA", asset_class="share")
        fac.add_trade(s, omega, "2025-01-06", "buy", 10, "5.00")
        s.commit()
        omega_id = omega.id

    page = client.get(PAGE, headers=HTML).text

    assert _input(page, f"units_{omega_id}") == ""
    row = page.split(">OMEGA<", 1)[1].split("</tr>", 1)[0]
    assert "no price" in row
