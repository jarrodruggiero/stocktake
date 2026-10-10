"""The plan's rotation editor, driven in a real browser.

The editor is chips over a hidden textarea, which is still the field that
submits, so every change has to land in that field, in order, repeats and all.
Its tests read the page's markup; none pressed a button. So a seeded walk
here adds, moves, drags, removes and edits as text, and after every step the
chips, the field, the count, the numbering and the arrows must all match a
plain list kept beside them. The preview's answers arrive late and out of
order, and the table must end on the rotation as it stands.

A press on a palette chip that wobbled near the list added the ticker twice:
the drop placed one, and the click that ends every press added another.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest
from test_trade_dialog_in_the_browser import CHROME, HTML, _standalone

import factories as fac
from test_routes import make_login

pytestmark = pytest.mark.visual

TICKERS = ["ALPHA", "BETAX", "GAMMA", "OMEGA", "ZULU", "VAS", "IVV", "NDQ", "A200"]

# The preview's answers come back after a delay, `DELAY`, a function of the
# request's number and a seeded draw; an abort is honoured, as a real fetch
# honours it.
PREVIEW = """
<script>
window.previewAsked = [];
(function () {
  var a = 7;
  var rand = function () {
    a |= 0; a = a + 0x6D2B79F5 | 0;
    var t = Math.imul(a ^ a >>> 15, 1 | a);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
  window.fetch = function (url, options) {
    if (String(url) !== "/schedule/preview") { return Promise.reject(new Error("offline")); }
    var tickers = options.body.get("tickers"), n = window.previewAsked.length;
    window.previewAsked.push(tickers);
    var rows = tickers ? tickers.split(", ").map(function (t, i) {
      return {date: "2026-09-" + String(10 + i).padStart(2, "0"), ticker: t};
    }) : [];
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () {
        resolve({ok: true, json: function () { return Promise.resolve({rows: rows}); }});
      }, DELAY);
      options.signal.addEventListener("abort", function () {
        clearTimeout(timer);
        reject(new DOMException("aborted", "AbortError"));
      });
    });
  };
})();
</script>
"""

# Often slower than the editor's 250ms debounce, so requests overlap.
SOMETIMES_SLOW = "Math.floor(rand() * 600)"


WALK = """
<script>
window.addEventListener("load", async function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  function mulberry(a) {
    return function () {
      a |= 0; a = a + 0x6D2B79F5 | 0;
      var t = Math.imul(a ^ a >>> 15, 1 | a);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  var field = document.getElementById("tickers");
  var list = document.getElementById("rotorder");
  var toggle = document.getElementById("rotastext");
  var count = document.querySelector(".rotcount");
  var empty = document.querySelector(".rotempty");
  var palette = Array.prototype.slice.call(document.querySelectorAll(".rotsource .add"));
  var chips = function () { return Array.prototype.slice.call(list.querySelectorAll("li.rotitem")); };
  var shown = function () { return chips().map(function (li) { return li.dataset.ticker; }); };
  var parse = function (text) {
    return text.replace(/\\n/g, ",").split(",").map(function (t) { return t.trim().toUpperCase(); })
      .filter(Boolean);
  };
  var press = function (el, x, y, type) {
    el.dispatchEvent(new PointerEvent(type, {bubbles: true, cancelable: true, clientX: x,
      clientY: y, button: 0, pointerType: "mouse", isPrimary: true, pointerId: 1}));
  };

  var runs = [];
  for (var s = 0; s < SEEDS.length; s++) {
    var rand = mulberry(SEEDS[s]);
    var pick = function (n) { return Math.floor(rand() * n); };
    field.value = "";
    field.dispatchEvent(new Event("change"));
    var model = [], log = [], faults = [];
    for (var step = 0; step < STEPS; step++) {
      var n = model.length;
      var kind = ["add", "add", "drop", "up", "down", "remove", "drag", "text"][pick(8)];
      if (n === 0 && kind !== "text" && kind !== "drop") { kind = "add"; }
      if (kind === "add") {
        var button = palette[pick(palette.length)];
        button.click();
        model.push(button.dataset.ticker);
        log.push("add " + button.dataset.ticker);
      } else if (kind === "drop") {
        // Dragged from the palette and let go on the list, before a chip or
        // after the last; the browser's click then goes to no chip at all.
        var source = palette[pick(palette.length)], at = pick(n + 1);
        var sb = source.getBoundingClientRect(), lb = list.getBoundingClientRect();
        var all = chips(), x = lb.left + 10;
        var y = at < n ? all[at].getBoundingClientRect().top + 2
          : (n ? all[n - 1].getBoundingClientRect().bottom + 6 : lb.top + 10);
        press(source, sb.left + 2, sb.top + 2, "pointerdown");
        press(document, x, y, "pointermove");
        press(at < n ? all[at] : list, x, y, "pointerup");
        model.splice(at, 0, source.dataset.ticker);
        log.push("drop " + source.dataset.ticker + " at " + at);
      } else if (kind === "up" || kind === "down") {
        var i = pick(n), li = chips()[i];
        var arrow = li.querySelector("[data-move='" + kind + "']");
        // A disabled button cannot be pressed; the walk presses it anyway,
        // as a click() would, and nothing may move.
        var stuck = arrow.disabled;
        arrow.click();
        var j = kind === "up" ? i - 1 : i + 1;
        if (!stuck && j >= 0 && j < n) { var t = model[i]; model[i] = model[j]; model[j] = t; }
        log.push(kind + " " + i);
      } else if (kind === "remove") {
        var r = pick(n);
        chips()[r].querySelector("[data-remove]").click();
        model.splice(r, 1);
        log.push("remove " + r);
      } else if (kind === "drag") {
        // Picked up by its name and let go over the upper half of another
        // chip, or below the last: it lands before that chip, or at the end.
        var from = pick(n), to = pick(n + 1);
        var items = chips(), name = items[from].querySelector(".rotname");
        var nb = name.getBoundingClientRect();
        press(name, nb.left + 2, nb.top + 2, "pointerdown");
        var target = to < n ? items[to].getBoundingClientRect() : null;
        var last = items[n - 1].getBoundingClientRect();
        var y = target ? target.top + 2 : last.bottom + 6;
        press(document, nb.left + 2, y, "pointermove");
        press(document, nb.left + 2, y, "pointerup");
        var rest = model.slice(0, from).concat(model.slice(from + 1));
        rest.splice(to === n ? rest.length : (to > from ? to - 1 : to), 0, model[from]);
        model = rest;
        log.push("drag " + from + " to " + to);
      } else {
        var words = [];
        for (var w = pick(4); w > 0; w--) {
          var tk = TICKERS[pick(TICKERS.length)];
          words.push(rand() < 0.3 ? tk.toLowerCase() : tk);
        }
        var text = words.join(rand() < 0.5 ? ", " : "\\n");
        toggle.click();                                   // to the text box
        field.value = text;
        toggle.click();                                   // and back to chips
        model = parse(text);
        log.push("text " + JSON.stringify(text));
      }
      var now = shown(), want = model.slice();
      var state = {
        chips: now, field: field.value, count: count.textContent, empty: empty.hidden,
        numbers: chips().map(function (li) { return li.querySelector(".rotpos").textContent; }),
        ups: chips().map(function (li) { return li.querySelector("[data-move='up']").disabled; }),
        downs: chips().map(function (li) { return li.querySelector("[data-move='down']").disabled; }),
      };
      var expect = {
        chips: want, field: want.join(", "),
        count: want.length ? want.length + (want.length === 1 ? " buy in the cycle"
                                                             : " buys in the cycle") : "",
        empty: want.length > 0,
        numbers: want.map(function (_t, k) { return String(k + 1); }),
        ups: want.map(function (_t, k) { return k === 0; }),
        downs: want.map(function (_t, k) { return k === want.length - 1; }),
      };
      if (JSON.stringify(state) !== JSON.stringify(expect)) {
        faults.push({step: step, did: log[log.length - 1], got: state, want: expect});
        model = now;                                      // carry on from what is there
      }
      if (rand() < 0.25) { await sleep(300); }            // let some previews land
    }
    await sleep(1200);
    var table = Array.prototype.map.call(
      document.querySelectorAll("#previewrows tr td:nth-child(2)"),
      function (td) { return td.textContent; });
    runs.push({seed: SEEDS[s], log: log, faults: faults, final: shown(), table: table,
               lastAsked: window.previewAsked[window.previewAsked.length - 1]});
  }
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(runs);
  document.body.appendChild(pre);
});
</script>
"""


def _page(client, session_factory) -> str:
    make_login(client, session_factory)
    with session_factory() as s:
        for ticker in TICKERS:
            fac.hold(s, fac.make_instrument(s, ticker))
        s.commit()
    page = client.get("/schedule?edit=1", headers=HTML)
    assert page.status_code == 200
    return page.text


def _chrome(target, *extra) -> str:
    return subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000", *extra,
         "--virtual-time-budget=600000", target.as_uri()],
        capture_output=True, text=True, timeout=180).stdout


def _result(dom: str):
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the driver did not finish"
    return json.loads(found.group(1).replace("&quot;", '"').replace("&amp;", "&"))


@pytest.mark.parametrize("seeds", [[1, 2, 3], [4, 5, 6]])
def test_every_step_lands_in_the_field_in_order(client, session_factory, tmp_path, seeds):
    target = tmp_path / "plan.html"
    driver = (WALK.replace("SEEDS", json.dumps(seeds)).replace("STEPS", "40")
              .replace("TICKERS", json.dumps(TICKERS)))
    target.write_text(_standalone(_page(client, session_factory), driver)
                      .replace("<head>", "<head>" + PREVIEW.replace("DELAY", SOMETIMES_SLOW), 1))
    runs = _result(_chrome(target, "--force-prefers-reduced-motion"))

    for run in runs:
        assert run["faults"] == [], (run["seed"], run["faults"][:2], run["log"])
        # The newest rotation is the one previewed, whatever order the
        # answers came back in.
        assert run["lastAsked"] == ", ".join(run["final"]), run["seed"]
        assert run["table"] == run["final"], run["seed"]
    done = [line.split()[0] for run in runs for line in run["log"]]
    assert {"add", "drop", "up", "down", "remove", "drag", "text"} <= set(done)


NEAR_THE_LIST = """
<script>
window.addEventListener("load", async function () {
  var list = document.getElementById("rotorder");
  var zone = list.getBoundingClientRect();
  // rotation.js takes a pointer within 40px of the list to be over it.
  var near = Array.prototype.slice.call(document.querySelectorAll(".rotsource .add"))
    .filter(function (b) {
      var box = b.getBoundingClientRect(), y = box.top + box.height / 2;
      return box.right - 4 >= zone.left - 40 && y >= zone.top - 40 && y <= zone.bottom + 40;
    });
  var out = {near: near.map(function (b) { return b.dataset.ticker; })};
  var chip = near[0];
  if (chip) {
    var box = chip.getBoundingClientRect();
    var x = box.right - 3, y = box.top + box.height / 2;
    var press = function (el, type, dx) {
      el.dispatchEvent(new PointerEvent(type, {bubbles: true, cancelable: true,
        clientX: x + dx, clientY: y, button: 0, pointerType: "mouse", isPrimary: true,
        pointerId: 1}));
    };
    var before = list.querySelectorAll("li.rotitem").length;
    // A click whose pointer wobbles a pixel: down, move, up, all on the chip,
    // and then the click the browser sends for a press that ended where it began.
    press(chip, "pointerdown", 0);
    press(document, "pointermove", -1);
    press(chip, "pointerup", -1);
    chip.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true, detail: 1}));
    await new Promise(function (r) { setTimeout(r, 50); });
    out.added = Array.prototype.slice.call(list.querySelectorAll("li.rotitem"), before)
      .map(function (li) { return li.dataset.ticker; });
    out.ticker = chip.dataset.ticker;
  }
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def test_a_click_that_wobbles_near_the_list_adds_once(client, session_factory, tmp_path):
    target = tmp_path / "plan.html"
    target.write_text(_standalone(_page(client, session_factory), NEAR_THE_LIST)
                      .replace("<head>", "<head>" + PREVIEW.replace("DELAY", "0"), 1))
    out = _result(_chrome(target))

    assert out["near"], "no palette chip sits within reach of the list at this width"
    assert out["added"] == [out["ticker"]]


# The page's own first preview is slow and the one after a click quick, so the
# first lands last unless it was abandoned.
RACE = """
<script>
window.addEventListener("load", async function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  await sleep(300);                         // the first preview is on its way
  document.querySelector(".rotsource .add").click();
  await sleep(1500);
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify({
    asked: window.previewAsked,
    chips: Array.prototype.map.call(document.querySelectorAll("#rotorder li.rotitem"),
                                    function (li) { return li.dataset.ticker; }),
    table: Array.prototype.map.call(document.querySelectorAll("#previewrows tr td:nth-child(2)"),
                                    function (td) { return td.textContent; })});
  document.body.appendChild(pre);
});
</script>
"""


def test_a_preview_that_arrives_late_does_not_replace_a_newer_one(client, session_factory,
                                                                 tmp_path):
    target = tmp_path / "plan.html"
    target.write_text(_standalone(_page(client, session_factory), RACE)
                      .replace("<head>", "<head>" + PREVIEW.replace("DELAY", "n === 0 ? 800 : 10"),
                               1))
    out = _result(_chrome(target, "--force-prefers-reduced-motion"))

    assert len(out["asked"]) == 2, "the click did not ask while the first was out"
    assert out["table"] == out["chips"]
