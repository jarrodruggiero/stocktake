"""A row of Manage holdings saving itself, in a real browser.

Fields save as they change. A refused one used to say only "not saved"; the
row now says why, and keeps saying it until the next save.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest
from test_trade_dialog_in_the_browser import CHROME, HTML, _standalone

import factories as fac
from test_routes import bind_to_only_portfolio, make_login

pytestmark = pytest.mark.visual

RULE = "A currency is a three-letter code, like AUD or USD."

SERVER = """
<script>
window.fetch = function (url, options) {
  var refused = options && options.body && options.body.get("currency") === "US";
  return Promise.resolve({ok: !refused, json: function () {
    return Promise.resolve(refused ? {detail: "%s"} : {});
  }});
};
</script>
""" % RULE

DRIVER = """
<script>
window.addEventListener("load", async function () {
  var wait = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var currency = document.querySelector('input[name="currency"][form]');
  var mark = document.querySelector('.savemark[data-for="' + currency.getAttribute("form") + '"]');
  var out = {};
  currency.value = "US";
  currency.dispatchEvent(new Event("change", {bubbles: true})); await wait(100);
  out.refused = mark.textContent;
  await wait(2500);
  out.later = mark.textContent;
  currency.value = "USD";
  currency.dispatchEvent(new Event("change", {bubbles: true})); await wait(100);
  out.saved = mark.textContent;
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def test_a_refused_field_says_why(client, session_factory, tmp_path):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fac.hold(s, fac.make_instrument(s, "ACME"))
        s.commit()
    page = client.get("/holdings", headers=HTML)
    target = tmp_path / "holdings.html"
    target.write_text(_standalone(page.text, DRIVER).replace("<head>", "<head>" + SERVER, 1))
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=10000", target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the driver did not finish"
    out = json.loads(found.group(1).replace("&quot;", '"'))
    assert out == {"refused": RULE, "later": RULE, "saved": "saved"}
