"""Editing and deleting what's already in the ledger.

The ledger is not append-only, and three things depended on it being so without
saying so: the series cache fingerprinted on row counts, the FIFO matcher
assumed parcels only ever arrived, and the balance guard only ever validated a
row being added. Editing breaks all three quietly — the numbers stay plausible,
they are just wrong — so the tests here lean on the consequences rather than on
the routes returning 303.

Four groups:

  * **the guard** — a change is checked against the WHOLE timeline, not just
    the row being changed, and the refusal names the trade that has to move
    first (a bare "would go negative" leaves you hunting through six years of
    ledger for the one that broke);
  * **the cache** — an edit that changes no row count still invalidates every
    derived number, which is what `updated_at` exists for;
  * **linked rows** — a DRP trade and its dividend are one event; a planned
    purchase outlives the trade it created;
  * **permissions** — a viewer has no way to do any of it.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import pytest
from freezegun import freeze_time
from sqlalchemy import select

import factories as fac
from app import queries
from app.models import Dividend, Instrument, PlannedPurchase, PortfolioMember, Trade
from test_routes import bind_to_only_portfolio, make_login, reading, session_csrf

HTML = {"accept": "text/html"}
TODAY = "2026-08-02"


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


def edit_trade(client, session_factory, trade_id, **overrides):
    data = {
        "type": "buy",
        "trade_date": "2026-03-02",
        "quantity": "100",
        "unit_price": "5.00",
        "brokerage": "9.50",
        "fx_rate": "",
        "note": "",
        "_csrf": session_csrf(session_factory),
    }
    data.update(overrides)
    return client.post(
        f"/trade/{trade_id}/edit", data=data, headers=HTML, follow_redirects=False
    )


def delete_trade(client, session_factory, trade_id):
    return client.post(
        f"/trade/{trade_id}/delete",
        data={"_csrf": session_csrf(session_factory)},
        headers=HTML,
        follow_redirects=False,
    )


# --------------------------------------------------------------------------- #
# Editing a trade
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_the_edit_form_is_prefilled_from_the_row(client, session_factory, ledger):
    resp = client.get(f"/trade/{ledger['buy_id']}/edit", headers=HTML)

    assert resp.status_code == 200
    assert 'value="2026-03-02"' in resp.text
    assert 'value="100"' in resp.text
    assert 'value="5"' in resp.text
    # The instrument is fixed on edit — moving a trade between instruments is a
    # delete and a re-record, not a field. (The picker's *id* still appears in
    # the shared script, which no-ops when the select isn't there.)
    assert 'name="instrument_id"' not in resp.text
    assert "ACME — Acme Industries" in resp.text


@freeze_time(TODAY)
def test_editing_changes_the_stored_trade(client, session_factory, ledger):
    resp = edit_trade(
        client, session_factory, ledger["buy_id"], quantity="120", unit_price="5.50"
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/holding/ACME"
    with reading(session_factory) as s:
        trade = s.get(Trade, ledger["buy_id"])
        assert trade.quantity == Decimal("120")
        assert trade.unit_price == Decimal("5.50")


@freeze_time(TODAY)
def test_an_edit_can_flip_a_buy_to_a_sell(client, session_factory, ledger):
    """Recording the wrong side is an ordinary mistake, and the timeline still
    has to survive the correction — so this one is refused: nothing was held
    before the buy that is being turned into a sell."""
    resp = edit_trade(client, session_factory, ledger["buy_id"], type="sell")

    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    with reading(session_factory) as s:
        assert s.get(Trade, ledger["buy_id"]).type == "buy"


@freeze_time(TODAY)
def test_the_edit_form_rejects_a_future_date(client, session_factory, ledger):
    resp = edit_trade(client, session_factory, ledger["buy_id"], trade_date="2027-01-01")

    assert "future" in resp.headers["location"]
    with reading(session_factory) as s:
        assert s.get(Trade, ledger["buy_id"]).date == dt.date(2026, 3, 2)


@freeze_time(TODAY)
def test_the_edit_form_rejects_nonsense_numbers(client, session_factory, ledger):
    resp = edit_trade(client, session_factory, ledger["buy_id"], quantity="not-a-number")

    assert "error=" in resp.headers["location"]
    with reading(session_factory) as s:
        assert s.get(Trade, ledger["buy_id"]).quantity == Decimal("100")


@freeze_time(TODAY)
@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"type": "drp"}, "Choose+buy+or+sell"),
        ({"quantity": "0"}, "greater+than+zero"),
        ({"unit_price": "-1"}, "greater+than+zero"),
        ({"brokerage": "-5"}, "can%27t+be+negative"),
        ({"fx_rate": "0"}, "FX+must+be+positive"),
        ({"set_time": "1", "trade_time": "half+past+ten"}, "isn%27t+a+time"),
    ],
    ids=["type", "zero-units", "negative-price", "negative-brokerage", "zero-fx", "bad-time"],
)
def test_the_edit_form_validates_like_the_create_form(
    client, session_factory, ledger, overrides, expected
):
    """Edit runs the same gauntlet as create. Two forms with one set of rules
    between them is how the strict one quietly becomes the lenient one."""
    resp = edit_trade(client, session_factory, ledger["buy_id"], **overrides)

    assert expected in resp.headers["location"]
    with reading(session_factory) as s:
        trade = s.get(Trade, ledger["buy_id"])
        assert trade.quantity == Decimal("100")
        assert trade.type == "buy"


@freeze_time(TODAY)
def test_setting_a_time_on_an_edit_stores_it(client, session_factory, ledger):
    resp = edit_trade(
        client, session_factory, ledger["buy_id"], set_time="1", trade_time="14:30"
    )

    assert resp.headers["location"] == "/holding/ACME"
    with reading(session_factory) as s:
        assert s.get(Trade, ledger["buy_id"]).time == dt.time(14, 30)


@freeze_time(TODAY)
def test_unticking_the_time_puts_the_trade_back_at_the_open(client, session_factory, ledger):
    """Unticking has to mean something — otherwise a time set by mistake can
    never be taken off again."""
    edit_trade(client, session_factory, ledger["buy_id"], set_time="1", trade_time="14:30")

    edit_trade(client, session_factory, ledger["buy_id"], set_time="", trade_time="14:30")

    with reading(session_factory) as s:
        assert s.get(Trade, ledger["buy_id"]).time == dt.time(10, 0)


def _checkbox(html: str, name: str) -> str:
    """The one <input> tag with this name, so 'checked' can be asserted on the
    tag itself rather than anywhere in the page."""
    match = re.search(rf"<input[^>]*name=\"{name}\"[^>]*>", html)
    assert match, f"no {name} input in the page"
    return match.group(0)


@freeze_time(TODAY)
def test_the_edit_form_prefills_a_time_that_was_set(client, session_factory, ledger):
    edit_trade(client, session_factory, ledger["buy_id"], set_time="1", trade_time="14:30")

    resp = client.get(f"/trade/{ledger['buy_id']}/edit", headers=HTML)

    assert 'value="14:30"' in resp.text
    assert "checked" in _checkbox(resp.text, "set_time")


@freeze_time(TODAY)
def test_a_trade_at_the_open_comes_back_with_the_box_unticked(client, session_factory, ledger):
    """A trade nobody set a time on shouldn't look like one somebody did."""
    resp = client.get(f"/trade/{ledger['buy_id']}/edit", headers=HTML)

    assert "checked" not in _checkbox(resp.text, "set_time")


