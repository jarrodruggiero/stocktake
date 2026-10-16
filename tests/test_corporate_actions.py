"""Splits, consolidations, returns of capital and AMIT cost base adjustments.

None could be recorded. A split typed as a free purchase restarted the
twelve-month discount clock on every unit; a return of capital or an AMIT
adjustment could not reach the cost base at all. Each is now an event on the
holding, and every figure built from its trades takes it in, in date order,
from the start of its day (decisions.md #146).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from freezegun import freeze_time

import factories as fac
from app import exports, fyreport, queries

TODAY = "2026-08-03"


@pytest.fixture
def acme(pf):
    return fac.make_instrument(pf, "ACME", asset_class="share")


def _held(pf, inst):
    """This instrument's holding, built as the dashboard builds it."""
    [holding] = [h for h in queries.all_holdings(pf) if h.instrument.id == inst.id]
    return holding


# --------------------------------------------------------------------------- #
# Splits and consolidations
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_a_split_multiplies_the_units_and_keeps_the_cost(pf, acme):
    fac.add_trade(pf, acme, "2024-01-02", "buy", 100, "10.00")
    fac.add_action(pf, acme, "2025-01-02", "split", ratio=(2, 1))
    fac.add_prices(pf, acme, [("2026-08-03", "6.00")])

    holding = _held(pf, acme)

    assert (holding.units, holding.cost_aud, holding.value_aud, holding.gain_aud) == (
        200, 1000, 1200, 200)
    assert holding.avg_price == 5     # 1,000 paid over the 200 units it is now


@freeze_time(TODAY)
def test_a_split_keeps_each_parcels_date(pf, acme):
    """A split is not a purchase: the units are the same parcel, so the
    discount clock runs from when it was bought."""
    fac.add_trade(pf, acme, "2024-01-02", "buy", 100, "10.00")
    fac.add_action(pf, acme, "2024-06-03", "split", ratio=(2, 1))
    fac.add_trade(pf, acme, "2025-02-03", "sell", 200, "7.00")

    [sale] = fyreport.instrument_disposals(acme, queries.FxBook(pf))
    [parcel] = sale.parcels

    assert (parcel.acquired, parcel.quantity, parcel.cost_base, parcel.discountable) == (
        dt.date(2024, 1, 2), 200, Decimal("1000.00"), True)
    assert sale.gain == Decimal("400.00")


@freeze_time(TODAY)
def test_a_consolidation_rounds_the_holding_once_and_its_parcels_add_up(pf, acme):
    """1 for 3 of 100 units is 33.33333333, the column's last place, and the
    parcels share exactly that, so selling it all leaves nothing behind."""
    for day, units in (("2024-01-02", 30), ("2024-02-01", 30), ("2024-03-01", 40)):
        fac.add_trade(pf, acme, day, "buy", units, "3.00")
    fac.add_action(pf, acme, "2025-01-02", "split", ratio=(1, 3))

    held = fyreport.walk(acme, queries.FxBook(pf)).held
    assert sum(p.remaining for p in held) == Decimal("33.33333333")

    fac.add_trade(pf, acme, "2025-02-03", "sell", "33.33333333", "9.00")
    pf.expire_all()
    assert fyreport.walk(acme, queries.FxBook(pf)).held == []
    assert _held(pf, acme).units == 0


@freeze_time(TODAY)
def test_a_consolidation_is_shared_out_not_rounded_parcel_by_parcel(pf, acme):
    """Three parcels of 1, consolidated 1 for 3: a third of a unit each, which
    rounded one by one is 0.99999999 and leaves the whole unit unsellable."""
    for day in ("2024-01-02", "2024-02-01", "2024-03-01"):
        fac.add_trade(pf, acme, day, "buy", 1, "3.00")
    fac.add_action(pf, acme, "2025-01-02", "split", ratio=(1, 3))
    fac.add_trade(pf, acme, "2025-02-03", "sell", 1, "9.00")

    [sale] = fyreport.instrument_disposals(acme, queries.FxBook(pf))

    assert sum(p.quantity for p in sale.parcels) == 1
    assert sale.cost_base == Decimal("9.00")


def test_a_sale_after_a_split_is_held_to_the_split_units(pf, acme):
    fac.add_trade(pf, acme, "2024-01-02", "buy", 100, "10.00")
    split = fac.add_action(pf, acme, "2025-01-02", "split", ratio=(2, 1))
    sale = fac.build_trade(acme, "2025-02-03", "sell", 250, "6.00")

    breach = queries.balance_breach(list(acme.trades), sale, actions=[split])

    assert (breach.held_before, breach.balance) == (200, -50)
    assert queries.balance_breach(list(acme.trades), fac.build_trade(
        acme, "2025-02-03", "sell", 200, "6.00"), actions=[split]) is None


