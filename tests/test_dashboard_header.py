"""The Portfolio page's header, and the holdings table's column chooser.

From test users: Refresh prices vanished while prices refreshed; the header
read View charts, Refresh prices, Record trade; and a portfolio held only in
AUD was offered Avg price (AUD), Cost (AUD), Price (AUD) and Dividends (AUD)
twice — a native column and a reporting one, showing the same numbers.
"""

from __future__ import annotations

import re

import pytest

import factories as fac
from test_routes import bind_to_only_portfolio, make_login, session_csrf

HTML = {"accept": "text/html"}


@pytest.fixture
def holding(client, session_factory):
    """A portfolio holding one AUD instrument."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME")
        fac.add_trade(s, acme, "2026-01-05", "buy", 10, "5.00")
        s.commit()
    return client


def _refresh_button(page: str) -> str:
    found = re.search(r"<button[^>]*>\s*Refresh prices\s*</button>", page)
    assert found, "no Refresh prices button"
    return found.group(0)


def test_refresh_prices_stays_while_prices_refresh(holding, app_module, monkeypatch):
    monkeypatch.setitem(app_module.feed_status, "running", True)
    button = _refresh_button(holding.get("/", headers=HTML).text)
    assert "disabled" in button and 'aria-busy="true"' in button


def test_refresh_prices_is_ready_when_nothing_runs(holding, app_module, monkeypatch):
    monkeypatch.setitem(app_module.feed_status, "running", False)
    button = _refresh_button(holding.get("/", headers=HTML).text)
    assert "disabled" not in button and "aria-busy" not in button


def test_the_header_reads_quietest_to_loudest(holding):
    """Refresh prices, View charts, then the one primary action, last."""
    page = holding.get("/", headers=HTML).text
    head = re.search(r'<div class="headactions">.*?</div>', page, re.S).group(0)
    labels = re.findall(r"<button[^>]*>\s*([^<]+?)\s*</button>", head)
    assert labels == ["Refresh prices", "View charts", "Record trade"], labels


def _chooser(page: str) -> list[tuple[str, bool, str]]:
    """(column key, ticked, label) for each box in the column chooser."""
    panel = re.search(r'<details class="colchooser.*?</details>', page, re.S).group(0)
    return [(key, "checked" in attrs, " ".join(label.split()))
            for key, attrs, label in re.findall(
                r'<input type="checkbox" name="column" value="([^"]+)"([^>]*)>\s*([^<]+)',
                panel)]


def test_one_currency_offers_each_column_once(holding):
    labels = [label for _key, _ticked, label in _chooser(holding.get("/", headers=HTML).text)]
    assert len(labels) == len(set(labels)), labels


def test_two_currencies_offer_both_kinds(holding, session_factory):
    with session_factory() as s:
        bind_to_only_portfolio(s)
        zulu = fac.make_instrument(s, "ZULU", exchange="NASDAQ", currency="USD")
        fac.add_trade(s, zulu, "2026-01-05", "buy", 10, "4.00", fx_rate="1.5")
        s.commit()
    keys = {key for key, _ticked, _label in _chooser(holding.get("/", headers=HTML).text)}
    assert {"cost", "cost_reporting", "dividends", "dividends_reporting"} <= keys


def test_a_layout_naming_the_reporting_column_keeps_it(holding, session_factory):
    """One of each pair is offered — the one the layout already names, so
    saving the chooser does not swap it for its twin."""
    holding.post("/profile/columns", data={
        "_csrf": session_csrf(session_factory),
        "column": ["ticker", "cost_reporting"]}, headers=HTML)
    boxes = {key: ticked for key, ticked, _label in _chooser(
        holding.get("/", headers=HTML).text)}
    assert boxes.get("cost_reporting") is True
    assert "cost" not in boxes
