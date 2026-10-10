"""Broker CSV import.

Parsing is strict on purpose: neither broker has an API, so a changed export
format must fail loudly with the offending text rather than quietly importing
something wrong into someone's tax records.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from sqlalchemy import select

import factories as fac
from app import brokercsv, queries
from app.models import MARKET_OPEN, Instrument, Trade
from app.settings import BrokerFormat, ImportSettings

MAPPED = BrokerFormat(
    kind="mapped", exchange="ASX", currency="AUD", date_format="%d/%m/%Y",
    columns={"date": "Trade Date", "action": "Buy/Sell", "ticker": "Code",
             "units": "Units", "price": "Price", "brokerage": "Brokerage"},
)
COMMSEC = BrokerFormat(kind="commsec_transactions", exchange="ASX", currency="AUD",
                       date_format="%d/%m/%Y")

STRICT = ImportSettings(allow_new_instruments=False)
PERMISSIVE = ImportSettings(allow_new_instruments=True)


# --------------------------------------------------------------------------- #
# The mapped format
# --------------------------------------------------------------------------- #

def test_a_well_formed_export_parses():
    csv_text = (
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "06/01/2025,Buy,acme,100,5.00,9.50\n"
        "07/02/2025,SELL,ACME,40,7.25,9.50\n"
    )

    result = brokercsv.parse_csv(csv_text, "testbroker", MAPPED)

    assert result.errors == []
    first, second = result.candidates
    assert (first.date, first.ticker, first.type) == (fac.d("2025-01-06"), "ACME", "buy")
    assert (first.quantity, first.unit_price, first.brokerage) == (
        Decimal("100"), Decimal("5.00"), Decimal("9.50"))
    # Case is normalised both ways: the action word and the ticker.
    assert (second.type, second.ticker) == ("sell", "ACME")


def test_amounts_carrying_currency_symbols_and_separators_parse():
    csv_text = (
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "06/01/2025,Buy,ACME,\"1,500\",\"$1,234.56\",$9.50\n"
    )

    candidate = brokercsv.parse_csv(csv_text, "testbroker", MAPPED).candidates[0]

    assert candidate.quantity == Decimal("1500")
    assert candidate.unit_price == Decimal("1234.56")


def test_a_missing_column_names_the_column_and_where_to_fix_it():
    """A broker changing its export is expected; the message has to say which
    column vanished and that the fix is config, not code."""
    csv_text = "Trade Date,Buy/Sell,Code,Units\n06/01/2025,Buy,ACME,100\n"

    result = brokercsv.parse_csv(csv_text, "testbroker", MAPPED)

    assert result.candidates == []
    assert len(result.errors) == 1
    assert "Price" in result.errors[0]
    assert "imports.brokers.testbroker.columns" in result.errors[0]


def test_a_row_that_is_not_a_trade_is_skipped_rather_than_failing():
    csv_text = (
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "06/01/2025,Dividend,ACME,0,0,0\n"
        "07/01/2025,Buy,ACME,10,5.00,9.50\n"
    )

    result = brokercsv.parse_csv(csv_text, "testbroker", MAPPED)

    assert len(result.candidates) == 1
    assert len(result.skipped) == 1
    assert result.errors == []


def test_one_unparseable_row_does_not_lose_the_good_ones():
    csv_text = (
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "06/01/2025,Buy,ACME,ten,5.00,9.50\n"
        "07/01/2025,Buy,ACME,10,5.00,9.50\n"
    )

    result = brokercsv.parse_csv(csv_text, "testbroker", MAPPED)

    assert len(result.candidates) == 1
    assert len(result.errors) == 1
    assert "line 2" in result.errors[0]


def test_an_empty_file_is_an_error_not_a_crash():
    assert brokercsv.parse_csv("", "testbroker", MAPPED).errors == ["empty file"]


def test_an_unknown_format_kind_says_so():
    odd = BrokerFormat(kind="something-else", exchange="ASX", currency="AUD")

    result = brokercsv.parse_csv("a,b\n1,2\n", "mystery", odd)

    assert "something-else" in result.errors[0]


# --------------------------------------------------------------------------- #
# The CommSec transactions format
# --------------------------------------------------------------------------- #

def test_the_commsec_detail_line_parses():
    csv_text = (
        "Date,Reference,Details,Debit($),Credit($),Balance($)\n"
        "06/01/2025,N123,B 50 WIDGET @ 20.000000,1009.50,,0.00\n"
    )

    candidate = brokercsv.parse_csv(csv_text, "commsec", COMMSEC).candidates[0]

    assert (candidate.ticker, candidate.type) == ("WIDGET", "buy")
    assert candidate.quantity == Decimal("50")
    assert candidate.unit_price == Decimal("20.000000")


def test_brokerage_is_recovered_from_the_cash_movement_on_a_buy():
    """CommSec's export has no brokerage column — it is the difference between
    the cash that left the account and the value of the shares."""
    csv_text = (
        "Date,Reference,Details,Debit($),Credit($),Balance($)\n"
        "06/01/2025,N123,B 50 WIDGET @ 20.000000,1009.50,,0.00\n"
    )

    candidate = brokercsv.parse_csv(csv_text, "commsec", COMMSEC).candidates[0]

    # gross = 50 x 20.00 = 1000.00; debit 1009.50 - 1000.00 = 9.50 brokerage
    assert candidate.brokerage == Decimal("9.50")


def test_brokerage_is_recovered_from_the_cash_movement_on_a_sell():
    csv_text = (
        "Date,Reference,Details,Debit($),Credit($),Balance($)\n"
        "07/02/2025,N124,S 50 WIDGET @ 20.000000,,990.50,0.00\n"
    )

    candidate = brokercsv.parse_csv(csv_text, "commsec", COMMSEC).candidates[0]

    # gross = 1000.00; credit 990.50 → 1000.00 - 990.50 = 9.50 brokerage
    assert candidate.brokerage == Decimal("9.50")
    assert candidate.type == "sell"


def test_non_trade_rows_are_skipped():
    csv_text = (
        "Date,Reference,Details,Debit($),Credit($),Balance($)\n"
        "06/01/2025,N1,Direct Credit Dividend WIDGET,,25.00,0.00\n"
        "07/01/2025,N2,B 10 WIDGET @ 20.000000,209.50,,0.00\n"
    )

    result = brokercsv.parse_csv(csv_text, "commsec", COMMSEC)

    assert len(result.candidates) == 1
    assert len(result.skipped) == 1


@pytest.mark.parametrize("fmt, header, row", [
    (MAPPED, "Units, Code, Trade Date, Buy/Sell, Price, Brokerage",
     "100, ACME, 06/01/2025, Buy, 5.00, 9.50"),
    (COMMSEC, "Date, Reference, Details, Debit($), Credit($), Balance($)",
     "06/01/2025, N123, B 100 ACME @ 5.000000, 509.50, , 0.00"),
], ids=["mapped", "commsec"])
def test_a_header_spaced_after_its_commas_reads_like_one_that_is_not(fmt, header, row):
    """The broker designer reads "Trade Date, Buy/Sell" as the columns it
    names, so the format it writes uses the names without the spaces. The
    import checked the header the same way but read each row by the spaced
    names: every mapped row failed, and every CommSec row was skipped."""
    result = brokercsv.parse_csv(f"{header}\n{row}\n", "testbroker", fmt)

    assert (result.errors, result.skipped) == ([], [])
    assert [(c.ticker, c.quantity, c.unit_price, c.brokerage) for c in result.candidates] == [
        ("ACME", Decimal(100), Decimal("5.00"), Decimal("9.50"))]


def test_an_implausible_brokerage_is_refused():
    """A cash figure that cannot be explained by brokerage means the row was
    misread — importing it would silently corrupt the cost base."""
    csv_text = (
        "Date,Reference,Details,Debit($),Credit($),Balance($)\n"
        "06/01/2025,N123,B 50 WIDGET @ 20.000000,1150.00,,0.00\n"
    )

    result = brokercsv.parse_csv(csv_text, "commsec", COMMSEC)

    # gross 1000.00 → implied brokerage 150.00, above the max(100, gross/10 = 100)
    # bound, so the row is rejected rather than imported.
    assert result.candidates == []
    assert "implausible brokerage" in result.errors[0]


def test_a_brokerage_just_inside_the_bound_is_accepted():
    csv_text = (
        "Date,Reference,Details,Debit($),Credit($),Balance($)\n"
        "06/01/2025,N123,B 50 WIDGET @ 20.000000,1100.00,,0.00\n"
    )

    result = brokercsv.parse_csv(csv_text, "commsec", COMMSEC)

    # implied brokerage 100.00 == max(100, 100) → allowed
    assert result.errors == []
    assert result.candidates[0].brokerage == Decimal("100.00")


@pytest.mark.parametrize("details, debit, refused_at", [
    ("B 10 WIDGET @ 20.000000", "300.00", None),           # $100 on $200: the floor is the bound
    ("B 100 WIDGET @ 50.000000", "5500.00", None),         # $500 on $5,000: a tenth is
    ("B 100 WIDGET @ 50.000000", "5500.01", "500.010000"),
    ("B 50 WIDGET @ 20.000000", "990.00", "-10.000000"),   # less than the shares cost
])
def test_brokerage_is_plausible_up_to_100_or_a_tenth_of_the_trade(details, debit, refused_at):
    """The bound is the larger of the two. Every other test sits on a $1,000
    trade, where they are equal, so either could have been dropped."""
    result = brokercsv.parse_csv(
        "Date,Reference,Details,Debit($),Credit($),Balance($)\n"
        f"06/01/2025,N123,{details},{debit},,0.00\n", "commsec", COMMSEC)

    if refused_at is None:
        assert (len(result.candidates), result.errors) == (1, [])
    else:
        assert result.candidates == [] and len(result.errors) == 1
        assert result.errors[0].startswith(f"line 2: implausible brokerage {refused_at} from")


@pytest.mark.parametrize("header", ["Foo,Bar",
                                    "Date,Reference,Details,Credit($),Balance($)",
                                    "Date,Reference,Debit($),Credit($),Balance($)"])
def test_the_wrong_header_shape_is_rejected(header):
    """Either of the two columns the layout reads, missing, is the wrong export."""
    result = brokercsv.parse_csv(f"{header}\n06/01/2025,1,2,3,4\n", "commsec", COMMSEC)

    assert len(result.errors) == 1 and "transactions export header" in result.errors[0]


def test_a_commsec_row_for_no_units_is_refused_as_the_trade_form_would():
    result = brokercsv.parse_csv(
        "Date,Reference,Details,Debit($),Credit($),Balance($)\n"
        "06/01/2025,N123,B 0 WIDGET @ 20.000000,9.50,,0.00\n", "commsec", COMMSEC)

    assert result.candidates == []
    assert result.errors[0].startswith("line 2: Units")


# --------------------------------------------------------------------------- #
# Annotation
# --------------------------------------------------------------------------- #

def _candidate(ticker="ACME", date="2025-01-06", type="buy", quantity="100",
               price="5.00", brokerage="9.50"):
    return brokercsv.CandidateTrade(
        date=fac.d(date), ticker=ticker, type=type, quantity=Decimal(quantity),
        unit_price=Decimal(price), brokerage=Decimal(brokerage), currency="AUD",
        exchange="ASX")


def test_a_row_already_in_the_ledger_is_flagged_as_a_duplicate(pf):
    acme = fac.make_instrument(pf, "ACME")
    existing = fac.add_trade(pf, acme, "2025-01-06", "buy", 100, "5.00", brokerage="9.50")
    pf.commit()
    candidates = [_candidate()]

    brokercsv.annotate(pf, candidates, STRICT)

    assert candidates[0].status == "duplicate"
    assert f"#{existing.id}" in candidates[0].detail


def test_a_row_differing_in_any_matched_field_is_new(pf):
    acme = fac.make_instrument(pf, "ACME")
    fac.add_trade(pf, acme, "2025-01-06", "buy", 100, "5.00", brokerage="9.50")
    pf.commit()
    # Same day and ticker, different quantity — a second parcel, not a repeat.
    candidates = [_candidate(quantity="50")]

    brokercsv.annotate(pf, candidates, STRICT)

    assert candidates[0].status == "new"


def test_a_ticker_we_do_not_track_is_flagged(pf):
    candidates = [_candidate(ticker="NOSUCH")]

    brokercsv.annotate(pf, candidates, STRICT)

    assert candidates[0].status == "unknown-instrument"
    assert "allow_new_instruments" in candidates[0].detail


# --------------------------------------------------------------------------- #
# Commit
# --------------------------------------------------------------------------- #

def test_committing_inserts_new_rows_into_this_portfolio(pf, owner):
    _, portfolio = owner
    fac.make_instrument(pf, "ACME")
    pf.commit()
    candidates = [_candidate()]
    brokercsv.annotate(pf, candidates, STRICT)

    counts = brokercsv.commit(pf, candidates, STRICT, "testbroker")

    assert counts["inserted"] == 1
    trade = pf.scalars(select(Trade)).one()
    assert trade.portfolio_id == portfolio.id       # tenancy, not a global insert
    assert trade.fx_rate == 1                   # AUD is 1:1
    assert "testbroker" in trade.note


def test_committing_skips_the_duplicates(pf):
    acme = fac.make_instrument(pf, "ACME")
    fac.add_trade(pf, acme, "2025-01-06", "buy", 100, "5.00", brokerage="9.50")
    pf.commit()
    candidates = [_candidate()]
    brokercsv.annotate(pf, candidates, STRICT)

    counts = brokercsv.commit(pf, candidates, STRICT, "testbroker")

    assert counts == {"inserted": 0, "duplicates_skipped": 1, "instruments_created": 0}
    assert len(pf.scalars(select(Trade)).all()) == 1


def test_an_unknown_instrument_is_refused_unless_creation_is_allowed(pf):
    candidates = [_candidate(ticker="NOSUCH")]
    brokercsv.annotate(pf, candidates, STRICT)

    with pytest.raises(ValueError, match="NOSUCH"):
        brokercsv.commit(pf, candidates, STRICT, "testbroker")


@pytest.mark.parametrize("exchange, symbol", [("ASX", "NOVA.AX"), ("NASDAQ", "NOVA")])
def test_with_creation_allowed_the_instrument_is_created(pf, exchange, symbol):
    """Only an ASX listing carries Yahoo's suffix: on any other exchange it
    names a symbol that has no prices. Reinvestment starts off."""
    candidates = [_candidate(ticker="NOVA")]
    candidates[0].exchange = exchange
    brokercsv.annotate(pf, candidates, PERMISSIVE)
    assert candidates[0].detail == "will be created"

    counts = brokercsv.commit(pf, candidates, PERMISSIVE, "testbroker")

    assert counts["instruments_created"] == 1
    inst = pf.scalars(select(Instrument).where(Instrument.ticker == "NOVA")).one()
    assert (inst.exchange, inst.yahoo_symbol, inst.drp) == (exchange, symbol, False)


def test_a_non_aud_trade_leaves_the_rate_for_the_feed_to_fill(pf):
    usd = BrokerFormat(kind="mapped", exchange="NASDAQ", currency="USD",
                       date_format="%d/%m/%Y", columns=MAPPED.columns)
    fac.make_instrument(pf, "NOVA", exchange="NASDAQ", currency="USD")
    pf.commit()
    candidate = _candidate(ticker="NOVA")
    candidate.currency, candidate.exchange = usd.currency, usd.exchange
    brokercsv.annotate(pf, [candidate], STRICT)

    brokercsv.commit(pf, [candidate], STRICT, "testbroker")

    # Left NULL deliberately: the price feed's repair pass backfills the rate
    # for the trade's own date.
    assert pf.scalars(select(Trade)).one().fx_rate is None


# --------------------------------------------------------------------------- #
# Staging round-trip
# --------------------------------------------------------------------------- #

def test_a_candidate_survives_the_staging_file_unchanged():
    """Between preview and commit a candidate is written to JSON and read back.
    A lossy round-trip here would silently alter what gets imported — Decimals
    are the risk, so check them to the last place, and the time with them."""
    original = brokercsv.CandidateTrade(
        date=fac.d("2025-01-06"), time=dt.time(14, 5), ticker="ACME", type="buy",
        quantity=Decimal("12.34567890"), unit_price=Decimal("1234.567890"),
        brokerage=Decimal("9.50"), currency="AUD", exchange="ASX",
        status="new", detail="something")

    restored = brokercsv.CandidateTrade.from_dict(original.as_dict())

    assert restored == original
    assert restored.quantity == Decimal("12.34567890")
    assert restored.unit_price == Decimal("1234.567890")


# --------------------------------------------------------------------------- #
# Times: what the export says, and what the day can mean
# --------------------------------------------------------------------------- #

def _one(csv_text: str, fmt: BrokerFormat = MAPPED):
    result = brokercsv.parse_csv(csv_text, "b", fmt)
    assert not result.errors, result.errors
    return result


def test_a_date_padded_with_midnight_parses_and_keeps_no_time():
    """Almost every broker export writes "2024-02-13 00:00:00".

    It must not fail (it used to: `unconverted data remains: 00:00:00`) and it
    must not store midnight, which would sort the trade ahead of every
    hand-entered one that day.
    """
    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "06/01/2025 00:00:00,Buy,ACME,100,5.00,9.50\n"
    )
    assert result.candidates[0].time is None
    # Not reported as an adjustment: no time was given to adjust.
    assert result.adjusted == 0


def test_a_real_intraday_time_is_kept():
    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "06/01/2025 14:32:05,Buy,ACME,100,5.00,9.50\n"
    )
    assert result.candidates[0].time == __import__("datetime").time(14, 32, 5)
    assert result.adjusted == 0


@pytest.mark.parametrize("stamp,expected", [
    ("07:30:00", None),                 # before the open — another timezone, or settlement
    ("17:30:00", "16:00:00"),           # after the close
    ("10:00:00", None),                 # the open itself means "no time worth showing"
])
def test_a_time_outside_the_session_moves_into_it(stamp, expected):
    """`time` feeds `trade_order` and nothing else, so an out-of-hours stamp
    misorders a day rather than mis-stating an amount. Moving it to the
    boundary keeps the ordering honest."""
    import datetime as dt

    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        f"06/01/2025 {stamp},Buy,ACME,100,5.00,9.50\n"
    )
    got = result.candidates[0].time
    assert got == (dt.time.fromisoformat(expected) if expected else None)


def test_moving_a_time_is_reported_but_the_open_is_not():
    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "06/01/2025 17:30:00,Buy,ACME,100,5.00,9.50\n"
        "07/01/2025 10:00:00,Buy,ACME,100,5.00,9.50\n"
        "08/01/2025 14:00:00,Buy,ACME,100,5.00,9.50\n"
    )
    assert result.adjusted == 1


@pytest.mark.parametrize("exchange, clock, kept", [
    ("ASX", dt.time(9, 59), (None, True)),             # before the open: booked at it, and said so
    ("ASX", dt.time(16, 0), (dt.time(16, 0), False)),  # the close is still inside the session
    ("ASX", dt.time(16, 1), (dt.time(16, 0), True)),
    ("LSE", dt.time(3, 0), (dt.time(3, 0), False)),    # no hours known: taken as given
])
def test_a_time_is_kept_to_its_exchanges_session(exchange, clock, kept):
    """A broker format names its exchange freely, so one with no known hours
    is as likely as a time outside them."""
    assert brokercsv.in_trading_hours(clock, exchange) == kept


def test_an_action_word_mapped_to_no_trade_type_is_not_one():
    """A format's action words are free text: one mapped to anything but buy,
    sell or drp skips its rows instead of recording a trade of that type."""
    assert brokercsv.trade_type("Transfer", {"Transfer": "transfer"}) is None


def test_a_row_is_committed_to_its_own_instrument(pf):
    """Every other commit test has one instrument, the first one there is."""
    fac.make_instrument(pf, "ACME")
    beta = fac.make_instrument(pf, "BETA")
    pf.commit()
    candidates = [_candidate(ticker="BETA")]
    brokercsv.annotate(pf, candidates, STRICT)

    brokercsv.commit(pf, candidates, STRICT, "testbroker")

    assert pf.scalars(select(Trade)).one().instrument_id == beta.id


def test_a_date_with_no_time_is_not_converted_from_another_zone():
    """Midnight pads a date rather than stating a time, so a format that reads
    its times in the account holder's zone has nothing to convert."""
    fmt = MAPPED.model_copy(update={"times_zone": "America/New_York"})
    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "06/01/2025 00:00:00,Buy,ACME,100,5.00,9.50\n", fmt)

    assert (result.candidates[0].date, result.candidates[0].time) == (dt.date(2025, 1, 6), None)


