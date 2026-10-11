"""The idle warning, driven in a real browser.

It is the one script every other walk strips out, so it had never run in a
page. The case that mattered: somebody half way through a trade in the Record
trade dialog when the warning comes up. A modal dialog sits in the browser's
top layer and makes the rest of the page inert, and the warning was an
ordinary element underneath it, so "Stay signed in" could be neither focused
nor pressed. The countdown ran out and the half-typed trade went with the
session.
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

# The server's half. The session has an hour left until the walk says
# otherwise, and keepalive gives it an hour again. Anything else the page asks
# for (the feed's status) gets nothing, so nothing reloads the page mid-walk.
SERVER = """
<script>
window.sessionLeft = 3600;
window.fetch = function (url) {
  url = String(url);
  var body = {};
  if (url === "/session/keepalive") {
    window.sessionLeft = 3600;
    body = {seconds_left: 3600};
  } else if (url === "/session/status") {
    body = {authenticated: true, seconds_left: window.sessionLeft};
  }
  return Promise.resolve({ok: true, json: function () { return Promise.resolve(body); }});
};
</script>
"""

WALK = """
<script>
window.addEventListener("load", async function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var out = {};
  var dialog = document.getElementById("tradedialog");
  var quantity = dialog.querySelector("#quantity");
  var warning = document.getElementById("idlewarn");
  var stay = document.getElementById("idlewarn-stay");
  var countdown = document.getElementById("idlewarn-countdown");

  document.getElementById("opentrade").click(); await sleep(100);
  quantity.value = "7";
  window.sessionLeft = 90;            // under the page's two minutes of warning
  await sleep(31000);                 // until the next poll asks
  var box = stay.getBoundingClientRect();
  out.focused = document.activeElement === stay;
  out.underThePointer = document.elementFromPoint(
    box.left + box.width / 2, box.top + box.height / 2) === stay;
  out.countdown = {role: countdown.getAttribute("role"),
                   live: countdown.getAttribute("aria-live")};
  stay.click(); await sleep(100);
  out.afterStay = {warning: warning.open === true, trade: dialog.open, typed: quantity.value};

  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def _walk(client, session_factory, tmp_path) -> dict:
    """The Portfolio page, every script inlined, the idle warning included."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fac.hold(s, fac.make_instrument(s, "ACME"))
        s.commit()
    page = client.get("/", headers=HTML)
    assert page.status_code == 200

    def script(match: re.Match) -> str:
        path = STATIC / match.group(1)
        return "<script>" + path.read_text() + "</script>" if path.is_file() else ""

    text = re.sub(r'<script src="/static/([^"?]+)[^"]*"[^>]*></script>', script, page.text)
    text = re.sub(r'<link rel="stylesheet" href="/static/style\.css[^"]*"[^>]*>',
                  lambda _m: "<style>" + (STATIC / "style.css").read_text() + "</style>", text)
    target = tmp_path / "dashboard.html"
    target.write_text(text.replace("<head>", "<head>" + SERVER, 1)
                      .replace("</body>", WALK + "</body>"))
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=60000", target.as_uri()],
        capture_output=True, text=True, timeout=120).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the walk did not finish"
    return json.loads(found.group(1).replace("&quot;", '"'))


def test_the_warning_can_be_answered_over_an_open_dialog(client, session_factory, tmp_path):
    out = _walk(client, session_factory, tmp_path)

    assert out["focused"] and out["underThePointer"], out
    assert out["afterStay"] == {"warning": False, "trade": True, "typed": "7"}
    # Read out once as the warning opens, not every second as it counts down.
    assert out["countdown"] == {"role": "timer", "live": None}
