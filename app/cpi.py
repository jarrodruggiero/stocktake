"""The Consumer Price Index the 2027 CGT rules index a cost base by
(decisions.md #149).

From 1 July 2027 money spent on a CGT asset is indexed by the index number for
the quarter of the CGT event over the number for the quarter it was spent
(ITAA 1997 s 960-275(1B)), worked to three decimal places, rounding up from 5
(s 960-275(5)), and not at all at a factor of 1 or less (s 960-270(2)). The
index number is the All Groups CPI for the eight capitals, first published for
the quarter (s 960-280(1)), so a quarter once stored is never overwritten by a
revision; after the ABS changes its reference period only numbers on the new
one count (s 960-280(3)), so the stored series is replaced.

Nothing before the September 2027 quarter is ever needed: an asset held through
30 June 2027 is treated as bought again on 1 July 2027, and is indexed from
there. So nothing is asked of the ABS until that quarter has finished.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import clock, providers
from .models import CpiQuarter

# All Groups CPI (10001), original series (10), weighted average of the eight
# capital cities (50), quarterly. The ABS data API needs no key.
ABS_URL = "https://data.api.abs.gov.au/rest/data/ABS,CPI,2.0.0/1.10001.10.50.Q"
# The first quarter any 2027 cost base is indexed from.
FIRST = dt.date(2027, 7, 1)
THOUSANDTHS = Decimal("0.001")


def quarter(day: dt.date) -> dt.date:
    """The first day of the calendar quarter `day` falls in."""
    return dt.date(day.year, (day.month - 1) // 3 * 3 + 1, 1)


def _label(start: dt.date) -> str:
    return f"{start.year}-Q{(start.month - 1) // 3 + 1}"


def _start(label: str) -> dt.date:
    year, q = label.split("-Q")
    return dt.date(int(year), (int(q) - 1) * 3 + 1, 1)


class CpiBook:
    """Every stored quarter, read once."""

    def __init__(self, session: Session):
        self._numbers = {row.quarter: row.index_number
                         for row in session.scalars(select(CpiQuarter))}

    def index(self, day: dt.date) -> Decimal | None:
        return self._numbers.get(quarter(day))

    @property
    def latest(self) -> dt.date | None:
        return max(self._numbers, default=None)

    def factor(self, spent: dt.date, event: dt.date) -> Decimal | None:
        """What money spent on `spent` is multiplied by for a CGT event on
        `event`: 1 when that would not raise it, None when a quarter's number
        is not published yet."""
        then, now = self.index(spent), self.index(event)
        if then is None or now is None:
            return None
        worked = (now / then).quantize(THOUSANDTHS, rounding=ROUND_HALF_UP)
        return worked if worked > 1 else Decimal(1)


def _due(session: Session, today: dt.date) -> bool:
    """Whether a quarter could be published that is not stored yet: the
    newest finished quarter, once there is one the rules use."""
    finished = quarter(quarter(today) - dt.timedelta(days=1))
    if finished < FIRST:
        return False
    newest = session.scalar(select(CpiQuarter.quarter).order_by(CpiQuarter.quarter.desc()))
    return newest is None or newest < finished


def refresh(session: Session, today: dt.date | None = None) -> int:
    """Ask the ABS for the quarters not stored yet, and store them; how many."""
    if not _due(session, today or clock.today()):
        return 0
    answer = providers._get_json(ABS_URL, {"format": "jsondata",
                                           "startPeriod": _label(FIRST)})
    published, reference = _read(answer)
    stored = {row.quarter: row for row in session.scalars(select(CpiQuarter))}
    if any(row.reference_period != reference for row in stored.values()):
        session.execute(delete(CpiQuarter))
        stored = {}
    added = 0
    for start, number in sorted(published.items()):
        if start in stored or start < FIRST:
            continue
        session.add(CpiQuarter(quarter=start, index_number=number,
                               reference_period=reference))
        added += 1
    session.flush()
    return added


def _read(answer: dict) -> tuple[dict[dt.date, Decimal], str]:
    """The quarters and their numbers, and the reference period, out of the
    ABS's SDMX-JSON."""
    data = answer["data"]
    structure = data["structures"][0]
    periods = [v["id"] for v in structure["dimensions"]["observation"][0]["values"]]
    dataset = data["dataSets"][0]
    base = next(a for a in structure["attributes"]["dataSet"] if a["id"] == "BASE_PERIOD")
    reference = base["values"][dataset["attributes"][0]]["name"]
    [series] = dataset["series"].values()
    numbers = {_start(periods[int(i)]): Decimal(str(values[0]))
               for i, values in series["observations"].items() if values[0] is not None}
    return numbers, reference