def test_a_time_from_the_export_is_committed_with_the_trade(pf):
    """Unset means the open; a time the export carried orders the day."""
    fac.make_instrument(pf, "ACME")
    pf.commit()
    candidate = _candidate()
    candidate.time = dt.time(14, 5)
    brokercsv.annotate(pf, [candidate], STRICT)

    brokercsv.commit(pf, [candidate], STRICT, "testbroker")

    assert pf.scalars(select(Trade)).one().time == dt.time(14, 5)


@pytest.mark.parametrize("ticker, exchange", [("NO TICKER", "ASX"), ("NOVA", "NOT AN EXCHANGE")])
def test_a_malformed_listing_is_never_created_from_a_file(pf, ticker, exchange):
    """Creation allowed, a file is still arbitrary input: a ticker or an
    exchange the trade form would refuse does not become an instrument."""
    candidate = _candidate(ticker=ticker)
    candidate.exchange = exchange
    brokercsv.annotate(pf, [candidate], PERMISSIVE)

    with pytest.raises(ValueError, match=f"got '{ticker}' on '{exchange}'"):
        brokercsv.commit(pf, [candidate], PERMISSIVE, "testbroker")
    assert pf.scalars(select(Instrument)).all() == []


def test_a_market_that_never_closes_keeps_the_time_it_was_given():
    """Crypto has no session to be outside of — decisions.md #14."""
    import datetime as dt

    crypto = MAPPED.model_copy(update={"exchange": "CRYPTO"})
    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "06/01/2025 03:00:00,Buy,ACME,1,5.00,0\n", crypto,
    )
    assert result.candidates[0].time == dt.time(3, 0)
    assert result.adjusted == 0


