"""Choosing which columns the holdings table shows.

Three things here are guarantees rather than conveniences, and each has a way
of quietly breaking:

  * **A stored preference must never be what stops the dashboard rendering.**
    It outlives the column it names — remove a column in a later version and
    every saved choice still mentions it.
  * **The choice belongs to the person, not the portfolio.** Two people sharing
    a portfolio each get their own, which is only observable with two accounts.
  * **Reset stores NULL, not a copy of the defaults.** The difference is
    invisible today and decides who a future change to `DEFAULTS` reaches.

The registry itself is pure and tested directly. The route tests go through the
real form post, because the interesting failure — a disabled checkbox is not
submitted, so the locked column disappears on save — lives in the template, not
in the Python.
"""

from __future__ import annotations

import re
from decimal import Decimal

from freezegun import freeze_time
from sqlalchemy import select

import factories as fac
import fixture_portfolio as ref
from app import columns, queries
from app.models import User
from test_routes import PASSWORD, bind_to_only_portfolio, make_login, session_csrf

# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #

class _FakeHolding:
    """Just enough of a Holding for the sort-key tests: they are about which
    attribute is read, not about how one is built."""

    def __init__(self, ticker: str, drp: bool = False):
        self.instrument = type("I", (), {"ticker": ticker})()
        self.drp = drp


def test_every_column_key_is_unique():
    """`BY_KEY` is a dict, so a duplicate key would silently shadow rather than
    fail — and the shadowed column would still render its header."""
    keys = [c.key for c in columns.COLUMNS]

    assert len(keys) == len(set(keys))


def test_the_defaults_all_exist():
    """A typo in `DEFAULTS` would drop a column from every dashboard belonging
    to somebody who never opened the chooser, with nothing to notice."""
    assert set(columns.DEFAULTS) <= set(columns.BY_KEY)


def test_no_preference_renders_exactly_the_pre_t19_columns():
    """The promise that nobody's dashboard moved. Order included: this is the
    header row people have been reading.
    """
    rendered = [c.key for c in columns.chosen(None)]

    assert rendered == [
        "ticker", "asset_class", "units", "avg_price", "cost",
        "price", "value_reporting", "gain_reporting", "day_change", "dividends",
    ]


def test_an_empty_stored_preference_is_treated_as_no_preference():
    """`[]` reaches `chosen()` from a row written before `clean()` existed, or
    from a hand-edited database. A table of nothing is not a useful answer."""
    assert [c.key for c in columns.chosen([])] == columns.DEFAULTS


def test_columns_render_in_the_order_they_were_chosen():
    """**Do not change this back to declaration order.**

    The argument for declaration order — "a table whose columns move about
    between sessions is harder to read" — is about *incidental* movement, the
    table changing shape on its own. It says nothing against an order somebody
    picked, which is stable by definition. See decisions.md #62.
    """
    backwards = ["dividends", "units", "ticker", "price"]

    assert [c.key for c in columns.chosen(backwards)] == backwards


def test_no_preference_renders_the_defaults_in_the_order_they_are_declared():
    assert [c.key for c in columns.chosen(None)] == columns.DEFAULTS


def test_a_repeated_key_appears_once():
    """A hand-edited row, or a move that raced itself. A column rendered twice
    is a table whose header row no longer matches its body."""
    rendered = [c.key for c in columns.chosen(["ticker", "units", "units", "price"])]

    assert rendered == ["ticker", "units", "price"]


def test_the_locked_column_keeps_the_position_it_was_given():
    """Locked means it cannot be turned off, not that it must come first. If
    somebody puts Ticker in the middle, that is their table."""
    rendered = [c.key for c in columns.chosen(["units", "ticker", "price"])]

    assert rendered == ["units", "ticker", "price"]


def test_clean_preserves_the_submitted_order():
    """The chooser's checkboxes submit in declaration order, so this only
    matters for the reordering form — but if `clean` sorted, every move would
    be undone on the next save."""
    assert columns.clean(["price", "ticker", "units"]) == ["price", "ticker", "units"]


def test_moving_a_column_up_swaps_it_with_its_neighbour():
    assert columns.move(["a", "b", "c"], "b", "up") == ["b", "a", "c"]


def test_moving_a_column_down_swaps_it_with_its_neighbour():
    assert columns.move(["a", "b", "c"], "b", "down") == ["a", "c", "b"]


