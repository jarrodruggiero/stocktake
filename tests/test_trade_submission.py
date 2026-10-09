"""One trade per form, however many times Save is pressed.

A test user pressed Save several times while a save waited on the price
provider, and every press recorded a trade. The form now carries an id minted
when it is rendered: the first POST claims it, and a repeat gets the first
one's answer instead of a second trade.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from sqlalchemy import select

import factories as fac
from app import submissions
from app.models import Trade
from test_routes import bind_to_only_portfolio, make_login, reading, session_csrf

HTML = {"accept": "text/html"}


@pytest.fixture
def acme(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "ACME")
        fac.hold(s, inst)
        s.commit()
        return inst.id


def _id_on_the_form(client) -> str:
    page = client.get("/trade/new", headers=HTML)
    found = re.search(r'name="submission" value="([^"]+)"', page.text)
    assert found, "the trade form carries no submission id"
    return found.group(1)


def _save(client, session_factory, instrument_id, submission, **overrides):
    data = {"_csrf": session_csrf(session_factory), "instrument_id": str(instrument_id),
            "type": "buy", "trade_date": "2026-03-02", "quantity": "10",
            "unit_price": "5.00", "brokerage": "9.50", "submission": submission}
    data.update(overrides)
    return client.post("/trade/new", data=data, headers=HTML, follow_redirects=False)


def _trades(session_factory) -> int:
    with reading(session_factory) as s:
        return len(s.scalars(select(Trade)).all())


def test_each_rendering_of_the_form_gets_its_own_id(client, acme):
    first, second = _id_on_the_form(client), _id_on_the_form(client)
    assert first != second
    assert submissions.valid(first) and submissions.valid(second)


def test_pressing_save_twice_records_one_trade(client, session_factory, acme):
    submission = _id_on_the_form(client)

    first = _save(client, session_factory, acme, submission)
    second = _save(client, session_factory, acme, submission)

    assert first.status_code == second.status_code == 303
    assert second.headers["location"] == first.headers["location"] == "/holding/ACME"
    assert _trades(session_factory) == 1


def test_a_refused_save_can_be_corrected_on_the_same_form(client, session_factory, acme):
    """The id is spent by a trade, not by an attempt: the refusal re-renders
    the form, and saving it once corrected must still work."""
    submission = _id_on_the_form(client)

    refused = _save(client, session_factory, acme, submission, quantity="0")
    corrected = _save(client, session_factory, acme, submission)

    assert refused.status_code == 200 and "greater than zero" in refused.text
    assert corrected.status_code == 303
    assert _trades(session_factory) == 1


def test_two_forms_record_two_trades(client, session_factory, acme):
    _save(client, session_factory, acme, _id_on_the_form(client))
    _save(client, session_factory, acme, _id_on_the_form(client))
    assert _trades(session_factory) == 2


@pytest.mark.parametrize("submission", ["", "x" * 5000, "not an id", "İ" * 40])
def test_an_id_that_is_not_one_is_ignored(client, session_factory, acme, submission):
    """Without a usable id the save is unguarded, as it was before; it must not
    fail, and nothing it sent is kept."""
    resp = _save(client, session_factory, acme, submission)
    assert resp.status_code == 303
    assert _trades(session_factory) == 1
    assert submission not in submissions._claims


def test_a_repeat_during_a_slow_save_waits_for_its_answer():
    """A double click sends the second POST while the first still waits on the
    provider. It must land where the first does, not record or guess."""
    async def scenario():
        token = submissions.mint()
        assert await submissions.claim(token) is None          # the first: go ahead
        repeat = asyncio.ensure_future(submissions.claim(token, wait=2.0))
        await asyncio.sleep(0.05)
        assert not repeat.done(), "the repeat should wait for the first"
        submissions.finish(token, "/holding/ACME")
        return await repeat
    assert asyncio.run(scenario()) == "/holding/ACME"


def test_a_repeat_after_a_failed_save_may_try_again():
    """A save that failed outright gives its id back."""
    async def scenario():
        token = submissions.mint()
        assert await submissions.claim(token) is None
        submissions.release(token)
        return await submissions.claim(token)
    assert asyncio.run(scenario()) is None
