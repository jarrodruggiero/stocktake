"""The DCA Schedule page's shape.

This page is what its name says: a **schedule** you follow, with
the plan that generates it behind a button. Two sections went — "Up next" and
"History" — because the calendar already showed both, and the only things they
carried that the calendar cannot (Record buy, Skip) moved onto the row they act
on.

These tests pin that arrangement, because it is the part a later edit undoes
without noticing.
"""

from __future__ import annotations

import re

from freezegun import freeze_time

import factories as fac
from test_routes import make_login

HTML = {"accept": "text/html"}

TODAY = "2026-08-08"


def _plan(client, session_factory, *, tickers="ALPHA", amount="500"):
    make_login(client, session_factory)
    with session_factory() as s:
        fac.make_instrument(s, "ALPHA")
        s.commit()
    token = re.search(
        r'name="_csrf" value="([^"]+)"', client.get("/schedule", headers=HTML).text).group(1)
    client.post("/schedule/save",
                data={"_csrf": token, "name": "Regular buys", "interval_days": "28",
                      "amount": amount, "brokerage": "9.50",
                      "start_date": "2026-01-05", "tickers": tickers},
                headers=HTML, follow_redirects=True)
    return client.get("/schedule", headers=HTML).text


@freeze_time(TODAY)
def test_the_page_is_called_the_dca_schedule(client, session_factory):
    """'Plan' on its own said nothing about what the page was for."""
    make_login(client, session_factory)

    page = client.get("/schedule", headers=HTML).text

    assert "DCA Schedule" in page


@freeze_time(TODAY)
def test_the_redundant_sections_are_gone(client, session_factory):
    """Both restated the calendar. If either comes back, so does the argument
    for why the calendar is not enough."""
    page = _plan(client, session_factory)

    assert "<h2>Up next</h2>" not in page
    assert "<h2>History</h2>" not in page


@freeze_time(TODAY)
def test_record_buy_and_skip_survived_the_sections_that_held_them(client, session_factory):
    """The one thing that had to be kept when Up next went."""
    page = _plan(client, session_factory)

    assert "/schedule/complete" in page, "Record buy is gone"
    assert 'action="/schedule/skip"' in page, "Skip is gone"


@freeze_time(TODAY)
def test_they_appear_once_not_on_every_row(client, session_factory):
    """The rotation has ONE next step. Offering Record buy on every row implies
    you can record the third buy before the first."""
    page = _plan(client, session_factory)

    assert page.count('action="/schedule/skip"') == 1


@freeze_time(TODAY)
def test_the_calendar_comes_before_coming_up(client, session_factory):
    """65/35, calendar on the left — so in source order the calendar is first.
    On a narrow screen that is also the stacking order, which is the reason it
    matters beyond aesthetics."""
    page = _plan(client, session_factory)

    assert page.index('class="calendar"') < page.index("Coming up")


@freeze_time(TODAY)
def test_the_plan_editor_is_a_dialog_holding_the_preview(client, session_factory):
    page = _plan(client, session_factory)

    found = re.search(r'<dialog id="plandialog".*?</dialog>', page, re.S)
    assert found, "the plan editor is not a dialog"
    dialog = found.group(0)

    assert 'action="/schedule/save"' in dialog, "the form is not in the dialog"
    assert "Preview plan" in dialog, "the preview is not in the dialog"


@freeze_time(TODAY)
def test_the_preview_shows_dates_against_tickers(client, session_factory):
    """A rotation of tickers with no dates does not
    answer "when do I next buy ALPHA"."""
    page = _plan(client, session_factory)
    preview = re.search(r'<div class="previewscroll">(.*?)</div>', page, re.S).group(1)

    assert "<th>Date</th>" in preview
    assert "<th>Ticker</th>" in preview
    assert re.search(r"<td>\d{4}-\d{2}-\d{2}</td>", preview), "no dates in the preview"


