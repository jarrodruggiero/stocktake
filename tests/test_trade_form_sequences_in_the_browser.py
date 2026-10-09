"""The Record trade dialog under sequences of edits, in a real browser.

Each of the trade form's bugs came from an ORDER of edits rather than one edit:
the exchange changed after the ticker kept the ASX guess, and an exchange that
found nothing kept the last answer's price. A test that makes one change and
looks sees none of them.

So this makes many. A seeded walk picks instruments, types, changes the
exchange and the date, and cancels and reopens, while the server's answers
arrive late and out of order. After every step each field the form fills must
hold the answer for what is on the form NOW, and a field somebody typed in must
hold what they typed.

The answers are the real endpoints', asked through the app before the browser
starts, with only the provider stubbed, and handed to the page as a table. A
hand-written stub drifts from the server: the one in
test_trade_dialog_in_the_browser.py answers a symbol with an FX rate, which the
endpoint never does.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import factories as fac
from app import clock, pricefeed, providers
from app.models import Portfolio
from test_routes import bind_to_only_portfolio, make_login
from test_trade_dialog_in_the_browser import _standalone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.browser import find_chrome  # noqa: E402

pytestmark = pytest.mark.visual

CHROME = find_chrome()
if CHROME is None:
    pytest.skip("no headless Chrome on this machine", allow_module_level=True)

HTML = {"accept": "text/html"}

# A date before any close, and two with different closes.
D0, D1, D2 = "2026-05-04", "2026-07-01", "2026-07-15"
DATES = [D0, D1, D2]
EXCHANGES = ["ASX", "NASDAQ", "NYSE", "CRYPTO"]
# Lower case too: the field shows upper case, but the value is what was typed.
TICKERS = ["GOOG", "goog", "ALPHA", "BTC", "ACME", "acme", "NOVA", "ZZZ"]
TYPED_SYMBOLS = ["GOOGL", "XYZ.AX", ""]

# What Yahoo knows, by symbol: what a lookup says, and its closes.
YAHOO = {
    "GOOG": ({"longName": "Alphabet Inc.", "currency": "USD", "quoteType": "EQUITY"},
             {D1: "250.10", D2: "251.00"}),
    "ALPHA.AX": ({"longName": "Alpha Fund", "currency": "AUD", "quoteType": "ETF"},
                 {D1: "5.00", D2: "5.10"}),
    "BTC-AUD": ({"longName": "Bitcoin AUD", "currency": "AUD",
                 "quoteType": "CRYPTOCURRENCY"}, {D1: "150000", D2: "152000.5"}),
    "GOOGL": ({}, {D1: "240.00", D2: "241.00"}),
}

# The portfolio's fees: local flat plus a percentage over a minimum, foreign a
# percentage over a higher one. Figures chosen so the minimum and the
# percentage each win somewhere in the walk.
FEES = {"brokerage_flat": "5", "brokerage_percent": "0.1", "brokerage_minimum": "9.50",
        "foreign_brokerage_flat": "0", "foreign_brokerage_percent": "0.25",
        "foreign_brokerage_minimum": "20"}


def _stub_yahoo(monkeypatch, app_module):
    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_info(self):
            return dict(YAHOO.get(self.symbol, ({}, {}))[0])

    def closes(symbol, start, end):
        rows = YAHOO.get(symbol, ({}, {}))[1]
        return [(dt.date.fromisoformat(d), Decimal(c)) for d, c in sorted(rows.items())
                if start <= dt.date.fromisoformat(d) <= end]

    monkeypatch.setattr(pricefeed, "_yf", SimpleNamespace(Ticker=Ticker))
    monkeypatch.setitem(providers.PROVIDERS, "equity", [("yahoo", closes)])
    monkeypatch.setattr(app_module.settings.price_feed, "enabled", True)


def _setup(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        portfolio = bind_to_only_portfolio(s)
        for name, value in FEES.items():
            setattr(portfolio, name, Decimal(value))
        acme = fac.make_instrument(s, "ACME", name="Acme")
        nova = fac.make_instrument(s, "NOVA", exchange="NASDAQ", currency="USD",
                                   asset_class="share", name="Nova")
        fac.add_prices(s, acme, [(D1, "1.20"), (D2, "1.30")])
        fac.add_prices(s, nova, [(D1, "10.00"), (D2, "11.00")])
        fac.add_fx_series(s, "USDAUD", [(D1, "1.50"), (D2, "1.55")])
        fac.hold(s, acme)
        fac.hold(s, nova)
        s.commit()
        return {"ACME": (acme.id, "AUD"), "NOVA": (nova.id, "USD")}


def _answers(client, listed) -> dict:
    """Every URL the form can ask, answered by the app itself."""
    urls = [f"/holdings/lookup?ticker={t}&exchange={x}" for t in TICKERS for x in EXCHANGES]
    symbols = {pricefeed.yahoo_symbol_for(t, x) for t in TICKERS for x in EXCHANGES}
    symbols |= {t for t in TICKERS} | {s for s in TYPED_SYMBOLS if s}
    # The form opens on today, and every reopen goes back to it.
    dates = DATES + [clock.today().isoformat()]
    urls += [f"/holdings/price?symbol={s}&date={d}" for s in sorted(symbols) for d in dates]
    urls += [f"/holdings/price?instrument={i}&date={d}" for i, _ in listed.values()
             for d in dates]
    table = {}
    for url in urls:
        resp = client.get(url)
        assert resp.status_code == 200, (url, resp.text)
        table[url] = resp.json()
    return table


STUB = """
<script>
window.ANSWERS = %(answers)s;
window.pending = 0;
window.unanswered = [];
window.errors = [];
window.addEventListener("error", function (e) { window.errors.push(String(e.message)); });
window.random = Math.random;      // replaced by the walk's own generator
window.fetch = function (url) {
  url = String(url);
  window.pending++;
  var delay = (window.DELAYS || {})[url];
  if (delay === undefined) { delay = Math.floor(window.random() * 60); }
  return new Promise(function (resolve) {
    setTimeout(function () {
      window.pending--;
      var body = window.ANSWERS[url];
      if (body === undefined) { window.unanswered.push(url); }
      resolve({ok: body !== undefined,
               json: function () { return Promise.resolve(body); }});
    }, delay);
  });
};
</script>
"""

WALK = """
<script>
window.addEventListener("load", async function () {
  var SEEDS = %(seeds)s, STEPS = %(steps)s;
  var TICKERS = %(tickers)s, EXCHANGES = %(exchanges)s, DATES = %(dates)s;
  var SYMBOLS = %(symbols)s;
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var settle = async function () {
    do { await sleep(5); } while (window.pending > 0);
    await sleep(5);
  };
  var mulberry = function (a) {
    return function () {
      a |= 0; a = a + 0x6D2B79F5 | 0;
      var t = Math.imul(a ^ a >>> 15, 1 | a);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  };
  var dialog = document.getElementById("tradedialog");
  var form = dialog.querySelector("form");
  var field = function (name) { return form.querySelector('[name="' + name + '"]'); };
  var byId = function (id) { return document.getElementById(id); };
  var pick = byId("instpick");
  var fire = function (el, kind) { el.dispatchEvent(new Event(kind, {bubbles: true})); };

  var state = function () {
    var cls = byId("new_class"), shown = byId("new_class-shown");
    return {
      picked: pick.value, newHidden: byId("newinst").hidden,
      ticker: field("new_ticker").value, exchange: field("new_exchange").value,
      name: field("new_name").value, yahoo: field("new_yahoo").value,
      currency: field("new_currency").value,
      cls: cls.value, clsPicker: !cls.hidden, clsShown: shown.hidden ? null : shown.textContent,
      price: byId("unit_price").value, fx: byId("fx_rate").value,
      fxHidden: byId("fxfield").hidden, quantity: byId("quantity").value,
      brokerage: byId("brokerage").value, date: byId("tradedate").value
    };
  };

  var SCRIPTS = %(scripts)s;
  var runs = [];
  var count = SCRIPTS ? SCRIPTS.length : SEEDS.length;
  for (var s = 0; s < count; s++) {
    var rand = mulberry(SCRIPTS ? s + 1 : SEEDS[s]);
    window.random = rand;
    var script = SCRIPTS ? SCRIPTS[s] : null;
    var one = function (list) { return list[Math.floor(rand() * list.length)]; };
    var log = [];
    var open = function () { byId("opentrade").click(); };
    open(); await settle();
    var steps = script ? script.length : STEPS;
    for (var step = 0; step < steps; step++) {
      var isNew = pick.value === "new";
      var actions = ["pick", "pick", "date", "type:quantity", "type:unit_price",
                     "type:brokerage", "reopen"];
      if (isNew) {
        actions = actions.concat(["ticker", "ticker", "ticker", "exchange", "exchange",
                                  "exchange", "type:new_name", "type:new_yahoo",
                                  "type:new_currency"]);
      }
      if (!byId("fxfield").hidden) { actions.push("type:fx_rate"); }
      var action = script ? script[step][0] : one(actions);
      var value = script ? script[step][1] : null;
      var given = function (list) { return script ? value : one(list); };
      if (action === "pick") {
        var values = Array.prototype.map.call(pick.options, function (o) { return o.value; });
        value = given(values);
        pick.value = value; fire(pick, "change");
      } else if (action === "ticker") {
        value = given(TICKERS);
        field("new_ticker").value = value; fire(field("new_ticker"), "change");
      } else if (action === "exchange") {
        value = given(EXCHANGES);
        field("new_exchange").value = value; fire(field("new_exchange"), "change");
      } else if (action === "date") {
        value = given(DATES.concat([DATES[1], DATES[2]]));
        byId("tradedate").value = value; fire(byId("tradedate"), "change");
      } else if (action === "reopen") {
        dialog.close(); await sleep(5); open();
      } else {
        var name = action.slice(5);
        var choices = {"quantity": ["10", "3", "1234.5", ""], "unit_price": ["7.50", "0", ""],
                       "brokerage": ["12.00", ""], "fx_rate": ["0.70", ""],
                       "new_name": ["My own name", ""], "new_yahoo": SYMBOLS,
                       "new_currency": ["USD", "AUD", "EUR"]}[name];
        value = given(choices);
        var el = field(name) || byId(name);
        el.value = value; fire(el, "input"); fire(el, "change");
      }
      // Now and then the next step comes before this one's answers do.
      var entry = {step: step, action: action, value: value};
      var wait = script ? script[step][2] !== false : rand() < %(settle)s;
      if (wait) { await settle(); entry.state = state(); }
      log.push(entry);
    }
    await settle();
    log.push({step: steps, action: "settled", value: null, state: state()});
    runs.push({seed: SCRIPTS ? "script " + (s + 1) : SEEDS[s], log: log});
  }
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify({runs: runs, unanswered: window.unanswered,
                                    errors: window.errors});
  document.body.appendChild(pre);
});
</script>
"""

CLASS_LABELS = {"etf": "ETF", "share": "Share", "crypto": "Crypto"}


def _fee(fees: dict, currency: str, quantity: str, price: str) -> str:
    """What brokerage.py charges, as the form should suggest it."""
    foreign = bool(currency) and currency != "AUD"
    prefix = "foreign_" if foreign else ""
    flat, percent, minimum = (Decimal(fees[f"{prefix}brokerage_{p}"])
                              for p in ("flat", "percent", "minimum"))

    def number(text):
        try:
            return Decimal(text) if text.strip() else Decimal(0)
        except ArithmeticError:
            return Decimal(0)

    value = number(quantity) * number(price)
    due = max(minimum, flat + percent * value / 100)
    return str(due.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _typed_after(log: list, upto: int, listed: dict) -> tuple[dict, set]:
    """What somebody has typed and the form still holds, replayed in order.

    Reopening starts clean. Picking an instrument with no FX, including by
    typing a listed ticker as new, clears a typed rate: there is nothing to
    convert, and the field is hidden. A name or symbol emptied by hand stays
    empty until the next lookup fills it: a blank symbol is the server's to
    guess. Emptied while a lookup is still out, it may be either — that is the
    second thing returned.
    """
    typed: dict = {}
    loose: set = set()
    in_flight = False
    picked, ticker, exchange = "", "", "ASX"
    no_fx = {str(i) for i, ccy in listed.values() if ccy == "AUD"} | {"", "new"}
    for entry in log[:upto + 1]:
        action, value = entry["action"], entry["value"]
        if action == "reopen":
            typed, picked, ticker, exchange = {}, "", "", "ASX"
            loose = set()
        elif action.startswith("type:"):
            typed[action[5:]] = value
            if in_flight and action[5:] in ("new_name", "new_yahoo") and value == "":
                loose.add(action[5:])
        elif action == "pick":
            picked = value
        elif action == "ticker":
            ticker = value
        elif action == "exchange":
            exchange = value
        if action in ("ticker", "exchange") and picked == "new":
            for name, (i, _ccy) in listed.items():
                listed_on = "NASDAQ" if name == "NOVA" else "ASX"
                if ticker.strip().upper() == name and exchange == listed_on:
                    picked = str(i)
        if action in ("pick", "ticker", "exchange") and picked in no_fx:
            typed.pop("fx_rate", None)
        if action in ("ticker", "exchange") and picked == "new" and ticker.strip():
            for name in ("new_name", "new_yahoo"):
                if typed.get(name) == "":
                    del typed[name]
            loose -= {"new_name", "new_yahoo"}
            in_flight = True
        if "state" in entry:
            in_flight = False
    return typed, loose


def _expected(state: dict, typed: dict, answers: dict, listed: dict) -> dict:
    """What each filled field should hold, for what is on the form now.

    Only the fields the app fills, and only where the form says what it is for:
    with nothing picked, or a new instrument with no ticker, there is nothing
    to answer and a field may still hold the last answer.
    """
    s = state
    want: dict = {}
    by_id = {str(i): (ticker, ccy) for ticker, (i, ccy) in listed.items()}
    listed_keys = {(t, "NASDAQ" if t == "NOVA" else "ASX") for t in listed}
    if s["picked"] in by_id:
        ticker, ccy = by_id[s["picked"]]
        answer = answers[f"/holdings/price?instrument={s['picked']}&date={s['date']}"]
        want.update(newHidden=True, fxHidden=ccy == "AUD", quantity="",
                    price=answer["price"] or "",
                    fx=(answer["fx"] or "") if ccy != "AUD" else "")
        want["brokerage"] = _fee(FEES, ccy, typed.get("quantity", ""),
                                 typed.get("unit_price", want["price"]))
    elif (s["picked"] == "new" and s["ticker"].strip()
          and (s["ticker"].strip().upper(), s["exchange"]) not in listed_keys):
        found = answers[f"/holdings/lookup?ticker={s['ticker']}&exchange={s['exchange']}"]
        want.update(newHidden=False, fxHidden=True, quantity="",
                    name=found["name"] or "", yahoo=found["symbol"],
                    currency=found["currency"] or "AUD")
        if found["asset_class"]:
            want.update(cls=found["asset_class"], clsPicker=False,
                        clsShown=CLASS_LABELS[found["asset_class"]])
        else:
            want.update(cls="", clsPicker=True, clsShown=None)
        symbol = s["yahoo"].strip() or s["ticker"].strip()
        answer = answers[f"/holdings/price?symbol={symbol}&date={s['date']}"]
        want.update(price=answer["price"] or "", fx="")
        currency = typed.get("new_currency", want["currency"]).strip().upper()
        want["brokerage"] = _fee(FEES, currency, typed.get("quantity", ""),
                                 typed.get("unit_price", want["price"]))
    elif s["picked"] == "":
        want.update(newHidden=True, fxHidden=True, quantity="")
    # What was typed wins over every answer above.
    for name, value in typed.items():
        key = {"new_name": "name", "new_yahoo": "yahoo", "new_currency": "currency",
               "unit_price": "price", "fx_rate": "fx"}.get(name, name)
        want[key] = value
    return want


def _walk(client, session_factory, tmp_path, monkeypatch, app_module, seeds=(), steps=40,
          settle=0.7, scripts=None):
    listed = _setup(client, session_factory)
    _stub_yahoo(monkeypatch, app_module)
    answers = _answers(client, listed)
    page = client.get("/", headers=HTML)
    assert page.status_code == 200
    symbols = TYPED_SYMBOLS
    if scripts:
        scripts = [[[a, v.replace("{nova}", str(listed["NOVA"][0])).replace(
            "{acme}", str(listed["ACME"][0])) if isinstance(v, str) else v, *rest]
            for a, v, *rest in script] for script in scripts]
    driver = WALK % {"seeds": json.dumps(list(seeds)), "steps": steps, "settle": settle,
                     "scripts": json.dumps(scripts),
                     "tickers": json.dumps(TICKERS), "exchanges": json.dumps(EXCHANGES),
                     "dates": json.dumps(DATES), "symbols": json.dumps(symbols)}
    head = STUB % {"answers": json.dumps(answers)}
    target = tmp_path / "dashboard.html"
    target.write_text(_standalone(page.text, driver).replace("<head>", "<head>" + head, 1))
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=600000", target.as_uri()],
        capture_output=True, text=True, timeout=300).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the walk did not finish"
    out = json.loads(found.group(1).replace("&quot;", '"').replace("&amp;", "&")
                     .replace("&lt;", "<").replace("&gt;", ">"))
    assert out["errors"] == [], out["errors"]
    assert out["unanswered"] == [], "the page asked for something the table does not hold"
    return out["runs"], answers, listed


def _check(runs, answers, listed):
    for run in runs:
        for i, entry in enumerate(run["log"]):
            if "state" not in entry:
                continue
            typed, loose = _typed_after(run["log"], i, listed)
            want = _expected(entry["state"], typed, answers, listed)
            for name in loose:
                want.pop({"new_name": "name", "new_yahoo": "yahoo"}[name], None)
                typed.pop(name, None)
            got = {k: entry["state"][k] for k in want}
            if got != want:
                steps = "\n".join(f"  {e['step']}: {e['action']} {e['value']!r}"
                                  + ("" if "state" in e else "  (not waited for)")
                                  for e in run["log"][:i + 1])
                wrong = {k: (got[k], want[k]) for k in want if got[k] != want[k]}
                pytest.fail(f"seed {run['seed']}, after:\n{steps}\n"
                            f"(field: (holds, should hold)) {wrong}\nstate: {entry['state']}")


@pytest.mark.parametrize("seeds", [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12]])
def test_every_filled_field_answers_for_what_is_on_the_form(
        client, session_factory, tmp_path, monkeypatch, app_module, seeds):
    runs, answers, listed = _walk(client, session_factory, tmp_path, monkeypatch,
                                  app_module, seeds)
    _check(runs, answers, listed)


def test_the_walk_reaches_the_states_it_is_about(
        client, session_factory, tmp_path, monkeypatch, app_module):
    """The checks above prove nothing about a state the walk never reaches."""
    runs, answers, _listed = _walk(client, session_factory, tmp_path, monkeypatch,
                                   app_module, [1, 2, 3, 4])
    states = [e["state"] for run in runs for e in run["log"] if "state" in e]
    actions = {e["action"] for run in runs for e in run["log"]}
    assert {"pick", "ticker", "exchange", "date", "reopen", "type:new_yahoo",
            "type:new_currency"} <= actions
    found = [s for s in states if s["picked"] == "new" and s["clsShown"]]
    missed = [s for s in states if s["picked"] == "new" and s["yahoo"] == "GOOG.AX"]
    priced = [s for s in states if s["picked"] == "new" and s["price"]]
    foreign = [s for s in states if s["picked"] not in ("", "new") and not s["fxHidden"]]
    assert found and missed and priced and foreign
    with_fee = {s["brokerage"] for s in states}
    assert "9.50" in with_fee and "20.00" in with_fee, with_fee
    assert any(v["price"] for k, v in answers.items() if "symbol=" in k)


def test_the_answers_are_the_apps_own(client, session_factory, monkeypatch, app_module):
    """The table is asked of the real endpoints, so a symbol has no FX rate
    and a lookup that finds nothing says so with no name or currency."""
    listed = _setup(client, session_factory)
    _stub_yahoo(monkeypatch, app_module)
    answers = _answers(client, listed)
    assert answers[f"/holdings/price?symbol=GOOG&date={D1}"]["price"] == "250.1"
    assert answers[f"/holdings/price?symbol=GOOG&date={D1}"]["fx"] is None
    missed = answers["/holdings/lookup?ticker=GOOG&exchange=ASX"]
    assert (missed["found"], missed["symbol"], missed["currency"]) == (False, "GOOG.AX", None)
    nova = answers[f"/holdings/price?instrument={listed['NOVA'][0]}&date={D2}"]
    assert (nova["price"], nova["fx"]) == ("11", "1.55")
    with session_factory() as s:
        assert s.scalar(select(Portfolio)).brokerage_minimum == Decimal("9.50")


RACE = """
<script>
window.DELAYS = {"/holdings/lookup?ticker=GOOG&exchange=ASX": 200,
                 "/holdings/lookup?ticker=GOOG&exchange=NASDAQ": 5,
                 "/holdings/price?instrument=%(nova)s&date=%(d1)s": 200,
                 "/holdings/price?instrument=%(nova)s&date=%(d2)s": 5};
window.addEventListener("load", async function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var form = document.getElementById("tradedialog").querySelector("form");
  var field = function (name) { return form.querySelector('[name="' + name + '"]'); };
  var fire = function (el) { el.dispatchEvent(new Event("change", {bubbles: true})); };
  var pick = document.getElementById("instpick");
  document.getElementById("opentrade").click(); await sleep(20);
  pick.value = "new"; fire(pick); await sleep(20);
  field("new_ticker").value = "GOOG"; fire(field("new_ticker"));
  field("new_exchange").value = "NASDAQ"; fire(field("new_exchange"));
  await sleep(400);
  var out = {symbol: field("new_yahoo").value, name: field("new_name").value,
             currency: field("new_currency").value};
  pick.value = "%(nova)s"; fire(pick); await sleep(20);
  var date = document.getElementById("tradedate");
  date.value = "%(d1)s"; fire(date);
  date.value = "%(d2)s"; fire(date);
  await sleep(400);
  out.price = document.getElementById("unit_price").value;
  out.fx = document.getElementById("fx_rate").value;
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def test_an_answer_that_arrives_late_does_not_overwrite_a_newer_one(
        client, session_factory, tmp_path, monkeypatch, app_module):
    """The ticker is looked up on the ASX, then on NASDAQ as the exchange
    changes, and the ASX answer comes back last. Answering the question the
    form no longer asks put back GOOG.AX, the bug the exchange fix was for."""
    listed = _setup(client, session_factory)
    _stub_yahoo(monkeypatch, app_module)
    answers = _answers(client, listed)
    page = client.get("/", headers=HTML).text
    driver = RACE % {"nova": listed["NOVA"][0], "d1": D1, "d2": D2}
    head = STUB % {"answers": json.dumps(answers)}
    target = tmp_path / "dashboard.html"
    target.write_text(_standalone(page, driver).replace("<head>", "<head>" + head, 1))
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--window-size=1400,1000",
         "--virtual-time-budget=20000", target.as_uri()],
        capture_output=True, text=True, timeout=120).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the driver did not finish"
    out = json.loads(found.group(1).replace("&quot;", '"'))
    assert out == {"symbol": "GOOG", "name": "Alphabet Inc.", "currency": "USD",
                   "price": "11", "fx": "1.55"}


