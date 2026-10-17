"""The CPI the 2027 CGT rules index a cost base by (decisions.md #149).

ITAA 1997 as amended by Act No. 49 of 2026: money spent from 1 July 2027 is
indexed by the index number for the quarter of the CGT event over the number
for the quarter it was spent (s 960-275(1B)), worked to three decimal places,
rounding up from 5 (s 960-275(5)), and not at all at a factor of 1 or less
(s 960-270(2)). The index number is the All Groups CPI for the eight capitals,
first published for the quarter (s 960-280(1)); after a change of reference
period only numbers on the new one count (s 960-280(3)).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from freezegun import freeze_time
from sqlalchemy import select

from app import cpi, providers
from app.models import CpiQuarter

BASE = "Sep 2025 = 100.0"
D = dt.date


def _abs(quarters: dict[str, float], base: str = BASE) -> dict:
    """What the ABS data API answers, cut to the parts that are read: the
    observation periods, one series of values, and the reference period."""
    periods = list(quarters)
    return {"data": {
        "structures": [{
            "dimensions": {"observation": [{"id": "TIME_PERIOD",
                                            "values": [{"id": p} for p in periods]}]},
            "attributes": {"dataSet": [{"id": "BASE_PERIOD", "values": [{"name": base}]}]},
        }],
        "dataSets": [{"attributes": [0], "series": {"0:0:0:0:0": {"observations": {
            str(i): [quarters[p]] for i, p in enumerate(periods)}}}}],
    }}


@pytest.fixture
def abs_says(monkeypatch):
    """The ABS, answering with whatever the test puts in `answer`, and the
    requests it was sent."""
    seen = {"calls": [], "answer": _abs({})}

    def get_json(url, params):
        seen["calls"].append((url, params))
        if isinstance(seen["answer"], Exception):
            raise seen["answer"]
        return seen["answer"]

    monkeypatch.setattr(providers, "_get_json", get_json)
    return seen


def _stored(pf) -> dict:
    return {row.quarter: (row.index_number, row.reference_period)
            for row in pf.scalars(select(CpiQuarter))}


def _store(pf, quarters: dict[dt.date, str], base: str = BASE):
    for quarter, number in quarters.items():
        pf.add(CpiQuarter(quarter=quarter, index_number=Decimal(number), reference_period=base))
    pf.flush()


@pytest.mark.parametrize("day, quarter", [
    (D(2027, 7, 1), D(2027, 7, 1)), (D(2027, 9, 30), D(2027, 7, 1)),
    (D(2027, 12, 31), D(2027, 10, 1)), (D(2028, 2, 29), D(2028, 1, 1)),
    (D(2028, 6, 30), D(2028, 4, 1)),
])
def test_a_day_falls_in_its_calendar_quarter(day, quarter):
    assert cpi.quarter(day) == quarter


def test_the_factor_is_the_ratio_to_three_places_rounding_up_from_five(pf):
    """103.25 over 100.00 is 1.0325: 1.033, where rounding half to even (what
    Decimal does by default) would give 1.032."""
    _store(pf, {D(2027, 7, 1): "100.00", D(2028, 4, 1): "103.25", D(2028, 7, 1): "110.28"})
    book = cpi.CpiBook(pf)

    assert book.factor(D(2027, 7, 1), D(2028, 5, 15)) == Decimal("1.033")
    assert book.factor(D(2027, 8, 20), D(2028, 9, 30)) == Decimal("1.103")


def test_at_one_or_less_nothing_is_indexed(pf):
    _store(pf, {D(2027, 7, 1): "100.00", D(2027, 10, 1): "99.80", D(2028, 1, 1): "100.04"})
    book = cpi.CpiBook(pf)

    assert book.factor(D(2027, 7, 1), D(2027, 11, 1)) == 1      # deflation: not below 1
    assert book.factor(D(2027, 7, 1), D(2028, 1, 1)) == 1      # 1.0004 is 1.000


def test_in_the_same_quarter_the_factor_is_one_without_its_number(pf):
    """A number over itself: a sale in the September 2027 quarter of a parcel
    held through 30 June 2027 need not wait for that quarter's CPI."""
    assert cpi.CpiBook(pf).factor(D(2027, 7, 1), D(2027, 8, 15)) == 1


def test_a_quarter_not_yet_published_gives_no_factor(pf):
    _store(pf, {D(2027, 7, 1): "100.00"})

    assert cpi.CpiBook(pf).factor(D(2027, 7, 1), D(2027, 12, 1)) is None


@freeze_time("2027-11-15")
def test_the_feed_stores_each_quarter_it_could_need(pf, abs_says):
    abs_says["answer"] = _abs({"2027-Q3": 103.12})

    added = cpi.refresh(pf)

    assert added == 1
    assert _stored(pf) == {D(2027, 7, 1): (Decimal("103.120"), BASE)}
    [(url, params)] = abs_says["calls"]
    # All Groups (10001), original (10), eight capitals (50), quarterly, from
    # the first quarter a 2027 cost base can be indexed from.
    assert url.endswith("/ABS,CPI,2.0.0/1.10001.10.50.Q")
    assert params == {"format": "jsondata", "startPeriod": "2027-Q3"}


@freeze_time("2028-02-10")
def test_a_revised_number_does_not_replace_the_first_published(pf, abs_says):
    _store(pf, {D(2027, 7, 1): "103.12"})
    abs_says["answer"] = _abs({"2027-Q3": 103.30, "2027-Q4": 104.01})

    added = cpi.refresh(pf)

    assert added == 1
    assert _stored(pf) == {D(2027, 7, 1): (Decimal("103.120"), BASE),
                           D(2027, 10, 1): (Decimal("104.010"), BASE)}


@freeze_time("2028-02-10")
def test_a_new_reference_period_replaces_the_series(pf, abs_says):
    """Numbers on two reference periods cannot be divided by each other."""
    _store(pf, {D(2027, 7, 1): "103.12"})
    abs_says["answer"] = _abs({"2027-Q3": 100.0, "2027-Q4": 100.69}, base="Sep 2027 = 100.0")

    cpi.refresh(pf)

    assert _stored(pf) == {D(2027, 7, 1): (Decimal("100.000"), "Sep 2027 = 100.0"),
                           D(2027, 10, 1): (Decimal("100.690"), "Sep 2027 = 100.0")}


@pytest.mark.parametrize("today, stored", [
    ("2027-06-30", {}),                                # nothing can be indexed yet
    ("2027-09-30", {}),                                # September's not finished
    ("2027-11-15", {D(2027, 7, 1): "103.12"}),         # the newest finished one is here
])
def test_nothing_is_asked_when_there_is_nothing_new_to_have(pf, abs_says, today, stored):
    _store(pf, stored)

    with freeze_time(today):
        added = cpi.refresh(pf)

    assert (added, abs_says["calls"]) == (0, [])


@freeze_time("2027-11-15")
def test_the_feed_run_asks_and_carries_on_when_the_abs_does_not_answer(pf, abs_says,
                                                                       monkeypatch):
    from app import pricefeed
    from app.settings import PortfolioSettings

    abs_says["answer"] = _abs({"2027-Q3": 103.12})
    counts = pricefeed.run_feed(pf, PortfolioSettings())
    abs_says["answer"] = TimeoutError("timed out")
    pf.query(CpiQuarter).delete()
    failed = pricefeed.run_feed(pf, PortfolioSettings())

    assert counts["cpi"] == 1
    assert failed["cpi"] == 0
    assert pricefeed.last_run_sources["cpi:ABS"] == "abs failed: timed out"
