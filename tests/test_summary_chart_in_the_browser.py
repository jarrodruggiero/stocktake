"""The Portfolio page's chart as a screen reader is told it, in a real browser.

A canvas is nothing to a screen reader. Its label says what is drawn and
where it ends, and a range button changes the window it is drawn through, so
the label follows the button.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest
from freezegun import freeze_time
from test_trade_dialog_in_the_browser import CHROME, HTML, _standalone

import fixture_portfolio as ref
from test_routes import bind_to_only_portfolio, make_login

pytestmark = pytest.mark.visual

WALK = """
<script>
window.addEventListener("load", async function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var canvas = document.getElementById("summarychart");
  var buttons = document.querySelectorAll("#summaryrange button");
  var out = {drawn: canvas.getAttribute("aria-label")};
  buttons[0].click(); await sleep(300);
  out.shortest = canvas.getAttribute("aria-label");
  buttons[buttons.length - 1].click(); await sleep(300);
  out.all = canvas.getAttribute("aria-label");
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def test_the_charts_label_follows_the_range_chosen(client, session_factory, tmp_path):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        ref.build_reference(s)
    with freeze_time(ref.TODAY):
        page = client.get("/", headers=HTML).text
    target = tmp_path / "dashboard.html"
    target.write_text(_standalone(page, WALK), encoding="utf-8")
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=6000", target.as_uri()],
        capture_output=True, text=True, timeout=120).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the walk did not finish"
    out = json.loads(found.group(1).replace("&quot;", '"'))

    ending = re.escape(out["all"].split(": ", 1)[1])
    assert re.fullmatch(r"Gain over time: -?[0-9]+\.[0-9]{2}% on [0-9-]{10}", out["all"])
    assert out["drawn"] == out["all"], "All is what the page draws first"
    assert re.fullmatch(rf"Gain over time since [0-9-]{{10}}: {ending}", out["shortest"])