def test_moving_the_first_column_up_does_nothing():
    assert columns.move(["a", "b"], "a", "up") == ["a", "b"]


def test_moving_the_last_column_down_does_nothing():
    assert columns.move(["a", "b"], "b", "down") == ["a", "b"]


def test_moving_a_column_that_is_not_there_does_nothing():
    """The stale-form case: two tabs open, one of them saved a different
    selection. Better a no-op than a 500 or a silent reordering of something
    else."""
    assert columns.move(["a", "b"], "zzz", "up") == ["a", "b"]


def test_an_unknown_direction_does_nothing():
    assert columns.move(["a", "b"], "a", "sideways") == ["a", "b"]


def test_move_does_not_mutate_the_stored_list():
    stored = ["a", "b", "c"]
    columns.move(stored, "b", "up")
    assert stored == ["a", "b", "c"]


def test_every_column_can_be_sorted_by():
    """A header with no sort key would be the one that silently does nothing
    when clicked."""
    for column in columns.COLUMNS:
        assert callable(columns.sort_key(column)), column.key


def test_the_ticker_column_sorts_by_its_ticker_not_by_the_holding():
    """`value` hands the renderer the whole holding, because the cell is a link
    plus a currency badge. Sorting on that would compare Holding objects, which
    raises."""
    holding = _FakeHolding(ticker="ALPHA")

    assert columns.sort_key(columns.BY_KEY["ticker"])(holding) == "ALPHA"


def test_the_drp_column_sorts_by_the_flag_not_the_word():
    """It renders "yes" or nothing, and nothing reads as blank — which would
    send every non-reinvesting holding to the bottom in both directions and
    make one of the two clicks useless."""
    on = _FakeHolding(ticker="A", drp=True)
    off = _FakeHolding(ticker="B", drp=False)
    getter = columns.sort_key(columns.BY_KEY["drp"])

    assert getter(on) is True
    assert getter(off) is False


def test_an_unknown_key_is_dropped_rather_than_breaking_the_page():
    """The forward-compatibility case: a preference naming a column this
    version no longer has must render the rest, not raise."""
    rendered = [c.key for c in columns.chosen(["ticker", "units", "column_from_v2"])]

    assert rendered == ["ticker", "units"]


def test_the_locked_column_is_added_back_even_if_the_preference_omits_it():
    """`ticker` is the row's identity and the link to its ledger. A stored
    preference without it — a stale row, a hand-edited value, a client that
    ignored the hidden input — must not produce anonymous numbers."""
    rendered = [c.key for c in columns.chosen(["units", "price"])]

    assert rendered[0] == "ticker"


def test_clean_keeps_a_real_selection():
    assert columns.clean(["ticker", "units", "gain_reporting"]) == ["ticker", "units", "gain_reporting"]


def test_clean_drops_keys_that_do_not_exist():
    """The submitted form is attacker-controlled; only names in the registry
    reach the database."""
    assert columns.clean(["units", "'; drop table", "price"]) == ["units", "price"]


def test_clean_falls_back_to_the_defaults_when_nothing_was_ticked():
    assert columns.clean([]) == columns.DEFAULTS


def test_clean_falls_back_when_only_the_locked_column_was_submitted():
    """Untick everything and the hidden input still carries `ticker`, so the
    submission is not empty — but a table of one identity column and no data is
    the same broken page. The fallback has to look past the locked ones.
    """
    assert columns.clean(["ticker"]) == columns.DEFAULTS


def test_clean_returns_a_copy_of_the_defaults():
    """It is stored on a JSON column and mutated nowhere, but returning the
    module-level list itself would make any future mutation global."""
    result = columns.clean([])
    result.append("name")

    assert "name" not in columns.DEFAULTS


def test_groups_contain_every_column_exactly_once():
    """The chooser renders from `groups()`; a column missing from it is a
    column nobody can turn on again once they have turned it off."""
    grouped = [c.key for _, items in columns.groups() for c in items]

    assert sorted(grouped) == sorted(c.key for c in columns.COLUMNS)


def test_a_group_appears_once_even_if_its_columns_are_not_adjacent():
    """`groups()` walks the declaration list and would open a second fieldset
    with the same legend if a group were ever split — worth pinning, because
    the fix is to merge and that is easy to lose in a later edit.
    """
    names = [name for name, _ in columns.groups()]

    assert len(names) == len(set(names))