@freeze_time(TODAY)
def test_the_preview_is_long_enough_to_be_worth_scrolling(client, session_factory):
    """A preview you cannot scroll is a list, not a preview."""
    page = _plan(client, session_factory)
    preview = re.search(r'<div class="previewscroll">(.*?)</div>', page, re.S).group(1)

    assert len(re.findall(r"<td>\d{4}-\d{2}-\d{2}</td>", preview)) >= 12


@freeze_time(TODAY)
def test_the_deep_link_opens_the_editor_without_script(client, session_factory):
    """`<dialog>` with no `open` is display:none, so with scripting off the
    form would be unreachable — the same trap the instruments page hit."""
    page = _plan(client, session_factory)
    assert "<noscript>" in page

    opened = client.get("/schedule?edit=1", headers=HTML).text
    assert re.search(r'<dialog id="plandialog"\s+open>', opened), (
        "?edit=1 does not open the dialog, so the no-script path is broken")


@freeze_time(TODAY)
def test_a_first_run_says_there_is_no_plan_rather_than_sitting_blank(client, session_factory):
    """An empty panel reads as something having failed."""
    make_login(client, session_factory)

    page = client.get("/schedule", headers=HTML).text

    assert "No plan yet" in page
    assert "Create a plan" in page, "nothing tells them how to fix it"


@freeze_time(TODAY)
def test_amounts_in_coming_up_carry_a_currency_symbol(client, session_factory):
    """They sit in a list, not a table, so no column header names the currency
    for them — these are the case `cash` exists for."""
    page = _plan(client, session_factory)
    coming = re.search(r'<ul class="upnext">(.*?)</ul>', page, re.S).group(1)

    assert "$500.00" in coming


# --------------------------------------------------------------------------- #
# The rotation editor
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_a_repeated_ticker_survives_the_round_trip(client, session_factory):
    """**The weighting feature.** A ticker listed twice is bought twice as
    often, so the rotation is an ordered list with repeats — not a set. The
    save path builds a `set()` for the instrument lookup, which is exactly the
    sort of place a de-duplication creeps in unnoticed.
    """
    make_login(client, session_factory)
    with session_factory() as s:
        fac.make_instrument(s, "ALPHA")
        fac.make_instrument(s, "BETAX")
        s.commit()
    token = re.search(
        r'name="_csrf" value="([^"]+)"', client.get("/schedule", headers=HTML).text).group(1)

    client.post("/schedule/save",
                data={"_csrf": token, "name": "Weighted", "interval_days": "28",
                      "amount": "500", "brokerage": "9.50",
                      "start_date": "2026-01-05", "tickers": "ALPHA, BETAX, ALPHA"},
                headers=HTML, follow_redirects=True)
    page = client.get("/schedule?edit=1", headers=HTML).text

    # Order and repeats both, read back out of the field that submits.
    field = re.search(r'<textarea name="tickers"[^>]*>(.*?)</textarea>', page, re.S)
    assert field is not None
    assert field.group(1).strip() == "ALPHA, BETAX, ALPHA"


@freeze_time(TODAY)
def test_the_rotation_palette_offers_every_instrument(client, session_factory):
    """The chips you drag from. One per instrument, carrying its ticker — the
    script builds nothing from a hard-coded list."""
    make_login(client, session_factory)
    with session_factory() as s:
        fac.make_instrument(s, "ALPHA")
        fac.make_instrument(s, "BETAX")
        s.commit()

    page = client.get("/schedule", headers=HTML).text

    palette = re.search(r'<ul class="rotsource".*?</ul>', page, re.S)
    assert palette is not None
    assert 'data-ticker="ALPHA"' in palette.group(0)
    assert 'data-ticker="BETAX"' in palette.group(0)


