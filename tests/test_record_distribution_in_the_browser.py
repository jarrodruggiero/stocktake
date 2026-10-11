"""Record a distribution, opened from the holding page in a real browser."""

from __future__ import annotations

import json
import re
import subprocess

import pytest
from test_trade_dialog_in_the_browser import CHROME, HTML, _standalone

import factories as fac
from test_routes import bind_to_only_portfolio, make_login

pytestmark = pytest.mark.visual

WALK = """
<script>
window.addEventListener("load", async function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var dialog = document.getElementById("distdialog");
  document.querySelector('[data-opens="distdialog"]').click(); await sleep(50);
  var out = {opened: dialog.open, modal: dialog.matches(":modal"),
             chosen: dialog.querySelector('[name="ticker"]').value};
  dialog.querySelector("[data-close-dialog]").click(); await sleep(50);
  out.closed = !dialog.open;
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def test_the_dialog_opens_for_its_holding_and_cancel_closes_it(client, session_factory, tmp_path):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fac.add_trade(s, fac.make_instrument(s, "ACME"), "2026-01-05", "buy", 10, "5.00")
        s.commit()
    page = client.get("/holding/ACME", headers=HTML).text
    target = tmp_path / "holding.html"
    target.write_text(_standalone(page, WALK), encoding="utf-8")
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--virtual-time-budget=4000", target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the walk did not finish"

    assert json.loads(found.group(1).replace("&quot;", '"')) == {
        "opened": True, "modal": True, "chosen": "ACME", "closed": True}
