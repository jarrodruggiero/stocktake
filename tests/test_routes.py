"""HTTP behaviour of the web routes.

The maths lives in the other modules; this file is about what the app does to a
request: who is let in, what is refused, and what comes back. Two things here
are guarantees rather than conveniences and are named as such — the login form
must not reveal whether an account exists, and every mutating POST must carry
the session's CSRF token.
"""

from __future__ import annotations

import csv
import io
import re
from contextlib import contextmanager

import pytest
from freezegun import freeze_time
from sqlalchemy import select

import factories as fac
import fixture_portfolio as ref
from app import auth as auth_mod
from app.models import HoldingPref, Instrument, SavedChart, Trade, User, UserSession

PASSWORD = "correct-horse-battery"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def pre_auth_csrf(client) -> str:
    """The double-submit token the logged-out forms use. GET the page first so
    the cookie exists, then echo it back in the form field."""
    client.get("/login", headers={"accept": "text/html"})
    return client.cookies.get(auth_mod.PRE_AUTH_CSRF_COOKIE)


def session_csrf(session_factory) -> str:
    """The live session's CSRF token, read straight from the database — the
    same value the templates render into their forms."""
    with session_factory() as s:
        return s.scalars(select(UserSession).order_by(UserSession.id.desc())).first().csrf_token


def do_setup(client, email: str = "first@example.test") -> None:
    """Run the first-run wizard as far as having an account.

    Two posts, not one: `/setup` is the wizard's welcome step and `/setup/profile`
    is where the account is made. The steps after this one are settings, and a
    caller that wanted them would walk them itself — see `test_setup_wizard.py`.
    """
    client.get("/setup", headers={"accept": "text/html"})
    token = client.cookies.get(auth_mod.PRE_AUTH_CSRF_COOKIE)
    client.post("/setup", data={"_csrf": token}, headers={"accept": "text/html"},
                follow_redirects=False)
    client.post(
        "/setup/profile",
        data={"name": "First Owner", "email": email, "password": PASSWORD, "_csrf": token},
        headers={"accept": "text/html"}, follow_redirects=False,
    )


def make_login(client, session_factory, *, email="user@example.test", admin=True,
               must_change=False):
    """Create an account directly, then log in through the real form."""
    with session_factory() as s:
        user = fac.make_user(s, email, password_hash=auth_mod.hash_password(PASSWORD),
                             is_admin=admin)
        user.must_change_password = must_change
        fac.make_portfolio(s, "Test portfolio", owner=user)
        s.commit()
    token = pre_auth_csrf(client)
    client.post("/login", data={"email": email, "password": PASSWORD, "_csrf": token},
                headers={"accept": "text/html"}, follow_redirects=False)
    return email


# --------------------------------------------------------------------------- #
# First run
# --------------------------------------------------------------------------- #

def test_with_no_accounts_every_page_sends_you_to_setup(client):
    resp = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup"


def test_setup_creates_the_admin_their_portfolio_and_signs_them_in(client, session_factory):
    do_setup(client)

    with session_factory() as s:
        user = s.scalars(select(User)).one()
        assert user.email == "first@example.test"
        # The bootstrap account administers the others.
        assert user.is_admin is True
        # …and lands in a portfolio of its own, or it would have nothing to read.
        assert [m.role for m in user_memberships(s, user.id)] == ["owner"]
    assert client.cookies.get("pf_session")


def user_memberships(session, user_id):
    from app.models import PortfolioMember

    return session.scalars(
        select(PortfolioMember).where(PortfolioMember.user_id == user_id)
    ).all()


def test_setup_is_refused_once_an_account_exists(client, session_factory):
    do_setup(client)
    client.cookies.clear()

    resp = client.get("/setup", headers={"accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# --------------------------------------------------------------------------- #
# Login / logout
# --------------------------------------------------------------------------- #

def test_an_unknown_email_and_a_wrong_password_are_indistinguishable(client, session_factory):
    """No account enumeration: if the two messages differed, the login form
    would be an oracle for which email addresses hold an account."""
    make_login(client, session_factory, email="real@example.test")
    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                headers={"accept": "text/html"})
    client.cookies.clear()

    token = pre_auth_csrf(client)
    wrong_password = client.post(
        "/login", data={"email": "real@example.test", "password": "not-the-password",
                        "_csrf": token},
        headers={"accept": "text/html"})
    token = pre_auth_csrf(client)
    unknown_email = client.post(
        "/login", data={"email": "nobody@example.test", "password": PASSWORD,
                        "_csrf": token},
        headers={"accept": "text/html"})

    assert "Invalid email or password." in wrong_password.text
    assert "Invalid email or password." in unknown_email.text
    # Neither may leak that one of the two addresses is real.
    assert ("nobody@example.test" in unknown_email.text) == (
        "real@example.test" in wrong_password.text
    )


def test_logout_clears_the_session_cookie(client, session_factory):
    make_login(client, session_factory)
    assert client.cookies.get("pf_session")

    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                headers={"accept": "text/html"})

    assert not client.cookies.get("pf_session")


