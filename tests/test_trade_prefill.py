"""What the trade form offers for price and FX, and why it offers nothing.

The form used to fill both from the LATEST stored close and rate, whatever date
the trade was on. For a trade recorded today that is right by accident; for a
backdated one it is wrong twice over — and the FX half is wrong *permanently*,
because the price feed's repair pass only fills `fx_rate IS NULL`, so a
prefilled wrong rate is never revisited (issue #35).

The rule these tests pin: **offer only what is known for that date, and leave
the rest empty.** An empty FX field is repairable by the feed. A plausible
wrong one is not, which makes guessing strictly worse than declining — the same
reasoning as decisions.md #5, arrived at from the other direction.

Note the deliberate asymmetry between the two lookups, which
`test_a_price_is_never_taken_from_after_the_trade_date` and its FX counterpart
exist to hold in place:

  * **price** — on or before, or nothing. It is primary data; the person has a
    contract note in front of them and a blank field asks them to read it.
  * **FX** — on or before, or nothing, and never the earliest-later rate that
    `FxBook.rate` would hand a *report*. A report must produce a figure or omit
    the holding; a form has the third option of saying "you type it".
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

import factories as fac
from app import main as main_mod
from app import providers, queries
from app.models import Trade
from test_routes import (
    bind_to_only_portfolio,
    make_login,
    reading,
    session_csrf,
)

HTML = {"accept": "text/html"}


def _provider_rows(rows, *, ok: bool = True) -> providers.Fetch:
    """A `providers.Fetch` the way a real one comes back. `source` is what
    `ok` reads, so a refusal is `source=None` rather than a flag."""
    return providers.Fetch(rows=list(rows), source="test" if ok else None)


@pytest.fixture
def acme(pf):
    inst = fac.make_instrument(pf, "ACME", asset_class="share")
    # Friday, Monday. Nothing on the Saturday or Sunday between them, which is
    # what a trade dated on a weekend has to fall back through.
    fac.add_prices(pf, inst, [("2026-09-18", "4.00"), ("2026-09-21", "4.50")])
    return inst


# --------------------------------------------------------------------------- #
# The price lookup
# --------------------------------------------------------------------------- #

def test_the_close_on_the_exact_date_is_used(pf, acme):
    found = queries.close_on_or_before(pf, acme.id, dt.date(2026, 9, 18))

    assert found == (Decimal("4.00"), dt.date(2026, 9, 18))


def test_a_non_trading_day_falls_back_to_the_last_close_before_it(pf, acme):
    """Saturday: the Friday close, and the date it is actually for.

    The date comes back so the form can say which day it used rather than
    implying the market traded on a Saturday.
    """
    found = queries.close_on_or_before(pf, acme.id, dt.date(2026, 9, 19))

    assert found == (Decimal("4.00"), dt.date(2026, 9, 18))


def test_a_price_is_never_taken_from_after_the_trade_date(pf, acme):
    """Before the stored history: nothing, NOT the earliest close.

    Offering the 2026 close for a 2019 trade would be a fabrication dressed as
    help, and the person can read the real number off their contract note.
    """
    assert queries.close_on_or_before(pf, acme.id, dt.date(2019, 1, 1)) is None


def test_an_instrument_with_no_prices_at_all_offers_nothing(pf):
    bare = fac.make_instrument(pf, "WIDGET", asset_class="share")

    assert queries.close_on_or_before(pf, bare.id, dt.date(2026, 9, 21)) is None


# --------------------------------------------------------------------------- #
# The FX lookup
# --------------------------------------------------------------------------- #

def test_the_rate_on_the_exact_date_is_used(pf):
    fac.add_fx(pf, "USDAUD", "2026-09-18", "1.50")

    found = queries.fx_on_or_before(pf, "USD", dt.date(2026, 9, 18))

    assert found == (Decimal("1.500000"), dt.date(2026, 9, 18))


def test_a_rate_falls_back_to_the_last_one_before_it(pf):
    fac.add_fx(pf, "USDAUD", "2020-01-02", "1.40")
    fac.add_fx(pf, "USDAUD", "2026-09-22", "1.50")

    found = queries.fx_on_or_before(pf, "USD", dt.date(2020, 6, 1))

    assert found == (Decimal("1.400000"), dt.date(2020, 1, 2))


def test_a_rate_is_never_taken_from_after_the_trade_date(pf):
    """The defect this feature was written for, stated as a rule.

    `FxBook.rate` deliberately reaches FORWARD to the earliest stored rate when
    a report asks about a date before the series — a few days out beats
    omitting the holding. A form must not: the value it offers gets WRITTEN,
    and a written rate is one the repair pass will never correct.
    """
    fac.add_fx(pf, "USDAUD", "2026-09-22", "1.50")

    assert queries.fx_on_or_before(pf, "USD", dt.date(2020, 1, 2)) is None
    # The contrast, so this test fails if the two ever converge.
    assert queries.FxBook(pf).rate("USD", dt.date(2020, 1, 2)) == Decimal("1.500000")


def test_the_reporting_currency_needs_no_rate(pf):
    """AUD declines even when an AUDAUD row exists to be found.

    The row is what makes this a test. Without it the function returns None
    because the query misses, so the early return could be deleted and nothing
    would notice — which is exactly what the first version of this test did.
    """
    fac.add_fx(pf, "AUDAUD", "2026-09-21", "1.00")

    assert queries.fx_on_or_before(pf, "AUD", dt.date(2026, 9, 21)) is None


# --------------------------------------------------------------------------- #
# The endpoint the form calls
# --------------------------------------------------------------------------- #

def test_the_endpoint_answers_with_the_price_and_the_date_it_is_for(
    client, session_factory
):
    make_login(client, session_factory)
    with session_factory() as s:
        inst = fac.make_instrument(s, "ACME", asset_class="share")
        fac.add_prices(s, inst, [("2026-09-18", "4.00")])
        s.commit()
        instrument_id = inst.id

    body = client.get(
        f"/holdings/price?instrument={instrument_id}&date=2026-09-19"
    ).json()

    assert body["price"] == "4"
    assert body["as_at"] == "2026-09-18"
    assert body["fx"] is None


def test_the_endpoint_answers_with_fx_for_a_foreign_instrument(
    client, session_factory
):
    make_login(client, session_factory)
    with session_factory() as s:
        inst = fac.make_instrument(s, "VERTEX", exchange="NASDAQ",
                                   asset_class="share", currency="USD")
        fac.add_prices(s, inst, [("2026-09-18", "10.00")])
        fac.add_fx(s, "USDAUD", "2026-09-18", "1.50")
        s.commit()
        instrument_id = inst.id

    body = client.get(
        f"/holdings/price?instrument={instrument_id}&date=2026-09-18"
    ).json()

    assert body["price"] == "10"
    assert body["fx"] == "1.5"
    assert body["fx_as_at"] == "2026-09-18"


def test_the_endpoint_says_nothing_rather_than_guessing(client, session_factory):
    """No price for that date: empty fields, not the nearest thing available."""
    make_login(client, session_factory)
    with session_factory() as s:
        inst = fac.make_instrument(s, "ACME", asset_class="share")
        fac.add_prices(s, inst, [("2026-09-18", "4.00")])
        s.commit()
        instrument_id = inst.id

    body = client.get(
        f"/holdings/price?instrument={instrument_id}&date=2019-01-01"
    ).json()

    assert body["price"] is None
    assert body["as_at"] is None


def test_the_endpoint_needs_a_session(client, session_factory):
    with session_factory() as s:
        inst = fac.make_instrument(s, "ACME", asset_class="share")
        s.commit()
        instrument_id = inst.id

    answer = client.get(
        f"/holdings/price?instrument={instrument_id}&date=2026-09-18",
        headers=HTML, follow_redirects=False,
    )

    assert answer.status_code in (302, 303, 307, 401)


def test_the_endpoint_rejects_a_date_that_is_not_a_date(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        inst = fac.make_instrument(s, "ACME", asset_class="share")
        s.commit()
        instrument_id = inst.id

    answer = client.get(
        f"/holdings/price?instrument={instrument_id}&date=not-a-date", headers=HTML
    )

    assert answer.status_code == 400


# --------------------------------------------------------------------------- #
# An instrument that does not exist yet
# --------------------------------------------------------------------------- #
# The case the date-driven prefill could not serve, and the one most likely to
# be met: recording the FIRST trade for something, created inline by the form.
# There is no local history for it — the feed has not run — so the stored-close
# lookup has nothing, and the field stayed empty.
#
# The ticker has already gone to the provider by this point: the name, currency
# and symbol on the same form came from `pricefeed.lookup`. So asking that same
# provider for a close on the date is no new disclosure, and it is gated on
# `price_feed.enabled` like everything else that reaches the network.

def test_a_symbol_not_yet_in_the_catalogue_can_be_priced(
    client, session_factory, monkeypatch
):
    make_login(client, session_factory)
    asked = {}

    def _fetch(kind, symbol, start, end):
        asked.update(kind=kind, symbol=symbol, start=start, end=end)
        return _provider_rows([(dt.date(2024, 9, 23), Decimal("29.31"))])

    monkeypatch.setattr(main_mod.providers, "fetch", _fetch)
    monkeypatch.setattr(main_mod.settings.price_feed, "enabled", True)

    body = client.get("/holdings/price?symbol=ANZ.AX&date=2024-09-23").json()

    assert body["price"] == "29.31"
    assert body["as_at"] == "2024-09-23"
    assert asked["symbol"] == "ANZ.AX"
    # A short window, not the whole history: one close is all the form wants,
    # and a weekend or a holiday is the only reason to look back at all.
    assert (asked["end"] - asked["start"]).days <= 14


def test_a_symbol_lookup_reaches_nothing_when_the_feed_is_off(
    client, session_factory, monkeypatch
):
    """`price_feed.enabled: false` is a promise about the network."""
    make_login(client, session_factory)

    def _fetch(*_args, **_kwargs):
        raise AssertionError("reached the provider with the feed disabled")

    monkeypatch.setattr(main_mod.providers, "fetch", _fetch)
    monkeypatch.setattr(main_mod.settings.price_feed, "enabled", False)

    body = client.get("/holdings/price?symbol=ANZ.AX&date=2024-09-23").json()

    assert body["price"] is None


def test_a_symbol_the_provider_cannot_serve_offers_nothing(
    client, session_factory, monkeypatch
):
    """A failed fetch is an empty field, not a 500 on somebody's trade form."""
    make_login(client, session_factory)
    monkeypatch.setattr(main_mod.providers, "fetch",
                        lambda *a, **k: _provider_rows([], ok=False))
    monkeypatch.setattr(main_mod.settings.price_feed, "enabled", True)

    answer = client.get("/holdings/price?symbol=NOSUCH.AX&date=2024-09-23")

    assert answer.status_code == 200
    assert answer.json()["price"] is None


