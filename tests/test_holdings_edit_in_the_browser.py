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
    target.write_text(_standalone(page.text, DRIVER).replace("<head>", "<head>" + SERVER, 1), encoding="utf-8")
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=10000", target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the driver did not finish"
    out = json.loads(found.group(1).replace("&quot;", '"'))
    assert out == {"refused": RULE, "later": RULE, "saved": "saved"}


# Every POST the page makes, and a confirm that answers as the walk says.
ASKING = """
<script>
window.posts = [];
window.answer = false;
window.asked = [];
window.confirm = function (text) { window.asked.push(text); return window.answer; };
window.fetch = function (url, options) {
  window.posts.push(options.body.get("currency"));
  return Promise.resolve({ok: true, json: function () { return Promise.resolve({}); }});
};
</script>
"""

ASKED = """
<script>
window.addEventListener("load", async function () {
  var wait = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var currency = document.querySelector('input[name="currency"][data-reach]');
  var note = document.querySelector('input[name="note"][form="' + currency.getAttribute("form") + '"]');
  var change = async function (field, value) {
    field.value = value;
    field.dispatchEvent(new Event("change", {bubbles: true})); await wait(100);
  };
  var out = {};
  await change(currency, "USD");
  out.declined = {posts: window.posts.slice(), value: currency.value};
  window.answer = true;
  await change(currency, "USD");
  out.accepted = window.posts.slice();
  await change(note, "watching it");
  out.asked = window.asked.length;
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def test_a_new_currency_that_reaches_other_portfolios_asks_first(client, session_factory,
                                                                 tmp_path):
    """Declined, nothing is saved and the old currency is back in the field.
    Accepted, it saves; and once saved, a later edit of the row asks nothing."""
    from test_holdings_edit import _setup

    _setup(client, session_factory, admin=True)
    page = client.get("/holdings", headers=HTML)
    target = tmp_path / "holdings.html"
    target.write_text(_standalone(page.text, ASKED).replace("<head>", "<head>" + ASKING, 1),
                      encoding="utf-8")
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=10000", target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the driver did not finish"
    out = json.loads(found.group(1).replace("&quot;", '"'))
    assert out == {"declined": {"posts": [], "value": "AUD"}, "accepted": ["USD"],
                   "asked": 2}