# --------------------------------------------------------------------------- #
# The login gate
# --------------------------------------------------------------------------- #

def test_an_unauthenticated_browser_request_is_redirected(client, session_factory):
    make_login(client, session_factory)
    client.cookies.clear()

    resp = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_an_unauthenticated_non_browser_request_gets_401_not_a_redirect(client, session_factory):
    """A stale fetch() must fail loudly rather than silently rendering the
    login page into whatever was expecting JSON."""
    make_login(client, session_factory)
    client.cookies.clear()

    resp = client.get("/", headers={"accept": "application/json"}, follow_redirects=False)

    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_probes_need_no_session(client, path):
    assert client.get(path).status_code == 200


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #

def test_a_post_without_the_csrf_token_is_refused(client, session_factory):
    make_login(client, session_factory)

    resp = client.post("/profile/appearance", data={"theme": "dark", "accent": "teal",
                                                    "nav_style": "both"})

    assert resp.status_code == 403


def test_a_post_carrying_the_session_token_succeeds(client, session_factory):
    make_login(client, session_factory)

    resp = client.post(
        "/profile/appearance",
        data={"theme": "dark", "accent": "teal", "nav_style": "both",
              "_csrf": session_csrf(session_factory)},
        headers={"accept": "text/html"}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/profile?saved=appearance"


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_the_dashboard_shows_the_open_positions(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)

    page = client.get("/", headers={"accept": "text/html"})

    assert page.status_code == 200
    for ticker in ("ALPHA", "BETAX", "GAMMA", "OMEGA"):
        assert ticker in page.text
    # ZULU is fully sold — it belongs under closed positions, not the open table.
    assert "5,647.50" in page.text or "5647.50" in page.text  # total cost


def bind_to_only_portfolio(session):
    """Bind a raw session to the single portfolio the test created."""
    from app import tenancy
    from app.models import Portfolio

    portfolio = session.scalars(select(Portfolio)).first()
    user = session.scalars(select(User)).first()
    tenancy.bind(session, portfolio.id, user.id)
    return portfolio


@contextmanager
def reading(session_factory):
    """A bound session for checking what a route wrote.

    Personal data can't be read without a portfolio on the session — that is
    the fail-closed guarantee doing its job, and it applies to test code too.
    """
    session = session_factory()
    try:
        bind_to_only_portfolio(session)
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Recording trades
# --------------------------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_recording_a_trade_stores_it_and_returns_to_the_instrument(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME", name="Acme Industries")
        fac.hold(s, acme)
        s.commit()
        acme_id = acme.id

    resp = client.post(
        "/trade/new",
        data={"instrument_id": str(acme_id), "type": "buy", "trade_date": "2026-07-01",
              "quantity": "10", "unit_price": "4.00", "brokerage": "9.50",
              "_csrf": session_csrf(session_factory)},
        headers={"accept": "text/html"}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/holding/ACME"
    with reading(session_factory) as s:
        trade = s.scalars(select(Trade)).one()
        assert trade.quantity == 10
        assert trade.unit_price == 4
        # An AUD instrument is 1:1, filled in by the route rather than the form.
        assert trade.fx_rate == 1


@freeze_time(ref.TODAY)
def test_a_sell_that_would_go_negative_is_refused_with_an_explanation(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME", name="Acme Industries")
        fac.add_trade(s, acme, "2026-01-05", "buy", 10, "4.00")
        s.commit()
        acme_id = acme.id

    resp = client.post(
        "/trade/new",
        data={"instrument_id": str(acme_id), "type": "sell", "trade_date": "2026-07-01",
              "quantity": "25", "unit_price": "5.00", "brokerage": "0",
              "_csrf": session_csrf(session_factory)},
        headers={"accept": "text/html"})

    # The refusal says what was actually wrong: 25 sold against 10 held.
    assert "sell of 25 units" in resp.text
    assert "10 units of ACME held on 2026-07-01" in resp.text
    with reading(session_factory) as s:
        # Nothing was written — the guard runs before the insert.
        assert len(s.scalars(select(Trade)).all()) == 1


@freeze_time(ref.TODAY)
def test_a_sale_is_held_to_its_own_holdings_units(client, session_factory):
    """ACME holds 10; WIDGET's 100 beside it must not cover a sale of 25 ACME.
    A mutation run took the instrument out of the check and nothing failed."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME")
        widget = fac.make_instrument(s, "WIDGET")
        fac.add_trade(s, acme, "2026-01-05", "buy", 10, "4.00")
        fac.add_trade(s, widget, "2026-01-05", "buy", 100, "1.00")
        s.commit()
        acme_id = acme.id

    resp = client.post("/trade/new", headers={"accept": "text/html"}, data={
        "instrument_id": str(acme_id), "type": "sell", "trade_date": "2026-07-01",
        "quantity": "25", "unit_price": "5.00", "brokerage": "0",
        "_csrf": session_csrf(session_factory)})

    assert "10 units of ACME held" in resp.text
    with reading(session_factory) as s:
        assert len(s.scalars(select(Trade)).all()) == 2


@freeze_time(ref.TODAY)
@pytest.mark.parametrize("overrides, says", [
    ({"type": "drp"}, "Choose buy or sell."),
    ({"brokerage": "-1"}, "Brokerage can"),
    ({"fx_rate": "0"}, "FX must be positive"),
    ({"fx_rate": "-0.5"}, "FX must be positive"),
    ({"set_time": "1", "trade_time": "25:99"}, "That time isn"),
])
def test_the_form_refuses_what_a_trade_cannot_be(client, session_factory, overrides, says):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME")
        fac.hold(s, acme)
        s.commit()
        acme_id = acme.id
    data = {"instrument_id": str(acme_id), "type": "buy", "trade_date": "2026-07-01",
            "quantity": "1", "unit_price": "1.00", "brokerage": "0",
            "_csrf": session_csrf(session_factory)}
    data.update(overrides)

    resp = client.post("/trade/new", data=data, headers={"accept": "text/html"})

    assert resp.status_code == 200 and says in resp.text
    with reading(session_factory) as s:
        assert s.scalars(select(Trade)).all() == []


@freeze_time(ref.TODAY)
def test_a_future_dated_trade_is_refused(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME")
        s.commit()
        acme_id = acme.id

    resp = client.post(
        "/trade/new",
        data={"instrument_id": str(acme_id), "type": "buy", "trade_date": "2026-09-01",
              "quantity": "1", "unit_price": "1.00", "brokerage": "0",
              "_csrf": session_csrf(session_factory)},
        headers={"accept": "text/html"})

    assert "future" in resp.text.lower()


@freeze_time(ref.TODAY)
def test_an_instrument_can_be_added_inline_while_recording_the_trade(client, session_factory,
                                                                    monkeypatch):
    """The whole point of the inline form: no round trip to another page, so
    nothing typed into the trade fields is lost."""
    from app import main as main_mod

    # The lookup would hit Yahoo; the form's own values must win regardless.
    monkeypatch.setattr(main_mod.pricefeed, "lookup",
                        lambda ticker, exchange: {"symbol": None, "name": None,
                                                  "currency": None, "found": False})
    make_login(client, session_factory)

    resp = client.post(
        "/trade/new",
        data={"instrument_id": "new", "type": "buy", "trade_date": "2026-07-01",
              "quantity": "5", "unit_price": "12.00", "brokerage": "9.50",
              "new_ticker": "nova", "new_name": "Nova Group", "new_exchange": "ASX",
              "new_asset_class": "share", "new_currency": "AUD",
              "_csrf": session_csrf(session_factory)},
        headers={"accept": "text/html"}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/holding/NOVA"
    with reading(session_factory) as s:
        inst = s.scalars(select(Instrument)).one()
        assert inst.ticker == "NOVA"   # upper-cased on the way in
        assert inst.name == "Nova Group"
        assert s.scalars(select(Trade)).one().instrument_id == inst.id


@pytest.mark.parametrize("existing", [False, True])
def test_a_refused_trade_adds_nothing_inline(client, session_factory, app_module,
                                             monkeypatch, existing):
    """A sell of something not held is refused, and the instrument typed in
    beside it is not added either: not to the catalogue, not to the list, and
    not reactivated if another portfolio had removed it. Found by a generated
    sequence (test_sequences_through_the_routes.py)."""
    kicked = []
    monkeypatch.setattr(app_module, "_kick_feed", lambda: kicked.append(1))
    make_login(client, session_factory)
    if existing:
        with session_factory() as s:
            fac.make_instrument(s, "NOVA", active=False)
            s.commit()
    before = _counts(session_factory)

    resp = client.post(
        "/trade/new",
        data={"instrument_id": "new", "type": "sell", "trade_date": "2026-07-01",
              "quantity": "5", "unit_price": "12.00", "new_ticker": "NOVA",
              "new_exchange": "ASX", "new_asset_class": "share", "new_currency": "AUD",
              "_csrf": session_csrf(session_factory)},
        headers={"accept": "text/html"}, follow_redirects=False)

    assert resp.status_code == 200 and 'class="panel error"' in resp.text
    assert _counts(session_factory) == before
    assert kicked == []


def _counts(session_factory) -> tuple:
    with reading(session_factory) as s:
        return (s.scalars(select(Instrument.active)).all(),
                len(s.scalars(select(HoldingPref)).all()),
                len(s.scalars(select(Trade)).all()))


# --------------------------------------------------------------------------- #
# Exports
# --------------------------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_every_year_export_works_with_an_empty_fy(client, session_factory):
    """`fy` is a STRING parameter because the form submits fy="" for "All
    years". Typing it as `int | None` made FastAPI reject the request before the
    handler ran, which broke EVERY download in 0.13.x — hence this test."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)

    resp = client.get("/export/download",
                      params={"report": "transactions", "fmt": "csv", "fy": "", "ticker": ""})

    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert rows[0][0] == "Date"
    assert len(rows) > 1


@freeze_time(ref.TODAY)
@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
def test_download_sets_a_filename(client, session_factory, fmt):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)

    resp = client.get("/export/download", params={"report": "holdings", "fmt": fmt})

    assert resp.status_code == 200
    assert 'filename="portfolio-holdings' in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith(f'.{fmt}"')


@pytest.mark.parametrize("fy", ["9" * 5000, "\u00b2\u2070\u00b2\u2076"],
                         ids=["5000 digits", "superscript digits"])
def test_a_financial_year_is_four_digits(client, session_factory, fy):
    """isdigit() takes both of these; int() refused one and could not parse
    the other, and either was a 500."""
    make_login(client, session_factory)

    resp = client.get("/export/download", params={"report": "holdings", "fmt": "csv", "fy": fy})

    assert resp.status_code == 400


def test_the_ticker_filter_has_to_be_a_ticker(client, session_factory):
    """It becomes part of the download's filename, in a header, which takes
    Latin-1 only: anything else was a 500."""
    make_login(client, session_factory)

    resp = client.get("/export/download",
                      params={"report": "holdings", "fmt": "csv", "ticker": "\u0130" * 10})

    assert resp.status_code == 400


def test_an_unknown_report_is_rejected(client, session_factory):
    make_login(client, session_factory)

    resp = client.get("/export/download", params={"report": "nonsense", "fmt": "csv"})

    assert resp.status_code == 400


def test_the_combined_workbook_needs_excel(client, session_factory):
    """"all" is many sheets; CSV has no way to express that, so it's refused
    rather than silently exporting only the first."""
    make_login(client, session_factory)

    resp = client.get("/export/download", params={"report": "all", "fmt": "csv"})

    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_the_default_charts_are_seeded_once_and_only_once(client, session_factory):
    from app import chart_templates

    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)

    client.get("/charts", headers={"accept": "text/html"})
    with reading(session_factory) as s:
        first = s.scalars(select(SavedChart)).all()
    client.get("/charts", headers={"accept": "text/html"})
    with reading(session_factory) as s:
        second = s.scalars(select(SavedChart)).all()

    assert len(first) == len(chart_templates.DEFAULT_KEYS)
    assert len(second) == len(first)


@freeze_time(ref.TODAY)
def test_a_deleted_chart_stays_deleted(client, session_factory):
    """Seeding is guarded on a flag rather than "has no charts", so removing
    one is a decision that sticks instead of being undone on the next load."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)
    client.get("/charts", headers={"accept": "text/html"})

    with reading(session_factory) as s:
        victim = s.scalars(select(SavedChart).order_by(SavedChart.id)).first()
        victim_id, victim_name = victim.id, victim.name
    client.post(f"/charts/{victim_id}/delete", data={"_csrf": session_csrf(session_factory)},
                headers={"accept": "text/html"})
    client.get("/charts", headers={"accept": "text/html"})

    with reading(session_factory) as s:
        remaining = [c.name for c in s.scalars(select(SavedChart)).all()]
    assert victim_name not in remaining


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_a_viewer_can_read_but_not_write(client, session_factory):
    """Viewers exist so family can be shown the numbers without any risk of an
    edit — reading must work, writing must not."""
    from app.models import PortfolioMember

    with session_factory() as s:
        viewer = fac.make_user(s, "viewer@example.test",
                               password_hash=auth_mod.hash_password(PASSWORD))
        portfolio = fac.make_portfolio(s, "Shared portfolio")
        s.add(PortfolioMember(portfolio_id=portfolio.id, user_id=viewer.id, role="viewer"))
        s.commit()
        from app import tenancy
        tenancy.bind(s, portfolio.id, viewer.id)
        acme = fac.make_instrument(s, "ACME")
        fac.add_trade(s, acme, "2026-01-05", "buy", 10, "4.00")
        fac.add_prices(s, acme, [("2026-08-01", "5.00")])
        s.commit()
        acme_id = acme.id

    token = pre_auth_csrf(client)
    client.post("/login", data={"email": "viewer@example.test", "password": PASSWORD,
                                "_csrf": token}, headers={"accept": "text/html"})

    assert client.get("/", headers={"accept": "text/html"}).status_code == 200

    resp = client.post(
        "/trade/new",
        data={"instrument_id": str(acme_id), "type": "buy", "trade_date": "2026-07-01",
              "quantity": "1", "unit_price": "1.00", "brokerage": "0",
              "_csrf": session_csrf(session_factory)},
        headers={"accept": "text/html"})
    assert resp.status_code == 403


def test_a_temporary_password_must_be_changed_before_anything_else(client, session_factory):
    make_login(client, session_factory, email="fresh@example.test", must_change=True)

    resp = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/profile"


# --------------------------------------------------------------------------- #
# Legacy paths
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "path,target",
    [
        ("/dca", "/schedule"),
        ("/calendar", "/schedule"),
        ("/fy", "/"),
        ("/reports", "/"),
        ("/export", "/imports-exports#exports"),
    ],
)
def test_the_moved_pages_still_resolve(client, session_factory, path, target):
    """Pages that merged elsewhere keep redirecting: a bookmark from an earlier
    version shouldn't 404."""
    make_login(client, session_factory)

    resp = client.get(path, headers={"accept": "text/html"}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == target


# The instruments page: cells that line up, and a reachable add form. Both were
# real complaints — the DRP checkbox, note box and Save button shared one cell
# with `colspan="3"`, so rows were three controls tall, the table needed
# scrolling, and "Add an instrument" below it was never found.
# --------------------------------------------------------------------------- #

def _instruments_page(client, session_factory) -> str:
    import factories as fac

    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fac.hold(s, fac.make_instrument(s, "ACME", name="Acme Industries"))
        s.commit()
    return client.get("/holdings", headers={"accept": "text/html"}).text


def test_the_row_cells_line_up_with_the_headers(client, session_factory):
    """No colspan across DRP and the note. A header row that does not describe
    the cells beneath it is worse than no header row."""
    page = _instruments_page(client, session_factory)

    body = page[page.index("<tbody>"):page.index("</tbody>")]
    assert 'colspan="3"' not in body
    assert "colspan" not in body


def test_the_drp_checkbox_and_the_note_are_separate_cells(client, session_factory):
    page = _instruments_page(client, session_factory)

    head = page[page.index("<thead>"):page.index("</thead>")]
    body = page[page.index("<tbody>"):page.index("</tbody>")]
    row = body[body.index("<tr"):body.index("</tr>")]

    # Counted against the HEADER rather than a literal. The claim is that the
    # body lines up with its own headings — which is what broke when the
    # controls shared one colspan cell — and a hardcoded number only restates
    # today's column list, so adding one makes this fail for no reason while
    # still not checking the thing it is named after.
    # `<th[ >]`, not `"<th"` — the latter also matches `<thead>` itself.
    import re as _re

    cells = len(_re.findall(r"<td[ >]", row))
    headers = len(_re.findall(r"<th[ >]", head))
    assert cells == headers, (
        f"{cells} cells against {headers} headers — the body no longer lines "
        "up with the header")


def test_the_controls_still_belong_to_one_form(client, session_factory):
    """Cells cannot be wrapped in a <form>, so the inputs reference one by id —
    the same `form=` attribute trick the column chooser uses. Without it,
    splitting the cells would have split the form and saved nothing."""
    page = _instruments_page(client, session_factory)

    import re
    form_id = re.search(r'<form[^>]*id="(pref-\d+)"', page)
    assert form_id, "no per-row preferences form"
    assert f'form="{form_id.group(1)}"' in page


def test_adding_an_instrument_is_a_button_not_a_buried_form(client, session_factory):
    """It has to be visible without scrolling past every holding."""
    page = _instruments_page(client, session_factory)

    button = page.index("addinst-open")
    table = page.index("<table")
    assert button < table, "the add button must come before the table"


def test_the_add_form_lives_in_a_dialog(client, session_factory):
    page = _instruments_page(client, session_factory)

    dialog = page[page.index("<dialog"):page.index("</dialog>")]
    assert 'action="/holdings/add"' in dialog
    assert 'name="ticker"' in dialog


def test_the_dialog_reopens_itself_when_the_server_rejects_the_form(
        client, session_factory):
    """Otherwise the error renders behind a closed dialog and the page looks
    like it simply did nothing."""
    make_login(client, session_factory)

    # An INVALID ticker, not an empty one: httpx drops empty strings from the
    # body, so FastAPI reports the field missing (422) and never reaches the
    # app's own validation — which is not the path being tested.
    page = client.post("/holdings/add",
                       data={"ticker": "!!!", "exchange": "ASX",
                             "asset_class": "etf", "currency": "AUD",
                             "_csrf": session_csrf(session_factory)},
                       headers={"accept": "text/html"}).text

    dialog_tag = page[page.index("<dialog"):page.index(">", page.index("<dialog")) + 1]
    assert " open" in dialog_tag, "the dialog must reopen to show the error"


def test_it_still_works_with_javascript_off(client, session_factory):
    """A <dialog> with no `open` is display:none, so without script the form
    would be unreachable — which would be a worse page than the one being
    replaced. The noscript rule makes it an ordinary block."""
    page = _instruments_page(client, session_factory)

    # EVERY <noscript> on the page, not the first one: the top bar grew its own
    # (the portfolio switcher's Switch button) when the account menu landed, and
    # taking `page.index` made this test assert about a block it did not mean.
    import re

    blocks = re.findall(r"<noscript>(.*?)</noscript>", page, re.S)
    assert blocks, "no <noscript> at all — the dialog is unreachable without script"
    assert any("addinst" in block for block in blocks), (
        "no <noscript> mentions the add-instrument dialog, so with scripting off "
        f"there is no way to open it. Found: {blocks!r}")


def test_the_login_page_itself_sends_a_first_run_to_setup(client):
    """/login is public, so the middleware's first-run redirect never sees
    it; the route's own check is the only thing between a fresh install and a
    sign-in form for accounts that do not exist."""
    resp = client.get("/login", headers={"accept": "text/html"}, follow_redirects=False)

    assert resp.status_code == 303 and resp.headers["location"] == "/setup"


def test_the_brand_mark_carries_the_apps_name_for_a_screen_reader(client, session_factory):
    """The mark is a picture; its accessible name is the text beside it.
    Dropping the template global left an empty span and nothing failed."""
    from app import branding

    with session_factory() as s:
        fac.make_user(s, "someone@example.test")
        s.commit()

    page = client.get("/login", headers={"accept": "text/html"}).text

    assert f'<span class="visually-hidden">{branding.NAME}</span>' in page


def test_the_trade_form_marks_only_a_foreign_instrument_and_keeps_fx_for_it(client,
                                                                            session_factory):
    """The reporting currency is a template global: without it every
    instrument read "(AUD)" and the FX box was offered for an AUD trade."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fac.hold(s, fac.make_instrument(s, "ALPHA"))
        fac.hold(s, fac.make_instrument(s, "ZULU", exchange="NASDAQ", currency="USD"))
        s.commit()

    page = client.get("/trade/new", headers={"accept": "text/html"}).text

    texts = [" ".join(t.split()) for t in re.findall(r"<option[^>]*>(.*?)</option>", page, re.S)]
    alpha = next(t for t in texts if t.startswith("ALPHA — "))
    zulu = next(t for t in texts if t.startswith("ZULU — "))
    assert not alpha.endswith(")") and zulu.endswith("(USD)")
    assert re.search(r'<label id="fxfield"\s+hidden', page)
    assert "FX rate to AUD" in page