def test_an_export_stamped_in_another_zone_is_read_on_the_exchanges_clock():
    """A Perth export of ASX trades is two hours behind the market in July.

    08:30 in Perth is 10:30 in Sydney — inside the session. Read as market time
    it is before the open and thrown away, so this also pins the ORDER of the
    two steps: convert first, then judge the session. Judging first discards
    the time before the conversion can rescue it.
    """
    import datetime as dt

    perth = MAPPED.model_copy(update={"times_zone": "Australia/Perth"})
    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "15/07/2025 08:30:00,Buy,ACME,100,5.00,9.50\n", perth,
    )
    assert result.candidates[0].time == dt.time(10, 30)


@pytest.mark.parametrize(("stamped", "exchange", "zone", "on_the_market"), [
    # US hours are the small hours of the next day in Sydney: a NASDAQ buy at
    # 09:45 New York on Wednesday 14 January is 01:45 on Thursday there.
    ("15/01/2026 01:45:00", "NASDAQ", "Australia/Sydney",
     (dt.date(2026, 1, 14), dt.time(9, 45))),
    # And the ASX opens in the New York evening of the day before.
    ("14/01/2026 18:30:00", "ASX", "America/New_York",
     (dt.date(2026, 1, 15), dt.time(10, 30))),
])
def test_a_conversion_across_midnight_moves_the_date_too(stamped, exchange, zone,
                                                        on_the_market):
    """The test above converts Perth to Sydney, two hours on the same day.
    The conversion this setting exists for crosses midnight, and converting
    the time alone dated every such trade a day out: its rate, its close and
    its place among that day's trades all from the wrong day."""
    fmt = MAPPED.model_copy(update={"times_zone": zone, "exchange": exchange,
                                    "currency": "AUD"})
    result = _one("Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
                  f"{stamped},Buy,ACME,10,5.00,0\n", fmt)

    candidate = result.candidates[0]
    assert (candidate.date, candidate.time) == on_the_market


