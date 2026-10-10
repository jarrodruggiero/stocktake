"""The chart builder, driven in a real browser.

The builder keeps a draft chart in the page and shows it as chips on three
shelves, a type, a grouping, a date and some filter boxes; the preview and the
saved chart are drawn from the draft. Its tests read the page's markup. So a
seeded walk here places fields by clicking and by dropping, removes them,
switches the kind of chart, the type, the grouping and the date, ticks filters
and loads templates, while the previews come back late and out of order. After
every step the shelves must hold nothing they cannot (a field twice, a field
from another kind of chart, a measure where a dimension goes), a template must
load as itself, and at the end the preview must be of the chart on the shelves
and saving must save that chart.
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

# Previews answer after a seeded delay that is often longer than the
# builder's 180ms debounce, echoing the draft they were asked about; saves
# answer at once. The renderer is replaced by a recorder: what is drawn is the
# question here, not how Chart.js draws it.
STUBS = """
<script>
window.asked = [];
window.saved = [];
window.drawn = [];
(function () {
  var a = 11;
  var rand = function () {
    a |= 0; a = a + 0x6D2B79F5 | 0;
    var t = Math.imul(a ^ a >>> 15, 1 | a);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
  window.fetch = function (url, options) {
    var body = JSON.parse(options.body);
    if (String(url) === "/charts/save") {
      window.saved.push(body);
      return Promise.resolve({ok: true, json: function () { return Promise.resolve({id: 7}); }});
    }
    if (String(url) !== "/charts/preview") { return Promise.reject(new Error("offline")); }
    window.asked.push(body);
    var echo = JSON.stringify(body);
    var data = {labels: ["2026-01"], datasets: [{key: "m", label: "m", kind: "money", data: [1]}],
                type: body.type, grain: body.grain, x_label: "X", echo: echo};
    return new Promise(function (resolve) {
      setTimeout(function () {
        resolve({ok: true, json: function () { return Promise.resolve(data); }});
      }, DELAY);
    });
  };
})();
</script>
"""

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
  window.portfolioCharts.renderSpec = function (canvas, table, data) {
    window.drawn.push(data.echo);
    return {destroy: function () { window.drawn.push(null); }};
  };
  var $ = function (id) { return document.getElementById(id); };
  var cat = JSON.parse($("catalogue").textContent);
  var templates = JSON.parse($("templates").textContent);
  var field = function (key) { return cat.fields.find(function (f) { return f.key === key; }); };
  var keysOn = function (id) {
    return Array.prototype.map.call(document.querySelectorAll("#" + id + " li.chip-field"),
                                    function (li) { return li.dataset.key; });
  };
  var choose = function (select, value) {
    select.value = value;
    select.dispatchEvent(new Event("change", {bubbles: true}));
  };
  var shelves = function () {
    return {grain: $("grain").value, x: keysOn("shelf-x")[0] || null,
            measures: keysOn("shelf-measures"), split: keysOn("shelf-split")[0] || null,
            type: $("type").value, bucket: $("bucket").value, since: $("since").value || null,
            ticked: Array.prototype.filter.call(document.querySelectorAll("#filters input"),
                                                function (i) { return i.checked; })
                                      .map(function (i) { return i.id; }).sort()};
  };
  var tickedFor = function (filters) {
    var ids = [];
    Object.keys(filters || {}).forEach(function (key) {
      filters[key].forEach(function (v) { ids.push(("f-" + key + "-" + v).replace(/\\W/g, "-")); });
    });
    return ids.sort();
  };
  var faults = function (now) {
    var out = [];
    var usable = cat.chart_types.filter(function (t) { return t.grains.indexOf(now.grain) >= 0; })
                                .map(function (t) { return t.key; });
    if (new Set(now.measures).size !== now.measures.length) { out.push("a measure twice"); }
    if (keysOn("shelf-x").length > 1 || keysOn("shelf-split").length > 1) {
      out.push("two fields on a one-field shelf");
    }
    now.measures.forEach(function (k) {
      var f = field(k);
      if (!f || f.role !== "measure" || f.grain !== now.grain) { out.push("measure " + k); }
    });
    [now.x].filter(Boolean).forEach(function (k) {
      var f = field(k);
      if (!f || f.role !== "dimension" || f.grain !== now.grain) { out.push("x " + k); }
    });
    // The splits fields.validate accepts: a holding attribute for a daily
    // series, another dimension than the X axis for holdings, none for periods.
    [now.split].filter(Boolean).forEach(function (k) {
      var f = field(k);
      var holding = f && f.grain === "positions" && f.role === "dimension";
      var fits = now.grain === "timeseries" ? holding
        : now.grain === "positions" ? holding && k !== now.x : false;
      if (!f || !fits) { out.push("split " + k); }
    });
    if (usable.indexOf(now.type) < 0) { out.push("type " + now.type + " on " + now.grain); }
    return out;
  };

  var runs = [];
  for (var s = 0; s < SEEDS.length; s++) {
    var rand = mulberry(SEEDS[s]);
    var pick = function (list) { return list[Math.floor(rand() * list.length)]; };
    var log = [], problems = [];
    for (var step = 0; step < STEPS; step++) {
      var kind = pick(["click", "click", "drop", "drop", "remove", "grain", "type", "bucket",
                       "since", "filter", "template"]);
      var did = kind;
      if (kind === "click" || kind === "drop") {
        var chips = document.querySelectorAll("#fieldlist li.chip-field");
        var chip = chips[Math.floor(rand() * chips.length)];
        if (!chip) { continue; }
        if (kind === "click") {
          chip.click();
        } else {
          var shelf = pick(["x", "measures", "split"]);
          var dt = new DataTransfer();
          dt.setData("text/plain", chip.dataset.key);
          document.querySelector('.shelf[data-shelf="' + shelf + '"]').dispatchEvent(
            new DragEvent("drop", {bubbles: true, cancelable: true, dataTransfer: dt}));
          did += " on " + shelf;
        }
        did += " " + chip.dataset.key;
      } else if (kind === "remove") {
        var buttons = document.querySelectorAll(".drop li.chip-field button");
        if (!buttons.length) { continue; }
        var button = buttons[Math.floor(rand() * buttons.length)];
        did += " " + button.closest("li").dataset.key;
        button.click();
      } else if (kind === "grain") {
        var grain = pick(cat.grains).key;
        choose($("grain"), grain);
        did += " " + grain;
        var cleared = shelves();
        if (cleared.x || cleared.measures.length || cleared.split) {
          problems.push({step: step, did: did, fault: "shelves not cleared"});
        }
      } else if (kind === "type" || kind === "bucket") {
        var opts = Array.prototype.map.call($(kind).options, function (o) { return o.value; });
        var value = pick(opts);
        choose($(kind), value);
        did += " " + value;
      } else if (kind === "since") {
        $("since").value = pick(["", "2025-01-01", "2026-03-15"]);
        $("since").dispatchEvent(new Event("change", {bubbles: true}));
        did += " " + ($("since").value || "none");
      } else if (kind === "filter") {
        var boxes = document.querySelectorAll("#filters input");
        if (!boxes.length) { continue; }
        var box = boxes[Math.floor(rand() * boxes.length)];
        box.click();
        did += " " + box.id;
      } else {
        var key = pick(Object.keys(templates));
        choose($("template"), key);
        did += " " + key;
        var t = templates[key].spec, got = shelves();
        var want = {grain: t.grain, x: t.x || null, measures: t.measures || [],
                    split: t.split || null, type: t.type, since: null, ticked: []};
        var have = {grain: got.grain, x: got.x, measures: got.measures, split: got.split,
                    type: got.type, since: got.since, ticked: got.ticked};
        if (t.bucket) { want.bucket = t.bucket; have.bucket = got.bucket; }
        if (JSON.stringify(have) !== JSON.stringify(want)) {
          problems.push({step: step, did: did, fault: "template", got: have, want: want});
        }
      }
      log.push(did);
      var wrong = faults(shelves());
      if (wrong.length) { problems.push({step: step, did: did, fault: wrong}); }
      if (rand() < 0.3) { await sleep(250); }        // let some previews land
    }
    await sleep(1500);
    var final = shelves();
    var lastDrawn = window.drawn[window.drawn.length - 1];
    var drawable = Boolean(final.x && final.measures.length);
    var drawnSpec = lastDrawn ? JSON.parse(lastDrawn) : null;
    if (drawable) {
      var view = drawnSpec && {grain: drawnSpec.grain, x: drawnSpec.x, measures: drawnSpec.measures,
                               split: drawnSpec.split || null, type: drawnSpec.type,
                               bucket: drawnSpec.bucket, since: drawnSpec.since || null,
                               ticked: tickedFor(drawnSpec.filters)};
      if (JSON.stringify(view) !== JSON.stringify(final)) {
        problems.push({step: "end", fault: "the preview is of another chart", got: view,
                       want: final});
      }
    } else if (lastDrawn) {
      problems.push({step: "end", fault: "a chart drawn with nothing to draw", got: drawnSpec});
    }
    $("chart-name").value = "Walk " + SEEDS[s];
    window.saved = [];
    $("saveform").dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}));
    await sleep(50);
    var body = window.saved[0];
    var savedView = body && {grain: body.spec.grain, x: body.spec.x || null,
                             measures: body.spec.measures, split: body.spec.split || null,
                             type: body.spec.type, bucket: body.spec.bucket,
                             since: body.spec.since || null, ticked: tickedFor(body.spec.filters)};
    if (JSON.stringify(savedView) !== JSON.stringify(final)) {
      problems.push({step: "save", fault: "saved another chart", got: savedView, want: final});
    }
    runs.push({seed: SEEDS[s], log: log, problems: problems});
  }
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(runs);
  document.body.appendChild(pre);
});
</script>
"""


def _page(client, session_factory) -> str:
    """Two holdings in two classes and two currencies, so the filter shelf
    has something to choose between."""
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        alpha = fac.make_instrument(s, "ALPHA", asset_class="etf")
        zulu = fac.make_instrument(s, "ZULU", asset_class="share", exchange="NASDAQ",
                                   currency="USD")
        fac.add_trade(s, alpha, "2026-01-05", "buy", 10, "5.00")
        fac.add_trade(s, zulu, "2026-01-05", "buy", 10, "5.00", fx_rate="1.50")
        s.commit()
    page = client.get("/charts/build", headers=HTML)
    assert page.status_code == 200
    return page.text


def _run(client, session_factory, tmp_path, seeds, delay="Math.floor(rand() * 600)"):
    target = tmp_path / "builder.html"
    driver = WALK.replace("SEEDS", json.dumps(seeds)).replace("STEPS", "40")
    target.write_text(_standalone(_page(client, session_factory), driver)
                      .replace("<head>", "<head>" + STUBS.replace("DELAY", delay), 1))
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=600000", target.as_uri()],
        capture_output=True, text=True, timeout=240).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the driver did not finish"
    return json.loads(found.group(1).replace("&quot;", '"').replace("&amp;", "&"))


