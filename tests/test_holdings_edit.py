"""Manage holdings: a row's details can be corrected, and Remove is a button.

Test users had a NASDAQ stock saved with an ASX symbol, MSFT.AX, so it never
had a price, and no way to fix it short of deleting everything. Name, class,
currency and price symbol are now fields on the row. They belong to the
shared catalogue, so they are fields only where an edit changes nobody else's
portfolio: an instrument this portfolio alone has, or for an instance admin.

The row's actions are buttons. Remove stays Remove when trades are recorded,
and opens the list of them, with Delete all trades, instead of removing.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

import factories as fac
from app import tenancy
from app.models import HoldingPref, Instrument, Price, User
from test_routes import make_login, session_csrf

HTML = {"accept": "text/html"}
FIELDS = ("name", "asset_class", "currency", "yahoo_symbol")


def _setup(client, session_factory, *, admin):
    """Mine holds ACME, which a stranger's portfolio holds too, and ZULU alone.
    OMEGA is only the stranger's."""
    make_login(client, session_factory, admin=admin)
    with session_factory() as s:
        mine = s.scalar(select(fac.Portfolio))
        stranger = fac.make_user(s, "stranger@example.test")
        theirs = fac.make_portfolio(s, "Theirs", owner=stranger)
        acme = fac.make_instrument(s, "ACME", name="Acme")
        zulu = fac.make_instrument(s, "ZULU", name="Zulu")
        omega = fac.make_instrument(s, "OMEGA", name="Omega")
        fac.add_trade(s, acme, "2026-01-05", "buy", 10, "5.00", portfolio_id=mine.id)
        fac.add_trade(s, acme, "2026-01-05", "buy", 3, "5.00", portfolio_id=theirs.id)
        fac.add_trade(s, omega, "2026-01-05", "buy", 3, "5.00", portfolio_id=theirs.id)
        fac.hold(s, zulu, portfolio_id=mine.id)
        s.commit()
        return {"acme": acme.id, "zulu": zulu.id, "omega": omega.id}


@pytest.fixture
def writer(client, session_factory):
    return _setup(client, session_factory, admin=False)


@pytest.fixture
def admin(client, session_factory):
    return _setup(client, session_factory, admin=True)


def _row(page: str, ticker: str) -> str:
    found = re.search(rf'<tr[^>]*>\s*<td class="tick"><a href="/holding/{ticker}\?.*?</tr>',
                      page, re.S)
    assert found, f"no row for {ticker}"
    return found.group(0)


def _fields(row: str) -> set[str]:
    return set(re.findall(r'<(?:input|select)[^>]*\bname="([a-z_]+)"', row)) & set(FIELDS)


def _save(client, session_factory, instrument_id, **fields):
    data = {"_csrf": session_csrf(session_factory), "catalogue": "1", "name": "",
            "asset_class": "share", "currency": "AUD", "yahoo_symbol": "", **fields}
    return client.post(f"/holdings/{instrument_id}/pref", data=data, headers=HTML,
                       follow_redirects=False)


def test_this_portfolios_own_instrument_can_be_corrected(client, session_factory, writer):
    page = client.get("/holdings", headers=HTML).text
    assert _fields(_row(page, "ZULU")) == set(FIELDS)
    assert _fields(_row(page, "ACME")) == set(), "another portfolio holds it too"


def test_an_admin_can_correct_a_shared_one(client, session_factory, admin):
    page = client.get("/holdings", headers=HTML).text
    assert _fields(_row(page, "ACME")) == set(FIELDS)


def test_saving_corrects_the_details(client, session_factory, writer):
    resp = _save(client, session_factory, writer["zulu"], name="Zulu Corp",
                 asset_class="share", currency="usd", yahoo_symbol="ZULU")
    assert resp.status_code == 303, resp.text
    with session_factory() as s:
        inst = s.get(Instrument, writer["zulu"])
        assert (inst.name, inst.asset_class, inst.currency, inst.yahoo_symbol) == (
            "Zulu Corp", "share", "USD", "ZULU")


def test_blank_name_and_symbol(client, session_factory, writer):
    """A blank name is no name; a blank symbol is the guess, as when adding."""
    _save(client, session_factory, writer["zulu"], name="", yahoo_symbol="")
    with session_factory() as s:
        inst = s.get(Instrument, writer["zulu"])
        assert (inst.name, inst.yahoo_symbol) == (None, "ZULU.AX")


def test_a_shared_ones_details_stay_as_they_are_for_a_writer(client, session_factory, writer):
    assert _save(client, session_factory, writer["acme"], name="Changed").status_code == 403
    with session_factory() as s:
        assert s.get(Instrument, writer["acme"]).name == "Acme"


def test_an_admin_saves_a_shared_one(client, session_factory, admin):
    assert _save(client, session_factory, admin["acme"], name="Acme Ltd").status_code == 303
    with session_factory() as s:
        assert s.get(Instrument, admin["acme"]).name == "Acme Ltd"


