"""The password meter and "Copy all", in a real browser.

The meter is a word and a bar from zxcvbn, guidance only: a weak password can
still be saved. "Copy all" has to work on plain HTTP, where the clipboard API
does not exist, which is how test users ran it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from test_routes import make_login, session_csrf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.browser import find_chrome  # noqa: E402

pytestmark = pytest.mark.visual

CHROME = find_chrome()
if CHROME is None:
    pytest.skip("no headless Chrome on this machine", allow_module_level=True)

HTML = {"accept": "text/html"}
STATIC = ROOT / "app" / "static"
ZXCVBN = (STATIC / "vendor" / "zxcvbn.js").read_text()


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

RESULT = """
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
"""

METER = """
<script>
window.addEventListener("load", async function () {
  var pause = function () { return new Promise(function (r) { setTimeout(r, 80); }); };
  var out = {};
  var form = document.querySelector('form[action$="/signup"]');
  var meter = form.querySelector(".strength");
  var word = function () { return meter.hidden ? "" : meter.querySelector(".strengthword").textContent; };
  var type = function (name, value) {
    var el = form.querySelector('[name="' + name + '"]');
    el.value = value; el.dispatchEvent(new Event("input", {bubbles: true}));
  };
  out.before = word();
  type("name", "Joiner"); type("email", "joiner@example.test");
  type("password", "password"); await pause(); out.password = word();
  type("password", "stocktake2026"); await pause(); out.ownWords = word();
  type("password", "quokka-tangent-maroon-ladle"); await pause(); out.passphrase = word();

  type("password", "password12"); type("confirm", "password12"); await pause();
  var stopped = [];
  window.addEventListener("submit", function (e) { stopped.push(e.defaultPrevented); e.preventDefault(); });
  form.requestSubmit();
  out.stopped = stopped;
""" + RESULT + """
});
</script>
"""

COPY = """
<script>
window.addEventListener("load", async function () {
  var pause = function () { return new Promise(function (r) { setTimeout(r, 80); }); };
  var out = {};
  // Plain HTTP: no clipboard API at all.
  Object.defineProperty(navigator, "clipboard", {value: undefined, configurable: true});
  var copied = null;
  document.execCommand = function (command) {
    if (command === "copy") { copied = document.activeElement && document.activeElement.value; }
    return true;
  };
  var button = document.querySelector("[data-copy-codes]");
  out.label = button ? button.textContent.trim() : null;
  if (!button) { button = document.createElement("button"); }
  button.click(); await pause();
  out.copied = copied;
  out.after = button.textContent.trim();
  out.codes = Array.prototype.map.call(document.querySelectorAll(".codes code"),
                                       function (c) { return c.textContent.trim(); });
""" + RESULT + """
});
</script>
"""


def _run(page: str, driver: str, tmp_path: Path, head: str = "") -> dict:
    target = tmp_path / "page.html"
    target.write_text(_standalone(page, driver).replace("<head>", "<head>" + head, 1))
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1200,1000",
         "--virtual-time-budget=8000", target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the driver did not finish"
    return json.loads(found.group(1).replace("&quot;", '"'))


def test_the_meter_says_how_strong_and_never_stops_a_save(client, session_factory, tmp_path):
    make_login(client, session_factory)
    resp = client.post("/members/invite", follow_redirects=False,
                       data={"_csrf": session_csrf(session_factory), "role": "member"})
    token = re.search(r"invite=([^&]+)", resp.headers["location"]).group(1)
    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)
    page = client.get(f"/invite/{token}", headers=HTML).text

    # zxcvbn is inlined: the page would load it from /static on first input.
    out = _run(page, METER, tmp_path, head="<script>" + ZXCVBN + "</script>")

    assert out["before"] == "", "nothing to say before anything is typed"
    assert out["password"] == "Very weak"
    # The app's name and the person's own email count against it.
    assert out["ownWords"] == "Weak"
    assert out["passphrase"] == "Very strong"
    assert out["stopped"] == [False]


def test_copy_all_works_without_the_clipboard_api(client, session_factory, tmp_path):
    make_login(client, session_factory)
    page = client.post("/profile/recovery", data={"_csrf": session_csrf(session_factory)},
                       headers=HTML).text

    out = _run(page, COPY, tmp_path)

    assert out["label"] == "Copy all"
    assert out["copied"] == "\n".join(out["codes"]) and len(out["codes"]) >= 8
    assert out["after"] == "Copied"