@freeze_time(TODAY)
def test_the_rotation_still_submits_from_a_plain_textarea(client, session_factory):
    """The no-JavaScript path, and the reason the editor needs no second
    parser: the chips are an interface onto this one field, which is still
    `required` in the markup so a scriptless browser enforces it."""
    make_login(client, session_factory)
    with session_factory() as s:
        fac.make_instrument(s, "ALPHA")
        s.commit()

    page = client.get("/schedule", headers=HTML).text

    field = re.search(r'<textarea name="tickers"[^>]*>', page)
    assert field is not None
    assert "required" in field.group(0)
    assert "/static/rotation.js" in page


# --------------------------------------------------------------------------- #
# The live preview
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_the_preview_projects_a_draft_without_saving_it(client, session_factory):
    """**The point of the endpoint.** Rearranging chips must not disturb the
    schedule anyone is following, so this asks for dates from a rotation the
    database has never seen and then proves the stored plan is untouched.
    """
    _plan(client, session_factory, tickers="ALPHA")
    with session_factory() as s:
        fac.make_instrument(s, "BETAX")
        s.commit()
    token = re.search(
        r'name="_csrf" value="([^"]+)"', client.get("/schedule", headers=HTML).text).group(1)

    resp = client.post("/schedule/preview",
                       data={"_csrf": token, "interval_days": "28",
                             "start_date": "2026-01-05", "tickers": "BETAX, ALPHA, BETAX"})

    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert [r["ticker"] for r in rows[:3]] == ["BETAX", "ALPHA", "BETAX"]
    # …and the saved plan still says what it said.
    after = client.get("/schedule?edit=1", headers=HTML).text
    field = re.search(r'<textarea name="tickers"[^>]*>(.*?)</textarea>', after, re.S)
    assert field.group(1).strip() == "ALPHA"


@freeze_time(TODAY)
def test_the_preview_uses_the_same_arithmetic_as_the_real_schedule(
        client, session_factory):
    """The reason it is a round trip to the server rather than a few lines of
    JavaScript. Previewing the rotation that is already saved must reproduce
    the page's own schedule exactly — if the two ever diverge, one of them is
    lying to somebody about when they are next buying."""
    page = _plan(client, session_factory, tickers="ALPHA")
    token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)

    resp = client.post("/schedule/preview",
                       data={"_csrf": token, "interval_days": "28",
                             "start_date": "2026-01-05", "tickers": "ALPHA"})

    # The dates the page rendered server-side, from `plans.schedule`.
    shown = re.findall(r"<td>(\d{4}-\d{2}-\d{2})</td>", page)
    assert shown, "the page rendered no preview rows to compare against"
    assert [r["date"] for r in resp.json()["rows"]][:len(shown)] == shown


@freeze_time(TODAY)
def test_the_preview_refuses_a_ticker_that_could_not_be_saved(
        client, session_factory):
    """Drawing a confident set of dates for a rotation the save path will
    reject is worse than saying nothing."""
    page = _plan(client, session_factory, tickers="ALPHA")
    token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)

    resp = client.post("/schedule/preview",
                       data={"_csrf": token, "interval_days": "28",
                             "start_date": "", "tickers": "ALPHA, NOPE"})

    assert resp.json()["rows"] == []
    assert "NOPE" in resp.json()["why"]


@freeze_time(TODAY)
def test_the_preview_needs_a_csrf_token_like_any_other_post(
        client, session_factory):
    _plan(client, session_factory, tickers="ALPHA")

    resp = client.post("/schedule/preview",
                       data={"interval_days": "28", "start_date": "", "tickers": "ALPHA"})

    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# The price feed, while it runs
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_the_dashboard_no_longer_reloads_itself_on_a_timer(client, session_factory):
    """It must not carry `setTimeout(() => location.reload(), 4000)` inline: a
    blind timer that threw away your sort order and your half-filled dialog
    every four seconds, and an inline script the CSP had to allow. The page now
    flags the state and `feedwatch.js` watches for it to end."""
    from app import main

    make_login(client, session_factory)
    main.feed_status["running"] = True
    try:
        page = client.get("/", headers=HTML).text
    finally:
        main.feed_status["running"] = False

    assert "location.reload" not in page
    assert 'data-feed-running="1"' in page
    assert "/static/feedwatch.js" in page


