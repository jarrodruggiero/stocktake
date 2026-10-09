"""Typed text is held to the column it is going into — issue #60.

The inputs carry `maxlength`, but a direct POST ignores it. SQLite stores any
length in a `String(n)` column, so on SQLite these tests cannot see the failure
that Postgres gives (`value too long for type character varying`). What they
assert instead is that the request is REFUSED and that NOTHING was saved, which
is the same fact from the other side and holds on both backends.

One test per entry point, each with an over-long case, and a control at the
limit so the check cannot pass by refusing everything.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select
from test_api import bearer, bound, issue_key
from test_edit_delete import edit_trade
from test_trade_prefill import _record

import factories as fac
import fixture_portfolio as ref
from app import auth as auth_mod
from app import tenancy, textfield
from app.models import (
    Dividend,
    HoldingPref,
    Instrument,
    InvestmentPlan,
    LoginAttempt,
    Portfolio,
    PortfolioInvite,
    Trade,
    User,
)
from test_routes import (
    PASSWORD,
    bind_to_only_portfolio,
    make_login,
    pre_auth_csrf,
    reading,
    session_csrf,
)

HTML = {"accept": "text/html"}


# Copied from test_edit_delete and test_api rather than imported: a fixture
# imported from another test module needs a noqa on every test that takes it,
# and nothing where it is defined says this file depends on it (testing.md).

@pytest.fixture
def ledger(client, session_factory):
    """A signed-in owner holding 100 ACME bought in March."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME", name="Acme Industries")
        buy = fac.add_trade(s, acme, "2026-03-02", "buy", 100, "5.00", brokerage="9.50")
        s.commit()
        return {"acme_id": acme.id, "buy_id": buy.id}


@pytest.fixture
def furnished(client, session_factory):
    """One portfolio holding the reference data, plus its owner."""
    with session_factory() as s:
        user = fac.make_user(s, "api@example.test")
        portfolio = fac.make_portfolio(s, "API portfolio", owner=user)
        s.commit()
        tenancy.bind(s, portfolio.id, user.id)
        ref.build_reference(s)
        ids = (portfolio.id, user.id)
    return ids


# --------------------------------------------------------------------------- #
# The helper
# --------------------------------------------------------------------------- #

def test_text_is_stripped_and_empty_text_is_none():
    assert textfield.fit("  a note  ", Trade.note) == "a note"
    assert textfield.fit("   ", Trade.note) is None
    assert textfield.fit(None, Trade.note) is None


def test_the_limit_is_the_columns_and_is_inclusive():
    limit = Trade.note.type.length

    assert textfield.fit("n" * limit, Trade.note) == "n" * limit
    with pytest.raises(textfield.TextError):
        textfield.fit("n" * (limit + 1), Trade.note)
    # Read from the column, so each one gets its own.
    assert Instrument.yahoo_symbol.type.length != Trade.note.type.length


def test_the_message_says_how_long_and_does_not_repeat_the_text():
    with pytest.raises(textfield.TextError) as caught:
        textfield.fit("secret " * 100, Trade.note, "Note")

    message = str(caught.value)
    assert message == "Note: that is 699 characters, and the most it can hold is 400"
    assert "secret" not in message
    assert isinstance(caught.value, ValueError)


def test_problem_is_the_message_or_none():
    assert textfield.problem("fine", Trade.note, "Note") is None
    assert "Note:" in textfield.problem("n" * 401, Trade.note, "Note")


# --------------------------------------------------------------------------- #
# Notes and names on the trade and dividend forms
# --------------------------------------------------------------------------- #

def _trade_count(session_factory) -> int:
    with reading(session_factory) as s:
        return len(s.scalars(select(Trade)).all())


@pytest.fixture
def acme(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "ACME", asset_class="share")
        fac.hold(s, inst)
        s.commit()
        return inst.id


def test_a_note_too_long_for_a_new_trade_is_refused_and_not_saved(
        client, session_factory, acme):
    resp = _record(client, session_factory, acme, note="n" * 401)

    assert resp.status_code == 200
    assert "Note: that is 401 characters" in resp.text
    assert _trade_count(session_factory) == 0


def test_a_note_at_the_limit_is_saved_whole(client, session_factory, acme):
    resp = _record(client, session_factory, acme, note="n" * 400)

    assert resp.status_code == 303
    with reading(session_factory) as s:
        assert s.scalars(select(Trade)).one().note == "n" * 400


