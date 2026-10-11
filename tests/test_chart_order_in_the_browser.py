"""The charts page's order, moved by button in a real browser.

Dragging was the only way to move a chart, and HTML drag fires for neither a
finger nor a keyboard (decisions.md #101). Each card now has buttons; the
script does their move in place, as a drag does, and saves the order the
same way. The walk presses one and holds the page to it: the order shown,
the order saved, which buttons can still move anything, and where focus is.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest
from test_trade_dialog_in_the_browser import CHROME, HTML, _standalone

from test_routes import make_login

pytestmark = pytest.mark.visual

SAVES = """
<script>
window.saved = [];
// A button the script did not handle submits its form and leaves the page.
window.submitted = 0;
document.addEventListener("submit", function (e) { window.submitted++; e.preventDefault(); });
window.fetch = function (url, options) {
  if (String(url) === "/charts/order") { window.saved.push(JSON.parse(options.body).order); }
  return Promise.resolve({ok: true, json: function () { return Promise.resolve({}); }});
};
</script>
"""

WALK = """
<script>
window.addEventListener("load", async function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var cards = function () { return Array.from(document.querySelectorAll("#chartgrid .chartcard")); };
  var button = function (card, which) { return card.querySelector('.cardmove [value="' + which + '"]'); };
  var start = cards().map(function (c) { return c.dataset.chart; });
  var moved = cards()[1];

  button(moved, "earlier").focus();
  button(moved, "earlier").click();
  await sleep(500);

  var after = cards();
  var out = {
    order: after.map(function (c) { return c.dataset.chart; }),
    start: start,
    saved: window.saved,
    submitted: window.submitted,
    pressable: after.map(function (c) {
      return [!button(c, "earlier").disabled, !button(c, "later").disabled];
    }),
    focus: document.activeElement === button(moved, "later") ? "its later button"
         : (document.activeElement && document.activeElement.outerHTML.slice(0, 80))
  };
  // And back: a button that stays pressable would submit its form if the
  // script let it.
  button(moved, "later").click();
  await sleep(500);
  out.back = cards().map(function (c) { return c.dataset.chart; });
  out.saved = window.saved;
  out.submitted = window.submitted;
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def test_a_chart_moves_by_button_in_place(client, session_factory, tmp_path):
    make_login(client, session_factory)
    page = client.get("/charts", headers=HTML)          # seeds the default charts
    assert page.status_code == 200
    target = tmp_path / "charts.html"
    target.write_text(_standalone(page.text, WALK).replace("<head>", "<head>" + SAVES, 1),
                      encoding="utf-8")
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=8000", target.as_uri()],
        capture_output=True, text=True, timeout=120).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the walk did not finish"
    out = json.loads(found.group(1).replace("&quot;", '"'))
    start = out["start"]

    assert len(start) >= 3
    assert out["order"] == [start[1], start[0], *start[2:]]
    assert out["back"] == start
    assert out["saved"] == [out["order"], start]
    assert out["submitted"] == 0, "done in place, not by the form's reload"
    # Now first, it can only go later, and the card it passed can go either way.
    assert out["pressable"][0] == [False, True] and out["pressable"][1] == [True, True]
    assert out["pressable"][-1] == [True, False]
    # Its own earlier button is spent, so focus goes to the other, not the page.
    assert out["focus"] == "its later button"