def test_the_endpoint_needs_an_instrument_or_a_symbol(client, session_factory):
    make_login(client, session_factory)

    assert client.get("/holdings/price?date=2024-09-23",
                      headers=HTML).status_code == 400


def test_the_endpoint_404s_on_an_unknown_instrument(client, session_factory):
    make_login(client, session_factory)

    answer = client.get("/holdings/price?instrument=99999&date=2026-09-18",
                        headers=HTML)

    assert answer.status_code == 404


def test_the_picker_carries_no_price_of_its_own(client, session_factory):
    """The stale source stays gone.

    Each <option> used to carry its instrument's LATEST close and rate, and
    picking one wrote them into the form — which is the bug, for any trade not
    dated today. They were removed rather than corrected, because a field with
    two sources eventually takes the wrong one again. The currency stays: it
    decides whether the FX field applies, and that needs no round trip.
    """
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "ACME", asset_class="share")
        fac.add_prices(s, inst, [("2026-09-18", "4.00")])
        fac.add_fx(s, "USDAUD", "2026-09-18", "1.50")
        s.commit()

    html = client.get("/trade/new", headers=HTML).text

    assert "data-price" not in html
    assert "data-fx" not in html
    assert 'data-currency="AUD"' in html


# --------------------------------------------------------------------------- #
# A price of zero
# --------------------------------------------------------------------------- #
# Refused until now, which made a genuinely free parcel unrecordable: a bonus
# issue, a demerger allocation, or an employer's reward-plan grant. There is no
# CHECK constraint to relax — `quantity > 0` is enforced in the database,
# `unit_price` never was, and the importers have always accepted zero. Only the
# forms disagreed.
#
# Zero is NOT the right cost base for an employee share scheme, which is what
# prompted the issue: the ATO resets it to market value at the taxing point.
# That belongs in the guide, not in a validator — the app takes the number it
# is given (docs/about/disclaimer.md).