@freeze_time(TODAY)
def test_the_page_does_not_flag_a_feed_that_is_not_running(client, session_factory):
    """The other direction: the watcher must not poll on a quiet page."""
    make_login(client, session_factory)

    page = client.get("/", headers=HTML).text

    assert "data-feed-running" not in page


@freeze_time(TODAY)
def test_the_feed_status_endpoint_says_whether_it_is_running(client, session_factory):
    from app import main

    make_login(client, session_factory)
    main.feed_status["running"] = True
    try:
        running = client.get("/feed/status").json()
    finally:
        main.feed_status["running"] = False
    stopped = client.get("/feed/status").json()

    assert running["running"] is True
    assert stopped["running"] is False


def test_the_feed_status_endpoint_needs_a_session(client):
    """It reports on the installation, so it is not public."""
    resp = client.get("/feed/status", follow_redirects=False)

    assert resp.status_code in (302, 303, 401, 403)


def test_the_status_endpoint_reports_when_live_prices_last_landed(client, session_factory,
                                                                  monkeypatch):
    """What an open dashboard compares against what it was rendered with.

    Without this the only way to see a fresh live price was to reload by hand,
    which makes a refresh that is working look like one that is not.
    """
    import datetime as dt

    import app.main as main
    from test_routes import make_login

    make_login(client, session_factory)
    when = dt.datetime(2026, 8, 12, 15, 24, tzinfo=dt.timezone.utc)
    monkeypatch.setitem(main.feed_status, "quoted", when)

    state = client.get("/feed/status").json()

    assert state["quoted"] == when.isoformat()


def test_the_status_endpoint_says_so_when_no_live_price_has_landed(client, session_factory,
                                                                   monkeypatch):
    import app.main as main
    from test_routes import make_login

    make_login(client, session_factory)
    monkeypatch.setitem(main.feed_status, "quoted", None)

    assert client.get("/feed/status").json()["quoted"] is None


def test_a_quote_run_records_when_it_happened(session_factory, monkeypatch):
    """The join between the two halves.

    `refresh_quotes` writing prices is useless to an open page unless the fact
    that it ran is published — that is what the dashboard polls for. Before
    this, the panel reported only the daily close run, so a five-minute-old
    price read as an hour stale and the refresh looked broken.
    """
    import app.main as main
    from app import pricefeed

    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setitem(main.feed_status, "quoted", None)
    monkeypatch.setattr(pricefeed, "refresh_quotes", lambda s, settings: 7)

    assert main._run_quotes() == 7
    assert main.feed_status["quoted"] is not None
    assert main.feed_status["quotes"] == 7


def test_a_quote_run_that_wrote_nothing_does_not_claim_a_refresh(session_factory,
                                                                 monkeypatch):
    """A closed market writes nothing, and saying "updated just now" then would
    be a lie the page has no way to check."""
    import app.main as main
    from app import pricefeed

    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setitem(main.feed_status, "quoted", None)
    monkeypatch.setattr(pricefeed, "refresh_quotes", lambda s, settings: 0)

    main._run_quotes()

    assert main.feed_status["quoted"] is None


def test_a_failed_quote_run_never_marks_the_feed_unhealthy(session_factory, monkeypatch):
    """`ok` drives the staleness alert on the thing that keeps history correct.
    A missing live price makes nothing wrong — the page shows the last close."""
    import app.main as main
    from app import pricefeed

    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setitem(main.feed_status, "ok", True)

    def boom(session, settings):
        raise RuntimeError("yahoo said no")

    monkeypatch.setattr(pricefeed, "refresh_quotes", boom)

    assert main._run_quotes() == 0
    assert main.feed_status["ok"] is True
