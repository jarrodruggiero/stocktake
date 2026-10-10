"""A portfolio can say what its broker charges.

Brokers charge a flat fee, a flat fee plus a percentage, a percentage, a
minimum or a percentage whichever is greater, or nothing. A portfolio can set
one fee for trades in its own currency and one for everything else; the trade
form fills the brokerage from it as units and price are typed. A kind left
blank falls back to the brokerage on the last trade of that kind.
"""

from __future__ import annotations

import json
import re
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

import factories as fac
from app.models import Instrument, Portfolio
from test_routes import bind_to_only_portfolio, make_login, session_csrf

HTML = {"accept": "text/html"}
APP_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def owner(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        s.commit()
    return client


def _settings(client, session_factory, **fees):
    data = {"_csrf": session_csrf(session_factory), "name": "Main",
            "reporting_currency": "AUD", "jurisdiction": "AU"}
    data.update(fees)
    return client.post("/portfolio/settings", data=data, headers=HTML,
                       follow_redirects=False)


def _portfolio(session_factory) -> Portfolio:
    with session_factory() as s:
        return s.query(Portfolio).one()


def test_a_portfolio_keeps_the_fees_it_is_given(owner, session_factory):
    resp = _settings(owner, session_factory, brokerage_flat="9.50",
                     foreign_brokerage_percent="0.11", foreign_brokerage_minimum="14.95")
    assert resp.status_code == 303, resp.text
    p = _portfolio(session_factory)
    assert (p.brokerage_flat, p.brokerage_percent, p.brokerage_minimum) == (
        Decimal("9.50"), None, None)
    assert (p.foreign_brokerage_flat, p.foreign_brokerage_percent,
            p.foreign_brokerage_minimum) == (None, Decimal("0.11"), Decimal("14.95"))


def test_free_is_not_the_same_as_unset(owner, session_factory):
    _settings(owner, session_factory, brokerage_flat="0")
    assert _portfolio(session_factory).brokerage_flat == Decimal("0")


@pytest.mark.parametrize("field, value", [
    ("brokerage_flat", "-1"), ("brokerage_percent", "abc"),
    ("brokerage_percent", "101"), ("foreign_brokerage_minimum", "1E+999999"),
])
def test_a_fee_that_is_not_one_is_refused(owner, session_factory, field, value):
    assert _settings(owner, session_factory, **{field: value}).status_code == 400
    p = _portfolio(session_factory)
    assert getattr(p, field) is None


def test_the_settings_show_what_is_set(owner, session_factory):
    _settings(owner, session_factory, brokerage_flat="9.50", foreign_brokerage_percent="0.11")
    page = owner.get("/members", headers=HTML).text
    assert re.search(r'name="brokerage_flat"[^>]*value="9.50"', page)
    assert re.search(r'name="foreign_brokerage_percent"[^>]*value="0.11"', page)
    assert re.search(r'name="brokerage_minimum"[^>]*value=""', page)


def _field(page: str) -> dict:
    tag = re.search(r'<input name="brokerage"[^>]*>', page).group(0)
    value = re.search(r'\bvalue="([^"]*)"', tag).group(1)
    fees = re.search(r"data-fees='([^']*)'", tag)
    return {"value": value, "fees": json.loads(fees.group(1)) if fees else None}


def _trade(session_factory, ticker, currency, brokerage):
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = s.query(Instrument).filter_by(ticker=ticker).one_or_none() or \
            fac.make_instrument(s, ticker, currency=currency,
                                exchange="ASX" if currency == "AUD" else "NASDAQ")
        fac.add_trade(s, inst, "2026-03-02", "buy", 10, "5.00", brokerage=brokerage,
                      fx_rate="1.5" if currency != "AUD" else "auto")
        s.commit()


def test_the_trade_form_carries_each_kinds_fee_and_last_trade(owner, session_factory):
    _settings(owner, session_factory, brokerage_flat="5", brokerage_percent="0.1")
    _trade(session_factory, "ACME", "AUD", "9.50")
    _trade(session_factory, "ZULU", "USD", "3.00")

    field = _field(owner.get("/trade/new", headers=HTML).text)

    assert field["fees"] == {
        "local": {"flat": "5", "percent": "0.1", "minimum": None},
        "foreign": None,
        "last": {"local": "9.50", "foreign": "3.00"},
    }
    # Before any units or price: the fee on nothing, which is the flat part.
    assert field["value"] == "5.00"


def test_a_minimum_is_what_the_form_starts_at(owner, session_factory):
    _settings(owner, session_factory, brokerage_percent="0.11", brokerage_minimum="14.95")
    assert _field(owner.get("/trade/new", headers=HTML).text)["value"] == "14.95"


@pytest.mark.parametrize("fees", [
    {"brokerage_percent": "0.1"},                                    # no minimum
    {"brokerage_flat": "0", "brokerage_percent": "0", "brokerage_minimum": "0"},   # free
])
def test_a_fee_that_comes_to_nothing_on_nothing_still_opens_the_forms(owner, session_factory,
                                                                     fees):
    """The form starts at the fee on nothing. A percentage with no minimum,
    or a broker that charges nothing, comes to zero, and the sum handed back
    the plain 0 `max` found first: the trade form and the plan form raised."""
    _settings(owner, session_factory, **fees)

    for path in ("/trade/new", "/schedule?edit=1"):
        page = owner.get(path, headers=HTML)
        assert page.status_code == 200, path
    assert _field(owner.get("/trade/new", headers=HTML).text)["value"] == "0.00"


def test_without_a_fee_the_last_trade_still_decides(owner, session_factory):
    _trade(session_factory, "ACME", "AUD", "9.50")
    assert _field(owner.get("/trade/new", headers=HTML).text)["value"] == "9.50"


def test_the_migration_adds_and_removes_the_fees_around_existing_data(tmp_path):
    """Run against a populated database, downgrade included (PR template)."""
    path = tmp_path / "fees.db"
    cfg = Config()
    cfg.set_main_option("script_location", str(APP_ROOT / "app" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(cfg, "0010")
    conn = sqlite3.connect(path)
    conn.executescript("""
        INSERT INTO portfolio (id, name, created_at) VALUES (1, 'Main', '2026-01-01');
        INSERT INTO instrument (id, ticker, exchange, currency, asset_class, drp, active)
        VALUES (1, 'ALPHA', 'ASX', 'AUD', 'etf', 0, 1);
        INSERT INTO trade (id, instrument_id, date, type, quantity, unit_price,
                           brokerage, fx_rate, portfolio_id)
        VALUES (1, 1, '2026-01-02', 'buy', 10, 100.5, 9.5, 1.0, 1);
    """)
    conn.commit()
    conn.close()

    command.upgrade(cfg, "0011")
    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT name, brokerage_flat, foreign_brokerage_minimum FROM portfolio"
    ).fetchall() == [("Main", None, None)]
    assert conn.execute("SELECT brokerage FROM trade").fetchall() == [(9.5,)]
    conn.close()

    command.downgrade(cfg, "0010")
    conn = sqlite3.connect(path)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(portfolio)")}
    assert not {c for c in columns if "brokerage" in c}
    assert conn.execute("SELECT name FROM portfolio").fetchall() == [("Main",)]
    conn.close()


def test_an_edit_keeps_the_brokerage_it_recorded(owner, session_factory):
    """No suggesting over a recorded figure: the edit form carries no fees."""
    _settings(owner, session_factory, brokerage_flat="5")
    _trade(session_factory, "ACME", "AUD", "12.34")
    with session_factory() as s:
        bind_to_only_portfolio(s)
        trade_id = s.query(fac.Trade).one().id
    field = _field(owner.get(f"/trade/{trade_id}/edit", headers=HTML).text)
    assert field == {"value": "12.34", "fees": None}


def test_a_planned_buy_keeps_the_plans_brokerage(owner, session_factory):
    """Today's DCA buy shows its working, brokerage included: the fee must not
    replace a figure the schedule's arithmetic used."""
    from freezegun import freeze_time
    from test_dca_prefill import TODAY, _planned

    _settings(owner, session_factory, brokerage_flat="5")
    with freeze_time(TODAY):
        _planned(session_factory, brokerage="9.50")
        field = _field(owner.get("/trade/new", headers=HTML).text)
    assert field == {"value": "9.5", "fees": None}


# The server only ever asked for the fee on nothing, the form's starting
# figure; the percentage is summed in tradeform.js as units and price are
# typed. So the sum itself went unchecked here: a percentage of a thousand
# dollars read as a thousand percent, or the minimum's blank read as a dollar,
# passed. Each shape the module's docstring names, on both sides of a minimum.
@pytest.mark.parametrize(("fee", "value", "due"), [
    ({"flat": "9.50"}, "1000", "9.50"),
    ({"flat": "5", "percent": "0.1"}, "10000", "15.00"),
    ({"percent": "0.1"}, "12345", "12.35"),                 # 12.345, half up
    ({"percent": "0.1"}, "12335", "12.34"),                 # 12.335, half up too
    ({"percent": "0.11", "minimum": "14.95"}, "1000", "14.95"),
    ({"percent": "0.11", "minimum": "14.95"}, "20000", "22.00"),
    ({"percent": "0.1"}, "100", "0.10"),                    # no minimum is no minimum
    ({"flat": "0", "percent": "0", "minimum": "0"}, "5000", "0.00"),
    ({}, "5000", "0.00"),
])
def test_the_fee_is_flat_plus_the_percentage_or_the_minimum(fee, value, due):
    from app import brokerage

    charged = brokerage.Fee(**{k: Decimal(v) for k, v in fee.items()}).on(Decimal(value))

    assert str(charged) == due
