"""Removing a holding, and what stops it.

The requirement: you must be able to remove a holding added by
accident, it must not have any trades to be removed … if I hover over the
delete button it should tell me why."

Two things make this more than a delete button.

  * **Instruments are shared.** The list, and the price history with it, belongs
    to everyone using the tracker. So removal *deactivates* rather than deletes,
    and the "is anything recorded against it" question has to be asked of every
    portfolio — not just the one asking.
  * **But the answer must not leak.** The panel lists the asking portfolio's own
    trades, with links; anything belonging to somebody else is counted and never
    shown.
"""

from __future__ import annotations

import re

from sqlalchemy import select

import factories as fac
from app.models import Instrument
from test_routes import bind_to_only_portfolio, make_login, session_csrf

HTML = {"accept": "text/html"}


def _page(client) -> str:
    response = client.get("/holdings", headers=HTML)
    assert response.status_code == 200
    return response.text


def test_an_untraded_instrument_offers_a_remove_button(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        fac.make_instrument(s, "SPARE")
        s.commit()

    page = _page(client)

    assert re.search(r'action="/holdings/\d+/remove"', page), "no Remove control"


def test_removing_it_stops_it_being_tracked(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        inst = fac.make_instrument(s, "SPARE")
        s.commit()
        instrument_id = inst.id

    client.post(f"/holdings/{instrument_id}/remove",
                data={"_csrf": session_csrf(session_factory)},
                headers=HTML, follow_redirects=True)

    with session_factory() as s:
        assert s.get(Instrument, instrument_id).active is False


def test_it_is_deactivated_rather_than_deleted(client, session_factory):
    """The row and its price history are shared. Deleting would take that
    history from anybody who later re-adds the same ticker."""
    make_login(client, session_factory)
    with session_factory() as s:
        inst = fac.make_instrument(s, "SPARE")
        fac.add_price(s, inst, "2026-01-05", "10.00")
        s.commit()
        instrument_id = inst.id

    client.post(f"/holdings/{instrument_id}/remove",
                data={"_csrf": session_csrf(session_factory)},
                headers=HTML, follow_redirects=True)

    with session_factory() as s:
        assert s.get(Instrument, instrument_id) is not None, "the row was deleted"
        from app.models import Price
        assert s.scalars(select(Price).where(
            Price.instrument_id == instrument_id)).all(), "price history was lost"


def test_an_instrument_with_trades_shows_what_is_blocking_it(client, session_factory):
    """Not a disabled button — one that opens and says why, with a link to each
    thing in the way. A `disabled` button cannot be hovered or focused in most
    browsers, so its explanation is unreachable exactly when it is wanted."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "HELD")
        fac.add_trade(s, inst, "2026-01-05", "buy", 10, "5.00")
        s.commit()

    page = _page(client)
    row = page[page.index("HELD"):]

    assert "blockedby" in row, "no explanation panel"
    assert re.search(r'/trade/\d+/edit\?return=/holdings', row), (
        "the panel does not link to the trade in the way")


def test_the_blocked_row_offers_no_remove_form(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "HELD")
        fac.add_trade(s, inst, "2026-01-05", "buy", 10, "5.00")
        s.commit()
        instrument_id = inst.id

    page = _page(client)

    assert f'action="/holdings/{instrument_id}/remove"' not in page


def test_the_route_refuses_it_too_not_just_the_page(client, session_factory):
    """The button being absent is presentation. A stale tab, or anybody
    posting directly, must hit the same wall."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "HELD")
        fac.add_trade(s, inst, "2026-01-05", "buy", 10, "5.00")
        s.commit()
        instrument_id = inst.id

    resp = client.post(f"/holdings/{instrument_id}/remove",
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML)

    assert resp.status_code == 409
    with session_factory() as s:
        assert s.get(Instrument, instrument_id).active is True


def test_a_dividend_blocks_it_as_well_as_a_trade(client, session_factory):
    """A dividend refers to the instrument too, and leaving one pointing at
    something no longer tracked is the same broken ledger."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "PAYER")
        fac.add_dividend(s, inst, "2026-02-01", "12.34")
        s.commit()
        instrument_id = inst.id

    resp = client.post(f"/holdings/{instrument_id}/remove",
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML)

    assert resp.status_code == 409


def test_the_page_shows_a_price_even_for_something_no_longer_held(client,
                                                                  session_factory):
    """The reason: a position you exited is still one you may be watching,
    and the dashboard deliberately hides it."""
    make_login(client, session_factory)
    with session_factory() as s:
        inst = fac.make_instrument(s, "GONE")
        fac.add_price(s, inst, "2026-01-05", "41.00")
        fac.add_price(s, inst, "2026-02-05", "42.50")
        s.commit()

    page = _page(client)

    assert "42.50" in page, "the latest close is not shown"
    assert "41.00" not in page, "an older close is being shown instead of the latest"
