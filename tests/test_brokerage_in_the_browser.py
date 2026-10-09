"""The trade form fills the brokerage from the portfolio's fees as it is typed.

`app/brokerage.py` says what the sum is; this is the browser doing it, which
is the only place it happens as units and price change.
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

DRIVER = """
<script>
window.addEventListener("load", async function () {
  var pause = function () { return new Promise(function (r) { setTimeout(r, 60); }); };
  var out = {};
  var form = document.getElementById("tradedialog").querySelector("form");
  var pick = form.querySelector("#instpick");
  var qty = form.querySelector("#quantity");
  var price = form.querySelector("#unit_price");
  var fee = form.querySelector("#brokerage");
  var type = function (el, value) {
    el.value = value; el.dispatchEvent(new Event("input", {bubbles: true}));
  };
  var choose = function (ticker) {
    var option = ticker === "new" ? pick.querySelector('option[value="new"]')
                                  : pick.querySelector('option[data-ticker="' + ticker + '"]');
    pick.value = option.value;
    pick.dispatchEvent(new Event("change", {bubbles: true}));
  };
  var open = function () { document.getElementById("opentrade").click(); };

  open(); await pause();
  out.opened = fee.value;
  choose("ACME"); type(qty, "100"); type(price, "50");
  out.local = fee.value;                         // 5 + 0.1% of 5000
  type(qty, "1"); type(price, "10");
  out.localSmall = fee.value;                    // 5 + 0.1% of 10

  choose("ZULU"); type(qty, "100"); type(price, "50");
  out.foreignMinimum = fee.value;                // 0.11% of 5000 is under the minimum
  type(qty, "10000");
  out.foreignPercent = fee.value;                // 0.11% of 500000

  choose("new");
  type(form.querySelector('[name="new_currency"]'), "USD");
  type(qty, "100"); type(price, "50");
  out.newForeign = fee.value;

  choose("ACME");
  type(fee, "7");
  type(qty, "500");
  out.typedKept = fee.value;

  open(); await pause();
  out.reopened = {value: fee.value, typed: fee.dataset.typed || ""};

  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def test_the_form_fills_the_brokerage_from_the_portfolios_fees(
        client, session_factory, tmp_path):
    make_login(client, session_factory)
    with session_factory() as s:
        portfolio = bind_to_only_portfolio(s)
        portfolio.brokerage_flat, portfolio.brokerage_percent = 5, 0.1
        portfolio.foreign_brokerage_percent = 0.11
        portfolio.foreign_brokerage_minimum = 14.95
        fac.make_instrument(s, "ACME")
        fac.make_instrument(s, "ZULU", exchange="NASDAQ", currency="USD")
        s.commit()
    page = client.get("/", headers=HTML)
    target = tmp_path / "dashboard.html"
    target.write_text(_standalone(page.text, DRIVER))
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=8000", target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the driver did not finish"
    out = json.loads(found.group(1).replace("&quot;", '"'))

    assert out["opened"] == "5.00"
    assert (out["local"], out["localSmall"]) == ("10.00", "5.01")
    assert (out["foreignMinimum"], out["foreignPercent"]) == ("14.95", "550.00")
    assert out["newForeign"] == "14.95"
    assert out["typedKept"] == "7"
    assert out["reopened"] == {"value": "5.00", "typed": ""}
