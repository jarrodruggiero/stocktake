"""FX honesty: what the app does when it doesn't know an exchange rate.

There are two different "don't know"s here, and conflating them is what the old
code did:

  * **a gap in a pair we track** — a trade or a chart point that sits before the
    stored series starts, or between two rows. There IS a rate; it just isn't on
    that exact date. Using the nearest one is a rounding error. Using 1 is not:
    it books a foreign holding as though the currency didn't exist, drawing a
    phantom gain or loss the size of the exchange rate over the whole early
    history of the chart.

  * **a pair we have nothing for at all** — a USD holding on an install whose
    feed has never run. No amount of interpolation invents a rate, so the AUD
    figure must be *withheld*, not guessed. The dashboard already did this
    (`totals.excluded`); the charts, exports and reports now agree with it.

`queries.FxBook` is the single answer to both, and these tests pin the boundary
between them: nearest-rate inside a tracked pair, refusal outside one.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import pytest
from freezegun import freeze_time

import factories as fac
from app import exports, fyreport, queries

TODAY = "2026-08-02"


@pytest.fixture
def usd(pf):
    return fac.make_instrument(pf, "VERTEX", exchange="NASDAQ", asset_class="share",
                               currency="USD", name="Vertex Systems")


# --------------------------------------------------------------------------- #
# FxBook itself
# --------------------------------------------------------------------------- #

def test_the_rate_on_a_stored_date_is_that_date_s_rate(pf):
    fac.add_fx_series(pf, "USDAUD", [("2026-03-01", "1.50"), ("2026-03-05", "1.60")])

    book = queries.FxBook(pf)

    assert book.rate("USD", dt.date(2026, 3, 1)) == Decimal("1.50")
    assert book.rate("USD", dt.date(2026, 3, 5)) == Decimal("1.60")


def test_a_date_between_rows_uses_the_last_rate_before_it(pf):
    fac.add_fx_series(pf, "USDAUD", [("2026-03-01", "1.50"), ("2026-03-05", "1.60")])

    book = queries.FxBook(pf)

    assert book.rate("USD", dt.date(2026, 3, 3)) == Decimal("1.50")


def test_a_date_before_the_series_starts_uses_the_earliest_rate(pf):
    """A portfolio seeded from a spreadsheet routinely predates the
    feed's backfill, and the earliest known rate is a far better estimate of an
    unknown one than 1.00 — which is not an estimate at all."""
    fac.add_fx_series(pf, "USDAUD", [("2026-03-01", "1.50"), ("2026-03-05", "1.60")])

    book = queries.FxBook(pf)

    assert book.rate("USD", dt.date(2020, 1, 1)) == Decimal("1.50")


def test_a_date_after_the_series_ends_uses_the_latest_rate(pf):
    fac.add_fx_series(pf, "USDAUD", [("2026-03-01", "1.50"), ("2026-03-05", "1.60")])

    book = queries.FxBook(pf)

    assert book.rate("USD", dt.date(2030, 1, 1)) == Decimal("1.60")


def test_an_unknown_pair_has_no_rate_at_all(pf):
    fac.add_fx_series(pf, "USDAUD", [("2026-03-01", "1.50")])

    book = queries.FxBook(pf)

    assert book.rate("GBP", dt.date(2026, 3, 1)) is None
    assert book.known("GBP") is False
    assert book.known("USD") is True


def test_aud_is_always_one_and_always_known(pf):
    """No FxRate row says AUDAUD, and none should have to."""
    book = queries.FxBook(pf)

    assert book.rate("AUD", dt.date(2026, 3, 1)) == Decimal(1)
    assert book.known("AUD") is True


def test_lookups_do_not_depend_on_the_order_they_are_asked_in(pf):
    """`_Stepper` is a forward-only cursor and would return a stale rate the
    first time it was asked to look backwards. FxBook uses bisect precisely so
    trade dates and price dates can interleave freely."""
    fac.add_fx_series(pf, "USDAUD", [("2026-03-01", "1.50"), ("2026-03-05", "1.60")])
    book = queries.FxBook(pf)

    forwards = [book.rate("USD", dt.date(2026, 3, d)) for d in (1, 3, 5)]
    backwards = [book.rate("USD", dt.date(2026, 3, d)) for d in (5, 3, 1)]

    assert forwards == [Decimal("1.50"), Decimal("1.50"), Decimal("1.60")]
    assert backwards == list(reversed(forwards))


def test_a_rate_captured_on_the_row_wins_over_the_series(pf, usd):
    """The ATO wants the transaction-date rate, and the row carries the one that
    actually applied — a later series row must not overwrite history."""
    fac.add_fx_series(pf, "USDAUD", [("2026-03-01", "9.99")])
    trade = fac.add_trade(pf, usd, "2026-03-01", "buy", 10, "100.00", fx_rate="1.50")

    book = queries.FxBook(pf)

    assert book.of(trade, "USD") == Decimal("1.50")


def test_a_row_with_no_captured_rate_falls_back_to_the_series(pf, usd):
    fac.add_fx_series(pf, "USDAUD", [("2026-03-01", "1.50")])
    trade = fac.add_trade(pf, usd, "2026-03-04", "buy", 10, "100.00", fx_rate=None)

    book = queries.FxBook(pf)

    assert book.of(trade, "USD") == Decimal("1.50")


# --------------------------------------------------------------------------- #
# A pair with nothing stored: excluded everywhere, and said out loud
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_the_series_excludes_and_names_a_holding_it_cannot_convert(pf, usd):
    fac.add_prices(pf, usd, [("2026-03-01", "100.00"), ("2026-03-02", "110.00")])
    fac.add_trade(pf, usd, "2026-03-01", "buy", 10, "100.00", fx_rate=None)

    series = queries.portfolio_series(pf)

    assert series["excluded"] == ["VERTEX"]
    # Not valued at 1:1 — not valued at all.
    assert series["value"] in ([], [0.0, 0.0])
    assert series["invested"] in ([], [0.0, 0.0])


@freeze_time(TODAY)
def test_a_convertible_holding_is_unaffected_by_an_excluded_neighbour(pf, usd):
    """Exclusion is per instrument. One unconvertible holding must not blank out
    the rest of the portfolio."""
    aud = fac.make_instrument(pf, "ALPHA")
    fac.add_prices(pf, aud, [("2026-03-01", "5.00"), ("2026-03-02", "6.00")])
    fac.add_trade(pf, aud, "2026-03-01", "buy", 100, "5.00")
    fac.add_prices(pf, usd, [("2026-03-01", "100.00"), ("2026-03-02", "110.00")])
    fac.add_trade(pf, usd, "2026-03-01", "buy", 10, "100.00", fx_rate=None)

    series = queries.portfolio_series(pf)

    assert series["excluded"] == ["VERTEX"]
    assert series["value"][-1] == 600.00  # ALPHA only, converted at 1:1 correctly
    assert series["invested"][-1] == 500.00


@freeze_time(TODAY)
def test_the_grouped_series_excludes_the_same_holdings(pf, usd):
    """The dashboard, the portfolio series and every split have to agree, or a
    page shows one total and the chart beside it shows another."""
    aud = fac.make_instrument(pf, "ALPHA")
    fac.add_prices(pf, aud, [("2026-03-01", "5.00")])
    fac.add_trade(pf, aud, "2026-03-01", "buy", 100, "5.00")
    fac.add_prices(pf, usd, [("2026-03-01", "100.00")])
    fac.add_trade(pf, usd, "2026-03-01", "buy", 10, "100.00", fx_rate=None)

    grouped = queries.grouped_series(pf, "ticker")

    assert grouped["excluded"] == ["VERTEX"]
    assert set(grouped["groups"]) == {"ALPHA"}


@freeze_time(TODAY)
def test_the_dashboard_and_the_series_exclude_the_same_holding(pf, usd):
    """`totals.excluded` came first and the series now matches it. Pinning them
    together is the point — they are two renderings of one portfolio."""
    fac.add_prices(pf, usd, [("2026-03-01", "100.00")])
    fac.add_trade(pf, usd, "2026-03-01", "buy", 10, "100.00", fx_rate=None)

    totals = queries.totals(queries.all_holdings(pf))
    series = queries.portfolio_series(pf)

    assert totals.excluded == series["excluded"] == ["VERTEX"]


@freeze_time(TODAY)
def test_the_charts_page_says_what_it_left_out(client, session_factory):
    """An excluded holding that nobody is told about is just a wrong chart."""
    from test_routes import bind_to_only_portfolio, make_login

    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        vertex = fac.make_instrument(s, "VERTEX", exchange="NASDAQ", currency="USD")
        fac.add_prices(s, vertex, [("2026-03-01", "100.00")])
        fac.add_trade(s, vertex, "2026-03-01", "buy", 10, "100.00", fx_rate=None)
        s.commit()

    resp = client.get("/charts", headers={"accept": "text/html"})

    assert resp.status_code == 200
    # The claim is that the page NAMES what it dropped and says why — not that
    # it says it in one particular sentence. Asserting the old phrasing meant a
    # tone edit that kept both facts failed the test anyway.
    notices = re.findall(r'<p class="note">(.*?)</p>', resp.text, re.S)
    named = [n for n in notices if "VERTEX" in n]
    assert named, f"no notice names the excluded holding; got {notices!r}"
    assert any("exchange rate" in n for n in named), (
        f"the holding is named but no reason is given: {named!r}"
    )


@freeze_time(TODAY)
def test_the_charts_page_says_nothing_when_there_is_nothing_to_say(client, session_factory):
    from test_routes import bind_to_only_portfolio, make_login

    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        alpha = fac.make_instrument(s, "ALPHA")
        fac.add_prices(s, alpha, [("2026-03-01", "5.00")])
        fac.add_trade(s, alpha, "2026-03-01", "buy", 100, "5.00")
        s.commit()

    resp = client.get("/charts", headers={"accept": "text/html"})

    assert "no stored exchange rate" not in resp.text


# --------------------------------------------------------------------------- #
# A pair with a gap: the nearest rate, never 1:1
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_a_holding_older_than_the_fx_series_is_valued_at_the_earliest_rate(pf, usd):
    """Cost is booked at the trade's own 1.50 and value at the earliest stored
    1.50, so a day on which nothing moved shows no gain. Valuing at 1:1 would
    open the chart with a manufactured 500 loss."""
    fac.add_prices(pf, usd, [("2026-01-01", "100.00"), ("2026-03-01", "100.00")])
    fac.add_fx_series(pf, "USDAUD", [("2026-03-01", "1.50")])
    fac.add_trade(pf, usd, "2026-01-01", "buy", 10, "100.00", fx_rate="1.50")

    series = queries.portfolio_series(pf)

    assert series["excluded"] == []
    assert series["invested"] == [1500.00, 1500.00]
    assert series["value"] == [1500.00, 1500.00]
    assert series["gain"] == [0.00, 0.00]


@freeze_time(TODAY)
def test_a_trade_with_no_captured_rate_is_booked_at_the_series_rate(pf, usd):
    """An imported trade often has no rate on it. Booking it at 1 puts a USD
    number into an AUD cost base — the error compounds through every gain
    figure downstream."""
    fac.add_prices(pf, usd, [("2026-03-04", "100.00")])
    fac.add_fx_series(pf, "USDAUD", [("2026-03-01", "1.50")])
    fac.add_trade(pf, usd, "2026-03-04", "buy", 10, "100.00", fx_rate=None)

    series = queries.portfolio_series(pf)

    assert series["invested"] == [1500.00]  # not 1000.00


@freeze_time(TODAY)
def test_the_fy_report_uses_the_series_rate_for_a_trade_that_has_none(pf, usd):
    """The CGT engine's cost base has to agree with the charts, or the FY report
    and the dashboard tell two different stories about one sale."""
    fac.add_fx_series(pf, "USDAUD", [("2024-01-01", "1.50")])
    fac.add_trade(pf, usd, "2024-02-01", "buy", 10, "100.00", fx_rate=None)
    fac.add_trade(pf, usd, "2026-02-01", "sell", 10, "120.00", fx_rate="1.50")

    (disposal,) = fyreport.instrument_disposals(usd, queries.FxBook(pf))

    # cost base 10 x 100.00 USD x 1.50 = 1500.00, not 1000.00
    assert disposal.parcels[0].cost_base == Decimal("1500.00")


@freeze_time(TODAY)
def test_unrealised_cgt_blanks_the_aud_columns_it_cannot_fill(pf, usd):
    """A blank cell is a question; a 1:1 number is a wrong answer that a
    spreadsheet will happily add to a real AUD column."""
    fac.add_prices(pf, usd, [("2026-08-01", "120.00")])
    fac.add_trade(pf, usd, "2024-01-05", "buy", 10, "100.00", fx_rate=None)

    headers, rows = exports.unrealised_cgt(pf)
    (row,) = rows
    cell = dict(zip(headers, row))

    assert cell["Cost base (AUD)"] == ""
    assert cell["Market value (AUD)"] == ""
    assert cell["Unrealised gain (AUD)"] == ""
    assert cell["Gain after discount (AUD)"] == ""
    # The native-currency facts are still reported — only the conversion is
    # withheld, so the row is still useful.
    assert cell["Units"] == Decimal("10.00000000")


@freeze_time(TODAY)
def test_unrealised_cgt_fills_them_in_once_a_rate_exists(pf, usd):
    """The mirror: the blanking must be caused by the missing rate and nothing
    else, or the test above would pass on a report that is simply broken."""
    fac.add_prices(pf, usd, [("2026-08-01", "120.00")])
    fac.add_fx_series(pf, "USDAUD", [("2024-01-05", "1.50")])
    fac.add_trade(pf, usd, "2024-01-05", "buy", 10, "100.00", fx_rate=None)

    headers, rows = exports.unrealised_cgt(pf)
    (row,) = rows
    cell = dict(zip(headers, row))

    assert cell["Cost base (AUD)"] == Decimal("1500.00")
    assert cell["Market value (AUD)"] == Decimal("1800.00")  # 10 x 120 x 1.50
    assert cell["Unrealised gain (AUD)"] == Decimal("300.00")


@freeze_time(TODAY)
def test_the_trades_export_blanks_an_aud_value_it_cannot_compute(pf, usd):
    fac.add_trade(pf, usd, "2026-03-01", "buy", 10, "100.00", fx_rate=None)

    headers, rows = exports.transactions(pf)
    cell = dict(zip(headers, rows[0]))

    assert cell["Value (AUD)"] == ""
    assert cell["Price (native)"] == Decimal("100.000000")


@freeze_time(TODAY)
def test_the_dividends_export_blanks_the_total_when_the_cash_cannot_convert(pf, usd):
    """The grossed-up column adds cash to franking. With no AUD cash figure
    there is no honest total, and printing the franking alone under it would
    read as though the distribution were zero."""
    fac.add_dividend(pf, usd, "2026-03-01", "40.00", fx_rate=None,
                     franking_credits="10.00")

    headers, rows = exports.dividends(pf)
    cell = dict(zip(headers, rows[0]))

    assert cell["Cash (AUD)"] == ""
    assert cell["Grossed up (AUD)"] == ""
    # Franking is already an AUD figure and stands on its own.
    assert cell["Franking credits (AUD)"] == Decimal("10.00")


# --------------------------------------------------------------------------- #
# The repair path
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_an_excluded_holding_heals_when_the_rate_arrives(pf, usd):
    """The exclusion is a statement about today's data, not a permanent verdict.
    When the feed stores the pair, the same call returns real numbers — and the
    cache fingerprint moves with it, so the page doesn't keep serving the gap.
    """
    fac.add_prices(pf, usd, [("2026-03-01", "100.00")])
    fac.add_trade(pf, usd, "2026-03-01", "buy", 10, "100.00", fx_rate=None)
    pf.commit()
    assert queries.cached_portfolio_series(pf)["excluded"] == ["VERTEX"]

    fac.add_fx_series(pf, "USDAUD", [("2026-03-01", "1.50")])
    pf.commit()

    healed = queries.cached_portfolio_series(pf)
    assert healed["excluded"] == []
    assert healed["value"] == [1500.00]


# A third "don't know" that was not one at all: an AUD row with no stored rate
# is not ignorance, because the rate is 1 by arithmetic — decisions.md #6.
#
# **The suite could not have caught it.** `factories.add_dividend` defaults an
# AUD dividend's `fx_rate` to 1, so every fixture was tidier than the imported
# data it stands in for. These pass `fx_rate=None` on purpose.

@freeze_time(TODAY)
def test_an_aud_dividend_with_no_stored_rate_still_counts(pf):
    aud = fac.make_instrument(pf, "ALPHA")
    fac.add_trade(pf, aud, "2026-03-01", "buy", 100, "5.00")
    fac.add_dividend(pf, aud, "2026-03-05", "40.00", fx_rate=None)

    totals = queries.totals(queries.all_holdings(pf))

    assert totals.dividends == Decimal("40.00")
    assert totals.dividends_excluded == []


@freeze_time(TODAY)
def test_one_rateless_aud_dividend_does_not_void_the_others(pf):
    """The shape of the live failure: 24 good rows discarded because of one.

    Breaking out on the first `fx_rate is None` returns None for
    the whole instrument, so a single distribution imported without a rate cost
    the entire history beside it.
    """
    aud = fac.make_instrument(pf, "ALPHA")
    fac.add_trade(pf, aud, "2026-03-01", "buy", 100, "5.00")
    fac.add_dividend(pf, aud, "2026-03-05", "40.00", fx_rate=None)
    fac.add_dividend(pf, aud, "2026-06-05", "60.00")  # rate stored, as most are

    totals = queries.totals(queries.all_holdings(pf))

    assert totals.dividends == Decimal("100.00")


@freeze_time(TODAY)
def test_a_foreign_dividend_with_no_rate_is_still_withheld_and_now_named(pf, usd):
    """The rule that must NOT have loosened.

    A USD distribution with no rate is real ignorance, and converting it at 1:1
    would invent money. It is still withheld — but it is now *named*, because
    the failure this whole section exists for was a total that shrank silently.
    """
    fac.add_prices(pf, usd, [("2026-03-01", "100.00")])
    fac.add_trade(pf, usd, "2026-03-01", "buy", 10, "100.00")
    fac.add_dividend(pf, usd, "2026-03-05", "25.00", fx_rate=None)

    totals = queries.totals(queries.all_holdings(pf))

    assert totals.dividends == Decimal(0)
    assert totals.dividends_excluded == ["VERTEX"]


@freeze_time(TODAY)
def test_an_aud_drp_with_no_stored_rate_still_gives_an_average_price(pf):
    """The same flaw sat on `avg_price_aud`, reached through DRP trades.

    Four of the live portfolio's DRP allocations had no rate, which blanked the
    optional "Avg price (AUD)" column for three holdings — quieter than the
    dividend tile, and wrong in exactly the same way.
    """
    aud = fac.make_instrument(pf, "ALPHA")
    fac.add_trade(pf, aud, "2026-03-01", "buy", 100, "5.00")
    fac.add_trade(pf, aud, "2026-06-01", "drp", 10, "6.00", fx_rate=None)

    holding = next(h for h in queries.all_holdings(pf) if h.instrument.ticker == "ALPHA")

    # (100 x 5.00 + 10 x 6.00) / 110 units
    assert holding.avg_price_aud == Decimal("560.00") / Decimal(110)

