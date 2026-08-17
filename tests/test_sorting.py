"""Clicking a column header to sort, on any table.

**Why this is a module and not part of `columns.py`.** The holdings table has a
registry — every column declared once, with a chooser. The FY report has three
tables of three different shapes (a holdings snapshot, dividend income, CGT
disposals), none of which is a list of `Holding`, and none of which wants a
column chooser on a tax report. Forcing them through the registry would mean
three more registries. Sorting is the only thing they share, so sorting is the
only thing that got shared.

The two rules worth stating out loud, because every naive implementation gets
them wrong:

  * **Blanks sort last in BOTH directions.** `sorted(reverse=True)` reverses the
    blanks along with everything else, so descending puts the empty rows on top.
  * **The sort key is never the displayed value.** "Ticker" renders a link and
    a currency badge; "DRP" renders "yes" or nothing. Sorting the rendered
    string sorts "1,234.50" as text and puts a row with no data wherever the
    browser felt like.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app import sorting


def rows(*values):
    """Rows carrying a value and their original position, so ties can be
    checked for stability."""
    return [{"v": v, "n": i} for i, v in enumerate(values)]


def values(result):
    return [r["v"] for r in result]


def get(row):
    return row["v"]


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #

def test_it_sorts_ascending():
    result = sorting.apply(rows(3, 1, 2), get, descending=False)
    assert values(result) == [1, 2, 3]


def test_it_sorts_descending():
    result = sorting.apply(rows(3, 1, 2), get, descending=True)
    assert values(result) == [3, 2, 1]


def test_none_sorts_last_ascending():
    result = sorting.apply(rows(3, None, 1), get, descending=False)
    assert values(result) == [1, 3, None]


def test_none_sorts_last_descending_too():
    """The one every naive comparator gets wrong: `reverse=True` moves the
    blanks to the top, so a closed position with no gain % becomes the most
    interesting row on the page."""
    result = sorting.apply(rows(3, None, 1), get, descending=True)
    assert values(result) == [3, 1, None]


def test_an_empty_string_counts_as_blank():
    """An instrument with no name recorded is missing data, not a name that
    sorts before every other name."""
    result = sorting.apply(rows("pear", "", "apple"), get, descending=False)
    assert values(result) == ["apple", "pear", ""]


def test_an_empty_string_is_last_descending_as_well():
    result = sorting.apply(rows("pear", "", "apple"), get, descending=True)
    assert values(result) == ["pear", "apple", ""]


def test_zero_is_not_blank():
    """Nought is a number and belongs in the ordering. Treating falsiness as
    absence would hide every position that has not moved today."""
    result = sorting.apply(rows(Decimal("2"), Decimal("0"), None), get, descending=False)
    assert values(result) == [Decimal("0"), Decimal("2"), None]


def test_false_is_not_blank():
    result = sorting.apply(rows(True, False, None), get, descending=False)
    assert values(result) == [False, True, None]


def test_ties_keep_their_original_order():
    """So a secondary sort is whatever the page's natural order was — for
    holdings, grouped by asset class then ticker."""
    result = sorting.apply(rows(1, 1, 1), get, descending=False)
    assert [r["n"] for r in result] == [0, 1, 2]


def test_ties_keep_their_original_order_descending_too():
    """`sorted(reverse=True)` is stable, but reversing a sorted list is not —
    which is the tempting way to write this."""
    result = sorting.apply(rows(1, 1, 1), get, descending=True)
    assert [r["n"] for r in result] == [0, 1, 2]


def test_decimals_and_ints_sort_together():
    result = sorting.apply(rows(Decimal("1.5"), 1, Decimal("2")), get, descending=False)
    assert values(result) == [1, Decimal("1.5"), Decimal("2")]


def test_the_input_list_is_not_mutated():
    original = rows(3, 1, 2)
    sorting.apply(original, get, descending=False)
    assert values(original) == [3, 1, 2]


def test_an_unsortable_mix_is_left_alone_rather_than_raising():
    """A column whose values are not comparable to each other would otherwise
    take the whole page down with a TypeError. The table is worth more than
    the ordering."""
    result = sorting.apply(rows("a", 1), get, descending=False)
    assert values(result) == ["a", 1]


# --------------------------------------------------------------------------- #
# Reading the request
# --------------------------------------------------------------------------- #

ALLOWED = {"ticker", "units", "gain_reporting"}


def test_no_parameters_means_unsorted():
    sort = sorting.read({}, "holdings", ALLOWED)
    assert sort.key is None
    assert sort.descending is False


def test_it_reads_its_own_table_only():
    """Two tables on one page must not sort each other. The FY report has
    three."""
    query = {"holdings_sort": "units", "income_sort": "cash"}
    assert sorting.read(query, "holdings", ALLOWED).key == "units"
    assert sorting.read(query, "income", {"cash"}).key == "cash"


def test_direction_defaults_to_ascending():
    assert sorting.read({"holdings_sort": "units"}, "holdings", ALLOWED).descending is False


def test_descending_is_read():
    sort = sorting.read({"holdings_sort": "units", "holdings_dir": "desc"}, "holdings", ALLOWED)
    assert sort.descending is True


def test_an_unknown_column_is_ignored():
    """A query parameter names an attribute to sort by, so it is caller-supplied
    input reaching a lookup. Anything not on the allow-list is dropped and the
    table renders unsorted — never a 500, and never an attribute nobody meant
    to expose."""
    sort = sorting.read({"holdings_sort": "password_hash"}, "holdings", ALLOWED)
    assert sort.key is None


def test_a_nonsense_direction_is_treated_as_ascending():
    sort = sorting.read({"holdings_sort": "units", "holdings_dir": "sideways"},
                        "holdings", ALLOWED)
    assert sort.descending is False


# --------------------------------------------------------------------------- #
# The header link
# --------------------------------------------------------------------------- #

def test_a_fresh_column_links_to_ascending():
    sort = sorting.read({}, "holdings", ALLOWED)
    assert sort.href("units") == "?holdings_sort=units"


def test_the_active_column_links_to_the_other_direction():
    sort = sorting.read({"holdings_sort": "units"}, "holdings", ALLOWED)
    assert sort.href("units") == "?holdings_sort=units&holdings_dir=desc"


def test_a_descending_column_links_back_to_ascending():
    sort = sorting.read({"holdings_sort": "units", "holdings_dir": "desc"},
                        "holdings", ALLOWED)
    assert sort.href("units") == "?holdings_sort=units"


def test_switching_column_starts_ascending_again():
    sort = sorting.read({"holdings_sort": "units", "holdings_dir": "desc"},
                        "holdings", ALLOWED)
    assert sort.href("ticker") == "?holdings_sort=ticker"


def test_other_query_parameters_survive():
    """The FY report lives at `/?fy=2026`. A header link that dropped that
    would sort the wrong year — silently, and with plausible numbers."""
    sort = sorting.read({"fy": "2026"}, "snapshot", {"units"})
    assert sort.href("units") == "?fy=2026&snapshot_sort=units"


def test_another_tables_sort_survives():
    query = {"income_sort": "cash", "income_dir": "desc"}
    sort = sorting.read(query, "snapshot", {"units"})
    href = sort.href("units")
    assert "income_sort=cash" in href and "income_dir=desc" in href


def test_the_link_is_url_encoded():
    sort = sorting.read({"q": "a b&c"}, "holdings", ALLOWED)
    assert " " not in sort.href("units")
    assert "a+b%26c" in sort.href("units") or "a%20b%26c" in sort.href("units")


# --------------------------------------------------------------------------- #
# What the header shows
# --------------------------------------------------------------------------- #

def test_only_the_sorted_column_gets_an_arrow():
    """An arrow on every header is decoration; it stops saying which one is
    active, which is the entire job."""
    sort = sorting.read({"holdings_sort": "units"}, "holdings", ALLOWED)
    assert sort.arrow("units")
    assert sort.arrow("ticker") == ""
    assert sort.arrow("gain_reporting") == ""


def test_the_arrow_points_the_way_it_is_sorted():
    up = sorting.read({"holdings_sort": "units"}, "holdings", ALLOWED)
    down = sorting.read({"holdings_sort": "units", "holdings_dir": "desc"},
                        "holdings", ALLOWED)
    assert up.arrow("units") != down.arrow("units")


def test_aria_sort_says_what_the_arrow_says():
    """The arrow is a glyph. A screen reader needs the table semantics."""
    sort = sorting.read({"holdings_sort": "units"}, "holdings", ALLOWED)
    assert sort.aria("units") == "ascending"
    assert sort.aria("ticker") == "none"

    down = sorting.read({"holdings_sort": "units", "holdings_dir": "desc"},
                        "holdings", ALLOWED)
    assert down.aria("units") == "descending"


@pytest.mark.parametrize("key", ["units", "ticker", "gain_reporting"])
def test_nothing_is_marked_sorted_when_nothing_is(key):
    sort = sorting.read({}, "holdings", ALLOWED)
    assert sort.arrow(key) == ""
    assert sort.aria(key) == "none"


# --------------------------------------------------------------------------- #
# Through the routes — the half that proves any of the above is wired up
# --------------------------------------------------------------------------- #

import re  # noqa: E402

from freezegun import freeze_time  # noqa: E402

import fixture_portfolio as ref  # noqa: E402
from test_routes import bind_to_only_portfolio, make_login, session_csrf  # noqa: E402

HTML = {"accept": "text/html"}


def tickers_in_order(page: str, table_class: str = "holdings") -> list[str]:
    """The ticker of each row, top to bottom, from the named table.

    Reads the rendered HTML rather than a context variable on purpose: the
    ordering is only real if it survives the template.
    """
    table = re.search(rf'<table class="{table_class}">.*?</table>', page, re.S)
    assert table, f"no {table_class} table on the page"
    body = re.search(r"<tbody>.*?</tbody>", table.group(0), re.S)
    return re.findall(r'/holding/([A-Z0-9.]+)"', body.group(0))


def _portfolio(session_factory) -> None:
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)


@freeze_time(ref.TODAY)
def test_the_holdings_table_is_unsorted_by_default(client, session_factory):
    """Natural order is grouped by asset class then ticker, and that is a
    deliberate order — arriving at the page must not silently replace it."""
    make_login(client, session_factory)
    _portfolio(session_factory)

    page = client.get("/", headers=HTML).text

    assert tickers_in_order(page) == sorted(tickers_in_order(page))


@freeze_time(ref.TODAY)
def test_sorting_by_ticker_descending_reverses_the_rows(client, session_factory):
    make_login(client, session_factory)
    _portfolio(session_factory)

    page = client.get("/?holdings_sort=ticker&holdings_dir=desc", headers=HTML).text

    assert tickers_in_order(page) == sorted(tickers_in_order(page), reverse=True)


@freeze_time(ref.TODAY)
def test_sorting_by_a_number_orders_by_the_number(client, session_factory):
    """Not by its rendered string — "1,234.50" sorts before "9.00" as text."""
    make_login(client, session_factory)
    _portfolio(session_factory)

    ascending = client.get("/?holdings_sort=value_reporting", headers=HTML).text
    descending = client.get("/?holdings_sort=value_reporting&holdings_dir=desc",
                            headers=HTML).text

    assert tickers_in_order(ascending) == list(reversed(tickers_in_order(descending)))
    assert tickers_in_order(ascending) != tickers_in_order(descending)


@freeze_time(ref.TODAY)
def test_exactly_one_header_carries_an_arrow(client, session_factory):
    make_login(client, session_factory)
    _portfolio(session_factory)

    page = client.get("/?holdings_sort=value_reporting", headers=HTML).text
    header = re.search(r"<thead>.*?</thead>", page, re.S).group(0)

    assert header.count("▲") + header.count("▼") == 1


@freeze_time(ref.TODAY)
def test_no_header_carries_an_arrow_when_nothing_is_sorted(client, session_factory):
    make_login(client, session_factory)
    _portfolio(session_factory)

    page = client.get("/", headers=HTML).text
    header = re.search(r"<thead>.*?</thead>", page, re.S).group(0)

    assert "▲" not in header and "▼" not in header


@freeze_time(ref.TODAY)
def test_the_active_header_is_marked_for_a_screen_reader(client, session_factory):
    make_login(client, session_factory)
    _portfolio(session_factory)

    page = client.get("/?holdings_sort=value_reporting&holdings_dir=desc", headers=HTML).text
    header = re.search(r"<thead>.*?</thead>", page, re.S).group(0)

    assert header.count('aria-sort="descending"') == 1
    assert 'aria-sort="ascending"' not in header


@freeze_time(ref.TODAY)
def test_an_unknown_sort_column_renders_the_page_unsorted(client, session_factory):
    """A hand-edited URL, or a link from a version that had a column this one
    does not. It must not be a 500."""
    make_login(client, session_factory)
    _portfolio(session_factory)

    page = client.get("/?holdings_sort=password_hash", headers=HTML)

    assert page.status_code == 200
    assert tickers_in_order(page.text) == sorted(tickers_in_order(page.text))


@freeze_time(ref.TODAY)
def test_a_hidden_column_can_still_be_sorted_by(client, session_factory):
    """The allow-list is every column that exists, not every column showing —
    otherwise a stale link 500s or silently does nothing, and the set of valid
    links would change per user."""
    make_login(client, session_factory)
    _portfolio(session_factory)

    page = client.get("/?holdings_sort=exchange", headers=HTML)

    assert page.status_code == 200


# ---- the FY report -------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_the_fy_snapshot_sorts_too(client, session_factory):
    make_login(client, session_factory)
    _portfolio(session_factory)

    page = client.get(f"/?fy={ref.FY2024}&snapshot_sort=ticker&snapshot_dir=desc",
                      headers=HTML).text

    assert tickers_in_order(page) == sorted(tickers_in_order(page), reverse=True)


@freeze_time(ref.TODAY)
def test_an_fy_header_link_keeps_the_financial_year(client, session_factory):
    """Losing `fy` would sort a different year, silently, with numbers that
    look entirely plausible — the worst kind of wrong."""
    make_login(client, session_factory)
    _portfolio(session_factory)

    page = client.get(f"/?fy={ref.FY2024}", headers=HTML).text

    for href in re.findall(r'href="(\?[^"]*_sort=[^"]*)"', page):
        assert f"fy={ref.FY2024}" in href, href


@freeze_time(ref.TODAY)
def test_the_three_fy_tables_sort_independently(client, session_factory):
    """One `?sort=` shared between them would sort all three at once."""
    make_login(client, session_factory)
    _portfolio(session_factory)

    page = client.get(f"/?fy={ref.FY2024}&snapshot_sort=units", headers=HTML).text

    # Count the SORT arrows, not the glyph. `▲`/`▼` also carry direction inside
    # gain cells — the pair that makes up/down readable without relying on
    # colour — so counting the character asked a different question from the
    # one this test is named after, and started failing the day the FY snapshot
    # gained its gain percentage.
    import re as _re

    assert len(_re.findall(r'class="sortarrow"', page)) == 1


@freeze_time(ref.TODAY)
def test_the_disposals_table_sorts(client, session_factory):
    make_login(client, session_factory)
    _portfolio(session_factory)

    page = client.get(f"/?fy={ref.FY2024}&disposals_sort=gain&disposals_dir=desc",
                      headers=HTML)

    assert page.status_code == 200
    assert 'aria-sort="descending"' in page.text


# ---- reordering the columns ----------------------------------------------- #

def _stored(session_factory) -> list[str] | None:
    from sqlalchemy import select

    from app.models import User
    with session_factory() as s:
        return s.scalars(select(User)).first().dashboard_columns


@freeze_time(ref.TODAY)
def test_moving_a_column_up_changes_the_rendered_header_row(client, session_factory):
    make_login(client, session_factory)
    _portfolio(session_factory)
    csrf = session_csrf(session_factory)
    client.post("/profile/columns",
                data={"column": ["ticker", "units", "price"], "_csrf": csrf}, headers=HTML)

    client.post("/profile/columns/order",
                data={"order": ["ticker", "units", "price"], "move": "price:up",
                      "_csrf": csrf}, headers=HTML)

    assert _stored(session_factory) == ["ticker", "price", "units"]
    header = re.search(r"<thead>.*?</thead>",
                       client.get("/", headers=HTML).text, re.S).group(0)
    assert header.index(">Price") < header.index(">Units")


@freeze_time(ref.TODAY)
def test_a_full_order_can_be_submitted_at_once(client, session_factory):
    """What the drag enhancement posts: the whole list, no `move`. Same route,
    so the keyboard path and the mouse path cannot diverge."""
    make_login(client, session_factory)
    _portfolio(session_factory)
    csrf = session_csrf(session_factory)

    client.post("/profile/columns/order",
                data={"order": ["price", "units", "ticker"], "_csrf": csrf}, headers=HTML)

    assert _stored(session_factory) == ["price", "units", "ticker"]


@freeze_time(ref.TODAY)
def test_reordering_cannot_smuggle_in_a_column_that_does_not_exist(client, session_factory):
    make_login(client, session_factory)
    _portfolio(session_factory)
    csrf = session_csrf(session_factory)

    client.post("/profile/columns/order",
                data={"order": ["ticker", "units", "../../etc/passwd"], "_csrf": csrf},
                headers=HTML)

    assert _stored(session_factory) == ["ticker", "units"]


@freeze_time(ref.TODAY)
def test_reordering_without_the_csrf_token_is_refused(client, session_factory):
    make_login(client, session_factory)
    _portfolio(session_factory)

    response = client.post("/profile/columns/order",
                           data={"order": ["price", "ticker"]}, headers=HTML)

    assert response.status_code == 403
    assert _stored(session_factory) is None


def test_reordering_requires_a_session(client):
    response = client.post("/profile/columns/order", data={"order": ["price"]},
                           headers=HTML, follow_redirects=False)

    assert response.status_code in (302, 303, 401, 403)


@freeze_time(ref.TODAY)
def test_the_chooser_offers_a_move_control_for_every_shown_column(client, session_factory):
    """Keyboard-operable without a mouse and without JavaScript, which is the
    requirement drag-and-drop cannot meet on its own."""
    make_login(client, session_factory)
    _portfolio(session_factory)

    page = client.get("/", headers=HTML).text
    # `class="colchooser menu"` — match the class among others
    # rather than pinning the whole attribute, or every class added to that
    # element breaks a test that does not care about classes.
    found = re.search(r'<details class="[^"]*colchooser[^"]*">.*?</details>', page, re.S)
    assert found, "no column chooser on the page"
    chooser = found.group(0)

    from app import columns as col
    shown = col.chosen(None)
    assert chooser.count('name="move"') == 2 * len(shown)


# --------------------------------------------------------------------------- #
# A guard against the trap that produced this file's own worst bug
# --------------------------------------------------------------------------- #

def test_no_template_puts_markup_in_its_title_block():
    """`{% block title %}` renders inside `<title>`, which the browser parses
    as text — so a `<script>` appended there is not a script that fails to
    load, it is a tag printed in the browser tab.

    It happens because appending to a Jinja template means finding the LAST
    `{% endblock %}`, and the title block's `{% endblock %}` is the first one
    in the file. It has now happened twice. This is cheaper than remembering.
    """
    import re as _re
    from pathlib import Path as _Path

    templates = _Path(__file__).resolve().parent.parent / "app" / "templates"
    for path in sorted(templates.glob("*.html")):
        block = _re.search(r"{% block title %}(.*?){% endblock %}",
                           path.read_text(), _re.S)
        if block:
            assert "<" not in block.group(1), f"{path.name}: markup inside <title>"


# `back` — a convenience that must not become an open redirect. It is a form
# field that ends up in a Location header: decisions.md #58.

@freeze_time(ref.TODAY)
def test_the_sort_survives_saving_columns(client, session_factory):
    """The reason `back` exists at all."""
    make_login(client, session_factory)
    _portfolio(session_factory)

    response = client.post(
        "/profile/columns",
        data={"column": ["ticker", "units"], "back": "?holdings_sort=units",
              "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False)

    assert response.headers["location"] == "/?holdings_sort=units"


@freeze_time(ref.TODAY)
@pytest.mark.parametrize("hostile", [
    "https://evil.example/",       # absolute
    "//evil.example/",             # scheme-relative, the one people forget
    "/profile?x=1",                # a path, not a query
    "?a=1\nLocation: /evil",       # header injection
    "?a=1 b",                      # space, which would need encoding
    "javascript:alert(1)",
])
def test_a_hostile_back_value_is_ignored(client, session_factory, hostile):
    """It may only ever be appended to "/" — no scheme, no host, no path. So
    anything that is not purely a query string is dropped and the redirect
    falls back to the dashboard."""
    make_login(client, session_factory)
    _portfolio(session_factory)

    response = client.post(
        "/profile/columns",
        data={"column": ["ticker", "units"], "back": hostile,
              "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False)

    assert response.headers["location"] == "/"


@freeze_time(ref.TODAY)
def test_a_hostile_back_value_is_ignored_when_reordering_too(client, session_factory):
    """Two routes take it, so two routes are checked. One guarded and one not
    is the usual shape of this bug."""
    make_login(client, session_factory)
    _portfolio(session_factory)

    response = client.post(
        "/profile/columns/order",
        data={"order": ["ticker", "units"], "back": "https://evil.example/",
              "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False)

    assert response.headers["location"] == "/"


# Numbers are sacred: sorting reorders the lists totals are computed from and
# must not move a single figure — decisions.md #56.

def _tiles(page: str) -> list[str]:
    return re.findall(r'<span class="big">([^<]*)</span>', page)


@freeze_time(ref.TODAY)
def test_sorting_the_holdings_table_changes_no_total(client, session_factory):
    make_login(client, session_factory)
    _portfolio(session_factory)

    plain = client.get("/", headers=HTML).text
    for query in ("?holdings_sort=value_reporting",
                  "?holdings_sort=value_reporting&holdings_dir=desc",
                  "?holdings_sort=ticker&holdings_dir=desc",
                  "?holdings_sort=gain_pct"):
        assert _tiles(client.get(query, headers=HTML).text) == _tiles(plain), query


@freeze_time(ref.TODAY)
def test_sorting_the_fy_report_changes_no_total(client, session_factory):
    """Its tables are reordered after the report is built, so the summary
    underneath them cannot see the change — including the CGT working, which
    is the figure somebody puts in a tax return."""
    make_login(client, session_factory)
    _portfolio(session_factory)

    plain = client.get(f"/?fy={ref.FY2024}", headers=HTML).text
    for query in (f"/?fy={ref.FY2024}&snapshot_sort=gain_aud&snapshot_dir=desc",
                  f"/?fy={ref.FY2024}&income_sort=cash",
                  f"/?fy={ref.FY2024}&disposals_sort=gain&disposals_dir=desc"):
        page = client.get(query, headers=HTML).text
        assert _tiles(page) == _tiles(plain), query
        # The CGT summary table is not a tile, and it is the one somebody
        # copies into a tax return. It carries no sortable headers, so it can
        # be compared whole.
        assert _cgt_summary(page) == _cgt_summary(plain), query


def _cgt_summary(page: str) -> str:
    """The working underneath the disposals — gains, losses, discount, net."""
    tables = re.findall(r"<table>.*?</table>", page, re.S)
    summary = [x for x in tables if "Net capital gain" in x]
    assert summary, "no CGT summary table on the page"
    return summary[-1]


# Headers that cycle through a pair of keys — amount ↑, % ↑, amount ↓, % ↓ —
# and render which of the four they are in: decisions.md #25.

PAIR = ("gain_reporting", "gain_pct")
MARKS = ("$", "%")
PAIR_ALLOWED = {"gain_reporting", "gain_pct"}


def _sorted_by(key=None, descending=False):
    query = {}
    if key:
        query["holdings_sort"] = key
    if descending:
        query["holdings_dir"] = "desc"
    return sorting.read(query, "holdings", PAIR_ALLOWED)


def test_an_unsorted_pair_starts_at_the_amount_ascending():
    assert _sorted_by().cycle(PAIR) == "?holdings_sort=gain_reporting"


def test_the_cycle_goes_amount_up_then_percent_up():
    assert _sorted_by("gain_reporting").cycle(PAIR) == "?holdings_sort=gain_pct"


def test_then_amount_down():
    assert _sorted_by("gain_pct").cycle(PAIR) == (
        "?holdings_sort=gain_reporting&holdings_dir=desc")


def test_then_percent_down():
    assert _sorted_by("gain_reporting", descending=True).cycle(PAIR) == (
        "?holdings_sort=gain_pct&holdings_dir=desc")


def test_and_back_to_the_start():
    """Four states, not five: there is no 'off'. Clicking the nav link is how
    you get back to the table's natural order."""
    assert _sorted_by("gain_pct", descending=True).cycle(PAIR) == (
        "?holdings_sort=gain_reporting")


