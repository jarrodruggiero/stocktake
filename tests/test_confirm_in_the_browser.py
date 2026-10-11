"""A form that asks first, in a real browser: declined, nothing is sent.

Removing a holding, or a template every account's imports are read with,
went at a click. Such a form now carries its question in `data-confirm`,
and confirm.js asks it.
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

WALK = """
<script>
window.addEventListener("load", async function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var asked = [], answer = false, sent = [];
  window.confirm = function (words) { asked.push(words); return answer; };
  // After the page's own listener: was the form let through? Never actually
  // leave the page, so the walk can finish.
  window.addEventListener("submit", function (e) { sent.push(!e.defaultPrevented); e.preventDefault(); });
  var remove = document.querySelector('form[action$="/remove"] button');
  remove.click(); await sleep(50);
  answer = true;
  remove.click(); await sleep(50);
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify({asked: asked, sent: sent});
  document.body.appendChild(pre);
});
</script>
"""


def test_a_declined_question_sends_nothing(client, session_factory, tmp_path):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fac.hold(s, fac.make_instrument(s, "SPARE"))
        s.commit()
    page = client.get("/holdings", headers=HTML).text
    target = tmp_path / "holdings.html"
    target.write_text(_standalone(page, WALK), encoding="utf-8")
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--virtual-time-budget=4000", target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the walk did not finish"
    out = json.loads(found.group(1).replace("&quot;", '"'))

    question = "Remove SPARE from this portfolio? Its note and reinvest setting go with it."
    assert out == {"asked": [question, question], "sent": [False, True]}
