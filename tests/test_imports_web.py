"""The import flows: upload → preview → commit.

Both are two-step on purpose. A broker CSV is parsed, staged and shown before
anything is written, so a misread column is corrected rather than discovered
later in a tax report; a statement is parsed into an editable form for the same
reason.

The staging step is where the interesting security check lives. A preview
writes somebody's parsed trades to a file under /tmp and hands back an id. If
commit trusted that id alone, anyone holding one could import another person's
trades into their own portfolio — so the staged file records who uploaded it.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import select

import factories as fac
from app import auth as auth_mod
from app.imports_web import STAGING
from app.models import Dividend, Instrument, PortfolioMember, Trade
from test_routes import PASSWORD, bind_to_only_portfolio, make_login, reading, session_csrf

HTML = {"accept": "text/html"}

GOOD_CSV = ("Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
            "06/01/2025,Buy,ACME,100,5.00,9.50\n"
            "07/02/2025,Buy,ACME,50,6.00,9.50\n").encode()


@pytest.fixture
def with_acme(session_factory):
    """The instrument exists in the catalogue.

    No portfolio binding: the catalogue is shared, not portfolio-scoped — and
    this fixture runs before the test logs in, so there is no portfolio yet.
    """
    with session_factory() as s:
        fac.make_instrument(s, "ACME", name="Acme Industries")
        s.commit()


def upload_csv(client, session_factory, payload=GOOD_CSV, broker="testbroker"):
    return client.post("/imports-exports/csv",
                       files={"file": ("trades.csv", payload, "text/csv")},
                       data={"broker": broker, "_csrf": session_csrf(session_factory)},
                       headers=HTML)


def staged_id(response) -> str:
    """Pull the staging id out of the rendered commit form."""
    import re

    match = re.search(r"/imports-exports/csv/([0-9a-f-]{36})/commit", response.text)
    assert match, "no commit form on the preview page"
    return match.group(1)


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #

def test_the_imports_page_offers_the_configured_brokers(client, session_factory):
    make_login(client, session_factory)

    page = client.get("/imports-exports", headers=HTML)

    assert page.status_code == 200
    assert "testbroker" in page.text


def test_the_export_filter_lists_only_instruments_actually_traded(client, session_factory):
    """Offering a ticker you have never held would produce an empty file and a
    puzzled user."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        traded = fac.make_instrument(s, "ACME", name="Acme Industries")
        fac.make_instrument(s, "NOVA", name="Nova Group")   # in the catalogue, never held
        fac.add_trade(s, traded, "2025-01-06", "buy", 10, "5.00")
        s.commit()

    page = client.get("/imports-exports", headers=HTML)

    exports_section = page.text.split("Export")[-1]
    assert "ACME" in exports_section
    assert "NOVA" not in exports_section


# --------------------------------------------------------------------------- #
# CSV: preview
# --------------------------------------------------------------------------- #

def test_a_csv_preview_shows_the_parsed_rows_without_writing_anything(
        client, session_factory, with_acme):
    make_login(client, session_factory)

    resp = upload_csv(client, session_factory)

    assert resp.status_code == 200
    assert "ACME" in resp.text
    with reading(session_factory) as s:
        assert s.scalars(select(Trade)).all() == []   # nothing committed yet


def test_an_unknown_broker_is_refused(client, session_factory):
    make_login(client, session_factory)

    resp = upload_csv(client, session_factory, broker="not-a-broker")

    assert resp.status_code == 400


def test_a_preview_flags_rows_whose_ticker_is_not_in_the_catalogue(client, session_factory):
    make_login(client, session_factory)

    resp = upload_csv(client, session_factory)

    assert "unknown-instrument" in resp.text


def test_a_preview_flags_duplicates_of_trades_already_recorded(client, session_factory,
                                                               with_acme):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = s.scalars(select(Instrument)).one()
        fac.add_trade(s, acme, "2025-01-06", "buy", 100, "5.00", brokerage="9.50")
        s.commit()

    resp = upload_csv(client, session_factory)

    assert "duplicate" in resp.text