def test_the_whole_cycle_visits_each_state_once():
    """Walked rather than asserted step by step, so a reordering that still
    passes each pairwise test cannot pass this one."""
    seen = []
    sort = _sorted_by()
    for _ in range(4):
        href = sort.cycle(PAIR)
        key = href.split("holdings_sort=")[1].split("&")[0]
        descending = "holdings_dir=desc" in href
        seen.append((key, descending))
        sort = _sorted_by(key, descending)

    assert seen == [("gain_reporting", False), ("gain_pct", False),
                    ("gain_reporting", True), ("gain_pct", True)]
    assert sort.cycle(PAIR) == "?holdings_sort=gain_reporting", "the cycle does not close"


def test_the_header_shows_which_half_is_active():
    """The orientation problem: a tooltip explains the mechanism, this says
    where you are."""
    assert _sorted_by("gain_reporting").unit(PAIR, MARKS) == "$"
    assert _sorted_by("gain_pct").unit(PAIR, MARKS) == "%"


def test_an_unsorted_pair_shows_no_state_at_all():
    """Exactly one header on a table wears a state, or it stops meaning
    'this is the sorted one'."""
    assert _sorted_by("units").unit(PAIR, MARKS) == ""
    assert _sorted_by("units").pair_arrow(PAIR) == ""
    assert _sorted_by("units").pair_aria(PAIR) == "none"


def test_the_arrow_and_aria_follow_either_half_of_the_pair():
    """Sorted by the percentage, the Gain header is still the sorted one — a
    plain `on(key)` check would call it unsorted and drop its arrow."""
    for key in PAIR:
        assert _sorted_by(key).pair_arrow(PAIR) == sorting.UP
        assert _sorted_by(key, descending=True).pair_arrow(PAIR) == sorting.DOWN
        assert _sorted_by(key).pair_aria(PAIR) == "ascending"
        assert _sorted_by(key, descending=True).pair_aria(PAIR) == "descending"


def test_a_paired_header_keeps_the_other_query_parameters():
    """Same trap as the plain header: losing `?fy=2026` sorts a different
    financial year, silently, with plausible numbers."""
    sort = sorting.read({"fy": "2026", "holdings_sort": "gain_reporting"},
                        "holdings", PAIR_ALLOWED)

    assert "fy=2026" in sort.cycle(PAIR)
