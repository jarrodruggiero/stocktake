"""Small things the pages said wrong, each checked where a person reads it.

A count of reports that was never true, a verb that did not agree, "(s)" on
every count, one financial year written two ways, slugs where words belong, a
title that was not the heading, two primary buttons, a tick for a feed that
had not run, a promise that a rate nobody provides would arrive, and AUD
written in where the reporting currency belongs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_imports_web import upload_csv

import factories as fac
from app import exports
from test_routes import bind_to_only_portfolio, make_login

HTML = {"accept": "text/html"}
TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_everything_is_as_many_reports_as_there_are(client, session_factory):
    make_login(client, session_factory)

    page = _text(client.get("/imports-exports", headers=HTML).text)

    assert f"“Everything” is {len(exports.TITLES)} reports" in page
    assert "nine" not in page


def test_a_password_alone_still_signs_you_in(client, session_factory):
    make_login(client, session_factory)

    page = _text(client.get("/profile/passkeys", headers=HTML).text)

    assert "Your password still signs you in." in page


@pytest.mark.parametrize("count, word, said", [
    (1, "trade", "1 trade"), (2, "trade", "2 trades"), (0, "row", "0 rows"),
    ("3", "duplicate", "3 duplicates"),
])
def test_a_count_agrees_with_its_noun(app_module, count, word, said):
    assert app_module.templates.env.filters["plural"](count, word) == said


def test_no_count_is_written_with_an_s_in_brackets():
    found = [f"{path.name}: {line.strip()}"
             for path in sorted(TEMPLATES.glob("*.html"))
             for line in path.read_text(encoding="utf-8").splitlines() if "(s)" in line]

    assert found == []


def test_a_one_trade_file_offers_to_commit_one_trade(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fac.hold(s, fac.make_instrument(s, "ACME"))
        s.commit()

    page = upload_csv(client, session_factory, b"Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
                                               b"06/01/2025,Buy,ACME,100,5.00,9.50\n").text

    assert ">Commit 1 trade</button>" in page


def test_a_financial_year_is_written_one_way(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fac.add_trade(s, fac.make_instrument(s, "ACME"), "2025-01-06", "buy", 1, "1.00")
        s.commit()

    page = client.get("/imports-exports", headers=HTML).text

    assert ">FY24/25</option>" in page
    assert not re.search(r"FY20[0-9]{2}/20[0-9]{2}", page)


def test_a_class_and_a_status_read_as_words(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME", asset_class="share")
        fac.add_trade(s, acme, "2025-01-06", "buy", 100, "5.00", brokerage="9.50")
        s.commit()

    holding = client.get("/holding/ACME", headers=HTML).text
    preview = upload_csv(client, session_factory,
                         b"Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
                         b"06/01/2025,Buy,ACME,100,5.00,9.50\n").text

    assert "ASX · Share · AUD" in _text(holding)
    assert ">already recorded</span>" in preview and ">duplicate</span>" not in preview


def test_the_admin_page_is_titled_as_it_is_headed(client, session_factory):
    make_login(client, session_factory)

    page = client.get("/admin", headers=HTML).text

    assert "<title>Admin — Stocktake</title>" in page


def test_a_first_run_offers_recording_a_trade_once(client, session_factory):
    make_login(client, session_factory)

    page = client.get("/", headers=HTML).text

    assert "Nothing recorded yet" in page
    assert len(re.findall(r'<button[^>]*class="primary"', page)) == 1


def test_a_feed_that_has_not_run_is_not_shown_as_fine(client, session_factory, app_module,
                                                      monkeypatch):
    monkeypatch.setitem(app_module.feed_status, "finished", None)
    monkeypatch.setitem(app_module.feed_status, "running", False)
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fac.add_trade(s, fac.make_instrument(s, "ACME"), "2025-01-06", "buy", 1, "1.00")
        s.commit()

    page = client.get("/", headers=HTML).text
    summary = re.search(r'<details class="feedstatus[^"]*">\s*<summary([^>]*)>(.*?)</summary>',
                        page, re.S)

    assert summary, "no feed status on the page"
    assert "✓" not in summary.group(2) and 'class="ok"' not in summary.group(1)


def test_a_missing_rate_is_not_promised_to_arrive():
    """A pair no provider has is never filled, and a promise that it will
    be sends nobody to fix it."""
    found = [path.name for path in sorted(TEMPLATES.glob("*.html"))
             if "refresh fills it in" in " ".join(path.read_text(encoding="utf-8").split())]

    assert found == []


def test_the_reporting_currency_is_not_written_in_as_aud():
    found = [f"{path.name}: {line.strip()}"
             for path in sorted(TEMPLATES.glob("*.html"))
             for line in path.read_text(encoding="utf-8").splitlines() if "'AUD'" in line]

    assert found == []