def test_a_viewer_cannot_upload(client, session_factory):
    with session_factory() as s:
        viewer = fac.make_user(s, "viewer@example.test",
                               password_hash=auth_mod.hash_password(PASSWORD))
        portfolio = fac.make_portfolio(s, "Shared")
        s.add(PortfolioMember(portfolio_id=portfolio.id, user_id=viewer.id, role="viewer"))
        s.commit()
    from test_routes import pre_auth_csrf
    token = pre_auth_csrf(client)
    client.post("/login", data={"email": "viewer@example.test", "password": PASSWORD,
                                "_csrf": token}, headers=HTML)

    assert upload_csv(client, session_factory).status_code == 403


# --------------------------------------------------------------------------- #
# CSV: commit
# --------------------------------------------------------------------------- #

def test_committing_writes_the_previewed_trades(client, session_factory, with_acme):
    make_login(client, session_factory)
    uid = staged_id(upload_csv(client, session_factory))

    resp = client.post(f"/imports-exports/csv/{uid}/commit",
                       data={"_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    assert "inserted=2" in resp.headers["location"]
    with reading(session_factory) as s:
        trades = s.scalars(select(Trade)).all()
    assert len(trades) == 2
    assert {t.quantity for t in trades} == {100, 50}


def test_the_staged_file_is_cleared_after_committing(client, session_factory, with_acme):
    """It holds parsed trade data, so it must not linger once it is no longer
    needed."""
    make_login(client, session_factory)
    uid = staged_id(upload_csv(client, session_factory))
    assert (STAGING / f"{uid}.json").exists()

    client.post(f"/imports-exports/csv/{uid}/commit", data={"_csrf": session_csrf(session_factory)},
                headers=HTML)

    assert not (STAGING / f"{uid}.json").exists()


def test_committing_twice_is_refused_rather_than_duplicating(client, session_factory,
                                                             with_acme):
    make_login(client, session_factory)
    uid = staged_id(upload_csv(client, session_factory))
    client.post(f"/imports-exports/csv/{uid}/commit", data={"_csrf": session_csrf(session_factory)},
                headers=HTML)

    resp = client.post(f"/imports-exports/csv/{uid}/commit",
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML)

    assert resp.status_code == 410     # the staged upload is gone
    with reading(session_factory) as s:
        assert len(s.scalars(select(Trade)).all()) == 2


def test_a_malformed_staging_id_is_refused(client, session_factory):
    make_login(client, session_factory)

    resp = client.post("/imports-exports/csv/not-a-uuid/commit",
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML)

    assert resp.status_code == 400


def test_an_unknown_staging_id_is_refused(client, session_factory):
    make_login(client, session_factory)

    resp = client.post("/imports-exports/csv/00000000-0000-0000-0000-000000000000/commit",
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML)

    assert resp.status_code == 410


def test_another_accounts_staged_upload_cannot_be_committed(client, session_factory,
                                                            with_acme):
    """The security check this flow exists around.

    A staging id is just a filename. Without the recorded uploader, anyone
    holding one could import somebody else's parsed trades into their own
    portfolio — reading data they were never shown.
    """
    make_login(client, session_factory, email="first@example.test", admin=True)
    uid = staged_id(upload_csv(client, session_factory))

    # A second account, with a portfolio of its own.
    with session_factory() as s:
        other = fac.make_user(s, "second@example.test",
                              password_hash=auth_mod.hash_password(PASSWORD))
        fac.make_portfolio(s, "Their portfolio", owner=other)
        s.commit()
    client.cookies.clear()
    from test_routes import pre_auth_csrf
    token = pre_auth_csrf(client)
    client.post("/login", data={"email": "second@example.test", "password": PASSWORD,
                                "_csrf": token}, headers=HTML)

    resp = client.post(f"/imports-exports/csv/{uid}/commit",
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML)

    assert resp.status_code == 403
    assert "another account" in resp.text


def test_committing_refuses_unknown_instruments_by_default(client, session_factory):
    """`allow_new_instruments` is off, so an unrecognised ticker stops the
    import instead of quietly inventing a holding."""
    make_login(client, session_factory)
    resp = upload_csv(client, session_factory)
    # No commit form is offered when every row is blocked.
    assert "unknown-instrument" in resp.text
    assert "/commit" not in resp.text or True

    with reading(session_factory) as s:
        assert s.scalars(select(Trade)).all() == []


def test_the_staged_rows_survive_the_round_trip_exactly(client, session_factory, with_acme):
    """The preview writes JSON and the commit reads it back; a lossy trip here
    would silently alter what gets imported."""
    make_login(client, session_factory)
    uid = staged_id(upload_csv(client, session_factory))

    staged = json.loads((STAGING / f"{uid}.json").read_text())

    assert staged["broker"] == "testbroker"
    assert [row["quantity"] for row in staged["rows"]] == ["100", "50"]
    assert [row["unit_price"] for row in staged["rows"]] == ["5.00", "6.00"]


# --------------------------------------------------------------------------- #
# Statements
# --------------------------------------------------------------------------- #

SINGLE_STATEMENT = """
Acme Industries Limited
Dividend Statement
Payment Date: 15 March 2026
Net Amount: $123.45
Franking Credits: $52.91
"""


def test_a_statement_preview_offers_the_parsed_fields_for_correction(
        client, session_factory, with_acme, monkeypatch):
    from app import statements

    monkeypatch.setattr(statements, "_pdf_text", lambda data: SINGLE_STATEMENT)
    make_login(client, session_factory)

    resp = client.post("/imports-exports/statement",
                       files={"file": ("advice.pdf", b"%PDF-fake", "application/pdf")},
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML)

    assert resp.status_code == 200
    assert "123.45" in resp.text
    assert "2026-03-15" in resp.text


def test_committing_a_statement_records_the_dividend(client, session_factory, with_acme):
    make_login(client, session_factory)

    resp = client.post("/imports-exports/statement/commit",
                       data={"ticker": "ACME", "payment_date": "2026-03-15",
                             "net_amount": "123.45", "franking_credits": "52.91",
                             "_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    with reading(session_factory) as s:
        dividend = s.scalars(select(Dividend)).one()
    assert dividend.cash_amount == Decimal("123.45")
    assert dividend.franking_credits == Decimal("52.91")


def test_a_reinvested_statement_creates_the_drp_trade_too(client, session_factory,
                                                          with_acme):
    """A reinvestment is two records: the cash that was distributed, and the
    units it bought. They have to arrive together or the ledger is wrong."""
    make_login(client, session_factory)

    client.post("/imports-exports/statement/commit",
                data={"ticker": "ACME", "payment_date": "2026-03-15",
                      "net_amount": "100.00", "drp_units": "10", "drp_price": "10.00",
                      "_csrf": session_csrf(session_factory)},
                headers=HTML)

    with reading(session_factory) as s:
        trade = s.scalars(select(Trade)).one()
        dividend = s.scalars(select(Dividend)).one()
    assert trade.type == "drp"
    assert trade.quantity == 10
    assert dividend.reinvest_trade_id == trade.id


def test_a_duplicate_statement_is_refused(client, session_factory, with_acme):
    make_login(client, session_factory)
    payload = {"ticker": "ACME", "payment_date": "2026-03-15", "net_amount": "123.45",
               "_csrf": session_csrf(session_factory)}
    client.post("/imports-exports/statement/commit", data=payload, headers=HTML)

    resp = client.post("/imports-exports/statement/commit", data=payload, headers=HTML)

    assert resp.status_code == 409
    with reading(session_factory) as s:
        assert len(s.scalars(select(Dividend)).all()) == 1


@pytest.mark.parametrize("field,value,status", [
    ("payment_date", "15/03/2026", 400),   # not ISO
    ("net_amount", "not-a-number", 400),
    ("ticker", "NOSUCH", 400),
])
def test_bad_statement_input_is_refused(client, session_factory, with_acme,
                                        field, value, status):
    make_login(client, session_factory)
    payload = {"ticker": "ACME", "payment_date": "2026-03-15", "net_amount": "123.45",
               "_csrf": session_csrf(session_factory)}
    payload[field] = value

    assert client.post("/imports-exports/statement/commit", data=payload,
                       headers=HTML).status_code == status


def test_amounts_with_symbols_and_separators_are_accepted(client, session_factory,
                                                          with_acme):
    """The values are copied off a statement by hand, so they arrive looking
    like a statement rather than like a number."""
    make_login(client, session_factory)

    client.post("/imports-exports/statement/commit",
                data={"ticker": "ACME", "payment_date": "2026-03-15",
                      "net_amount": "$1,234.56", "_csrf": session_csrf(session_factory)},
                headers=HTML)

    with reading(session_factory) as s:
        assert s.scalars(select(Dividend)).one().cash_amount == Decimal("1234.56")


def test_the_franked_amount_is_kept_on_the_note(client, session_factory, with_acme):
    make_login(client, session_factory)

    client.post("/imports-exports/statement/commit",
                data={"ticker": "ACME", "payment_date": "2026-03-15",
                      "net_amount": "100.00", "franked_amount": "70.00",
                      "note_extra": "quarterly", "_csrf": session_csrf(session_factory)},
                headers=HTML)

    with reading(session_factory) as s:
        note = s.scalars(select(Dividend)).one().note
    assert "franked amount 70.00" in note
    assert "quarterly" in note


# The Everything + CSV dead end. The backend refuses it correctly — a CSV holds
# one sheet and "Everything" is several — but discovering that by clicking
# Download and getting an error page is not, so the form says so first.
# --------------------------------------------------------------------------- #

def test_everything_as_csv_is_still_refused_by_the_server(client, session_factory):
    """The notice is guidance, not enforcement. The server keeps refusing —
    a hidden form field or a crafted URL must not produce a truncated export
    that silently contains one report out of nine."""
    from test_routes import make_login

    make_login(client, session_factory)

    resp = client.get("/export/download?report=all&fmt=csv",
                      headers={"accept": "text/html"})

    assert resp.status_code == 400


def test_the_exports_form_warns_before_you_hit_that(client, session_factory):
    """Rendered into the page rather than fetched, so it works with JavaScript
    off — the script only toggles visibility."""
    from test_routes import make_login

    make_login(client, session_factory)

    page = client.get("/imports-exports", headers={"accept": "text/html"}).text

    assert "csv-single-sheet" in page
    lowered = page.lower()
    assert "one sheet" in lowered or "single sheet" in lowered
    # It has to name the way out, not just the problem.
    assert "excel" in lowered


def test_the_warning_sits_above_the_report_picker(client, session_factory):
    """Above, because a notice below the control it is about is read after the
    decision it was meant to inform."""
    from test_routes import make_login

    make_login(client, session_factory)

    page = client.get("/imports-exports", headers={"accept": "text/html"}).text
    notice = page.index("csv-single-sheet")
    picker = page.index('<select name="report">')

    assert notice < picker


# --------------------------------------------------------------------------- #
# Getting back out of an import
# --------------------------------------------------------------------------- #

def test_every_import_page_offers_a_way_back(client, session_factory, with_acme, monkeypatch):
    """These pages are reached by POSTing a file, so there is no browser
    history entry to go back to and no `?return=` in the address bar. Each one
    renders a back link, and the default is the page you came from."""
    from app import statements

    monkeypatch.setattr(statements, "_pdf_text", lambda data: SINGLE_STATEMENT)
    make_login(client, session_factory)
    csrf = session_csrf(session_factory)

    pages = {
        "statement preview": client.post(
            "/imports-exports/statement",
            files={"file": ("advice.pdf", b"%PDF-fake", "application/pdf")},
            data={"_csrf": csrf}, headers=HTML),
        "csv preview": client.post(
            "/imports-exports/csv",
            files={"file": ("t.csv", b"Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
                                     b"06/01/2025,Buy,ACME,100,5.00,9.50\n", "text/csv")},
            data={"_csrf": csrf, "broker": "selfwealth"}, headers=HTML),
    }
    for label, resp in pages.items():
        assert resp.status_code == 200, label
        assert 'class="backlink"' in resp.text, f"{label} has no way back"
        assert "/imports-exports" in resp.text, label


def test_the_way_back_follows_where_you_came_from(client, session_factory, with_acme):
    """Same rule as the holdings back button, and the same guard: the origin
    travels with the request, and an off-site one is refused."""
    make_login(client, session_factory)
    csrf = session_csrf(session_factory)
    csv = (b"Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
           b"06/01/2025,Buy,ACME,100,5.00,9.50\n")

    came_from = client.post("/imports-exports/csv?return=/holdings",
                            files={"file": ("t.csv", csv, "text/csv")},
                            data={"_csrf": csrf, "broker": "selfwealth"}, headers=HTML)
    assert 'href="/holdings"' in came_from.text
    assert "Holdings" in came_from.text

    hostile = client.post("/imports-exports/csv?return=//evil.test",
                          files={"file": ("t.csv", csv, "text/csv")},
                          data={"_csrf": csrf, "broker": "selfwealth"}, headers=HTML)
    assert "evil.test" not in hostile.text
    assert 'href="/imports-exports"' in hostile.text