def test_a_split_on_the_day_of_a_trade_comes_first(pf, acme):
    """It takes effect from the start of its day: a sale that day is in the
    new units."""
    fac.add_trade(pf, acme, "2024-01-02", "buy", 100, "10.00")
    split = fac.add_action(pf, acme, "2025-01-02", "split", ratio=(2, 1))

    assert queries.balance_breach(list(acme.trades), fac.build_trade(
        acme, "2025-01-02", "sell", 200, "6.00"), actions=[split]) is None


# --------------------------------------------------------------------------- #
# Returns of capital and cost base adjustments
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_a_return_of_capital_lowers_each_parcels_cost_by_its_share(pf, acme):
    """$100 back on 200 units held in two parcels of 100: 50 off each."""
    fac.add_trade(pf, acme, "2023-01-03", "buy", 100, "10.00")
    fac.add_trade(pf, acme, "2024-01-02", "buy", 100, "12.00")
    fac.add_action(pf, acme, "2025-01-02", "capital_return", amount="100.00")
    fac.add_trade(pf, acme, "2025-02-03", "sell", 100, "11.00")

    [sale] = fyreport.instrument_disposals(acme, queries.FxBook(pf))

    assert sale.cost_base == Decimal("950.00")
    assert _held(pf, acme).cost_aud == Decimal("1150")


@freeze_time(TODAY)
def test_a_return_beyond_the_cost_base_is_a_capital_gain(pf, acme):
    """CGT event G1: what comes back beyond a parcel's cost base is a gain on
    the day, discountable on a parcel held over a year, and the cost base is
    nil from then on."""
    fac.add_trade(pf, acme, "2024-01-02", "buy", 100, "1.00")
    fac.add_action(pf, acme, "2025-03-03", "capital_return", amount="150.00")

    cgt = fyreport.fy_cgt(pf, 2025)
    [gain] = cgt.disposals

    assert (gain.event, gain.date, gain.gain) == (
        "capital_return", dt.date(2025, 3, 3), Decimal("50.00"))
    assert (cgt.gains_discountable, cgt.net_capital_gain) == (50, 25)
    assert _held(pf, acme).cost_aud == 0


@freeze_time(TODAY)
def test_a_gain_beyond_the_cost_base_says_what_it_was(client, session_factory):
    """On the FY page and in the realised gains export: nothing was sold."""
    from test_routes import bind_to_only_portfolio, make_login

    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME", asset_class="share")
        fac.add_trade(s, acme, "2024-01-02", "buy", 100, "1.00")
        fac.add_action(s, acme, "2025-03-03", "capital_return", amount="150.00")
        s.commit()
        headers, rows = exports.realised_cgt(s, fy=2025)

    page = client.get("/?fy=2025", headers={"accept": "text/html"}).text

    assert dict(zip(headers, rows[0]))["Event"] == "return of capital"
    assert '<span class="sub">return of capital</span>' in page


@freeze_time(TODAY)
def test_an_amit_increase_raises_the_cost_base_and_a_decrease_lowers_it(pf, acme):
    fac.add_trade(pf, acme, "2024-01-02", "buy", 100, "10.00")
    fac.add_action(pf, acme, "2024-06-30", "cost_base", amount="30.00")
    fac.add_action(pf, acme, "2025-06-30", "cost_base", amount="-10.00")

    assert _held(pf, acme).cost_aud == Decimal("1020")


@freeze_time(TODAY)
def test_a_foreign_return_of_capital_is_converted_at_its_own_rate(pf):
    nova = fac.make_instrument(pf, "NOVA", exchange="NASDAQ", currency="USD")
    fac.add_trade(pf, nova, "2024-01-02", "buy", 100, "10.00", fx_rate="1.50")
    fac.add_action(pf, nova, "2025-01-02", "capital_return", amount="100.00", fx_rate="1.60")

    holding = _held(pf, nova)

    assert (holding.cost, holding.cost_aud) == (900, Decimal("1340"))   # 1,500 − 160


