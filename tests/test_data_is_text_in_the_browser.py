"""Text from the data reaches the page as text, in a real browser.

The chart builder's filters and the chart tables built their HTML from strings,
and a currency could hold markup (decision #130). The currency is now refused
on the way in — `test_currency_codes.py` — but a value stored before that still
sits in the database, so what protects an existing install is the page treating
it as text. Only a browser can show that, so these run with the visual tests.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from freezegun import freeze_time
from sqlalchemy import update

import factories as fac
import fixture_portfolio as ref
from app.models import Instrument
from test_routes import bind_to_only_portfolio, make_login, session_csrf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.browser import find_chrome  # noqa: E402

pytestmark = pytest.mark.visual

CHROME = find_chrome()
if CHROME is None:
    pytest.skip("no headless Chrome on this machine", allow_module_level=True)

HTML = {"accept": "text/html"}
STATIC = ROOT / "app" / "static"
# Lower case: it is stored directly, the way a value from before the fix would be.
MARKUP = "<img src=x onerror=\"document.title='INJECTED'\">"


@pytest.fixture
def poisoned(client, session_factory):
    """Two holdings, one whose currency was stored as markup before the rule."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME", asset_class="share")
        zulu = fac.make_instrument(s, "ZULU", asset_class="share", currency="USD")
        fac.add_trade(s, acme, "2026-01-05", "buy", 10, "4.00")
        fac.add_trade(s, zulu, "2026-01-05", "buy", 10, "4.00")
        s.commit()
        # Around the model's validator on purpose: this is what is already there.
        s.execute(update(Instrument).where(Instrument.ticker == "ZULU")
                  .values(currency=MARKUP))
        s.commit()
    return client


def _in_chrome(page: str, tmp_path: Path) -> str:
    """The page with its scripts inlined, as Chrome leaves it once they ran."""
    def inline(match: re.Match) -> str:
        path = STATIC / match.group(1)
        # idle.js signs the page out after a while; nothing to measure here.
        if match.group(1) == "idle.js" or not path.is_file():
            return ""
        return "<script>" + path.read_text(encoding="utf-8") + "</script>"

    page = re.sub(r'<script src="/static/([^"?]+)[^"]*"[^>]*></script>', inline, page)
    target = tmp_path / "page.html"
    target.write_text(page, encoding="utf-8")
    return subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--virtual-time-budget=4000",
         target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout


def _assert_shown_as_text(dom: str) -> None:
    assert "<title>INJECTED</title>" not in dom, "markup from the data ran as script"
    assert '<img src="x"' not in dom, "markup from the data became an element"
    assert "&lt;img src=x" in dom, "the value should still be shown, as text"


@freeze_time(ref.TODAY)
def test_a_currency_with_markup_is_text_in_the_chart_builders_filters(
        poisoned, tmp_path):
    page = poisoned.get("/charts/build", headers=HTML).text

    _assert_shown_as_text(_in_chrome(page, tmp_path))


def _table_page(client, session_factory, **spec) -> str:
    saved = client.post("/charts/save", json={
        "name": "A table", "width": "half",
        "spec": {"grain": "positions", "measures": ["pos_value"], "type": "table", **spec}},
        headers={"X-CSRF-Token": session_csrf(session_factory)})
    assert saved.status_code == 200
    return client.get("/charts", headers=HTML).text


@freeze_time(ref.TODAY)
def test_a_currency_with_markup_is_text_in_a_chart_tables_rows(
        poisoned, session_factory, tmp_path):
    page = _table_page(poisoned, session_factory, x="currency")

    _assert_shown_as_text(_in_chrome(page, tmp_path))


@freeze_time(ref.TODAY)
def test_a_currency_with_markup_is_text_in_a_chart_tables_header(
        poisoned, session_factory, tmp_path):
    """Split by currency, each currency is a series, and a series name is a
    column heading."""
    page = _table_page(poisoned, session_factory, x="ticker", split="currency")

    _assert_shown_as_text(_in_chrome(page, tmp_path))


# The rotation editor upper-cases what it reads, so this payload is written in
# capitals: the script is carried as hex character references, and the quote
# closes the attribute the old code wrote the ticker into.
_SCRIPT = "".join(f"&#X{ord(c):X};" for c in "document.title='INJECTED'")
TICKER_MARKUP = f'X"><IMG SRC=X ONERROR={_SCRIPT}>'


@freeze_time(ref.TODAY)
def test_a_ticker_with_markup_is_text_in_the_plan_rotation(
        client, session_factory, tmp_path):
    """Tickers are checked on the way in, but one stored before that check is
    still read back into the rotation editor, from the database."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        zulu = fac.make_instrument(s, "ZULU", asset_class="share")
        fac.add_trade(s, zulu, "2026-01-05", "buy", 10, "4.00")
        s.commit()
        s.execute(update(Instrument).where(Instrument.ticker == "ZULU")
                  .values(ticker=TICKER_MARKUP))
        s.commit()

    dom = _in_chrome(client.get("/schedule", headers=HTML).text, tmp_path)

    assert "<title>INJECTED</title>" not in dom, "markup from the data ran as script"
    assert '<img src="X"' not in dom, "markup from the data became an element"
    assert 'aria-label="Move X&quot;&gt;&lt;IMG' in dom, "the ticker should still label its button"
