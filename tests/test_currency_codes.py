"""A currency is a three-letter code, wherever it comes in.

It was free text in three places — adding a holding, the trade form's new
instrument, and the broker designer — and only upper-cased. Instruments are
shared by every portfolio on an instance, and the chart pages put the currency
into HTML, so markup typed there ran in whoever opened a chart next (decision
#130). The browser half, which covers a value stored before this, is
`test_data_is_text_in_the_browser.py`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_brokerdesign import SELFWEALTH

from app import pricefeed
from app.models import Instrument, Trade, currency_problem
from app.settings import BrokerFormat
from test_routes import make_login, reading, session_csrf

HTML = {"accept": "text/html"}
# Upper case on purpose: it is what survives the `.upper()` the routes apply.
MARKUP = "<IMG SRC=X ONERROR=&#X61;>"


@pytest.mark.parametrize("code", ["AUD", "USD", "usd", " NZD "])
def test_a_three_letter_code_is_a_currency(code):
    assert currency_problem(code) is None


@pytest.mark.parametrize("code", ["", "AU", "AUDX", "A1D", "A D", MARKUP])
def test_anything_else_is_not(code):
    assert currency_problem(code) == "A currency is a three-letter code, like AUD or USD."


def test_an_instrument_refuses_anything_but_a_code_whatever_set_it():
    """The backstop under every route: a path that forgets to ask still cannot
    store markup."""
    with pytest.raises(ValueError, match="three-letter code"):
        Instrument(ticker="ACME", exchange="ASX", currency=MARKUP, asset_class="share")
    inst = Instrument(ticker="ACME", exchange="ASX", currency="USD", asset_class="share")
    with pytest.raises(ValueError, match="three-letter code"):
        inst.currency = "usd"   # the routes normalise; the model does not guess


def _add(client, session_factory, **fields):
    data = {"ticker": "ZULU", "name": "Zulu Ltd", "exchange": "ASX",
            "asset_class": "share", "currency": "AUD",
            "_csrf": session_csrf(session_factory), **fields}
    return client.post("/holdings/add", data=data, headers=HTML, follow_redirects=True)


def _instruments(session_factory) -> list[Instrument]:
    with reading(session_factory) as s:
        return list(s.scalars(select(Instrument)))


def test_adding_a_holding_refuses_a_currency_that_is_not_a_code(client, session_factory):
    make_login(client, session_factory)

    page = _add(client, session_factory, currency=MARKUP)

    assert "A currency is a three-letter code" in page.text
    assert _instruments(session_factory) == []


def test_adding_a_holding_still_takes_a_code_in_lower_case(client, session_factory):
    make_login(client, session_factory)

    _add(client, session_factory, currency="usd")

    assert [i.currency for i in _instruments(session_factory)] == ["USD"]


def test_a_currency_from_yahoo_that_is_not_a_code_is_not_used(
        client, session_factory, monkeypatch, feed_on):
    """Yahoo's answer is a suggestion, held to the same rule as typing."""
    monkeypatch.setattr(pricefeed, "lookup", lambda ticker, exchange: {
        "symbol": "ZULU.AX", "name": "Zulu Ltd", "currency": MARKUP, "found": True})
    make_login(client, session_factory)

    # Blank, not empty: httpx drops an empty field, and the form's default
    # currency would then be what was "typed", so Yahoo would never be asked.
    _add(client, session_factory, currency=" ", name=" ")

    assert [i.currency for i in _instruments(session_factory)] == ["AUD"]


def test_the_trade_form_refuses_a_new_instrument_with_a_bad_currency(
        client, session_factory, monkeypatch):
    monkeypatch.setattr(pricefeed, "lookup", lambda ticker, exchange: {})
    make_login(client, session_factory)

    page = client.post("/trade/new", data={
        "instrument_id": "new", "new_ticker": "ZULU", "new_name": "Zulu Ltd",
        "new_exchange": "ASX", "new_asset_class": "share", "new_currency": MARKUP,
        "type": "buy", "trade_date": "2026-07-01", "quantity": "10",
        "unit_price": "4.00", "brokerage": "0",
        "_csrf": session_csrf(session_factory)}, headers=HTML)

    assert "A currency is a three-letter code" in page.text
    assert _instruments(session_factory) == []
    with reading(session_factory) as s:
        assert s.scalars(select(Trade)).all() == []


def test_a_broker_format_holds_a_code():
    assert BrokerFormat(kind="mapped", currency="usd").currency == "USD"
    with pytest.raises(ValidationError, match="three-letter code"):
        BrokerFormat(kind="mapped", currency=MARKUP)


BAD_BROKER = f"""kind: mapped
exchange: ASX
currency: "{MARKUP}"
date_format: "%d/%m/%Y"
columns: {{date: Trade Date, action: Buy/Sell, ticker: Code, units: Units, price: Price}}
""".encode()


@pytest.fixture
def admin(client, session_factory, tmp_path, monkeypatch, app_module):
    """Signed in as an admin, with the drop-in directory pointed at tmp_path."""
    monkeypatch.setattr(app_module.settings.imports, "templates_dir", str(tmp_path))
    make_login(client, session_factory)
    return tmp_path


def test_installing_a_broker_format_with_a_bad_currency_is_refused(
        client, session_factory, admin):
    import re

    csrf = re.search(r'name="_csrf" value="([^"]+)"',
                     client.get("/imports-exports", headers=HTML).text).group(1)

    page = client.post("/imports-exports/formats",
                       data={"_csrf": csrf, "kind": "broker"},
                       files={"file": ("evil.yaml", BAD_BROKER, "application/yaml")},
                       headers=HTML, follow_redirects=True)

    assert "three-letter code" in page.text
    assert not list(admin.rglob("*.yaml"))


def test_a_bad_format_installed_before_the_fix_is_skipped_not_fatal(
        client, session_factory, admin, app_module):
    """One bad file must not take the imports page down with it."""
    (admin / "brokers").mkdir()
    (admin / "brokers" / "evil.yaml").write_bytes(BAD_BROKER)

    assert "evil" not in app_module.settings.imports.brokers_available()
    assert client.get("/imports-exports", headers=HTML).status_code == 200


def test_the_broker_designer_refuses_a_bad_currency(client, session_factory):
    make_login(client, session_factory)

    page = client.post("/imports-exports/broker/design", data={
        "_csrf": session_csrf(session_factory), "text": SELFWEALTH,
        "name": "SelfWealth", "exchange": "ASX", "currency": MARKUP,
        "date_format": "%d/%m/%Y",
        "col_date": "Trade Date", "col_action": "Buy/Sell", "col_ticker": "Code",
        "col_units": "Units", "col_price": "Price"}, headers=HTML)

    assert page.status_code == 200
    assert "A currency is a three-letter code" in page.text
    # No format is generated to install or contribute.
    assert "kind: mapped" not in page.text