@freeze_time(TODAY)
@pytest.mark.parametrize("trade_rate, action_rate", [
    ("1.50", None),
    (None, "1.60"),
], ids=["no-rate-for-the-return", "no-cost-for-the-units"])
def test_a_return_that_cannot_be_priced_is_named_not_assumed_within_cost(pf, trade_rate,
                                                                        action_rate):
    """Whether it went below a cost base cannot be told in AUD, so the year
    names the holding, as it names a sale it cannot price (decisions.md #5)."""
    nova = fac.make_instrument(pf, "NOVA", exchange="NASDAQ", currency="USD")
    fac.add_trade(pf, nova, "2024-01-02", "buy", 100, "10.00", fx_rate=trade_rate)
    fac.add_action(pf, nova, "2025-01-02", "capital_return", amount="100.00",
                   fx_rate=action_rate)

    cgt = fyreport.fy_cgt(pf, 2025)
    headers, rows = exports.realised_cgt(pf, fy=2025)
    row = dict(zip(headers, rows[0]))

    assert cgt.withheld == ["NOVA"]
    assert [(d.event, d.known) for d in cgt.disposals] == [("capital_return", False)]
    # Blanks: nothing is known to have been sold at nil cost.
    assert (row["Event"], row["Cost base (AUD)"], row["Gross gain (AUD)"]) == (
        "return of capital", "", "")


@freeze_time(TODAY)
def test_the_fy_page_lists_an_unpriced_return_and_names_the_holding(client, session_factory):
    from test_routes import bind_to_only_portfolio, make_login

    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        nova = fac.make_instrument(s, "NOVA", exchange="NASDAQ", currency="USD")
        fac.add_trade(s, nova, "2024-01-02", "buy", 100, "10.00", fx_rate="1.50")
        fac.add_action(s, nova, "2025-01-02", "capital_return", amount="100.00", fx_rate=None)
        s.commit()

    page = client.get("/?fy=2025", headers={"accept": "text/html"})

    assert page.status_code == 200
    assert "Totals exclude NOVA — no exchange rate stored." in page.text
    assert '<span class="sub">return of capital</span>' in page.text


@freeze_time(TODAY)
def test_an_increase_with_no_rate_names_nothing(pf):
    """Raising a cost base cannot make a gain."""
    nova = fac.make_instrument(pf, "NOVA", exchange="NASDAQ", currency="USD")
    fac.add_trade(pf, nova, "2024-01-02", "buy", 100, "10.00", fx_rate="1.50")
    fac.add_action(pf, nova, "2025-01-02", "cost_base", amount="30.00", fx_rate=None)

    cgt = fyreport.fy_cgt(pf, 2025)

    assert (cgt.withheld, cgt.disposals) == ([], [])


@freeze_time(TODAY)
def test_a_return_of_capital_comes_back_as_money_in_total_return(pf, acme):
    """Value 1,000 + 100 returned − 1,000 put in."""
    fac.add_trade(pf, acme, "2025-01-06", "buy", 100, "10.00")
    fac.add_action(pf, acme, "2025-03-03", "capital_return", amount="100.00")
    fac.add_prices(pf, acme, [("2025-01-06", "10.00"), ("2026-08-03", "10.00")])

    series = queries.portfolio_series(pf)

    assert (series["invested"][-1], series["value"][-1], series["gain"][-1]) == (
        1000.0, 1000.0, 100.0)


@freeze_time(TODAY)
def test_the_daily_series_follows_a_split(pf, acme):
    """Valued at the day's close: before the split at the old units, after it
    at the new, so the line does not halve on the day."""
    fac.add_trade(pf, acme, "2025-01-06", "buy", 100, "10.00")
    fac.add_action(pf, acme, "2025-03-03", "split", ratio=(2, 1))
    fac.add_prices(pf, acme, [("2025-01-06", "10.00"), ("2025-02-28", "12.00"),
                              ("2025-03-03", "6.00")])

    series = queries.portfolio_series(pf)

    assert series["value"] == [1000.0, 1200.0, 1200.0]


@freeze_time(TODAY)
def test_the_ledger_shows_each_action(pf, acme):
    fac.add_trade(pf, acme, "2024-01-02", "buy", 100, "10.00")
    fac.add_action(pf, acme, "2025-01-02", "split", ratio=(2, 1))
    fac.add_action(pf, acme, "2025-02-03", "capital_return", amount="20.00")

    _inst, _holding, events = queries.ledger(pf, "ACME")

    assert [(e.date.isoformat(), e.label, e.row_kind, e.cash) for e in events][:2] == [
        ("2025-02-03", "capital return", "action", Decimal("20.0000")),
        ("2025-01-02", "split 2 for 1", "action", None)]


@freeze_time(TODAY)
def test_the_transactions_export_lists_each_action(pf, acme):
    fac.add_trade(pf, acme, "2024-01-02", "buy", 100, "10.00")
    fac.add_action(pf, acme, "2025-01-02", "split", ratio=(2, 1))

    headers, rows = exports.transactions(pf)
    split = dict(zip(headers, rows[1]))

    assert (split["Date"], split["Type"], split["Units"]) == ("2025-01-02", "split 2 for 1", "")