@pytest.mark.parametrize("overrides, message", [
    ({"new_name": "n" * 81}, "Name: that is 81 characters"),
    ({"new_yahoo": "y" * 21}, "Yahoo symbol: that is 21 characters"),
], ids=["name", "yahoo-symbol"])
def test_a_new_instruments_text_is_checked_on_the_trade_form(
        client, session_factory, acme, overrides, message):
    resp = _record(client, session_factory, "new", new_ticker="NEWCO", new_exchange="ASX",
                   new_asset_class="share", new_currency="AUD", **overrides)

    assert resp.status_code == 200
    assert message in resp.text
    with reading(session_factory) as s:
        assert s.scalars(select(Instrument).where(Instrument.ticker == "NEWCO")).all() == []
    assert _trade_count(session_factory) == 0


def test_a_note_too_long_on_a_trade_edit_is_refused_and_the_trade_is_unchanged(
        client, session_factory, ledger):
    resp = edit_trade(client, session_factory, ledger["buy_id"], note="n" * 401)

    assert "Note%3A+that+is+401+characters" in resp.headers["location"]
    with reading(session_factory) as s:
        assert s.get(Trade, ledger["buy_id"]).note is None


def test_a_note_too_long_on_a_dividend_is_refused_and_the_dividend_is_unchanged(
        client, session_factory, ledger):
    with session_factory() as s:
        tenancy.bind(s, s.scalars(select(Portfolio)).first().id,
                     s.scalars(select(User)).first().id)
        dividend = fac.add_dividend(s, s.get(Instrument, ledger["acme_id"]),
                                    "2026-04-15", "24.00")
        s.commit()
        dividend_id = dividend.id
    data = {"div_date": "2026-04-15", "cash_amount": "24.00", "franking_credits": "",
            "note": "n" * 401, "_csrf": session_csrf(session_factory)}

    resp = client.post(f"/dividend/{dividend_id}/edit", data=data, headers=HTML,
                       follow_redirects=False)

    assert "Note%3A+that+is+401+characters" in resp.headers["location"]
    with reading(session_factory) as s:
        assert s.get(Dividend, dividend_id).note is None


