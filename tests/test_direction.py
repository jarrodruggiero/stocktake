"""Up, down, or no change, said the same way everywhere: a colour and an arrow.

The ledger and the disposals coloured a gain but gave it no ▲ or ▼, so to
somebody who cannot tell the red from the green a loss read as a gain with a
minus sign easily missed. And zero was a green ▲: no change, shown as a gain.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from freezegun import freeze_time

import factories as fac
import fixture_portfolio as ref
from test_routes import bind_to_only_portfolio, make_login

HTML = {"accept": "text/html"}
UP, DOWN = '<span class="dir" aria-hidden="true">▲</span>', '<span class="dir" aria-hidden="true">▼</span>'


@pytest.mark.parametrize("value, trend, arrow", [
    (Decimal("0.01"), "up", UP), (Decimal("-0.01"), "down", DOWN),
    (Decimal("0"), "", ""), (None, "", ""),
])
def test_a_figure_is_up_down_or_neither(app_module, value, trend, arrow):
    filters = app_module.templates.env.filters

    assert filters["trend"](value) == trend
    assert str(filters["arrow"](value)) == arrow


@freeze_time(ref.TODAY)
@pytest.mark.parametrize("page", ["/holding/GAMMA", f"/?fy={ref.FY2024}", "/"])
def test_every_coloured_figure_carries_its_arrow(client, session_factory, page):
    """The holding's ledger and the year's disposals had the colour alone."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)

    html = client.get(page, headers=HTML).text
    coloured = re.findall(r'<td class="num (?:up|down)">(.*?)</td>', html, re.S)

    assert coloured, "no gains on the page to check"
    assert [c for c in coloured if UP not in c and DOWN not in c] == []


@freeze_time("2026-03-04")
def test_no_change_is_neither_a_gain_nor_a_loss(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        flat = fac.make_instrument(s, "FLAT")
        fac.add_trade(s, flat, "2026-03-02", "buy", 100, "5.00")
        fac.add_prices(s, flat, [("2026-03-02", "5.00"), ("2026-03-03", "5.00")])
        s.commit()

    page = client.get("/", headers=HTML).text
    gain_tile = re.search(r'<div class="tile[^"]*">\s*<span class="label">Unrealised gain</span>.*?</div>',
                          page, re.S).group(0)

    assert not re.search(r'class="tile (?:up|down)"', gain_tile), gain_tile
    assert UP not in gain_tile and DOWN not in gain_tile
    assert "+0.00%" not in gain_tile and ">0.00%<" in gain_tile