@freeze_time(TODAY)
def test_a_trade_in_another_portfolio_is_not_found(client, session_factory, ledger):
    """`db.get` doesn't go through the tenancy filter, so the route checks the
    owner itself. Without that check, an id from someone else's ledger would
    open in the edit form."""
    with session_factory() as s:
        other = fac.make_portfolio(s, "Someone else")
        acme = s.get(Instrument, ledger["acme_id"])
        theirs = fac.add_trade(
            s, acme, "2026-04-01", "buy", 5, "5.00", portfolio_id=other.id
        )
        s.commit()
        theirs_id = theirs.id

    assert client.get(f"/trade/{theirs_id}/edit", headers=HTML).status_code == 404
    assert delete_trade(client, session_factory, theirs_id).status_code == 404


# --------------------------------------------------------------------------- #
# The guard: the whole timeline, and a message that names the offender
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_cutting_an_old_buy_below_a_later_sell_is_refused(client, session_factory, ledger):
    """The case the plan was written around: reducing a buy from three months
    ago strands a sell that was fine until now. Validating only the edited row
    would let this through and break the FY report."""
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = s.get(Trade, ledger["buy_id"]).instrument
        fac.add_trade(s, acme, "2026-05-10", "sell", 80, "6.00")
        s.commit()

    resp = edit_trade(client, session_factory, ledger["buy_id"], quantity="50")

    assert resp.status_code == 303
    location = resp.headers["location"]
    # The message has to identify the sell, not just say "negative".
    assert "sell+of+80+units" in location
    assert "2026-05-10" in location
    assert "Edit+or+delete+that+sell+first" in location
    with reading(session_factory) as s:
        assert s.get(Trade, ledger["buy_id"]).quantity == Decimal("100")