def test_a_note_still_saves_on_a_shared_one(client, session_factory, writer):
    resp = client.post(f"/holdings/{writer['acme']}/pref",
                       data={"_csrf": session_csrf(session_factory), "note": "long term"},
                       headers=HTML, follow_redirects=False)
    assert resp.status_code == 303
    with session_factory() as s:
        tenancy.allow_unscoped(s)
        notes = [p.note for p in s.scalars(select(HoldingPref).where(
            HoldingPref.instrument_id == writer["acme"]))]
        assert notes == ["long term"]


@pytest.mark.parametrize("fields", [{}, {"catalogue": "1", "yahoo_symbol": "EVIL.AX"}])
def test_an_instrument_that_is_not_this_portfolios_is_not_found(
        client, session_factory, writer, fields):
    resp = client.post(f"/holdings/{writer['omega']}/pref",
                       data={"_csrf": session_csrf(session_factory), **fields},
                       headers=HTML, follow_redirects=False)
    assert resp.status_code == 404
    with session_factory() as s:
        tenancy.allow_unscoped(s)
        assert s.get(Instrument, writer["omega"]).yahoo_symbol == "OMEGA.AX"
        assert s.scalars(select(HoldingPref).where(
            HoldingPref.instrument_id == writer["omega"])).all() == []


@pytest.mark.parametrize("fields", [{"currency": "US"}, {"currency": ""},
                                    {"asset_class": "bond"}])
def test_a_bad_currency_or_class_is_refused(client, session_factory, writer, fields):
    assert _save(client, session_factory, writer["zulu"], **fields).status_code == 400
    with session_factory() as s:
        inst = s.get(Instrument, writer["zulu"])
        assert (inst.currency, inst.asset_class) == ("AUD", "etf")


def test_a_new_symbol_keeps_the_prices_and_fetches_its_own(
        client, session_factory, writer, app_module, monkeypatch):
    kicked = []
    monkeypatch.setattr(app_module, "_kick_feed", lambda: kicked.append(1) or False)
    with session_factory() as s:
        fac.add_prices(s, s.get(Instrument, writer["zulu"]), [("2026-01-05", "5.00")])
        s.commit()
    _save(client, session_factory, writer["zulu"], yahoo_symbol="ZULU.AX")
    assert kicked == [], "the same symbol fetches nothing new"
    _save(client, session_factory, writer["zulu"], yahoo_symbol="ZULU")
    assert kicked == [1]
    with session_factory() as s:
        assert len(s.scalars(select(Price).where(
            Price.instrument_id == writer["zulu"])).all()) == 1


def test_a_viewer_gets_no_fields(client, session_factory, admin):
    from app.models import PortfolioMember

    with session_factory() as s:
        for member in s.scalars(select(PortfolioMember)):
            if member.user.email == "user@example.test":
                member.role = "viewer"
        s.commit()
    page = client.get("/holdings", headers=HTML).text
    assert _fields(_row(page, "ZULU")) == set()


def test_another_portfolios_plan_makes_it_shared(client, session_factory, writer):
    from app import plans
    from app.models import InvestmentPlan, Portfolio

    with session_factory() as s:
        theirs = s.scalar(select(Portfolio).where(Portfolio.name == "Theirs"))
        stranger = s.scalar(select(User).where(User.email == "stranger@example.test"))
        tenancy.bind(s, theirs.id, stranger.id)
        plan = tenancy.owned(s, InvestmentPlan(name="Theirs", interval_days=28))
        s.add(plan)
        s.flush()
        plans.set_entries(s, plan, [writer["zulu"]])
        s.commit()
    page = client.get("/holdings", headers=HTML).text
    assert _fields(_row(page, "ZULU")) == set()


def test_the_row_actions_are_buttons(client, session_factory, writer):
    page = client.get("/holdings", headers=HTML).text
    zulu, acme = _row(page, "ZULU"), _row(page, "ACME")
    assert re.search(r'<button class="secondary small" type="submit" form="pref-\d+">Save</button>',
                     zulu)
    assert "linkish" not in zulu and "linkish" not in acme
    assert re.search(r'<button class="danger small" type="submit">Remove</button>', zulu)
    # Trades recorded: still Remove, opening what is in the way and Delete all trades.
    summary = re.search(r"<summary[^>]*>([^<]*)</summary>", acme)
    assert summary and summary.group(1).strip() == "Remove"
    assert "recorded" not in summary.group(0)
    assert re.search(r'href="/trade/\d+/edit', acme)
    assert re.search(r'action="/holdings/\d+/delete-trades"', acme)


def test_the_page_does_not_explain_sharing(client, session_factory, writer):
    assert "shared by everyone" not in client.get("/holdings", headers=HTML).text


def test_whoever_is_signed_in_is_a_writer_here(session_factory, writer):
    """The fixture's premise: the person signed in is not an instance admin."""
    with session_factory() as s:
        assert s.scalar(select(User).where(User.email == "user@example.test")).is_admin is False
