"""Broker CSV import.

Parsing is strict on purpose: neither broker has an API, so a changed export
format must fail loudly with the offending text rather than quietly importing
something wrong into someone's tax records.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

import factories as fac
from app import brokercsv
from app.models import Instrument, Trade
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


def test_the_wrong_header_shape_is_rejected():
    result = brokercsv.parse_csv("Foo,Bar\n1,2\n", "commsec", COMMSEC)

    assert "transactions export header" in result.errors[0]


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


def test_with_creation_allowed_the_instrument_is_created(pf):
    candidates = [_candidate(ticker="NOVA")]
    brokercsv.annotate(pf, candidates, PERMISSIVE)

    counts = brokercsv.commit(pf, candidates, PERMISSIVE, "testbroker")

    assert counts["instruments_created"] == 1
    inst = pf.scalars(select(Instrument).where(Instrument.ticker == "NOVA")).one()
    assert inst.yahoo_symbol == "NOVA.AX"     # ASX listings carry the suffix


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
    are the risk, so check them to the last place."""
    original = brokercsv.CandidateTrade(
        date=fac.d("2025-01-06"), ticker="ACME", type="buy",
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


def test_times_are_left_alone_when_no_zone_is_declared():
    """The default, and it must stay the default: a conversion applied to times
    that never needed one moves every trade by hours."""
    import datetime as dt

    result = _one(
        "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
        "15/07/2025 14:00:00,Buy,ACME,100,5.00,9.50\n"
    )
    assert result.candidates[0].time == dt.time(14, 0)