def test_times_are_left_alone_when_no_zone_is_declared():
    """The default, and it must stay the default: a conversion applied to times
    that never needed one moves every trade by hours."""
    import datetime as dt

    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "15/07/2025 14:00:00,Buy,ACME,100,5.00,9.50\n"
    )
    assert result.candidates[0].time == dt.time(14, 0)


# --------------------------------------------------------------------------- #
# Resolving an instrument the import has never seen
# --------------------------------------------------------------------------- #

def test_unknown_instruments_are_grouped_by_instrument_not_by_row():
    """Ninety trades in six instruments asks six questions, not ninety."""
    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "06/01/2025,Buy,ACME,100,5.00,9.50\n"
        "07/01/2025,Buy,ACME,100,6.00,9.50\n"
        "08/01/2025,Buy,NOVA,10,7.00,9.50\n"
    )
    for c in result.candidates:
        c.status = "unknown-instrument"
    assert brokercsv.unresolved(result.candidates) == [
        ("ACME", "ASX", 2), ("NOVA", "ASX", 1)]


def test_correcting_an_exchange_moves_every_trade_of_that_instrument():
    """The case this exists for: one ticker listed on two exchanges.

    A correction is per instrument, so fixing it once fixes the whole file —
    and a row of a DIFFERENT instrument that happens to share the ticker string
    must not be dragged along with it.
    """
    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "06/01/2025,Buy,ACME,100,5.00,9.50\n"
        "07/01/2025,Buy,ACME,100,6.00,9.50\n"
        "08/01/2025,Buy,NOVA,10,7.00,9.50\n"
    )
    moved = brokercsv.relabel(result.candidates, {("ACME", "ASX"): ("ACME", "NASDAQ")})
    assert moved == 2
    assert [(c.ticker, c.exchange) for c in result.candidates] == [
        ("ACME", "NASDAQ"), ("ACME", "NASDAQ"), ("NOVA", "ASX")]