@freeze_time(TODAY)
def test_moving_a_buy_later_than_its_sell_is_refused(client, session_factory, ledger):
    """A date move breaks the timeline just as a quantity cut does — the units
    arrive after they were sold."""
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = s.get(Trade, ledger["buy_id"]).instrument
        fac.add_trade(s, acme, "2026-05-10", "sell", 100, "6.00")
        s.commit()

    resp = edit_trade(client, session_factory, ledger["buy_id"], trade_date="2026-06-01")

    assert "error=" in resp.headers["location"]
    with reading(session_factory) as s:
        assert s.get(Trade, ledger["buy_id"]).date == dt.date(2026, 3, 2)


@freeze_time(TODAY)
def test_an_edit_that_keeps_the_timeline_valid_is_accepted(client, session_factory, ledger):
    """The mirror of the two above: the guard has to allow the ordinary case,
    or it is just a wall."""
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = s.get(Trade, ledger["buy_id"]).instrument
        fac.add_trade(s, acme, "2026-05-10", "sell", 80, "6.00")
        s.commit()

    resp = edit_trade(client, session_factory, ledger["buy_id"], quantity="90")

    assert resp.headers["location"] == "/holding/ACME"
    with reading(session_factory) as s:
        assert s.get(Trade, ledger["buy_id"]).quantity == Decimal("90")


def test_the_breach_message_names_the_later_trade():
    """Unit-level, so the wording is pinned somewhere a route test won't
    obscure it. This is the copy agreed in the plan."""
    inst = type("I", (), {"ticker": "ALPHA", "currency": "AUD"})()
    buy = Trade(id=1, date=dt.date(2024, 1, 5), type="buy",
                quantity=Decimal("100"), unit_price=Decimal("90"),
                brokerage=Decimal("0"))
    sell = Trade(id=2, date=dt.date(2024, 3, 10), type="sell",
                 quantity=Decimal("100"), unit_price=Decimal("95"),
                 brokerage=Decimal("0"))
    candidate = Trade(id=1, date=dt.date(2024, 1, 5), type="buy",
                      quantity=Decimal("50"), unit_price=Decimal("90"),
                      brokerage=Decimal("0"))

    breach = queries.balance_breach([buy, sell], candidate, exclude_ids={1})
    message = queries.breach_message(inst.ticker, breach)

    assert breach.trade is sell
    assert breach.balance == Decimal("-50")
    assert message == (
        "That change would leave ALPHA at -50 units from 2024-03-10: the sell of "
        "100 units on 2024-03-10 would then be more than the 50 units held. "
        "Edit or delete that sell first."
    )


def test_the_breach_message_is_different_when_the_new_row_is_the_problem():
    """Telling someone to "edit or delete that sell first" when the sell IS what
    they are typing would be nonsense."""
    buy = Trade(id=1, date=dt.date(2024, 1, 5), type="buy",
                quantity=Decimal("10"), unit_price=Decimal("4"),
                brokerage=Decimal("0"))
    candidate = Trade(date=dt.date(2024, 7, 1), type="sell",
                      quantity=Decimal("25"), unit_price=Decimal("5"),
                      brokerage=Decimal("0"))

    breach = queries.balance_breach([buy], candidate)

    assert breach.is_candidate
    assert queries.breach_message("ACME", breach) == (
        "That sell of 25 units is more than the 10 units of ACME held on "
        "2024-07-01 — check the date and units against the ledger."
    )


# --------------------------------------------------------------------------- #
# Deleting
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_deleting_a_trade_removes_it(client, session_factory, ledger):
    resp = delete_trade(client, session_factory, ledger["buy_id"])

    assert resp.headers["location"] == "/holding/ACME"
    with reading(session_factory) as s:
        assert s.get(Trade, ledger["buy_id"]) is None


@freeze_time(TODAY)
def test_deleting_a_buy_that_a_later_sell_depends_on_is_refused(
    client, session_factory, ledger
):
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = s.get(Trade, ledger["buy_id"]).instrument
        fac.add_trade(s, acme, "2026-05-10", "sell", 100, "6.00")
        s.commit()

    resp = delete_trade(client, session_factory, ledger["buy_id"])

    # Back to the ledger, where the sell it named can be dealt with.
    assert resp.headers["location"].startswith("/holding/ACME?error=")
    with reading(session_factory) as s:
        assert s.get(Trade, ledger["buy_id"]) is not None


