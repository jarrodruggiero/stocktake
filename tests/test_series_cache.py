"""The fingerprint that decides whether a cached series is still true.

The daily series is expensive enough to cache and is asked for several times per
page, so `queries` keeps it against a fingerprint of its inputs. While rows were
append-only, a count was enough. Editing and deleting broke that in a way no
page would ever show you: the numbers stay plausible, they are just the numbers
from before the change.

So the fingerprint is (max price date, then per table: count, max id, max
updated_at). Each of the three covers a case the others miss:

  * **count** — a row added or removed;
  * **max id** — a delete and an add that leave the count where it started;
  * **max updated_at** — an edit, which changes neither of the above.

These tests go through `cached_portfolio_series` rather than inspecting the
fingerprint tuple, because what matters is that the second read is right, not
that some tuple differs. The autouse `_clear_caches` fixture in conftest gives
each test an empty cache to start from.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from freezegun import freeze_time
from sqlalchemy import func, select, update

import factories as fac
from app import queries
from app.models import Trade
from appcore import ensure_utc


def _series_value(session) -> Decimal:
    """Latest portfolio value from the cached series — the number a stale cache
    would get wrong."""
    series = queries.cached_portfolio_series(session)
    return Decimal(str(series["value"][-1])) if series["value"] else Decimal(0)


def _commit(session) -> None:
    """Commit, then expire — so the next read comes from SQL.

    The test sessions are `expire_on_commit=False`, so a loaded
    `instrument.trades` collection stays as it was even after a write. Every
    request gets a fresh session in production, so expiring here is what makes
    the test see what a page would. It does NOT weaken the cache assertions:
    the caches are module-level dicts, and a fingerprint that failed to change
    would return the cached series without touching the database at all.
    """
    session.commit()
    session.expire_all()


def _setup(session):
    inst = fac.make_instrument(session, "ALPHA")
    trade = fac.add_trade(session, inst, "2026-03-02", "buy", 100, "5.00")
    fac.add_prices(session, inst, [("2026-03-02", "5.00"), ("2026-03-03", "6.00")])
    _commit(session)
    return inst, trade


# --------------------------------------------------------------------------- #
# The three cases
# --------------------------------------------------------------------------- #

def test_adding_a_trade_invalidates_the_cache(pf):
    """The case that always worked — here so a change to the fingerprint can't
    quietly lose it while fixing the others."""
    inst, _ = _setup(pf)
    assert _series_value(pf) == Decimal("600")  # 100 units @ 6.00

    fac.add_trade(pf, inst, "2026-03-03", "buy", 50, "6.00")
    _commit(pf)

    assert _series_value(pf) == Decimal("900")  # 150 units @ 6.00


def test_editing_a_trade_invalidates_the_cache(pf):
    """The case that made the fingerprint fix part of the same task as editing:
    the row count is identical before and after, so without `updated_at` the
    charts would keep serving 600."""
    inst, trade = _setup(pf)
    assert _series_value(pf) == Decimal("600")
    before = pf.scalar(select(func.count()).select_from(Trade))

    trade.quantity = Decimal("200")
    _commit(pf)

    assert pf.scalar(select(func.count()).select_from(Trade)) == before
    assert _series_value(pf) == Decimal("1200")


def test_deleting_a_trade_invalidates_the_cache(pf):
    inst, trade = _setup(pf)
    assert _series_value(pf) == Decimal("600")

    pf.delete(trade)
    _commit(pf)

    assert _series_value(pf) == Decimal("0")


def test_a_delete_and_an_add_that_keep_the_count_invalidate_the_cache(pf):
    """Count alone can't see this one — one row out, one row in. The max id
    moves, which is what catches it."""
    inst, trade = _setup(pf)
    assert _series_value(pf) == Decimal("600")
    before = pf.scalar(select(func.count()).select_from(Trade))

    pf.delete(trade)
    pf.flush()
    fac.add_trade(pf, inst, "2026-03-02", "buy", 30, "5.00")
    _commit(pf)

    assert pf.scalar(select(func.count()).select_from(Trade)) == before
    assert _series_value(pf) == Decimal("180")  # 30 units @ 6.00


def test_editing_a_dividend_invalidates_the_cache(pf):
    """Dividends get the same treatment — they feed the income side of every
    report the same way trades feed the value side."""
    inst, _ = _setup(pf)
    dividend = fac.add_dividend(pf, inst, "2026-03-03", "40.00")
    _commit(pf)
    holdings = {h.instrument.ticker: h for h in queries.cached_holdings(pf)}
    assert holdings["ALPHA"].dividends_cash == Decimal("40.00")

    dividend.cash_amount = Decimal("55.00")
    _commit(pf)

    holdings = {h.instrument.ticker: h for h in queries.cached_holdings(pf)}
    assert holdings["ALPHA"].dividends_cash == Decimal("55.00")


# --------------------------------------------------------------------------- #
# updated_at itself
# --------------------------------------------------------------------------- #

# SQLite hands back naive datetimes and Postgres aware ones, so every
# comparison below goes through `ensure_utc` — the same helper the app uses.
# Asserting on the raw value would pass on one backend and TypeError on the
# other, which is the thing the two-backend suite exists to catch.

def test_updated_at_is_stamped_on_insert_and_moves_on_edit(pf):
    inst, trade = _setup(pf)
    first = ensure_utc(trade.updated_at)
    assert first is not None

    trade.quantity = Decimal("101")
    _commit(pf)

    assert ensure_utc(trade.updated_at) > first


def test_updated_at_does_not_move_when_nothing_changes(pf):
    """`onupdate` fires per UPDATE statement, and SQLAlchemy doesn't emit one
    for an unmodified object — so a page that merely reads a trade doesn't
    invalidate every cache in the process."""
    inst, trade = _setup(pf)
    first = ensure_utc(trade.updated_at)

    _commit(pf)  # no changes pending

    assert ensure_utc(trade.updated_at) == first


def test_the_price_feeds_fx_repair_bumps_updated_at(pf):
    """The FX backfill writes through Core `update()`, not the ORM. Column
    `onupdate` defaults apply there too — worth pinning, because if they didn't
    the repair would fix a trade's AUD value and leave every chart showing the
    unrepaired one."""
    inst = fac.make_instrument(pf, "GAMMA", currency="USD")
    trade = fac.add_trade(pf, inst, "2026-03-02", "buy", 10, "100.00", fx_rate=None)
    _commit(pf)
    before = ensure_utc(trade.updated_at)

    pf.execute(update(Trade).where(Trade.id == trade.id).values(fx_rate=Decimal("1.5")))
    _commit(pf)

    assert trade.fx_rate == Decimal("1.5")
    assert ensure_utc(trade.updated_at) > before


# --------------------------------------------------------------------------- #
# Scoping
# --------------------------------------------------------------------------- #

def test_the_fingerprint_only_sees_the_bound_portfolio(pf):
    """The fingerprint's queries are bare column selects — no ORM entity is
    being loaded — and the tenancy filter has to reach them anyway.

    If it didn't, two things would go wrong at once: a neighbour's write would
    invalidate this portfolio's cache on every page load, and the count would
    leak how many trades they have. Both are quiet failures, which is why this
    is pinned here rather than left to the count test in test_tenancy.py.
    """
    inst, _ = _setup(pf)
    mine = queries._series_fingerprint(pf)

    neighbour = fac.make_portfolio(pf, "Someone else")
    fac.add_trade(pf, inst, "2026-03-04", "buy", 999, "5.00",
                  portfolio_id=neighbour.id)
    fac.add_dividend(pf, inst, "2026-03-04", "99.00", portfolio_id=neighbour.id)
    _commit(pf)

    assert queries._series_fingerprint(pf) == mine


def test_a_neighbours_edit_does_not_invalidate_this_portfolios_cache(pf):
    """The same guarantee stated as a consequence: our cached series survives
    somebody else editing theirs."""
    inst, _ = _setup(pf)
    neighbour = fac.make_portfolio(pf, "Someone else")
    theirs = fac.add_trade(pf, inst, "2026-03-02", "buy", 999, "5.00",
                           portfolio_id=neighbour.id)
    _commit(pf)
    assert _series_value(pf) == Decimal("600")

    theirs.quantity = Decimal("1000")
    _commit(pf)

    assert _series_value(pf) == Decimal("600")


def test_reordering_the_holdings_handed_out_leaves_the_cache_alone(pf):
    """The cache handed out its own list, so a caller that sorted it in place
    reordered every later caller's holdings too."""
    for ticker in ("ALPHA", "BETAX"):
        fac.add_trade(pf, fac.make_instrument(pf, ticker), "2026-01-05", "buy", 10, "5.00")
    pf.flush()

    queries.cached_holdings(pf).reverse()        # the call that fills the cache
    queries.cached_holdings(pf).reverse()        # and one served from it

    assert [h.instrument.ticker for h in queries.cached_holdings(pf)] == ["ALPHA", "BETAX"]


