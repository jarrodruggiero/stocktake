"""A form that asks first, in a real browser: declined, nothing is sent.

Removing a holding, or a template every account's imports are read with,
went at a click. Such a form now carries its question in `data-confirm`,
and confirm.js asks it.

Every question is asked that way. Written into an inline handler, the
words were part of a script, and an apostrophe in a name broke it; the
Restart button's question, written with `tojson` inside a double-quoted
handler, was cut short, so it never asked at all.
"""

from __future__ import annotations

import html
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
  var button = document.querySelector(SELECTOR);
  button.click(); await sleep(50);
  answer = true;
  button.click(); await sleep(50);
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify({asked: asked, sent: sent, ran: window.ran || null});
  document.body.appendChild(pre);
});
</script>
"""


def _walk(page: str, tmp_path, selector: str) -> dict:
    """Press the button `selector` finds twice, declining and then accepting."""
    target = tmp_path / "page.html"
    target.write_text(_standalone(page, WALK.replace("SELECTOR", json.dumps(selector))),
                      encoding="utf-8")
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--virtual-time-budget=4000", target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the walk did not finish"
    return json.loads(html.unescape(found.group(1)))


def test_a_declined_question_sends_nothing(client, session_factory, tmp_path):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fac.hold(s, fac.make_instrument(s, "SPARE"))
        s.commit()
    page = client.get("/holdings", headers=HTML).text

    out = _walk(page, tmp_path, 'form[action$="/remove"] button')

    question = "Remove SPARE from this portfolio? Its note and reinvest setting go with it."
    assert out == {"asked": [question, question], "sent": [False, True], "ran": None}


CRAFTED = "x'+(window.ran='name')+'"


def test_a_name_in_a_question_is_only_ever_text(client, session_factory, tmp_path):
    make_login(client, session_factory)
    with session_factory() as s:
        other = fac.make_user(s, "other@example.test", name=CRAFTED)
        fac.make_portfolio(s, "p'+(window.ran='portfolio')+'", owner=other)
        s.commit()
    page = client.get("/admin/accounts", headers=HTML).text

    out = _walk(page, tmp_path, 'form[action$="/active"] button')

    question = (f"Disabling {CRAFTED} takes p'+(window.ran='portfolio')+' out of everyone's view"
                " — nobody else is in it. Continue?")
    assert out == {"asked": [question, question], "sent": [False, True], "ran": None}


@pytest.mark.parametrize("selector, question", [
    ('form[action$="/restart"] button', "This will STOP the application, and nothing will start it"
                                        " again. Continue?"),
    ('button[name="restart"]', "Save these settings and restart the application?"),
])
def test_a_restart_asks_first(client, session_factory, tmp_path, monkeypatch, app_module,
                              selector, question):
    make_login(client, session_factory)
    page = client.get("/admin/settings", headers=HTML).text

    out = _walk(page, tmp_path, selector)

    assert out == {"asked": [question, question], "sent": [False, True], "ran": None}
