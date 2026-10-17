"""The pages the 2027 rules add to, in a browser at a desk and a phone width
(decisions.md #150).

The visual suite renders its pages at today's date, before 1 July 2027, so none
of them has the FY page's 2027 columns or the simulator's indexation. These are
rendered after it, and measured with the same probe.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
from freezegun import freeze_time
from test_cgt_2027 import _cpi
from test_visual import WIDTHS, faults, probe

import factories as fac
from test_routes import bind_to_only_portfolio, make_login

pytestmark = pytest.mark.visual

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
HTML = {"accept": "text/html"}


def _standalone(html: str) -> str:
    """The page with its stylesheet and scripts inlined, as screenshot.py
    writes one, so a file:// page renders as the app's does."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    html = re.sub(r'<link rel="stylesheet" href="/static/style.css[^"]*">',
                  lambda _m: f"<style>{css}</style>", html)

    def inline(match: re.Match) -> str:
        path = STATIC / match.group(1)
        return f"<script>{path.read_text(encoding='utf-8')}</script>" if path.is_file() else ""

    return re.sub(r'<script src="/static/([^"?]+)[^"]*"[^>]*></script>', inline, html)


@pytest.fixture
def pages(client, session_factory, tmp_path) -> dict[str, Path]:
    """In January 2029: FY2029's two sales of parcels held through 30 June
    2027, and the simulator selling what is left, with a parcel bought after."""
    with freeze_time("2029-01-15"):
        make_login(client, session_factory)
        with session_factory() as s:
            portfolio = bind_to_only_portfolio(s)
            _cpi(s)
            made = {}
            for ticker, bought in (("ALPHA", "2025-01-06"), ("GAMMA", "2027-05-03"),
                                   ("DELTA", "2027-08-02")):
                made[ticker] = inst = fac.make_instrument(s, ticker, asset_class="share")
                fac.add_trade(s, inst, bought, "buy", 100, "10.00", brokerage="10")
                fac.add_prices(s, inst, [("2027-06-30", "15.00"), ("2029-01-14", "20.00")])
            fac.add_trade(s, made["ALPHA"], "2028-08-01", "sell", 60, "20.00", brokerage="10")
            fac.add_trade(s, made["GAMMA"], "2028-09-01", "sell", 100, "16.00", brokerage="10")
            # Under 30%, so the minimum tax's row is drawn too.
            portfolio.income_tax_rate, portfolio.medicare_levy = Decimal(15), Decimal(2)
            s.commit()
            selling = {f"units_{made[t].id}": "40" for t in ("ALPHA", "DELTA")}
        rendered = {"fy": client.get("/", params={"fy": 2029}, headers=HTML).text,
                    "simulate": client.get("/simulate", params=selling, headers=HTML).text}
    assert "Indexation (AUD)" in rendered["fy"]
    assert "<td>Indexation</td>" in rendered["simulate"]
    assert "Minimum tax top-up" in rendered["simulate"]
    out = {}
    for name, page in rendered.items():
        out[name] = tmp_path / f"{name}.html"
        out[name].write_text(_standalone(page), encoding="utf-8")
    return out


@pytest.mark.parametrize("width_name", sorted(WIDTHS))
@pytest.mark.parametrize("page", ["fy", "simulate"])
def test_the_2027_working_lays_out_at_both_widths(pages, page, width_name):
    report = probe(pages[page], WIDTHS[width_name])
    assert not report.get("error"), report["error"]

    assert faults(report) == []
