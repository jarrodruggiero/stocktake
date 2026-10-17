"""Where Simulate sale's fields land, measured in a real browser.

Reported from the first try: a USD holding's brokerage box sat further left
than an AUD holding's, pushed over by the currency code beside it, so the
column of boxes was ragged. No markup test can see that.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from freezegun import freeze_time

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
  var edges = function (name) {
    return Array.prototype.map.call(document.querySelectorAll('input[name^="' + name + '"]'),
      function (el) { var r = el.getBoundingClientRect(); return [r.left, r.right]; });
  };
  document.title = "VC" + JSON.stringify({units: edges("units_"), brokerage: edges("brokerage_"),
    width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth});
});
</script>
"""

# An iframe of the wanted width, as test_dashboard_header_in_the_browser.py does:
# headless Chrome will not make a window narrower than 500px.
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
    page = re.sub(r'<script src="/static/([^"?]+)[^"]*"[^>]*></script>', "", page)
    page = re.sub(r'<link rel="stylesheet" href="/static/style\.css[^"]*"[^>]*>',
                  lambda _m: "<style>" + (STATIC / "style.css").read_text(encoding="utf-8")
                  + "</style>", page)
    return page.replace("</body>", MEASURE + "</body>")


@pytest.fixture
def two_currencies(client, session_factory, tmp_path):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        alpha = fac.make_instrument(s, "ALPHA", asset_class="share")
        fac.add_trade(s, alpha, "2025-01-06", "buy", 5, "8.00", brokerage="9.50")
        fac.add_prices(s, alpha, [("2026-10-16", "8.32")])
        nova = fac.make_instrument(s, "NOVA", exchange="NASDAQ", currency="USD")
        fac.add_trade(s, nova, "2025-01-06", "buy", 15, "500.00", brokerage="15",
                      fx_rate="1.50")
        fac.add_prices(s, nova, [("2026-10-16", "529.05")])
        fac.add_fx(s, "USDAUD", "2026-10-16", "1.44")
        s.commit()
        ids = alpha.id, nova.id
    with freeze_time("2026-10-17"):
        page = client.get(f"/simulate?rate=45&units_{ids[0]}=5&units_{ids[1]}=15", headers=HTML)
    assert page.status_code == 200
    (tmp_path / "page.html").write_text(_standalone(page.text), encoding="utf-8")
    return tmp_path


def _measure(folder: Path, width: int) -> dict:
    (folder / "wrapper.html").write_text(WRAPPER % ("page.html", width), encoding="utf-8")
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", f"--window-size={max(width + 80, 620)},960",
         "--virtual-time-budget=6000", (folder / "wrapper.html").as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r"<title>VC(.*?)</title>", dom, re.S)
    assert found, "the page did not report its layout"
    return json.loads(found.group(1).replace("&quot;", '"'))


@pytest.mark.parametrize("width", [1400, 412])
def test_each_column_of_boxes_lines_up(two_currencies, width):
    at = _measure(two_currencies, width)

    for column in ("units", "brokerage"):
        edges = at[column]
        assert len(edges) == 2, at
        assert abs(edges[0][0] - edges[1][0]) <= 1 and abs(edges[0][1] - edges[1][1]) <= 1, (
            column, edges)