def test_a_correction_that_matches_an_existing_instrument_stops_being_new(pf):
    """The point of rechecking: a corrected ticker that already exists must
    read as an existing holding before anything is written, not as a second
    instrument about to be created beside it."""
    fac.make_instrument(pf, "ACME", exchange="NASDAQ", currency="USD")
    pf.commit()

    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "06/01/2025,Buy,ACME,100,5.00,9.50\n"
    )
    brokercsv.annotate(pf, result.candidates, PERMISSIVE)
    assert result.candidates[0].status == "unknown-instrument"

    brokercsv.relabel(result.candidates, {("ACME", "ASX"): ("ACME", "NASDAQ")})
    brokercsv.annotate(pf, result.candidates, PERMISSIVE)
    assert result.candidates[0].status == "new"
    assert brokercsv.unresolved(result.candidates) == []


def test_creating_instruments_is_the_default():
    """An import of a broker's own export is normally the first thing an
    install does, and every ticker in it is unknown at that point."""
    assert ImportSettings().allow_new_instruments is True


# --------------------------------------------------------------------------- #
# This broker's vocabulary
# --------------------------------------------------------------------------- #

def test_a_broker_word_maps_to_a_trade_type():
    """"In" is what one broker calls a DRP allotment. Mapping it is
    configuration, not code."""
    fmt = MAPPED.model_copy(update={"actions": {"Purchase": "buy", "Disposal": "sell"}})
    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "06/01/2025,Purchase,ACME,100,5.00,9.50\n"
        "07/01/2025,Disposal,ACME,40,7.25,9.50\n", fmt,
    )
    assert [c.type for c in result.candidates] == ["buy", "sell"]


