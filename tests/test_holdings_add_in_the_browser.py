"""Manage holdings' "+ Add an instrument", driven in a real browser.

Its lookup kept the first answer it gave: a NASDAQ ticker typed before its
exchange was changed stayed on the ASX guess, CODE.AX, which has no prices.
The lookup's own answers now give way to the next one, and typed text never
does. The class comes from the answer too, as it does when recording a trade.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest
from test_trade_dialog_in_the_browser import CHROME, HTML, _standalone

from test_routes import make_login

pytestmark = pytest.mark.visual

BY_EXCHANGE = """
<script>
window.fetch = function (url) {
  url = String(url);
  if (url.indexOf("/holdings/lookup") !== 0) { return Promise.reject(new Error("offline")); }
  var answer = url.indexOf("exchange=NASDAQ") > 0
    ? {found: true, name: "Alphabet Inc.", currency: "USD", symbol: "GOOG", asset_class: "share"}
    : {found: false, name: "", currency: "", symbol: "GOOG.AX", asset_class: null};
  return Promise.resolve({ok: true, json: function () { return Promise.resolve(answer); }});
};
</script>
"""

DRIVER = """
<script>
window.addEventListener("load", async function () {
  var pause = function () { return new Promise(function (r) { setTimeout(r, 60); }); };
  var form = document.querySelector('form[action="/holdings/add"]');
  var field = function (name) { return form.querySelector('[name="' + name + '"]'); };
  var change = function (name) { field(name).dispatchEvent(new Event("change", {bubbles: true})); };
  var out = {};
  document.getElementById("addinst-open").click(); await pause();

  field("ticker").value = "GOOG"; change("ticker"); await pause();
  out.onTheAsx = field("yahoo_symbol").value;
  field("exchange").value = "NASDAQ"; change("exchange"); await pause();
  out.onNasdaq = {symbol: field("yahoo_symbol").value, name: field("name").value,
                  currency: field("currency").value, cls: field("asset_class").value};

  field("yahoo_symbol").value = "GOOGL";
  field("asset_class").value = "etf"; change("asset_class");
  change("exchange"); await pause();     // NASDAQ again: an answer with a class
  out.typedKept = {symbol: field("yahoo_symbol").value, cls: field("asset_class").value};

  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def test_changing_the_exchange_looks_the_ticker_up_again(client, session_factory, tmp_path):
    make_login(client, session_factory)
    page = client.get("/holdings", headers=HTML)
    assert page.status_code == 200
    target = tmp_path / "holdings.html"
    target.write_text(_standalone(page.text, DRIVER).replace("<head>", "<head>" + BY_EXCHANGE, 1))
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=8000", target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the driver did not finish"
    out = json.loads(found.group(1).replace("&quot;", '"'))

    assert out["onTheAsx"] == "GOOG.AX"
    assert out["onNasdaq"] == {"symbol": "GOOG", "name": "Alphabet Inc.", "currency": "USD",
                               "cls": "share"}
    assert out["typedKept"] == {"symbol": "GOOGL", "cls": "etf"}