# --------------------------------------------------------------------------- #
# Saving a choice
# --------------------------------------------------------------------------- #

def _stored(session_factory, email: str) -> list[str] | None:
    """What the user row actually holds. Not portfolio-scoped, so no binding is
    needed — which is the point of the column being on `user`."""
    with session_factory() as s:
        return s.scalars(select(User).where(User.email == email)).one().dashboard_columns


def test_a_fresh_account_has_no_preference_stored(client, session_factory):
    """NULL means "never chosen", which is what makes a later change to the
    defaults reach the people who never expressed an opinion."""
    email = make_login(client, session_factory)

    assert _stored(session_factory, email) is None


def test_saving_stores_the_selection_and_returns_to_the_dashboard(client, session_factory):
    email = make_login(client, session_factory)

    resp = client.post(
        "/profile/columns",
        data={"column": ["ticker", "units", "value_reporting"],
              "_csrf": session_csrf(session_factory)},
        headers={"accept": "text/html"}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert _stored(session_factory, email) == ["ticker", "units", "value_reporting"]


def _holdings_table(page: str) -> str:
    """Just the holdings table.

    The dashboard carries other markup that mentions instruments — the Record
    trade dialog's picker names every one of them — so a whole-page search
    answers a different question from the one these tests ask.
    """
    found = re.search(r'<table class="holdings".*?</table>', page, re.S)
    assert found, "no holdings table on the page"
    return found.group(0)


def _one_holding(session_factory) -> None:
    """A single open position, so the table has a row to render.

    A bare instrument is not enough — the dashboard lists *holdings*, which
    means an instrument with units still held.
    """
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME", name="Acme Industries")
        fac.add_trade(s, acme, "2026-01-05", "buy", 100, "4.00")
        s.commit()


def _header_row(page: str) -> str:
    """Just the table's `<thead>`.

    The chooser below the table renders every column's *label* whether it is
    showing or not, so matching on a bare word would pass no matter what the
    table did. Matching `>Units</th>` is not enough, because a
    header wraps its label in a sort link, so the markup no longer says it —
    narrowing to the header row does, and does not care what is inside it.
    """
    return re.search(r"<thead>.*?</thead>", page, re.S).group(0)


@freeze_time(ref.TODAY)
def test_a_saved_choice_changes_the_rendered_header_row(client, session_factory):
    """The end of the chain: registry → stored preference → the actual table.
    Asserting on the header is what proves the two halves are wired together.
    """
    make_login(client, session_factory)
    _one_holding(session_factory)

    client.post("/profile/columns",
                data={"column": ["ticker", "units"], "_csrf": session_csrf(session_factory)},
                headers={"accept": "text/html"})
    header = _header_row(client.get("/", headers={"accept": "text/html"}).text)

    assert ">Units<" in header
    # "Dividends" is a default, so its absence is the choice taking effect
    # rather than a column that was never there to begin with.
    assert ">Dividends<" not in header


@freeze_time(ref.TODAY)
def test_turning_a_column_on_shows_it(client, session_factory):
    """The other direction, so the test above cannot pass by rendering nothing.
    `name` is off by default precisely because it is wide — and asserting on
    the cell rather than the header proves the column's `value` callable runs.
    """
    make_login(client, session_factory)
    _one_holding(session_factory)

    before = client.get("/", headers={"accept": "text/html"}).text
    client.post("/profile/columns",
                data={"column": ["ticker", "units", "name"],
                      "_csrf": session_csrf(session_factory)},
                headers={"accept": "text/html"})
    after = client.get("/", headers={"accept": "text/html"}).text

    # Scoped to the holdings TABLE. The Record trade dialog renders an
    # instrument picker listing every instrument by name, so "is this string
    # anywhere on the page" stopped meaning "is this column showing" the day
    # that dialog landed. The claim was always about the table.
    assert "Acme Industries" not in _holdings_table(before), (
        "the name column is off, so it must not appear in the holdings table")
    assert "<td>Acme Industries</td>" in _holdings_table(after)


def test_the_locked_column_survives_a_save_made_from_the_real_form(client, session_factory):
    """The bug the hidden input exists for.

    A **disabled** checkbox is not submitted, so a form posted exactly as the
    browser would post it must still carry `ticker` — from the hidden input
    beside it. Scraping the rendered page rather than hand-writing the field is
    the whole point: hand-writing it would test the fallback in `chosen()`
    instead of the template that has to get this right.
    """
    make_login(client, session_factory)
    # A holding, because the column chooser now lives with the table it
    # configures: a dashboard with nothing recorded shows the first-run
    # invitation instead, and offering "Edit columns" for a table that isn't
    # there was never the point.
    _one_holding(session_factory)
    page = client.get("/", headers={"accept": "text/html"}).text

    # The visible checkbox is disabled — which is precisely what makes it
    # absent from the submission…
    checkbox = re.search(r'<input type="checkbox"[^>]*value="ticker"[^>]*>', page)
    assert checkbox is not None
    assert "disabled" in checkbox.group(0)
    # …so a hidden input has to carry it.
    assert '<input type="hidden" name="column" value="ticker">' in page

    client.post("/profile/columns",
                data={"column": ["ticker", "gain_reporting"],   # as the browser posts it
                      "_csrf": session_csrf(session_factory)},
                headers={"accept": "text/html"})

    assert "ticker" in _stored(session_factory, "user@example.test")


def test_an_empty_submission_falls_back_to_the_defaults(client, session_factory):
    """Untick the lot and press Save: the page must stay usable rather than
    rendering an empty table."""
    email = make_login(client, session_factory)

    client.post("/profile/columns", data={"_csrf": session_csrf(session_factory)},
                headers={"accept": "text/html"})

    assert _stored(session_factory, email) == columns.DEFAULTS


def test_saving_without_the_csrf_token_is_refused(client, session_factory):
    """Every mutating POST carries the session token; this one is no different
    for being a preference."""
    email = make_login(client, session_factory)

    resp = client.post("/profile/columns", data={"column": ["ticker", "units"]},
                       headers={"accept": "text/html"})

    assert resp.status_code == 403
    assert _stored(session_factory, email) is None


def test_saving_requires_a_session(client, session_factory):
    make_login(client, session_factory)
    token = session_csrf(session_factory)
    client.cookies.clear()

    resp = client.post("/profile/columns", data={"column": ["ticker"], "_csrf": token},
                       headers={"accept": "text/html"}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


# --------------------------------------------------------------------------- #
# Resetting
# --------------------------------------------------------------------------- #

def test_reset_writes_null_rather_than_a_copy_of_the_defaults(client, session_factory):
    """The distinction that matters later: NULL means "tracking the defaults",
    so changing `DEFAULTS` in a future version reaches this person. A stored
    copy would freeze today's list on their account forever.
    """
    email = make_login(client, session_factory)
    client.post("/profile/columns",
                data={"column": ["ticker", "units"], "_csrf": session_csrf(session_factory)},
                headers={"accept": "text/html"})
    assert _stored(session_factory, email) == ["ticker", "units"]

    resp = client.post("/profile/columns/reset",
                       data={"_csrf": session_csrf(session_factory)},
                       headers={"accept": "text/html"}, follow_redirects=False)

    assert resp.status_code == 303
    assert _stored(session_factory, email) is None


def test_reset_without_the_csrf_token_is_refused(client, session_factory):
    email = make_login(client, session_factory)
    client.post("/profile/columns",
                data={"column": ["ticker", "units"], "_csrf": session_csrf(session_factory)},
                headers={"accept": "text/html"})

    resp = client.post("/profile/columns/reset", headers={"accept": "text/html"})

    assert resp.status_code == 403
    assert _stored(session_factory, email) == ["ticker", "units"]


# --------------------------------------------------------------------------- #
# Whose choice is it?
# --------------------------------------------------------------------------- #

def test_the_choice_follows_the_person_not_the_portfolio(client, session_factory):
    """Two accounts sharing one portfolio choose independently — the reason the
    column lives on `user` and not on `portfolio` or on the membership.

    Both log in through the real form, one after the other, because the whole
    question is which row the route wrote to.
    """
    from app import auth as auth_mod
    from app.models import Portfolio

    first = make_login(client, session_factory, email="first@example.test")
    with session_factory() as s:
        portfolio = s.scalars(select(Portfolio)).one()
        second_user = fac.make_user(s, "second@example.test",
                                    password_hash=auth_mod.hash_password(PASSWORD))
        fac.add_member(s, portfolio, second_user, role="member")
        s.commit()

    client.post("/profile/columns",
                data={"column": ["ticker", "units"], "_csrf": session_csrf(session_factory)},
                headers={"accept": "text/html"})

    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                headers={"accept": "text/html"})
    client.cookies.clear()
    client.get("/login", headers={"accept": "text/html"})
    client.post("/login",
                data={"email": "second@example.test", "password": PASSWORD,
                      "_csrf": client.cookies.get(auth_mod.PRE_AUTH_CSRF_COOKIE)},
                headers={"accept": "text/html"}, follow_redirects=False)

    # The second person has expressed no preference and sees the defaults…
    assert _stored(session_factory, "second@example.test") is None
    client.post("/profile/columns",
                data={"column": ["ticker", "dividends"],
                      "_csrf": session_csrf(session_factory)},
                headers={"accept": "text/html"})

    # …and choosing differently leaves the first person's choice alone.
    assert _stored(session_factory, "second@example.test") == ["ticker", "dividends"]
    assert _stored(session_factory, first) == ["ticker", "units"]


# --------------------------------------------------------------------------- #
# Surviving a stored preference that has gone stale
# --------------------------------------------------------------------------- #

def test_a_dashboard_renders_with_a_preference_naming_a_removed_column(
        client, session_factory):
    """The end-to-end version of the unit test above: a row written by a
    previous version must not produce a 500 after an upgrade drops a column.
    """
    email = make_login(client, session_factory)
    _one_holding(session_factory)
    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        user.dashboard_columns = ["ticker", "units", "column_removed_in_v2"]
        s.commit()

    resp = client.get("/", headers={"accept": "text/html"})

    assert resp.status_code == 200
    assert ">Units<" in _header_row(resp.text)
    assert "/holding/ACME" in resp.text


# --------------------------------------------------------------------------- #
# The first run
# --------------------------------------------------------------------------- #

def test_a_dashboard_with_nothing_recorded_invites_rather_than_showing_zeros(
        client, session_factory):
    """Five $0.00 tiles and an empty table is a page that looks broken, not new.

    The claim is about which of the two states renders, so it asserts on both
    halves: the invitation is there, and the tiles that would otherwise be all
    zeroes are not.
    """
    make_login(client, session_factory)

    page = client.get("/", headers={"accept": "text/html"}).text

    assert "Nothing recorded yet" in page
    assert 'class="tiles"' not in page
    # Both ways in are offered — importing is the faster one for anyone
    # arriving with history, so it must not be reachable only from the nav.
    assert "/imports-exports" in page


def test_the_first_run_state_goes_away_once_something_is_recorded(
        client, session_factory):
    """The other direction, so the test above cannot pass by never rendering
    the real dashboard at all."""
    make_login(client, session_factory)
    _one_holding(session_factory)

    page = client.get("/", headers={"accept": "text/html"}).text

    assert "Nothing recorded yet" not in page
    assert 'class="tiles"' in page


def test_selling_everything_is_not_treated_as_a_first_run(client, session_factory):
    """A closed position is history, not a blank slate: the tiles still have a
    story (realised gain, dividends) and the FY pages need the year tabs. The
    empty state here is about the TABLE, not the page."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME", name="Acme Industries")
        fac.add_trade(s, acme, "2026-01-05", "buy", 100, "4.00")
        fac.add_trade(s, acme, "2026-02-05", "sell", 100, "5.00")
        s.commit()

    page = client.get("/", headers={"accept": "text/html"}).text

    assert "Nothing recorded yet" not in page
    assert 'class="tiles"' in page
    assert "No open positions." in page


# The three optional columns added in T35. All read a value off
# `queries.Holding`, so what matters is that the number is the one the header
# claims — two have a plausible-but-wrong formula sitting beside the right one.
# --------------------------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_the_reporting_avg_price_is_the_avg_price_converted_not_cost_over_units(pf):
    """The one that is easy to get wrong, and looks right when it is.

    `cost_aud / units` is tempting: both figures are already on the holding. But
    `cost` is buys only and includes brokerage, `avg_price` is buys AND DRP
    allocations and excludes it, and `units` is net of sales — so that division
    is a third quantity wearing the Avg price column's name. Two columns headed
    "Avg price" must differ ONLY by the exchange rate.
    """
    usd = fac.make_instrument(pf, "NOVA", exchange="NASDAQ", asset_class="share",
                              currency="USD", name="Nova Inc")
    # 100 @ $10 with $20 brokerage at 0.50, then a DRP of 10 @ $12 at 0.40.
    fac.add_trade(pf, usd, "2025-01-06", "buy", 100, "10.00",
                  brokerage="20.00", fx_rate="0.50")
    fac.add_trade(pf, usd, "2025-06-02", "drp", 10, "12.00", fx_rate="0.40")
    fac.add_fx(pf, "USDAUD", "2026-08-01", "0.60")
    fac.add_price(pf, usd, "2026-08-01", "15.00")

    holding = next(h for h in queries.all_holdings(pf)
                   if h.instrument.ticker == "NOVA")

    # (100 x 10 x 0.50) + (10 x 12 x 0.40) = 548.00 over 110 accumulated units.
    assert holding.avg_price_aud == Decimal("548.00") / Decimal(110)
    # The shortcut would have been (100 x 10 + 20) x 0.50 / 110 = 4.6363…,
    # which is neither the avg price nor in the same currency as one.
    assert holding.avg_price_aud != holding.cost_aud / holding.units
    # Brokerage is excluded here exactly as it is from the native avg price.
    assert holding.avg_price == Decimal("1120") / Decimal(110)   # 1000 + 120


@freeze_time(ref.TODAY)
def test_the_reporting_price_is_the_latest_close_at_the_latest_rate(pf):
    usd = fac.make_instrument(pf, "NOVA", exchange="NASDAQ", asset_class="share",
                              currency="USD", name="Nova Inc")
    fac.add_trade(pf, usd, "2025-01-06", "buy", 100, "10.00", fx_rate="0.50")
    fac.add_fx(pf, "USDAUD", "2026-08-01", "0.60")
    fac.add_price(pf, usd, "2026-08-01", "15.00")

    holding = next(h for h in queries.all_holdings(pf)
                   if h.instrument.ticker == "NOVA")

    assert holding.price_aud == Decimal("9.00")     # 15.00 x 0.60


@freeze_time(ref.TODAY)
def test_the_drp_residual_is_the_latest_balance_and_not_a_running_total(pf):
    """Each dividend stores the balance held AFTER it, so summing them counts
    the same few cents once per distribution."""
    acme = fac.make_instrument(pf, "ACME", name="Acme Pty")
    fac.add_trade(pf, acme, "2025-01-06", "buy", 100, "10.00")
    for date, residual in (("2025-03-10", "4.10"), ("2025-09-10", "7.25"),
                           ("2026-03-10", "2.80")):
        fac.add_dividend(pf, acme, date, "50.00").residual_carried = Decimal(residual)
    pf.flush()

    holding = next(h for h in queries.all_holdings(pf)
                   if h.instrument.ticker == "ACME")

    assert holding.drp_residual == Decimal("2.8000")


@freeze_time(ref.TODAY)
def test_a_residual_that_was_never_recorded_stays_distinct_from_a_recorded_zero(pf):
    """NULL means nobody knows; 0 means the registry kept nothing. 0018 made the
    column nullable for exactly this, and the column must not flatten it."""
    blank = fac.make_instrument(pf, "BLNK", name="Blank Pty")
    fac.add_trade(pf, blank, "2025-01-06", "buy", 100, "10.00")
    fac.add_dividend(pf, blank, "2025-03-10", "50.00")

    kept = fac.make_instrument(pf, "ZERO", name="Zero Pty")
    fac.add_trade(pf, kept, "2025-01-06", "buy", 100, "10.00")
    fac.add_dividend(pf, kept, "2025-03-10", "50.00").residual_carried = Decimal(0)
    pf.flush()

    holdings = {h.instrument.ticker: h for h in queries.all_holdings(pf)}

    assert holdings["BLNK"].drp_residual is None
    assert holdings["ZERO"].drp_residual == Decimal(0)


def test_the_new_columns_are_all_off_by_default():
    """Nobody's dashboard moves because a column was added."""
    for key in ("avg_price_reporting", "price_reporting", "residual"):
        assert key in columns.BY_KEY
        assert key not in columns.DEFAULTS


def test_the_reporting_columns_name_their_currency_in_the_header():
    """`avg_price` and `avg_price_reporting` share the label "Avg price" and are
    told apart ONLY by the currency, which is why the chooser always marks."""
    native = columns.BY_KEY["avg_price"].heading("USD")
    reporting = columns.BY_KEY["avg_price_reporting"].heading("USD")

    assert native == "Avg price (USD)"
    assert reporting == "Avg price (AUD)"