# --------------------------------------------------------------------------- #
# The holding page: its note and the Yahoo symbol
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("data, message", [
    ({"note": "n" * 401}, "Note: that is 401 characters"),
    ({"yahoo_symbol": "y" * 21}, "Yahoo symbol: that is 21 characters"),
], ids=["note", "yahoo-symbol"])
def test_the_holding_preferences_are_checked_and_nothing_moves(
        client, session_factory, acme, data, message):
    resp = client.post(f"/holdings/{acme}/pref",
                       data={**data, "_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 400
    assert message in resp.text
    with reading(session_factory) as s:
        # The setting it already had, untouched.
        assert [p.note for p in s.scalars(select(HoldingPref))] == [None]
        assert s.get(Instrument, acme).yahoo_symbol != "y" * 21


@pytest.mark.parametrize("overrides, message", [
    ({"name": "n" * 81}, "Name%3A+that+is+81+characters"),
    ({"yahoo_symbol": "y" * 21}, "Yahoo+symbol%3A+that+is+21+characters"),
], ids=["name", "yahoo-symbol"])
def test_adding_an_instrument_checks_its_text(
        client, session_factory, acme, overrides, message):
    data = {"ticker": "OTHER", "name": "", "exchange": "ASX", "asset_class": "etf",
            "currency": "AUD", "yahoo_symbol": "", "_csrf": session_csrf(session_factory)}
    data.update(overrides)

    resp = client.post("/holdings/add", data=data, headers=HTML, follow_redirects=False)

    assert message in resp.headers["location"]
    with reading(session_factory) as s:
        assert s.scalars(select(Instrument).where(Instrument.ticker == "OTHER")).all() == []


# --------------------------------------------------------------------------- #
# The DCA plan's name — empty keeps its default
# --------------------------------------------------------------------------- #

def _save_plan(client, session_factory, name):
    return client.post("/schedule/save",
                       data={"name": name, "interval_days": "28", "amount": "500",
                             "brokerage": "9.50", "start_date": "2026-08-10",
                             "tickers": "ACME", "_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)


def test_a_plan_name_too_long_is_refused_and_no_plan_is_saved(
        client, session_factory, acme):
    resp = _save_plan(client, session_factory, "p" * 81)

    assert resp.status_code == 400
    assert "Name: that is 81 characters" in resp.text
    with reading(session_factory) as s:
        assert s.scalars(select(InvestmentPlan)).all() == []


def test_an_empty_plan_name_keeps_its_default(client, session_factory, acme):
    resp = _save_plan(client, session_factory, "   ")

    assert resp.status_code == 303
    with reading(session_factory) as s:
        assert s.scalars(select(InvestmentPlan)).one().name == "My plan"


# --------------------------------------------------------------------------- #
# The statement commit builds its note from what was typed
# --------------------------------------------------------------------------- #

def test_a_statement_note_too_long_is_refused_and_nothing_is_saved(
        client, session_factory, acme):
    resp = client.post("/imports-exports/statement/commit",
                       data={"ticker": "ACME", "payment_date": "2026-03-15",
                             "net_amount": "100.00", "drp_units": "10",
                             "drp_price": "10.00", "note_extra": "n" * 400,
                             "_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 400
    assert "Note: that is" in resp.text
    with reading(session_factory) as s:
        assert s.scalars(select(Dividend)).all() == []
        assert s.scalars(select(Trade)).all() == []


# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path, body", [
    ("/api/v1/trades", {"ticker": "ALPHA", "type": "buy", "date": "2026-07-01",
                        "units": "1", "unit_price": "10.00"}),
    ("/api/v1/dividends", {"ticker": "ALPHA", "date": "2026-07-01",
                           "cash_amount": "10.00"}),
], ids=["trade", "dividend"])
def test_an_api_note_too_long_is_refused_and_nothing_is_saved(
        client, session_factory, furnished, path, body):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, scopes="read,write", created_by=user_id)

    def rows():
        with bound(session_factory, portfolio_id) as s:
            return len(s.scalars(select(Trade)).all()) + len(s.scalars(select(Dividend)).all())

    before = rows()

    refused = client.post(path, json={**body, "note": "n" * 401}, headers=bearer(raw))
    after_refused = rows()
    allowed = client.post(path, json={**body, "note": "n" * 400}, headers=bearer(raw))

    assert refused.status_code == 422
    assert after_refused == before             # nothing saved by the refused one
    assert allowed.status_code == 201          # the control: exactly the limit is fine
    assert rows() == before + 1


# --------------------------------------------------------------------------- #
# People: names and emails
# --------------------------------------------------------------------------- #

LONG_EMAIL = "a" * 310 + "@example.test"          # 323 characters, over the 320 limit
AT_LIMIT_EMAIL = "a" * 307 + "@example.test"      # exactly 320


def _users(session_factory):
    with session_factory() as s:
        return s.scalars(select(User).order_by(User.id)).all()


PEOPLE = [
    pytest.param("name", "n" * 121, "Name: that is 121 characters", id="name"),
    pytest.param("email", LONG_EMAIL, "That email address is too long.", id="email"),
    # 313 characters before `.lower()`, 613 after: "İ".lower() is two characters.
    pytest.param("email", "İ" * 300 + "@example.test", "That email address is too long.",
                 id="email-that-grows-when-lowercased"),
]


@pytest.mark.parametrize("field, value, message", PEOPLE)
def test_an_admin_cannot_create_an_account_with_text_too_long(
        client, session_factory, field, value, message):
    make_login(client, session_factory, admin=True)
    before = len(_users(session_factory))
    data = {"name": "Someone", "email": "someone@example.test",
            "password": "a-long-enough-one", "_csrf": session_csrf(session_factory)}
    data[field] = value

    resp = client.post("/users/add", data=data, headers=HTML)

    assert message in resp.text
    assert len(_users(session_factory)) == before


@pytest.mark.parametrize("field, value, message", PEOPLE)
def test_an_invitation_cannot_create_an_account_with_text_too_long(
        client, session_factory, field, value, message):
    make_login(client, session_factory)
    resp = client.post("/members/invite", follow_redirects=False,
                       data={"_csrf": session_csrf(session_factory), "role": "member"})
    token = re.search(r"invite=([^&]+)", resp.headers["location"]).group(1)
    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)
    before = len(_users(session_factory))
    data = {"_csrf": pre_auth_csrf(client), "name": "Joiner",
            "email": "joiner@example.test", "password": PASSWORD}
    data[field] = value

    page = client.post(f"/invite/{token}/signup", headers=HTML, follow_redirects=False,
                       data=data)

    assert message in page.text
    assert len(_users(session_factory)) == before
    with session_factory() as s:
        assert s.scalars(select(PortfolioInvite)).one().used_at is None   # not burned


@pytest.mark.parametrize("field, value, message", PEOPLE)
def test_the_setup_wizard_cannot_create_the_first_account_with_text_too_long(
        client, session_factory, field, value, message):
    client.get("/setup", headers=HTML)
    token = client.cookies.get(auth_mod.PRE_AUTH_CSRF_COOKIE)
    client.post("/setup", data={"_csrf": token}, headers=HTML, follow_redirects=False)
    data = {"name": "First Owner", "email": "first@example.test", "password": PASSWORD,
            "_csrf": token}
    data[field] = value

    resp = client.post("/setup/profile", data=data, headers=HTML, follow_redirects=False)

    assert message in resp.text
    assert _users(session_factory) == []


def _attempts(session_factory):
    with session_factory() as s:
        return s.scalars(select(LoginAttempt)).all()


def test_a_sign_in_with_an_email_too_long_to_record_is_refused_like_any_wrong_one(
        client, session_factory):
    """Anyone can post this, signed in or not. It used to reach `record_attempt`
    and fail on Postgres, so a stranger could cause a 500. The answer is the one
    the form gives for a wrong email, so it tells them nothing."""
    resp = client.post("/login", headers=HTML, follow_redirects=False,
                       data={"email": LONG_EMAIL, "password": "whatever-it-is",
                             "_csrf": pre_auth_csrf(client)})

    assert resp.status_code == 200
    assert "Invalid email or password." in resp.text
    assert _attempts(session_factory) == []


def test_an_email_at_the_limit_is_still_recorded_as_an_attempt(client, session_factory):
    """The control: only what cannot be stored is refused early."""
    client.post("/login", headers=HTML, follow_redirects=False,
                data={"email": AT_LIMIT_EMAIL, "password": "whatever-it-is",
                      "_csrf": pre_auth_csrf(client)})

    assert [a.email for a in _attempts(session_factory)] == [AT_LIMIT_EMAIL]


def test_recovery_with_an_email_too_long_to_record_is_refused_like_any_wrong_one(
        client, session_factory):
    client.get("/login/recover", headers=HTML)

    resp = client.post("/login/recover", headers=HTML, follow_redirects=False,
                       data={"email": LONG_EMAIL, "code": "ABCDE-FGHJK-MNPQR",
                             "_csrf": pre_auth_csrf(client)})

    assert resp.status_code == 200
    assert "That email and recovery code do not match." in resp.text
    assert _attempts(session_factory) == []


# --------------------------------------------------------------------------- #
# A blank name falls back to the email, which can be longer than the name column
# --------------------------------------------------------------------------- #

LONG_BUT_VALID_EMAIL = "a" * 190 + "@example.test"        # 203 characters, under 320


def _only_new_user(session_factory, email):
    with session_factory() as s:
        return s.scalars(select(User).where(User.email == email)).one()


def test_a_blank_name_with_a_long_email_is_cut_to_fit_for_an_admin_created_account(
        client, session_factory):
    """The fallback is a value the app picks, so it is cut rather than refused:
    the name column holds 120 and an email 320."""
    make_login(client, session_factory, admin=True)

    client.post("/users/add", headers=HTML,
                data={"name": "   ", "email": LONG_BUT_VALID_EMAIL,
                      "password": "a-long-enough-one", "_csrf": session_csrf(session_factory)})

    assert _only_new_user(session_factory, LONG_BUT_VALID_EMAIL).name == (
        LONG_BUT_VALID_EMAIL[:120])


def test_a_blank_name_with_a_long_email_is_cut_to_fit_on_an_invitation(
        client, session_factory):
    make_login(client, session_factory)
    resp = client.post("/members/invite", follow_redirects=False,
                       data={"_csrf": session_csrf(session_factory), "role": "member"})
    token = re.search(r"invite=([^&]+)", resp.headers["location"]).group(1)
    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)

    client.post(f"/invite/{token}/signup", headers=HTML, follow_redirects=False,
                data={"_csrf": pre_auth_csrf(client), "name": "   ",
                      "email": LONG_BUT_VALID_EMAIL, "password": PASSWORD})

    assert _only_new_user(session_factory, LONG_BUT_VALID_EMAIL).name == (
        LONG_BUT_VALID_EMAIL[:120])


def test_a_blank_name_with_a_long_email_is_cut_to_fit_in_the_setup_wizard(
        client, session_factory):
    client.get("/setup", headers=HTML)
    token = client.cookies.get(auth_mod.PRE_AUTH_CSRF_COOKIE)
    client.post("/setup", data={"_csrf": token}, headers=HTML, follow_redirects=False)

    client.post("/setup/profile", headers=HTML, follow_redirects=False,
                data={"name": "   ", "email": LONG_BUT_VALID_EMAIL, "password": PASSWORD,
                      "_csrf": token})

    assert _only_new_user(session_factory, LONG_BUT_VALID_EMAIL).name == (
        LONG_BUT_VALID_EMAIL[:120])
