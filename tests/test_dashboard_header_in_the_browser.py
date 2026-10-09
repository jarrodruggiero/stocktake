"""Where the Portfolio page's buttons land, measured in a real browser.

Test users reported two things no markup test can see: with ten years of
financial-year tabs the header's buttons dropped under the title instead of
staying beside it, and the holdings table's Manage holdings and Edit columns
sat on the left rather than over the table, on the right.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import factories as fac
from test_routes import bind_to_only_portfolio, make_login

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.browser import find_chrome  # noqa: E402

pytestmark = pytest.mark.visual

CHROME = find_chrome()
if CHROME is None:
    pytest.skip("no headless Chrome on this machine", allow_module_level=True)

HTML = {"accept": "text/html"}
STATIC = ROOT / "app" / "static"

MEASURE = """
<script>
window.addEventListener("load", function () {
  var box = function (el) { var r = el.getBoundingClientRect(); return {top: r.top, bottom: r.bottom, left: r.left, right: r.right}; };
  var head = document.querySelector(".tablehead");
  var tools = head.querySelector(".tabletools");
  var heading = document.querySelector(".pagehead h1");
  document.title = "VC" + JSON.stringify({
    record: box(document.getElementById("opentrade")),
    heading: box(heading),
    tabs: box(document.querySelector(".fytabs")),
    head: box(head),
    tools: box(tools),
    width: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth
  });
});
</script>
"""

# The page goes in an iframe of the wanted width: headless Chrome will not
# make a window narrower than 500px. Same approach as test_visual.py.
WRAPPER = """<!doctype html><html><body style="margin:0">
<iframe id="f" src="%s" style="width:%dpx;height:900px;border:0"></iframe>
<script>
var frame = document.getElementById("f"), tries = 0;
function collect() {
  var title = "";
  try { title = frame.contentDocument.title; } catch (e) { title = "BLOCKED"; }
  if (title.indexOf("VC") === 0 || ++tries > 80) { document.title = title; return; }
  setTimeout(collect, 50);
}
frame.addEventListener("load", collect);
</script></body></html>
"""


def _standalone(page: str) -> str:
    def script(match: re.Match) -> str:
        path = STATIC / match.group(1)
        if match.group(1) == "idle.js" or not path.is_file():
            return ""
        return "<script>" + path.read_text() + "</script>"

    page = re.sub(r'<script src="/static/([^"?]+)[^"]*"[^>]*></script>', script, page)
    page = re.sub(r'<link rel="stylesheet" href="/static/style\.css[^"]*"[^>]*>',
                  lambda _m: "<style>" + (STATIC / "style.css").read_text() + "</style>",
                  page)
    return page.replace("</body>", MEASURE + "</body>")


@pytest.fixture
def ten_years(client, session_factory, tmp_path):
    """A holding bought once a year for ten years: ten financial-year tabs."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME")
        for year in range(2017, 2027):
            fac.add_trade(s, acme, f"{year}-01-05", "buy", 10, "5.00")
        s.commit()
    page = client.get("/", headers=HTML)
    assert page.status_code == 200
    (tmp_path / "page.html").write_text(_standalone(page.text))
    return tmp_path


def _measure(folder: Path, width: int) -> dict:
    (folder / "wrapper.html").write_text(WRAPPER % ("page.html", width))
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", f"--window-size={max(width + 80, 620)},960",
         "--virtual-time-budget=6000", (folder / "wrapper.html").as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r"<title>VC(.*?)</title>", dom, re.S)
    assert found, "the page did not report its layout"
    return json.loads(found.group(1).replace("&quot;", '"'))


def test_the_header_buttons_stay_beside_the_title(ten_years):
    at = _measure(ten_years, 1000)
    # Beside the heading block, not under its tabs.
    assert at["record"]["top"] < at["tabs"]["top"], at
    assert at["record"]["left"] > at["heading"]["right"], at
    assert at["scroll"] <= at["width"], "the page scrolls sideways"


def test_a_phone_still_stacks_them(ten_years):
    at = _measure(ten_years, 412)
    assert at["record"]["top"] > at["heading"]["bottom"], at
    assert at["scroll"] <= at["width"], "the page scrolls sideways"


def test_the_table_tools_sit_on_the_right(ten_years):
    at = _measure(ten_years, 1000)
    assert abs(at["tools"]["right"] - at["head"]["right"]) <= 2, at
    assert at["tools"]["left"] > at["head"]["left"] + 300, at