@pytest.mark.parametrize("seeds", [[1, 2, 3], [4, 5, 6]])
def test_the_draft_on_the_shelves_is_the_chart_previewed_and_saved(client, session_factory,
                                                                  tmp_path, seeds):
    runs = _run(client, session_factory, tmp_path, seeds)

    for run in runs:
        assert run["problems"] == [], (run["seed"], run["problems"][:3], run["log"])
    done = {line.split()[0] for run in runs for line in run["log"]}
    assert {"click", "drop", "remove", "grain", "type", "since", "filter", "template"} <= done


# The first preview is slow and the next quick, so the first lands last; and
# then the shelves are emptied while a third is out.
RACE = """
<script>
window.addEventListener("load", async function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  window.portfolioCharts.renderSpec = function (canvas, table, data) {
    window.drawn.push(data.echo);
    return {destroy: function () { window.drawn.push(null); }};
  };
  var click = function (key) {
    document.querySelector('#fieldlist li.chip-field[data-key="' + key + '"]').click();
  };
  click("date"); click("value");                 // preview 1 (slow)
  await sleep(300);
  click("invested");                             // preview 2 (quick)
  await sleep(1500);
  var afterTwo = window.drawn.filter(Boolean).map(function (e) { return JSON.parse(e).measures; });
  click("gain");                                 // preview 3 (slow)...
  await sleep(300);
  document.querySelector("#shelf-x li.chip-field button").click();   // ...then nothing to draw
  await sleep(1500);
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify({afterTwo: afterTwo, last: window.drawn[window.drawn.length - 1],
                                    asked: window.asked.length,
                                    message: document.getElementById("preview-msg").textContent});
  document.body.appendChild(pre);
});
</script>
"""