@freeze_time(TODAY)
def test_deleting_a_trade_keeps_the_planned_purchase_as_history(
    client, session_factory, ledger
):
    """The buy happened on schedule; deleting the trade doesn't unschedule it.
    The row stays, its link goes, and it says why — otherwise the rotation would
    silently re-offer a slot that was already filled."""
    with session_factory() as s:
        bind_to_only_portfolio(s)
        from app import tenancy

        s.add(
            tenancy.owned(
                s,
                PlannedPurchase(
                    due_date=dt.date(2026, 3, 2),
                    instrument_id=ledger["acme_id"],
                    status="done",
                    trade_id=ledger["buy_id"],
                ),
            )
        )
        s.commit()

    delete_trade(client, session_factory, ledger["buy_id"])

    with reading(session_factory) as s:
        purchase = s.scalars(select(PlannedPurchase)).one()
        assert purchase.trade_id is None
        assert purchase.status == "done"
        assert "trade removed" in purchase.note


# --------------------------------------------------------------------------- #
# DRP: the trade and the dividend are one event
# --------------------------------------------------------------------------- #

@pytest.fixture
def with_drp(client, session_factory, ledger):
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = s.get(Trade, ledger["buy_id"]).instrument
        trade, dividend = fac.add_drp(s, acme, "2026-04-15", "24.00", 4, "6.00")
        s.commit()
        return {**ledger, "drp_id": trade.id, "dividend_id": dividend.id}


@freeze_time(TODAY)
def test_a_drp_trade_cannot_be_edited_on_its_own(client, session_factory, with_drp):
    """Editing the units without the cash would leave the two halves of one
    statement line disagreeing, and the dividend income wrong."""
    resp = client.get(f"/trade/{with_drp['drp_id']}/edit", headers=HTML)

    assert resp.status_code == 409
    assert "through its dividend" in resp.text


@freeze_time(TODAY)
def test_a_drp_trade_cannot_be_deleted_on_its_own(client, session_factory, with_drp):
    resp = delete_trade(client, session_factory, with_drp["drp_id"])

    assert resp.status_code == 409
    with reading(session_factory) as s:
        assert s.get(Trade, with_drp["drp_id"]) is not None


@freeze_time(TODAY)
def test_the_ledger_points_a_drp_row_at_its_dividend(client, session_factory, with_drp):
    """The affordance has to match the rule: a DRP row's edit link goes to the
    dividend, or people meet the 409 by following the obvious path."""
    resp = client.get("/holding/ACME", headers=HTML)

    assert f"/dividend/{with_drp['dividend_id']}/edit" in resp.text
    assert f"/trade/{with_drp['drp_id']}/edit" not in resp.text


@freeze_time(TODAY)
def test_the_dividend_form_shows_units_only_for_a_reinvested_one(
    client, session_factory, with_drp, ledger
):
    """A cash distribution has no units to edit, and offering the fields would
    invite someone to invent a DRP that never happened."""
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = s.get(Instrument, ledger["acme_id"])
        cash_only = fac.add_dividend(s, acme, "2026-05-01", "40.00")
        s.commit()
        cash_id = cash_only.id

    reinvested = client.get(f"/dividend/{with_drp['dividend_id']}/edit", headers=HTML)
    plain = client.get(f"/dividend/{cash_id}/edit", headers=HTML)

    assert reinvested.status_code == 200
    assert "Units reinvested" in reinvested.text
    assert 'value="24"' in reinvested.text  # the cash
    assert plain.status_code == 200
    assert "Units reinvested" not in plain.text


@freeze_time(TODAY)
def test_a_dividend_in_another_portfolio_is_not_found(client, session_factory, ledger):
    with session_factory() as s:
        other = fac.make_portfolio(s, "Someone else")
        acme = s.get(Instrument, ledger["acme_id"])
        theirs = fac.add_dividend(s, acme, "2026-04-01", "5.00", portfolio_id=other.id)
        s.commit()
        theirs_id = theirs.id

    assert client.get(f"/dividend/{theirs_id}/edit", headers=HTML).status_code == 404


