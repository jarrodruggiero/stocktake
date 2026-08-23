"""Trade times, and the two clocks they can be read on.

Storage is always the exchange's own clock. `models.trade_order` sequences a
day's trades for FIFO, and a stored time that moved with whoever was looking
would reorder a same-day buy and sell — which decides a capital gain.

So this converts for DISPLAY only, and the conversion needs the trade's date
rather than just its time: an ASX trade in January and one in July are an hour
apart, because the offset is a property of the moment, not of the zone.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from .pricefeed import MARKETS


def market_zone(exchange: str) -> ZoneInfo | None:
    """The exchange's timezone, or None where it has no session (crypto)."""
    market = MARKETS.get((exchange or "").upper())
    if market is None or market.zone is None:
        return None
    return ZoneInfo(market.zone)


def aware(date: dt.date, clock: dt.time | None, exchange: str) -> dt.datetime | None:
    """A trade's moment, on the exchange's clock, with its offset attached.

    None where the exchange has no timezone — an offset invented for a market
    that never closes would be a fact this app does not have.
    """
    zone = market_zone(exchange)
    if zone is None:
        return None
    from .models import MARKET_OPEN  # noqa: PLC0415 - avoids a circular import

    return dt.datetime.combine(date, clock or MARKET_OPEN, tzinfo=zone)


def to_market(clock: dt.time, date: dt.date, from_zone: str, exchange: str) -> dt.time:
    """A wall-clock time somewhere else, read on the exchange's clock.

    For an export that stamps trades in the account holder's own timezone
    rather than the market's. Returns the time unchanged when either zone is
    unknown, because a half-applied conversion is worse than none.
    """
    zone = market_zone(exchange)
    if zone is None or not from_zone:
        return clock
    try:
        origin = ZoneInfo(from_zone)
    except Exception:  # noqa: BLE001 - an unknown zone name is user input
        return clock
    return dt.datetime.combine(date, clock, tzinfo=origin).astimezone(zone).time()
