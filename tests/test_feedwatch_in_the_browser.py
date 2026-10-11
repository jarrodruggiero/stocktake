"""A price refresh finishing, in a real browser, with a trade half typed.

The page reloads once a refresh it watched has finished, so every figure
comes from one render. The live-price watcher asks first whether somebody is
in the middle of something; the refresh watcher did not, and a refresh that
was running when the page loaded (a scheduled one included) took the Record
trade dialog and whatever was typed into it.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest
from test_trade_dialog_in_the_browser import CHROME, HTML, STATIC

import factories as fac
from test_routes import bind_to_only_portfolio, make_login

pytestmark = pytest.mark.visual

# The tab's own memory across a reload: how many times the page has loaded,
# and what the walk saw before it did.
MEMORY = """
<script>
window.memory = JSON.parse(window.name || "{}");
window.memory.loads = (window.memory.loads || 0) + 1;
window.name = JSON.stringify(window.memory);
// The refresh has finished; after the reload it is running again, so the
// walk ends there rather than reloading for ever.
window.fetch = function (url) {
  var body = {};
  if (String(url) === "/feed/status") {
    body = {running: window.memory.loads > 1, quoted: "2026-08-02T10:00:00"};
  }
  return Promise.resolve({ok: true, json: function () { return Promise.resolve(body); }});
};
</script>
"""

WALK = """
<script>
window.addEventListener("load", async function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var done = function (out) {
    var pre = document.createElement("pre");
    pre.id = "result";
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  };
  if (window.memory.loads > 1) {
    return done(window.memory);
  }
  var dialog = document.getElementById("tradedialog");
  document.getElementById("opentrade").click(); await sleep(100);
  dialog.querySelector("#quantity").value = "7";
  await sleep(12000);                    // several polls, each saying "finished"
  window.memory.keptWhileTyping = dialog.open && dialog.querySelector("#quantity").value;
  window.name = JSON.stringify(window.memory);
  // Cancelled with the figure still in it: the form starts afresh when the
  // dialog next opens, so nothing is in use and the reload may come.
  dialog.close();
  await sleep(12000);
  done({loads: window.memory.loads, stillHere: true});
});
</script>
"""


def test_a_finished_refresh_waits_for_the_trade_being_typed(client, session_factory, tmp_path):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fac.hold(s, fac.make_instrument(s, "ACME"))
        s.commit()
    page = client.get("/", headers=HTML)
    assert page.status_code == 200

    def script(match: re.Match) -> str:
        path = STATIC / match.group(1)
        if match.group(1) == "idle.js" or not path.is_file():
            return ""
        return "<script>" + path.read_text(encoding="utf-8") + "</script>"

    text = re.sub(r'<script src="/static/([^"?]+)[^"]*"[^>]*></script>', script, page.text)
    text = text.replace("<body ", '<body data-feed-running="1" ', 1)
    target = tmp_path / "dashboard.html"
    target.write_text(text.replace("<head>", "<head>" + MEMORY, 1)
                      .replace("</body>", WALK + "</body>"), encoding="utf-8")
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=40000", target.as_uri()],
        capture_output=True, text=True, timeout=120).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the walk did not finish"
    out = json.loads(found.group(1).replace("&quot;", '"'))

    # Kept while the dialog was open with a figure in it; reloaded once free.
    assert out == {"loads": 2, "keptWhileTyping": "7"}
