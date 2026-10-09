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
        fac.make_instrument(s, "ACME")
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