@freeze_time(TODAY)
@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"cash_amount": "lots"}, "must+be+numbers"),
        ({"cash_amount": "0"}, "more+than+zero"),
        ({"franking_credits": "-1"}, "can%27t+be+negative"),
        ({"div_date": "2027-01-01"}, "future"),
        ({"quantity": "many"}, "must+be+numbers"),
        ({"quantity": "0"}, "greater+than+zero"),
    ],
    ids=["bad-cash", "zero-cash", "negative-franking", "future", "bad-units", "zero-units"],
)
def test_the_dividend_form_validates(
    client, session_factory, with_drp, overrides, expected
):
    data = {"div_date": "2026-04-15", "cash_amount": "24.00", "franking_credits": "",
            "quantity": "4", "unit_price": "6.00", "note": "",
            "_csrf": session_csrf(session_factory)}
    data.update(overrides)

    resp = client.post(
        f"/dividend/{with_drp['dividend_id']}/edit", data=data, headers=HTML,
        follow_redirects=False,
    )

    assert expected in resp.headers["location"]
    with reading(session_factory) as s:
        # Nothing moved — and in particular the DRP trade didn't move without
        # its dividend, which is the failure mode that matters here.
        assert s.get(Dividend, with_drp["dividend_id"]).cash_amount == Decimal("24.00")
        assert s.get(Trade, with_drp["drp_id"]).quantity == Decimal("4")


@freeze_time(TODAY)
def test_growing_a_drp_is_fine_but_shrinking_it_below_a_sell_is_not(
    client, session_factory, with_drp
):
    """The reinvested units are checked against the timeline exactly as a
    trade's are — the dividend form is not a side door around the balance
    guard."""
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = s.get(Instrument, with_drp["acme_id"])
        fac.add_trade(s, acme, "2026-06-01", "sell", 104, "7.00")
        s.commit()

    data = {"div_date": "2026-04-15", "cash_amount": "24.00", "franking_credits": "",
            "quantity": "2", "unit_price": "6.00", "note": "",
            "_csrf": session_csrf(session_factory)}
    resp = client.post(
        f"/dividend/{with_drp['dividend_id']}/edit", data=data, headers=HTML,
        follow_redirects=False,
    )

    assert "error=" in resp.headers["location"]
    assert "sell+of+104+units" in resp.headers["location"]
    with reading(session_factory) as s:
        assert s.get(Trade, with_drp["drp_id"]).quantity == Decimal("4")


@freeze_time(TODAY)
def test_editing_a_dividend_moves_both_halves(client, session_factory, with_drp):
    resp = client.post(
        f"/dividend/{with_drp['dividend_id']}/edit",
        data={"div_date": "2026-04-20", "cash_amount": "30.00", "franking_credits": "",
              "quantity": "5", "unit_price": "6.00", "note": "corrected",
              "_csrf": session_csrf(session_factory)},
        headers=HTML,
        follow_redirects=False,
    )

    assert resp.headers["location"] == "/holding/ACME"
    with reading(session_factory) as s:
        dividend = s.get(Dividend, with_drp["dividend_id"])
        trade = s.get(Trade, with_drp["drp_id"])
        assert dividend.cash_amount == Decimal("30.00")
        assert dividend.date == dt.date(2026, 4, 20)
        # The units followed the cash to the new date.
        assert trade.quantity == Decimal("5")
        assert trade.date == dt.date(2026, 4, 20)


@freeze_time(TODAY)
def test_deleting_a_dividend_takes_its_reinvested_units_with_it(
    client, session_factory, with_drp
):
    resp = client.post(
        f"/dividend/{with_drp['dividend_id']}/delete",
        data={"_csrf": session_csrf(session_factory)},
        headers=HTML,
        follow_redirects=False,
    )

    assert resp.headers["location"] == "/holding/ACME"
    with reading(session_factory) as s:
        assert s.get(Dividend, with_drp["dividend_id"]) is None
        assert s.get(Trade, with_drp["drp_id"]) is None
        # The ordinary buy is untouched.
        assert s.get(Trade, with_drp["buy_id"]) is not None


@freeze_time(TODAY)
def test_deleting_a_dividend_is_refused_when_its_units_were_sold(
    client, session_factory, with_drp
):
    """The reinvested units are real units — if they have been sold, removing
    them strands the sell."""
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = s.get(Trade, with_drp["buy_id"]).instrument
        fac.add_trade(s, acme, "2026-06-01", "sell", 104, "7.00")
        s.commit()

    resp = client.post(
        f"/dividend/{with_drp['dividend_id']}/delete",
        data={"_csrf": session_csrf(session_factory)},
        headers=HTML,
        follow_redirects=False,
    )

    assert resp.headers["location"].startswith("/holding/ACME?error=")
    with reading(session_factory) as s:
        assert s.get(Dividend, with_drp["dividend_id"]) is not None
        assert s.get(Trade, with_drp["drp_id"]) is not None