def test_the_builtin_words_still_work_with_a_vocabulary_set():
    fmt = MAPPED.model_copy(update={"actions": {"In": "drp"}})
    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "06/01/2025,Buy,ACME,100,5.00,9.50\n", fmt,
    )
    assert result.candidates[0].type == "buy"


@pytest.mark.parametrize("price", ["", " "], ids=["empty", "a space"])
def test_a_drp_allotment_with_no_price_is_refused_not_booked_at_zero(price):
    """The row the whole mapping question came from.

    An allotment carries units and no price; the registry bought them out of
    the distribution. Booking it at zero understates the parcel's cost base and
    overstates the gain when it is sold, which is a tax figure. A file spaced
    after its commas writes the missing price as a space.
    """
    fmt = MAPPED.model_copy(update={"actions": {"In": "drp"}})
    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        f"06/01/2025,In,ACME,7,{price},\n", fmt,
    )
    assert result.candidates == []
    assert "no price" in result.skipped[0]
    assert "dividend statement" in result.skipped[0]


def test_a_drp_allotment_that_does_carry_a_price_is_imported():
    fmt = MAPPED.model_copy(update={"actions": {"In": "drp"}})
    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "06/01/2025,In,ACME,7,12.50,\n", fmt,
    )
    assert result.candidates[0].type == "drp"
    assert result.candidates[0].unit_price == Decimal("12.50")


# --------------------------------------------------------------------------- #
# Figures that are not figures, or that the trade form would refuse
# --------------------------------------------------------------------------- #

def _one_row(units="100", price="5.00", brokerage="9.50"):
    return ("Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
            f"06/01/2025,Buy,ACME,{units},{price},{brokerage}\n")


@pytest.mark.parametrize("kwargs, message", [
    ({"units": "NaN"}, "line 2: Units: 'NaN' is not a number"),
    ({"price": "Infinity"}, "line 2: Price: 'Infinity' is not a number"),
    ({"brokerage": "NaN"}, "line 2: Brokerage: 'NaN' is not a number"),
    ({"units": "1E+999999"}, "line 2: Units: '1E+999999' is too large"),
    ({"price": "(12.50)"}, "line 2: Price: '(12.50)' is not a number"),
    ({"units": "0"}, "line 2: Units: 0 has to be more than zero"),
    # Refused, not flipped: whether a negative means a sell is the export's to say.
    ({"units": "-100"}, "line 2: Units: -100 has to be more than zero"),
    ({"price": "-5"}, "line 2: Price: -5 can't be negative"),
    ({"brokerage": "-9.50"}, "line 2: Brokerage: -9.50 can't be negative"),
    # Only a DRP is skipped for having no price; a buy without one is wrong.
    ({"price": ""}, "line 2: Price: '' is not a number"),
], ids=["nan-units", "infinite-price", "nan-brokerage", "huge-units", "brackets",
        "zero-units", "negative-units", "negative-price", "negative-brokerage",
        "missing-price"])
def test_a_row_the_trade_form_would_refuse_is_an_error_naming_field_and_value(
        kwargs, message):
    result = brokercsv.parse_csv(_one_row(**kwargs), "testbroker", MAPPED)

    assert result.candidates == []
    assert [e for e in result.errors if message in e], result.errors
    assert "InvalidOperation" not in " ".join(result.errors)


