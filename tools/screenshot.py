#!/usr/bin/env python
"""Render the app's pages to standalone HTML, for looking at.

    uv run python tools/screenshot.py            # writes ./shots/*.html

    # then, to actually see one:
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\
      --headless --disable-gpu --screenshot=shots/12-dashboard.png \\
      --window-size=1100,900 --hide-scrollbars --virtual-time-budget=1500 \\
      "file://$PWD/shots/12-dashboard.html"

**Why this exists.** A palette validator checks colour and a test checks
markup; neither notices that two flex children wrapped onto separate lines, or
that a long path ran past the edge of its panel. Both of those shipped in the
setup wizard and were caught only by rendering it and looking. The visual
polish) is entirely this kind of problem, so the workflow is worth keeping
rather than rebuilding from scratch.

It drives the real app through TestClient against a throwaway database, so
these are the pages themselves rather than fixtures of them, then inlines
`style.css` so each file opens standalone from `file://`.

Both the stylesheet and the page's scripts are inlined, because a `file://`
page can load neither — and without the scripts most of what the two template
designers build never appears at all.

Two things to check on every page, because both have bitten:

  * **Light as well as dark.** Add `data-theme="light"` to `<body>` in the
    written file, or flip the OS preference. Tokens are declared for both and
    only one tends to get looked at.
  * **Narrow.** `--window-size=420,900`: below 48rem the wizard rail becomes a
    row and the `.detected` grid collapses to a single column.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "shots")

WORK = Path(tempfile.mkdtemp(prefix="shot-"))
(WORK / "data").mkdir()
for name in [k for k in os.environ if k.startswith("APP_")]:
    del os.environ[name]
os.environ.pop("STOCKTAKE_TEST_DB", None)
os.environ["APP_CONFIG_FILE"] = str(WORK / "config" / "config.yaml")
sys.path.insert(0, str(APP_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import lifecycle, main  # noqa: E402

# Never actually SIGTERM the interpreter running this, and pretend to be a
# container so the restart step is offered — as a plain process the wizard
# correctly declines to offer a restart nothing would undo.
lifecycle.request_stop = lambda reason: None
lifecycle.supervision = lambda: lifecycle.Supervision(
    restarts=True, certain=True, platform="Kubernetes",
    detail="Kubernetes starts a replacement immediately.")

client = TestClient(main.app)
HTML = {"accept": "text/html"}
CSS = (APP_ROOT / "app" / "static" / "style.css").read_text()


STATIC = APP_ROOT / "app" / "static"


def save(name: str, html: str) -> None:
    """Write a page with its stylesheet AND scripts inlined, so file:// renders
    it as the browser would.

    Inlining the scripts matters more than it looks. A `file://` page cannot
    load `/static/designer.js`, so anything the page builds in JavaScript —
    which is most of both template designers — simply does not appear, and the
    render shows an empty-looking interface that works perfectly in the app.
    That cost a round of "why is this broken" on a screenshot of the editor.
    """
    html = html.replace(
        '<link rel="stylesheet" href="/static/style.css?v=',
        "<style>" + CSS + '</style><link rel="x" href="#')

    def inline(match: re.Match) -> str:
        path = STATIC / match.group(1)
        return "<script>" + path.read_text() + "</script>" if path.is_file() else ""

    html = re.sub(r'<script src="/static/([^"?]+)[^"]*"[^>]*></script>', inline, html)
    (OUT / f"{name}.html").write_text(html)
    print("wrote", OUT / f"{name}.html")


def token(path: str = "/setup") -> str:
    page = client.get(path, headers=HTML)
    found = re.search(r'name="_csrf" value="([^"]+)"', page.text)
    return found.group(1) if found else client.cookies.get("pf_csrf")


def render_all() -> None:
    OUT.mkdir(exist_ok=True)
    # Clear the last run's pages first. `visual_check.py` reads whatever is in
    # this directory, so a file left behind by an earlier session — a renamed
    # page, a one-off saved while debugging — is reported as a live failure
    # forever. Two of the six "remaining" popup faults on 2026-08-11 were
    # exactly that: stale copies of pages the fix had already corrected.
    for stale in OUT.glob("*.html"):
        stale.unlink()

    save("1-welcome", client.get("/setup", headers=HTML).text)
    t = token()
    client.post("/setup", data={"_csrf": t}, headers=HTML, follow_redirects=False)
    save("2-database", client.get("/setup/database", headers=HTML).text)
    # A failed connection test, because an error state needs looking at too.
    save("2b-database-error", client.post(
        "/setup/database/test",
        data={"_csrf": t, "kind": "postgres", "host": "no-such.invalid",
              "port": "5432", "name": "portfolio", "user": "portfolio",
              "password": "x"}, headers=HTML).text)
    client.post("/setup/database",
                data={"_csrf": t, "kind": "sqlite",
                      "path": str(WORK / "data" / "p.db")},
                headers=HTML, follow_redirects=False)
    save("3-account", client.get("/setup/profile", headers=HTML).text)
    client.post("/setup/profile",
                data={"_csrf": t, "name": "Jo", "email": "jo@example.test",
                      "password": "correct-horse-battery"},
                headers=HTML, follow_redirects=False)
    save("4-recovery-codes", client.get("/setup/recovery", headers=HTML).text)
    save("6-portfolio", client.get("/setup/portfolio", headers=HTML).text)
    client.post("/setup/portfolio",
                data={"_csrf": token("/setup/portfolio"), "name": "Family",
                      "timezone": "Australia/Melbourne", "price_feed": "on"},
                headers=HTML, follow_redirects=False)
    # Optional features, then the environment — the order the wizard actually
    # walks. Rendering them out of order silently exercised a path a real
    # install never takes.
    save("7-features", client.get("/setup/features", headers=HTML).text)
    client.post("/setup/features",
                data={"_csrf": token("/setup/features"), "feature": "dca_schedule"},
                headers=HTML, follow_redirects=False)
    save("7b-environment", client.get("/setup/environment", headers=HTML).text)
    client.post("/setup/environment",
                data={"_csrf": token("/setup/environment"),
                      "trusted_proxies": "10.1.0.0/16",
                      "external_url": "https://portfolio.example.com"},
                headers=HTML, follow_redirects=False)
    save("8-finish", client.get("/setup/finish", headers=HTML).text)
    save("9-restarting", client.get(
        "/setup/restarting?to=https%3A%2F%2Fportfolio.example.com%2F",
        headers=HTML).text)

    # Past the wizard: the pages themselves.
    save("10-dashboard", client.get("/", headers=HTML).text)
    save("11-profile", client.get("/profile", headers=HTML).text)
    save("12-charts", client.get("/charts", headers=HTML).text)
    # The admin pages: two-column on a desktop, and the Settings page now
    # carries a console, which is a scrolling panel inside a form page.
    save("22-admin", client.get("/members", headers=HTML).text)
    save("23-admin-settings", client.get("/admin/settings", headers=HTML).text)
    # The builder is four columns of controls, which is exactly the sort of
    # layout that only reveals its problems when rendered.
    save("12b-chart-builder", client.get("/charts/build", headers=HTML).text)
    # Signed OUT, in its own cookie jar. `client` is authenticated by this
    # point, so `GET /login` on it followed the redirect to the dashboard and
    # "13-login.html" was a second copy of the dashboard wearing the login
    # page's name — for months, silently. Found 2026-08-11 while changing the
    # login card, which is the only page where the brand is the whole design.
    anon = TestClient(main.app)
    save("13-login", anon.get("/login", headers=HTML).text)
    save("14-recover", anon.get("/login/recover", headers=HTML).text)

    # Seed an instrument so the instruments table has a row to look at —
    # an empty table hides exactly the layout problems worth seeing.
    client.post("/holdings/add",
                data={"_csrf": token("/holdings"), "ticker": "ALPHA",
                      "exchange": "ASX", "asset_class": "etf", "currency": "AUD",
                      "name": "Vanguard Australian Shares Index ETF"},
                headers=HTML, follow_redirects=True)
    save("15-instruments", client.get("/holdings", headers=HTML).text)

    # A holding, not just an instrument: the dashboard lists HOLDINGS, so
    # without a trade the table renders empty and hides every layout problem
    # in it — which is most of what there is to look at on that page.
    import re as _re
    page = client.get("/trade/new", headers=HTML)
    instrument_id = _re.search(r'<option value="(\d+)"', page.text).group(1)
    client.post("/trade/new",
                data={"_csrf": token("/trade/new"), "instrument_id": instrument_id,
                      "trade_date": "2026-01-05", "type": "buy", "quantity": "100",
                      "unit_price": "92.50", "brokerage": "9.50"},
                headers=HTML, follow_redirects=True)
    save("16-dashboard-holdings", client.get("/", headers=HTML).text)
    # Sorted, so the arrow and the active-header styling are visible.
    save("17-dashboard-sorted",
         client.get("/?holdings_sort=value_aud&holdings_dir=desc", headers=HTML).text)

    # The DCA schedule, both states. Empty first — a first run is the page most
    # likely to look broken and the least likely to be looked at — then with a
    # plan, which is what makes the calendar and Coming up have anything in
    # them to line up against each other.
    # Sorted by the gain PERCENTAGE — the second state of the four-state Gain
    # header, and the only way to see that the header renders which half of the
    # pair is active rather than just an arrow.
    save("17c-dashboard-gain-pct",
         client.get("/?holdings_sort=gain_pct&holdings_dir=desc", headers=HTML).text)

    # The holdings page AFTER a trade exists, which is when Remove turns into
    # the panel explaining what is in the way — the state worth looking at, and
    # the one the earlier render (before any trade) cannot show.
    save("17b-holdings-blocked", client.get("/holdings", headers=HTML).text)

    # Imports & exports, and the templates dialog open — half its controls
    # render only when a dialog is open, which no default screenshot shows.
    save("18-imports", client.get("/imports-exports", headers=HTML).text)
    save("18b-imports-templates",
         client.get("/imports-exports?templates=1", headers=HTML).text)

    save("19-plan-empty", client.get("/schedule", headers=HTML).text)
    client.post("/schedule/save",
                data={"_csrf": token("/schedule"), "name": "Regular buys",
                      "interval_days": "28", "amount": "500", "brokerage": "9.50",
                      "start_date": "2026-01-05", "tickers": "ALPHA"},
                headers=HTML, follow_redirects=True)
    save("20-plan", client.get("/schedule", headers=HTML).text)
    # The editor open, since it holds half the page's controls and renders
    # only when a dialog is open — which no default screenshot would show.
    save("21-plan-editing", client.get("/schedule?edit=1", headers=HTML).text)


if __name__ == "__main__":
    render_all()
    print(f"\n{OUT}/ — screenshot them with headless Chrome (see the docstring),"
          f"\nand look at light mode and a narrow window, not just this one.")