def test_a_preview_that_arrives_late_does_not_replace_a_newer_one(client, session_factory,
                                                                 tmp_path):
    target = tmp_path / "builder.html"
    target.write_text(_standalone(_page(client, session_factory), RACE).replace(
        "<head>", "<head>" + STUBS.replace("DELAY", "window.asked.length % 2 ? 800 : 10"), 1))
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=60000", target.as_uri()],
        capture_output=True, text=True, timeout=120).stdout
    out = json.loads(re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1)
                     .replace("&quot;", '"').replace("&amp;", "&"))

    assert out["asked"] == 3, "the walk did not ask what it meant to"
    assert out["afterTwo"] == [["value", "invested"]]       # the slow first never drawn
    assert out["last"] is None                               # nothing drawn once undrawable
    assert out["message"] == "Pick an X axis and a measure."


# The two placements the walk never happened on: the date dropped on the split
# shelf of a daily series while the X axis is empty, and a holdings chart's
# split field dropped onto the X axis.
SPLITS = """
<script>
window.addEventListener("load", async function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var $ = function (id) { return document.getElementById(id); };
  var keysOn = function (id) {
    return Array.prototype.map.call(document.querySelectorAll("#" + id + " li.chip-field"),
                                    function (li) { return li.dataset.key; });
  };
  var drop = function (shelf, key) {
    var dt = new DataTransfer();
    dt.setData("text/plain", key);
    document.querySelector('.shelf[data-shelf="' + shelf + '"]').dispatchEvent(
      new DragEvent("drop", {bubbles: true, cancelable: true, dataTransfer: dt}));
  };
  var out = {};
  drop("split", "date");
  out.dateSplit = keysOn("shelf-split");
  $("grain").value = "positions";
  $("grain").dispatchEvent(new Event("change", {bubbles: true}));
  drop("x", "ticker"); drop("split", "currency"); drop("x", "currency");
  out.holdings = {x: keysOn("shelf-x"), split: keysOn("shelf-split")};
  await sleep(50);
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def test_a_split_the_chart_cannot_draw_is_not_placed(client, session_factory, tmp_path):
    target = tmp_path / "builder.html"
    target.write_text(_standalone(_page(client, session_factory), SPLITS).replace(
        "<head>", "<head>" + STUBS.replace("DELAY", "0"), 1))
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=20000", target.as_uri()],
        capture_output=True, text=True, timeout=120).stdout
    out = json.loads(re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1)
                     .replace("&quot;", '"').replace("&amp;", "&"))

    assert out["dateSplit"] == []                       # the date is the X axis
    assert out["holdings"] == {"x": ["currency"], "split": []}