def test_a_zero_price_is_allowed_for_a_bonus_issue_or_demerger():
    result = brokercsv.parse_csv(_one_row(price="0"), "testbroker", MAPPED)

    assert result.errors == []
    assert result.candidates[0].unit_price == Decimal("0")


def test_one_bad_row_does_not_hide_the_good_ones_but_is_still_reported():
    text = _one_row() + "07/01/2025,Buy,ACME,NaN,5.00,9.50\n"

    result = brokercsv.parse_csv(text, "testbroker", MAPPED)

    assert len(result.candidates) == 1
    assert len(result.errors) == 1


# --------------------------------------------------------------------------- #
# What a file would leave each holding at
# --------------------------------------------------------------------------- #
# The trade form, an edit, a delete, a move and the API each refuse a sale of
# more than is held. An import did not: a broker export covering the last year
# carries sales of shares bought before it starts, and committing one left a
# negative holding that made the FY page raise. Each row is now walked as the
# form would walk it, in date order.

def test_a_sale_of_more_than_is_held_is_refused_with_the_forms_words(pf):
    fac.make_instrument(pf, "ACME")
    pf.commit()
    candidates = [_candidate(type="sell", date="2025-03-02")]

    brokercsv.annotate(pf, candidates, STRICT)

    assert candidates[0].status == "refused"
    assert "more than the 0 units of ACME held on 2025-03-02" in candidates[0].detail


def test_a_sale_covered_by_an_earlier_row_of_the_file_is_new_whatever_its_order(pf):
    fac.make_instrument(pf, "ACME")
    pf.commit()
    candidates = [_candidate(type="sell", date="2025-03-02"), _candidate(date="2025-01-06")]

    brokercsv.annotate(pf, candidates, STRICT)

    assert [c.status for c in candidates] == ["new", "new"]


def test_a_sale_covered_by_what_is_already_held_is_new(pf):
    acme = fac.make_instrument(pf, "ACME")
    fac.add_trade(pf, acme, "2025-01-02", "buy", 100, "4.00")
    pf.commit()
    candidates = [_candidate(type="sell", date="2025-03-02")]

    brokercsv.annotate(pf, candidates, STRICT)

    assert candidates[0].status == "new"


def test_a_sale_that_would_strand_one_already_recorded_is_refused(pf):
    """Held 100 and sold 100 in June; a file selling 50 in March leaves the
    June sale 50 short."""
    acme = fac.make_instrument(pf, "ACME")
    fac.add_trade(pf, acme, "2025-01-02", "buy", 100, "4.00")
    fac.add_trade(pf, acme, "2025-06-02", "sell", 100, "6.00")
    pf.commit()
    candidates = [_candidate(type="sell", date="2025-03-02", quantity="50")]

    brokercsv.annotate(pf, candidates, STRICT)

    assert candidates[0].status == "refused"
    assert "2025-06-02" in candidates[0].detail


def test_the_recorded_sale_named_is_the_first_left_short(pf):
    """After a file sale of 50 in March, June's recorded sale leaves exactly
    nothing, which is allowed; July's is the one left short."""
    acme = fac.make_instrument(pf, "ACME")
    fac.add_trade(pf, acme, "2025-01-02", "buy", 100, "4.00")
    fac.add_trade(pf, acme, "2025-06-02", "sell", 50, "6.00")
    fac.add_trade(pf, acme, "2025-07-02", "sell", 1, "6.00")
    pf.commit()
    candidates = [_candidate(type="sell", date="2025-03-02", quantity="50")]

    brokercsv.annotate(pf, candidates, STRICT)

    assert candidates[0].status == "refused"
    assert "leave ACME at -1 units from 2025-07-02" in candidates[0].detail


def test_a_sale_already_recorded_is_a_duplicate_not_a_shortfall(pf):
    """The same export uploaded twice: its sale matches the recorded one and
    is skipped. Walked as a new row, it would sell the holding a second time
    and refuse the file."""
    acme = fac.make_instrument(pf, "ACME")
    fac.add_trade(pf, acme, "2025-01-06", "buy", 100, "5.00", brokerage="9.50")
    fac.add_trade(pf, acme, "2025-02-03", "sell", 100, "6.00", brokerage="9.50")
    pf.commit()
    candidates = [_candidate(type="sell", date="2025-02-03", price="6.00"),
                  _candidate(date="2025-03-03", quantity="50")]

    brokercsv.annotate(pf, candidates, STRICT)

    assert [c.status for c in candidates] == ["duplicate", "new"]


def test_a_sale_is_judged_against_its_own_holding_only(pf):
    """ACME's units, alone or added to BETA's, do not cover a BETA sale."""
    acme = fac.make_instrument(pf, "ACME")
    beta = fac.make_instrument(pf, "BETA")
    fac.add_trade(pf, acme, "2025-01-06", "buy", 100, "5.00")
    fac.add_trade(pf, beta, "2025-01-06", "buy", 50, "5.00")
    pf.commit()
    candidates = [_candidate(ticker="BETA", type="sell", date="2025-02-03", quantity="80")]

    brokercsv.annotate(pf, candidates, STRICT)

    assert candidates[0].status == "refused"
    assert "more than the 50 units of BETA held" in candidates[0].detail