# Each fault the walk found, as the shortest sequence that shows it, checked
# by the same model. [action, value, wait for the answers first (default yes)]
SCRIPTS = {
    "a symbol typed by hand is the one priced": [
        ["pick", "new"], ["date", D1], ["ticker", "ZZZ"], ["type:new_yahoo", "GOOGL"],
        ["exchange", "NYSE"]],
    "nothing found keeps nothing from the last answer": [
        ["pick", "new"], ["date", D1], ["exchange", "NASDAQ"], ["ticker", "GOOG"],
        ["exchange", "ASX"]],
    "the newest answer wins": [
        ["pick", "new"], ["date", D2], ["ticker", "GOOG", False], ["exchange", "NASDAQ"]],
    "a typed rate cleared by an AUD instrument does not stay empty": [
        ["date", D2], ["pick", "{nova}"], ["type:fx_rate", "0.70"], ["pick", "{acme}"],
        ["pick", "{nova}"]],
    "reopening hides the FX field": [["pick", "{nova}"], ["reopen", None]],
}


@pytest.mark.parametrize("name", SCRIPTS)
def test_each_fault_the_walk_found(client, session_factory, tmp_path, monkeypatch,
                                   app_module, name):
    runs, answers, listed = _walk(client, session_factory, tmp_path, monkeypatch,
                                  app_module, scripts=[SCRIPTS[name]])
    _check(runs, answers, listed)