def _record(client, session_factory, instrument_id, **overrides):
    data = {
        "instrument_id": str(instrument_id), "type": "buy",
        "trade_date": "2026-07-01", "quantity": "10", "unit_price": "0",
        "brokerage": "0", "_csrf": session_csrf(session_factory),
    }
    data.update(overrides)
    return client.post("/trade/new", data=data, headers=HTML,
                       follow_redirects=False)


def test_a_free_parcel_can_be_recorded(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "ACME", asset_class="share")
        s.commit()
        instrument_id = inst.id

    resp = _record(client, session_factory, instrument_id)

    assert resp.status_code == 303
    with reading(session_factory) as s:
        assert s.scalars(select(Trade)).one().unit_price == 0


def test_a_negative_price_is_still_refused(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "ACME", asset_class="share")
        s.commit()
        instrument_id = inst.id

    resp = _record(client, session_factory, instrument_id, unit_price="-1")

    # Create re-renders the form with the message rather than redirecting to
    # it, which is why this reads the body where the edit test reads Location.
    assert resp.status_code == 200
    assert "can&#39;t be negative" in resp.text
    with reading(session_factory) as s:
        assert s.scalars(select(Trade)).all() == []


def test_zero_units_is_still_refused(client, session_factory):
    """The other half of the old combined check, kept separate on purpose."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "ACME", asset_class="share")
        s.commit()
        instrument_id = inst.id

    resp = _record(client, session_factory, instrument_id, quantity="0")

    assert resp.status_code == 200
    assert "Units must be greater than zero" in resp.text
    with reading(session_factory) as s:
        assert s.scalars(select(Trade)).all() == []


def test_an_existing_trade_can_be_edited_down_to_free(client, session_factory):
    """Edit has to agree with create, or the strict form becomes the lenient
    one the moment somebody corrects a row."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        inst = fac.make_instrument(s, "ACME", asset_class="share")
        fac.add_trade(s, inst, "2026-07-01", "buy", 10, "4.00")
        s.commit()
        trade_id = s.scalars(select(Trade)).one().id

    resp = client.post(
        f"/trade/{trade_id}/edit",
        data={"type": "buy", "trade_date": "2026-07-01", "quantity": "10",
              "unit_price": "0", "brokerage": "0", "fx_rate": "", "note": "",
              "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False,
    )

    assert "error=" not in resp.headers["location"]
    with reading(session_factory) as s:
        assert s.get(Trade, trade_id).unit_price == 0


# The API carries the same rule; its test lives beside the other API tests,
# with the key-minting helpers — see `test_api.py`.