def test_a_refused_row_is_never_committed(pf):
    fac.make_instrument(pf, "ACME")
    pf.commit()
    candidates = [_candidate(type="sell")]
    brokercsv.annotate(pf, candidates, STRICT)

    with pytest.raises(ValueError, match="more than"):
        brokercsv.commit(pf, candidates, STRICT, "testbroker")
    assert pf.scalars(select(Trade)).all() == []


def _one_row_at_a_time(held, rows):
    """The form's walk for each row in turn, which is what the importer first
    did: too slow for a big file, but plainly what is meant."""
    held, refused = list(held), []
    for c in sorted(rows, key=lambda r: (r.date, r.time or MARKET_OPEN)):
        row = Trade(date=c.date, time=c.time, type=c.type, quantity=c.quantity)
        breach = queries.balance_breach(held, row)
        if breach is None:
            held.append(row)
        else:
            refused.append((c, breach))
    return refused


_moments = st.tuples(st.integers(0, 12), st.sampled_from([None, dt.time(9, 30), dt.time(14)]))
_steps = st.tuples(_moments, st.sampled_from(["buy", "sell", "sell", "drp"]),
                   st.integers(1, 60))


@given(recorded=st.lists(_steps, max_size=12), file=st.lists(_steps, max_size=12))
# Sold out and bought back: a recorded sale down to nothing is not short.
@example(recorded=[((0, None), "buy", 10), ((1, None), "sell", 10)],
         file=[((2, None), "buy", 5)])
# A row that sells out ahead of a recorded purchase leaves nothing owed.
@example(recorded=[((0, None), "buy", 10), ((5, None), "buy", 10)],
         file=[((2, None), "sell", 10)])
def test_one_walk_refuses_what_the_form_would_row_by_row(recorded, file):
    """Recorded trades that may already be short, file rows on the same days
    and at the same times as them and each other: the single walk refuses the
    same rows, naming the same breach."""
    start = dt.date(2025, 3, 3)
    held = [Trade(id=n, date=start + dt.timedelta(days=day), time=at, type=kind,
                  quantity=Decimal(units))
            for n, ((day, at), kind, units) in enumerate(recorded, start=1)]
    rows = [brokercsv.CandidateTrade(
        date=start + dt.timedelta(days=day), time=at, ticker="ACME", type=kind,
        quantity=Decimal(units), unit_price=Decimal(5), brokerage=Decimal(0), currency="AUD",
        exchange="ASX") for (day, at), kind, units in file]

    def seen(refused):
        return [(id(c), b.is_candidate, b.balance, b.held_before,
                 queries.breach_message("ACME", b), None if b.is_candidate else id(b.trade))
                for c, b in refused]

    assert seen(brokercsv._shortfalls(held, rows)) == seen(_one_row_at_a_time(held, rows))


def test_a_big_file_is_judged_in_one_walk():
    """Walking the whole timeline again for each row took 27 seconds over
    4,000 rows, and most of an hour over the 40,000 of the upload cap's own
    test. One walk takes a fraction of a second, so the budget is generous."""
    import time

    held = [Trade(id=n, date=dt.date(2024, 1 + n % 12, 1 + n % 28), type="buy",
                  quantity=Decimal(5)) for n in range(1, 2001)]
    rows = [_candidate(date=f"2025-{1 + n % 12:02d}-{1 + n % 28:02d}",
                       type="sell" if n % 3 == 2 else "buy",
                       quantity="1" if n % 3 == 2 else "10")
            for n in range(4000)]

    began = time.perf_counter()
    refused = brokercsv._shortfalls(held, rows)

    assert time.perf_counter() - began < 5
    assert refused == []


def test_importing_into_a_removed_instrument_brings_it_back(pf):
    """Removed from every portfolio, an instrument is deactivated; trades
    imported for it make it a holding again, as recording one by hand does."""
    fac.make_instrument(pf, "ACME", active=False)
    pf.commit()
    candidates = [_candidate()]
    brokercsv.annotate(pf, candidates, STRICT)

    brokercsv.commit(pf, candidates, STRICT, "testbroker")

    assert pf.scalars(select(Instrument)).one().active is True


def test_a_field_past_the_csv_modules_limit_is_an_error_not_a_crash():
    """A file that is not a CSV at all, or one runaway quote: the csv module
    refuses a field past 128 KiB from inside the reader, and the upload page
    answered with a server error."""
    result = brokercsv.parse_csv("Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
                                 + "x" * 200_000 + ",Buy,ACME,1,1.00,0\n", "b", MAPPED)

    assert result.candidates == []
    assert result.errors and "could not be read as a CSV" in result.errors[0]
    assert "could not be read" in brokercsv.parse_csv("a" * 200_000, "b", MAPPED).errors[0]


def test_a_row_shorter_than_the_header_is_skipped_by_line_not_a_crash():
    """Broker exports end with a footer — "End of report" — one field long.
    The reader filled the missing columns with None, and the first `.strip()`
    took the import page down."""
    result = brokercsv.parse_csv("Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
                                 "06/01/2025,Buy,ACME,100,5.00,9.50\n"
                                 "End of report\n", "b", MAPPED)

    assert [c.ticker for c in result.candidates] == ["ACME"]
    assert result.skipped == ["line 3: action ''"]
