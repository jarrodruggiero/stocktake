"""The Record trade dialog, driven in a real browser.

Three reports from test users, none of which a request-level test can see:
Cancel left what was typed in the form for next time; Save could be pressed
again while a slow save was still working; and on a desktop the dialog was a
phone-width column.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

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

DRIVER = """
<script>
window.addEventListener("load", async function () {
  var pause = function () { return new Promise(function (r) { setTimeout(r, 60); }); };
  var out = {};
  var dialog = document.getElementById("tradedialog");
  var form = dialog.querySelector("form");
  var open = function () { document.getElementById("opentrade").click(); };
  var pick = form.querySelector("#instpick");
  var qty = form.querySelector("#quantity");
  var note = form.querySelector('[name="note"]');
  var choose = function (value) {
    pick.value = value;
    pick.dispatchEvent(new Event("change", {bubbles: true}));
  };

  open(); await pause();
  out.width = Math.round(dialog.getBoundingClientRect().width);
  choose(pick.options[1].value);
  qty.value = "7"; qty.dispatchEvent(new Event("input", {bubbles: true}));
  note.value = "typed";
  form.querySelector("[data-close-dialog]").click(); await pause();
  open(); await pause();
  out.afterCancel = {instrument: pick.value, quantity: qty.value, note: note.value,
                     typed: qty.dataset.typed || ""};

  choose("new");
  out.widthAdding = Math.round(dialog.getBoundingClientRect().width);
  form.querySelector('[name="new_ticker"]').value = "MSFT";
  dialog.close(); await pause();          // what Escape ends in
  open(); await pause();
  out.afterEscape = {instrument: pick.value,
                     ticker: form.querySelector('[name="new_ticker"]').value,
                     newHidden: document.getElementById("newinst").hidden};

  choose(pick.options[1].value);
  qty.value = "7";
  form.querySelector("#unit_price").value = "5";
  var stopped = [];
  window.addEventListener("submit", function (e) {
    stopped.push(e.defaultPrevented);
    e.preventDefault();                   // stay on the page to look
  });
  var save = form.querySelector('.commitbar button[type="submit"]');
  form.requestSubmit(save);
  out.busy = save.getAttribute("aria-busy");
  form.requestSubmit(save);
  out.stopped = stopped;

  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def _standalone(page: str, driver: str) -> str:
    """The page with its stylesheet and scripts inlined, for `file://`."""
    def script(match: re.Match) -> str:
        path = STATIC / match.group(1)
        if match.group(1) == "idle.js" or not path.is_file():
            return ""
        return "<script>" + path.read_text() + "</script>"

    page = re.sub(r'<script src="/static/([^"?]+)[^"]*"[^>]*></script>', script, page)
    page = re.sub(r'<link rel="stylesheet" href="/static/style\.css[^"]*"[^>]*>',
                  lambda _m: "<style>" + (STATIC / "style.css").read_text() + "</style>",
                  page)
    return page.replace("</body>", driver + "</body>")


def _drive(client, session_factory, tmp_path, driver: str, head: str = "") -> dict:
    """The Portfolio page, one listed instrument, driven by `driver`."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fac.hold(s, fac.make_instrument(s, "ACME"))
        s.commit()
    page = client.get("/", headers=HTML)
    assert page.status_code == 200
    target = tmp_path / "dashboard.html"
    target.write_text(_standalone(page.text, driver).replace("<head>", "<head>" + head, 1))
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=8000", target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the driver did not finish"
    return json.loads(found.group(1).replace("&quot;", '"'))


def test_the_trade_dialog(client, session_factory, tmp_path):
    out = _drive(client, session_factory, tmp_path, DRIVER)

    # Cancel leaves nothing behind, including what the form protects as typed.
    assert out["afterCancel"] == {"instrument": "", "quantity": "", "note": "", "typed": ""}
    # Escape is the other way out, and a half-typed new instrument goes too.
    assert out["afterEscape"] == {"instrument": "", "ticker": "", "newHidden": True}
    # Pressed once, Save says it is working, and a second press goes nowhere.
    assert out["busy"] == "true"
    assert out["stopped"] == [False, True]
    # A desktop gets a dialog the width of a form, not of a phone, and more
    # while an instrument is being added beside the trade.
    assert out["width"] >= 800, out["width"]
    assert out["widthAdding"] > out["width"], out


# The provider, as the lookup endpoint would answer for MSFT.
LOOKUP = """
<script>
window.fetch = function (url) {
  if (String(url).indexOf("/holdings/lookup") === 0) {
    return Promise.resolve({ok: true, json: function () {
      return Promise.resolve({found: true, name: "Microsoft Corporation",
                              currency: "USD", symbol: "MSFT", asset_class: "share"});
    }});
  }
  return Promise.reject(new Error("offline"));
};
</script>
"""

TYPING = """
<script>
window.addEventListener("load", async function () {
  var pause = function () { return new Promise(function (r) { setTimeout(r, 60); }); };
  var out = {};
  var form = document.getElementById("tradedialog").querySelector("form");
  var pick = form.querySelector("#instpick");
  var ticker = form.querySelector('[name="new_ticker"]');
  var exchange = form.querySelector('[name="new_exchange"]');
  var cls = form.querySelector("#new_class");
  var shown = form.querySelector("#new_class-shown");
  document.getElementById("opentrade").click(); await pause();
  pick.value = "new"; pick.dispatchEvent(new Event("change", {bubbles: true}));

  out.askedFirst = {pickerShown: !cls.hidden, required: cls.required, value: cls.value};
  exchange.value = "NASDAQ";
  ticker.value = "MSFT";
  ticker.dispatchEvent(new Event("change", {bubbles: true})); await pause();
  out.found = {pickerShown: !cls.hidden, shown: shown.hidden ? "" : shown.textContent,
               value: cls.value,
               currency: form.querySelector('[name="new_currency"]').value};

  exchange.value = "ASX";
  ticker.value = "acme";
  ticker.dispatchEvent(new Event("change", {bubbles: true})); await pause();
  var listed = pick.querySelector('option[data-ticker="ACME"]');
  out.switched = {picked: pick.value === listed.value,
                  newHidden: document.getElementById("newinst").hidden};

  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def test_typing_an_instrument(client, session_factory, tmp_path):
    out = _drive(client, session_factory, tmp_path, TYPING, head=LOOKUP)

    # Nothing is assumed before the provider answers: no class until asked.
    assert out["askedFirst"] == {"pickerShown": True, "required": True, "value": ""}
    # Once it does, the class is shown as the value alone, and the currency
    # is the instrument's own rather than the field's starting AUD.
    assert out["found"] == {"pickerShown": False, "shown": "Share", "value": "share",
                            "currency": "USD"}
    # A ticker that is already listed, typed as new, switches to it.
    assert out["switched"] == {"picked": True, "newHidden": True}


# Yahoo by exchange: GOOG on the ASX is nothing, on NASDAQ it is Alphabet. A
# price comes back only for the symbol that exists.
BY_EXCHANGE = """
<script>
window.fetch = function (url) {
  url = String(url);
  var answer = null;
  if (url.indexOf("/holdings/lookup") === 0) {
    answer = url.indexOf("exchange=NASDAQ") > 0
      ? {found: true, name: "Alphabet Inc.", currency: "USD", symbol: "GOOG", asset_class: "share"}
      : {found: false, name: "", currency: "", symbol: "GOOG.AX", asset_class: null};
  } else if (url.indexOf("/holdings/price?symbol=GOOG&") === 0) {
    answer = {price: "250.10", fx: "0.65"};
  } else if (url.indexOf("/holdings/price") === 0) {
    answer = {price: "", fx: ""};
  }
  if (!answer) { return Promise.reject(new Error("offline")); }
  return Promise.resolve({ok: true, json: function () { return Promise.resolve(answer); }});
};
</script>
"""

EXCHANGE_AFTER_TICKER = """
<script>
window.addEventListener("load", async function () {
  var pause = function () { return new Promise(function (r) { setTimeout(r, 60); }); };
  var out = {};
  var form = document.getElementById("tradedialog").querySelector("form");
  var field = function (name) { return form.querySelector('[name="' + name + '"]'); };
  var pick = form.querySelector("#instpick");
  document.getElementById("opentrade").click(); await pause();
  pick.value = "new"; pick.dispatchEvent(new Event("change", {bubbles: true}));

  field("new_ticker").value = "GOOG";
  field("new_ticker").dispatchEvent(new Event("change", {bubbles: true})); await pause();
  out.onTheAsx = field("new_yahoo").value;
  field("new_exchange").value = "NASDAQ";
  field("new_exchange").dispatchEvent(new Event("change", {bubbles: true})); await pause();
  out.onNasdaq = {symbol: field("new_yahoo").value, name: field("new_name").value,
                  price: document.getElementById("unit_price").value};

  field("new_yahoo").value = "GOOGL";
  field("new_yahoo").dispatchEvent(new Event("input", {bubbles: true}));
  field("new_exchange").value = "ASX";
  field("new_exchange").dispatchEvent(new Event("change", {bubbles: true})); await pause();
  out.typedKept = field("new_yahoo").value;

  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def test_changing_the_exchange_looks_the_ticker_up_again(client, session_factory, tmp_path):
    """Reported: a NASDAQ ticker typed before its exchange kept the ASX guess,
    GOOG.AX, so the price never came. The lookup's own answers give way to
    the next one; what was typed by hand still does not."""
    out = _drive(client, session_factory, tmp_path, EXCHANGE_AFTER_TICKER, head=BY_EXCHANGE)
    assert out["onTheAsx"] == "GOOG.AX"
    assert out["onNasdaq"] == {"symbol": "GOOG", "name": "Alphabet Inc.", "price": "250.10"}
    assert out["typedKept"] == "GOOGL"


EXCHANGE_THAT_FINDS_NOTHING = """
<script>
window.addEventListener("load", async function () {
  var pause = function () { return new Promise(function (r) { setTimeout(r, 60); }); };
  var form = document.getElementById("tradedialog").querySelector("form");
  var field = function (name) { return form.querySelector('[name="' + name + '"]'); };
  var pick = form.querySelector("#instpick");
  document.getElementById("opentrade").click(); await pause();
  pick.value = "new"; pick.dispatchEvent(new Event("change", {bubbles: true}));
  field("new_exchange").value = "NASDAQ";
  field("new_ticker").value = "GOOG";
  field("new_ticker").dispatchEvent(new Event("change", {bubbles: true})); await pause();
  field("new_exchange").value = "ASX";
  field("new_exchange").dispatchEvent(new Event("change", {bubbles: true})); await pause();
  var out = {name: field("new_name").value, currency: field("new_currency").value,
             symbol: field("new_yahoo").value,
             price: document.getElementById("unit_price").value,
             fx: document.getElementById("fx_rate").value,
             cls: document.getElementById("new_class").value,
             picker: !document.getElementById("new_class").hidden};
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def test_an_exchange_where_nothing_is_found_keeps_nothing_from_the_last_answer(
        client, session_factory, tmp_path):
    """GOOG on NASDAQ fills a name, USD, a price and a rate. Moved to the ASX,
    where there is no GOOG, the form is as it was before the first answer: a
    NASDAQ price left in the field records a trade at somebody else's price."""
    out = _drive(client, session_factory, tmp_path, EXCHANGE_THAT_FINDS_NOTHING,
                 head=BY_EXCHANGE)
    assert out == {"name": "", "currency": "AUD", "symbol": "GOOG.AX", "price": "",
                   "fx": "", "cls": "", "picker": True}


FX_SHOWN = """
<script>
window.addEventListener("load", function () {
  setTimeout(function () {
    var field = document.getElementById("fxfield");
    var pre = document.createElement("pre");
    pre.id = "result";
    pre.textContent = JSON.stringify({shown: !field.hidden,
                                      rate: document.getElementById("fx_rate").value});
    document.body.appendChild(pre);
  }, 100);
});
</script>
"""

NO_PRICES = """
<script>
window.fetch = function () {
  return Promise.resolve({ok: true, json: function () {
    return Promise.resolve({price: null, fx: null});
  }});
};
</script>
"""


def _fx_on(client, tmp_path, path: str) -> dict:
    page = client.get(path, headers=HTML)
    assert page.status_code == 200, page.text
    target = tmp_path / "page.html"
    target.write_text(_standalone(page.text, FX_SHOWN).replace("<head>", "<head>" + NO_PRICES, 1))
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=8000", target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the driver did not finish"
    return json.loads(found.group(1).replace("&quot;", '"'))


def test_the_fx_rate_is_there_wherever_the_instrument_is_foreign(
        client, session_factory, tmp_path):
    """The field appeared only when an instrument was PICKED, so a US trade's
    recorded rate could not be seen on its own edit page, nor on a trade
    started from a US holding's page."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        nova = fac.make_instrument(s, "NOVA", exchange="NASDAQ", currency="USD",
                                   asset_class="share")
        acme = fac.make_instrument(s, "ACME")
        usd = fac.add_trade(s, nova, "2026-07-01", "buy", 10, "10.00", fx_rate="1.5")
        aud = fac.add_trade(s, acme, "2026-07-01", "buy", 10, "1.00")
        s.commit()
        usd_id, aud_id = usd.id, aud.id
    assert _fx_on(client, tmp_path, f"/trade/{usd_id}/edit") == {"shown": True, "rate": "1.5"}
    assert _fx_on(client, tmp_path, "/trade/new?ticker=NOVA")["shown"] is True
    assert _fx_on(client, tmp_path, f"/trade/{aud_id}/edit")["shown"] is False
    assert _fx_on(client, tmp_path, "/trade/new?ticker=ACME")["shown"] is False