@freeze_time(TODAY)
def test_a_plain_dividend_edits_without_units(client, session_factory, ledger):
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = s.get(Trade, ledger["buy_id"]).instrument
        dividend = fac.add_dividend(s, acme, "2026-04-15", "40.00")
        s.commit()
        dividend_id = dividend.id

    resp = client.post(
        f"/dividend/{dividend_id}/edit",
        data={"div_date": "2026-04-15", "cash_amount": "45.00",
              "franking_credits": "19.28", "note": "",
              "_csrf": session_csrf(session_factory)},
        headers=HTML,
        follow_redirects=False,
    )

    assert resp.headers["location"] == "/holding/ACME"
    with reading(session_factory) as s:
        dividend = s.get(Dividend, dividend_id)
        assert dividend.cash_amount == Decimal("45.00")
        assert dividend.franking_credits == Decimal("19.28")


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_a_viewer_cannot_edit_or_delete(client, session_factory, ledger):
    """Viewers read everything and change nothing — including through a URL
    they were never shown."""
    with reading(session_factory) as s:
        s.scalars(select(PortfolioMember)).one().role = "viewer"
        s.commit()

    assert client.get(f"/trade/{ledger['buy_id']}/edit", headers=HTML).status_code == 403
    assert delete_trade(client, session_factory, ledger["buy_id"]).status_code == 403
    with reading(session_factory) as s:
        assert s.get(Trade, ledger["buy_id"]) is not None


@freeze_time(TODAY)
def test_the_ledger_shows_no_edit_links_to_a_viewer(client, session_factory, ledger):
    with reading(session_factory) as s:
        s.scalars(select(PortfolioMember)).one().role = "viewer"
        s.commit()

    resp = client.get("/holding/ACME", headers=HTML)

    assert resp.status_code == 200
    assert "/edit" not in resp.text


@freeze_time(TODAY)
def test_edit_and_delete_need_a_csrf_token(client, session_factory, ledger):
    for url in (f"/trade/{ledger['buy_id']}/edit", f"/trade/{ledger['buy_id']}/delete"):
        resp = client.post(url, data={"type": "buy", "trade_date": "2026-03-02",
                                      "quantity": "1", "unit_price": "1"},
                           headers=HTML, follow_redirects=False)
        assert resp.status_code == 403, url
    with reading(session_factory) as s:
        assert s.get(Trade, ledger["buy_id"]).quantity == Decimal("100")


# Going back where you came from: carried through the form, never read from
# `Referer` — decisions.md #40.

def test_the_edit_page_carries_a_return_target(client, session_factory, ledger):
    trade_id = ledger["buy_id"]

    page = client.get(f"/trade/{trade_id}/edit?return=/holdings", headers=HTML).text

    assert 'name="return_to" value="/holdings"' in page


def test_deleting_goes_back_there(client, session_factory, ledger):
    trade_id = ledger["buy_id"]

    resp = client.post(f"/trade/{trade_id}/delete",
                       data={"_csrf": session_csrf(session_factory),
                             "return_to": "/holdings"},
                       headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/holdings"


def test_without_one_it_falls_back_to_the_instrument(client, session_factory, ledger):
    """Not the dashboard: the instrument's own page is nearer to where they
    were, and it is where the rest of that trade's history is."""
    trade_id = ledger["buy_id"]

    resp = client.post(f"/trade/{trade_id}/delete",
                       data={"_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.headers["location"].startswith("/holding/")


@pytest.mark.parametrize("hostile", [
    "//evil.test",              # protocol-relative: a browser leaves the site
    "/\\evil.test",             # some browsers treat this the same way
    "https://evil.test",
    "javascript:alert(1)",
    "",
])
def test_a_hostile_return_target_is_refused(client, session_factory, ledger, hostile):
    """It ends up in a Location header, so it is an open-redirect surface.
    "Starts with a slash" is not enough — `//evil.test` does too."""
    trade_id = ledger["buy_id"]

    resp = client.post(f"/trade/{trade_id}/delete",
                       data={"_csrf": session_csrf(session_factory),
                             "return_to": hostile},
                       headers=HTML, follow_redirects=False)

    assert resp.headers["location"].startswith("/holding/"), (
        f"{hostile!r} was honoured as a redirect target")
