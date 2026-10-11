"""The Portfolio table's column order, dragged in a real browser.

Drag is layered over the arrow buttons: it moves the list items and rewrites
what they carry, and the form posts the same either way. Its tests read the
markup, and none had dragged anything. So a seeded walk drags columns about
and, after every drop, holds the list to a plain list kept beside it: the
order shown, the order the form will post, the positions shown, and which
arrows can still move anything.

The positions were left behind: after a drag the numbers stayed where they
started, so the list read 3, 1, 2 down the page. The numbers exist because a
two-column grid of names does not say whether it reads across or down.
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
window.addEventListener("load", function () {
  function mulberry(a) {
    return function () {
      a |= 0; a = a + 0x6D2B79F5 | 0;
      var t = Math.imul(a ^ a >>> 15, 1 | a);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  var rand = mulberry(SEED);
  var list = document.getElementById("colorderlist");
  document.querySelector("details.colchooser").open = true;
  var items = function () { return Array.from(list.children); };
  var model = items().map(function (li) { return li.dataset.key; });
  var problems = [], drops = 0;

  function fire(el, type, transfer, x) {
    var box = el.getBoundingClientRect();
    el.dispatchEvent(new DragEvent(type, {
      bubbles: true, cancelable: true, dataTransfer: transfer,
      clientX: x, clientY: box.top + box.height / 2}));
  }
  function check(step) {
    var now = {
      shown: items().map(function (li) { return li.dataset.key; }),
      posted: Array.from(list.querySelectorAll('input[name="order"]'))
        .map(function (input) { return input.value; }),
      numbers: Array.from(list.querySelectorAll(".colpos"))
        .map(function (span) { return span.textContent.trim(); }),
      upOff: items().map(function (li) { return li.querySelectorAll("button")[0].disabled; }),
      downOff: items().map(function (li) { return li.querySelectorAll("button")[1].disabled; })
    };
    var want = {
      shown: model, posted: model,
      numbers: model.map(function (_k, i) { return String(i + 1); }),
      upOff: model.map(function (_k, i) { return i === 0; }),
      downOff: model.map(function (_k, i) { return i === model.length - 1; })
    };
    Object.keys(want).forEach(function (what) {
      if (JSON.stringify(now[what]) !== JSON.stringify(want[what])) {
        problems.push({step: step, what: what, got: now[what], wanted: want[what]});
      }
    });
  }

  for (var step = 0; step < 14; step++) {
    var row = items(), from = Math.floor(rand() * row.length), to = Math.floor(rand() * row.length);
    if (from === to) { continue; }
    var after = rand() < 0.5, source = row[from], target = row[to];
    var box = target.getBoundingClientRect();
    var x = after ? box.right - 2 : box.left + 2;
    var transfer = new DataTransfer();
    fire(source, "dragstart", transfer, box.left);
    fire(target, "dragover", transfer, x);
    fire(target, "drop", transfer, x);
    fire(source, "dragend", transfer, x);
    var key = model.splice(from, 1)[0];
    model.splice(model.indexOf(target.dataset.key) + (after ? 1 : 0), 0, key);
    drops++;
    check(step);
  }
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify({drops: drops, columns: model.length,
                                    problems: problems.slice(0, 3)});
  document.body.appendChild(pre);
});
</script>
"""


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_dragging_columns_keeps_every_part_of_the_list_in_step(client, session_factory,
                                                              tmp_path, seed):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fac.add_trade(s, fac.make_instrument(s, "ACME"), "2026-01-05", "buy", 10, "5.00")
        s.commit()
    page = client.get("/", headers=HTML)
    assert page.status_code == 200
    target = tmp_path / "dashboard.html"
    target.write_text(_standalone(page.text, WALK.replace("SEED", str(seed))))

    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=5000", target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the walk did not finish"
    out = json.loads(found.group(1).replace("&quot;", '"'))

    assert out["columns"] >= 3 and out["drops"] >= 5, out      # the walk did walk
    assert out["problems"] == [], out["problems"]