# --------------------------------------------------------------------------- #
# Rows rewritten in place: prices, rates, instruments and holding settings
# --------------------------------------------------------------------------- #
# The fingerprint counted price and FX rows and took their latest date, and a
# second quote on a day, or the settled close replacing a provisional one, is
# the same row rewritten: the tiles read the new price and the chart the old
# one, all weekend after a Friday close. Instrument and holding-setting edits
# were not in it at all. Each is written here through the code that really
# writes it.

def _quoted(session, monkeypatch, price: str) -> None:
    """A live quote through the real refresh, at noon in Sydney with the ASX
    open, so the date is one the market has not settled."""
    from test_pricefeed import SETTINGS

    from app import pricefeed, providers

    noon = dt.datetime(2026, 3, 4, 1, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(providers, "yahoo_quotes",
                        lambda symbols: {"ALPHA.AX": (noon, Decimal(price))})
    pricefeed.refresh_quotes(session, SETTINGS)
    _commit(session)


@freeze_time("2026-03-04 01:00:00", tick=True)      # two writes, two moments
def test_a_price_quoted_again_the_same_day_reaches_the_chart(pf, monkeypatch):
    inst, _ = _setup(pf)
    inst.yahoo_symbol = "ALPHA.AX"
    _commit(pf)

    _quoted(pf, monkeypatch, "11.50")
    assert _series_value(pf) == Decimal("1150")
    _quoted(pf, monkeypatch, "12.00")                # the same row, rewritten

    assert _series_value(pf) == Decimal("1200")


def test_the_settled_close_replacing_a_live_price_reaches_the_chart(pf, monkeypatch):
    from test_pricefeed import SETTINGS, stub_closes, stub_no_fx, stub_yf

    from app import pricefeed

    # One timeline, moving forward as a real one does: rows written on the
    # real clock would carry today's date, later than either write below.
    with freeze_time("2026-03-04 01:00:00", tick=True):
        inst, _ = _setup(pf)
        inst.yahoo_symbol = "ALPHA.AX"
        _quoted(pf, monkeypatch, "11.50")
        assert _series_value(pf) == Decimal("1150")
    stub_closes(monkeypatch, {"ALPHA.AX": [("2026-03-04", "12.25")]})
    stub_yf(monkeypatch)
    stub_no_fx(monkeypatch)

    with freeze_time("2026-03-05 01:00:00"):          # the 4th has closed
        pricefeed.run_feed(pf, SETTINGS)
        _commit(pf)
        assert _series_value(pf) == Decimal("1225")


@freeze_time("2026-03-05 01:00:00", tick=True)
def test_an_exchange_rate_rewritten_in_place_reaches_the_chart(pf, monkeypatch):
    from test_pricefeed import SETTINGS, stub_closes, stub_yf

    from app import pricefeed, providers

    inst = fac.make_instrument(pf, "NOVA", exchange="NASDAQ", currency="USD")
    fac.add_trade(pf, inst, "2026-03-02", "buy", 100, "5.00", fx_rate="1.50")
    fac.add_prices(pf, inst, [("2026-03-02", "5.00"), ("2026-03-03", "6.00")])
    fac.add_fx(pf, "USDAUD", "2026-03-03", "1.50")
    _commit(pf)
    assert _series_value(pf) == Decimal("900")       # 600 USD at 1.50
    stub_closes(monkeypatch, {})
    stub_yf(monkeypatch)
    monkeypatch.setattr(providers, "fetch_fx", lambda *_a, **_k: providers.Fetch(
        rows=[(dt.date(2026, 3, 3), Decimal("1.60"))], source="test"))

    pricefeed.run_feed(pf, SETTINGS)                # the same row, at 1.60
    _commit(pf)

    assert _series_value(pf) == Decimal("960")


def test_an_instruments_class_edited_reaches_the_split_chart(pf):
    inst, _ = _setup(pf)
    assert set(queries.cached_grouped_series(pf, "asset_class")["groups"]) == {"etf"}

    inst.asset_class = "share"
    _commit(pf)

    assert set(queries.cached_grouped_series(pf, "asset_class")["groups"]) == {"share"}


def test_a_holding_setting_edited_reaches_the_cached_holdings(pf):
    inst, _ = _setup(pf)
    pref = fac.hold(pf, inst)
    _commit(pf)
    assert [h.drp for h in queries.cached_holdings(pf)] == [False]

    pref.drp = True
    _commit(pf)

    assert [h.drp for h in queries.cached_holdings(pf)] == [True]




def _counting_builds(monkeypatch) -> list[str]:
    """The ticker filter of every split series actually built, not served."""
    built: list[str] = []
    real = queries.grouped_series

    def counting(session, by, filters=None):
        built.append(filters["ticker"][0])
        return real(session, by, filters)

    monkeypatch.setattr(queries, "grouped_series", counting)
    return built


def _ask(session, name: str) -> dict:
    return queries.cached_grouped_series(session, "ticker", {"ticker": [name]})


def test_the_split_cache_keeps_only_the_charts_asked_for_most_recently(pf, monkeypatch):
    """Every filter tried in the chart builder's preview made a new entry, each
    a whole series, and none was ever dropped: memory grew for as long as
    anybody kept dragging. Now the few asked for most recently are kept."""
    _setup(pf)
    built = _counting_builds(monkeypatch)

    for n in range(queries.GROUPED_CACHE_SIZE):
        _ask(pf, f"T{n}")
    _ask(pf, "T0")                         # asked again, so now the most recent
    _ask(pf, "ONE-TOO-MANY")               # and the least recent, T1, makes room
    built.clear()
    _ask(pf, "T0")
    _ask(pf, "T1")

    assert len(queries._grouped_cache) == queries.GROUPED_CACHE_SIZE
    assert built == ["T1"]


def test_a_split_chart_rebuilt_after_a_change_counts_as_just_asked_for(pf, monkeypatch):
    inst, _trade = _setup(pf)
    built = _counting_builds(monkeypatch)

    for n in range(queries.GROUPED_CACHE_SIZE):
        _ask(pf, f"T{n}")
    fac.add_prices(pf, inst, [("2026-03-04", "7.00")])
    _commit(pf)
    _ask(pf, "T0")                         # out of date, so rebuilt: now the most recent
    _ask(pf, "ONE-TOO-MANY")
    built.clear()
    _ask(pf, "T0")

    assert built == []
