/* The trade form's behaviour, for both places it appears: the /trade/new page
   and the Record trade dialog on the Portfolio page.

   Lifted out of `trade_new.html` when the form became a shared include. Two
   reasons, and the second is the one that matters: the form must behave
   identically wherever it is shown, and inline handlers were three of the ~7
   inline scripts holding `'unsafe-inline'` in the CSP.

   Everything here is delegated from `document`, so it works on a form that was
   in the page at load AND on one inside a dialog — there is no setup step to
   forget when the form appears somewhere new. */
(function () {
  "use strict";

  function byName(name, root) {
    return (root || document).querySelector('[name="' + name + '"]');
  }

  /* "+ Something not listed…" reveals the new-instrument fieldset. `required`
     is toggled with it: a required field inside a hidden fieldset blocks
     submit with a validation message pointing at something invisible. */
  function toggleNew() {
    var picker = document.getElementById("instpick");
    if (!picker) { return; }              // editing: the instrument is fixed
    var box = document.getElementById("newinst");
    if (!box) { return; }
    var picking = picker.value === "new";
    box.hidden = !picking;
    var ticker = box.querySelector('[name="new_ticker"]');
    if (ticker) { ticker.required = picking; }
    var cls = document.getElementById("new_class");
    if (cls) { cls.required = picking; }
  }

  /* The class the provider gave, shown as the value alone. The picker is for
     when it has no answer, and starts on no choice rather than a guess. */
  function showClass(cls) {
    var select = document.getElementById("new_class");
    var shown = document.getElementById("new_class-shown");
    if (!select || !shown) { return; }
    var known = cls ? select.querySelector('option[value="' + cls + '"]') : null;
    if (known) {
      select.value = cls;
      shown.textContent = known.textContent;
    } else if (shown.textContent) {
      select.value = "";                   // the last lookup's answer, not this one's
      shown.textContent = "";
    }
    select.hidden = !!known;
    shown.hidden = !known;
  }

  /* A ticker already listed, typed as new, switches the form to it: the
     instrument exists, and its own details are the ones that count. */
  function switchToListed() {
    var picker = document.getElementById("instpick");
    var ticker = byName("new_ticker");
    var exchange = byName("new_exchange");
    if (!picker || !ticker || picker.value !== "new") { return false; }
    var wanted = ticker.value.trim().toUpperCase();
    var where = exchange ? exchange.value : "ASX";
    for (var i = 0; i < picker.options.length; i++) {
      var option = picker.options[i];
      if (option.dataset.ticker === wanted && option.dataset.exchange === where) {
        picker.value = option.value;
        toggleNew();
        instrumentChosen();
        priceForDate();
        suggestBrokerage();
        return true;
      }
    }
    return false;
  }

  function toggleTime() {
    var check = document.getElementById("settime");
    var field = document.getElementById("timefield");
    if (check && field) { field.hidden = !check.checked; }
  }

  /* Ask the server what the ticker is as soon as there is something to look
     up. Anything typed by hand is kept, because overwriting someone's
     correction is worse than not helping. What the lookup itself filled in is
     replaced by its next answer: the exchange often changes after the ticker,
     and the first answer, the ASX guess, is then wrong. */
  /* Only the newest question's answer is used. The exchange is usually
     changed straight after the ticker, so two lookups are in flight, and the
     first can come back last: answering a question nobody is asking any more
     put GOOG.AX back on a NASDAQ stock. */
  var asked = {};
  function ask(kind) {
    asked[kind] = (asked[kind] || 0) + 1;
    var mine = asked[kind];
    return function () { return asked[kind] === mine; };
  }

  function autofill(prefix) {
    var ticker = byName(prefix + "ticker");
    var exchange = byName(prefix + "exchange");
    if (!ticker || !ticker.value.trim()) { return; }
    var status = document.getElementById(prefix + "lookup-status");
    if (status) { status.textContent = "looking up…"; }

    var url = "/holdings/lookup?ticker=" + encodeURIComponent(ticker.value) +
              "&exchange=" + encodeURIComponent(exchange ? exchange.value : "ASX");
    var newest = ask("lookup" + prefix);
    fetch(url)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!newest()) { return; }
        if (!d) { if (status) { status.textContent = ""; } return; }
        var fill = function (name, value) {
          var el = byName(prefix + name);
          if (!el || (el.value.trim() && el.value !== el.dataset.filled)) { return; }
          el.value = value || "";
          el.dataset.filled = el.value;
        };
        fill("name", d.name);
        /* Not `fill`: the field starts at AUD, so it was never empty and a USD
           stock stayed AUD. Replaced unless somebody typed it, and back to AUD
           when the answer has none. */
        var currency = byName(prefix + "currency");
        if (currency && !currency.dataset.typed) {
          currency.value = d.currency || currency.defaultValue;
        }
        fill("yahoo", d.symbol);
        fill("yahoo_symbol", d.symbol);
        showClass(d.asset_class);
        if (status) {
          status.textContent = d.found
            ? "found: " + (d.name || d.symbol) + (d.currency ? " (" + d.currency + ")" : "")
            : "no match for " + d.symbol + " — check the ticker and exchange";
        }
        /* Now that the symbol is known, the price for the date can be. Here
           rather than beside the ticker's own handler because the symbol
           arrives with this response — asking any earlier has nothing to ask
           about. Asked when nothing was found too: the last answer's price
           has to go, and a symbol typed by hand may still have one. */
        priceForDate();
      })
      .catch(function () { if (status) { status.textContent = ""; } });
  }

  /* What the price and FX fields should hold for the DATE on the form, asked
     of the server whenever that date moves. Empty answers are left empty
     rather than filled with the nearest figure — decisions.md #124.

     Anything TYPED is never overwritten, the same rule `autofill` follows for
     the instrument fields and `instrumentChosen` follows for units. */
  function priceForDate() {
    var form = document.querySelector("form[data-instrument]");
    var date = document.getElementById("tradedate");
    var picker = document.getElementById("instpick");
    var id = picker ? picker.value : (form ? form.dataset.instrument : "");
    if (!date || !date.value) { return; }

    /* "Something not listed" has no id to ask about, so ask by SYMBOL — the
       one the lookup just filled in, or the ticker for the server to guess
       from. Without this the first trade for anything new got an empty price
       field, which is the trade most likely to be typed off a contract note
       and the one nobody can check against a holding page yet. */
    var query;
    if (id && id !== "new") {
      query = "instrument=" + encodeURIComponent(id);
    } else if (id === "new") {
      var symbol = byName("new_yahoo");
      var ticker = byName("new_ticker");
      var which = (symbol && symbol.value.trim()) ||
                  (ticker && ticker.value.trim());
      if (!which) { return; }
      query = "symbol=" + encodeURIComponent(which);
    } else {
      return;                              // nothing chosen yet
    }

    var price = document.getElementById("unit_price");
    var fx = document.getElementById("fx_rate");
    var url = "/holdings/price?" + query +
              "&date=" + encodeURIComponent(date.value);
    var newest = ask("price");
    fetch(url)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !newest()) { return; }
        if (price && !price.dataset.typed) { price.value = d.price || ""; }
        if (fx && !fx.dataset.typed) { fx.value = d.fx || ""; }
        suggestBrokerage();
      })
      .catch(function () { /* leave whatever is in the fields */ });
  }

  /* The brokerage the portfolio's fee gives for what is on the form: a flat
     fee plus a percentage of the value, or the minimum if that is more — the
     sum app/brokerage.py does. Trades in another currency have their own fee.
     Without a fee, the last trade of that kind. Never over a typed figure. */
  function suggestBrokerage() {
    var field = document.getElementById("brokerage");
    if (!field || !field.dataset.fees || field.dataset.typed) { return; }
    var fees = JSON.parse(field.dataset.fees);
    var picker = document.getElementById("instpick");
    var currency = "";
    if (picker && picker.value === "new") {
      var typed = byName("new_currency");
      currency = typed ? typed.value.trim().toUpperCase() : "";
    } else if (picker && picker.selectedIndex >= 0) {
      currency = picker.options[picker.selectedIndex].dataset.currency || "";
    }
    var foreign = currency && currency !== (window.reportingCurrency || "AUD");
    var fee = foreign ? fees.foreign : fees.local;
    if (!fee) {
      field.value = (foreign ? fees.last.foreign : fees.last.local) || "";
      return;
    }
    var units = parseFloat((document.getElementById("quantity") || {}).value) || 0;
    var price = parseFloat((document.getElementById("unit_price") || {}).value) || 0;
    var charged = (parseFloat(fee.flat) || 0) + (parseFloat(fee.percent) || 0) * units * price / 100;
    var due = Math.max(parseFloat(fee.minimum) || 0, charged);
    field.value = (Math.round((due + Number.EPSILON) * 100) / 100).toFixed(2);
  }

  /* Picking an instrument decides whether the FX field applies at all, off the
     selected <option>'s currency. The VALUES come from `priceForDate`, which
     is the only thing that fills them — the option used to carry a price and a
     rate of its own, and that is where the wrong-cost-base bug lived
     (decisions.md #124).

     **Units are cleared when the instrument changes**, because a quantity
     worked out for one holding is meaningless against another. The exception
     is a quantity somebody TYPED: `data-prefilled-for` records which
     instrument the app filled it in for, so returning to that instrument
     restores it and a hand-typed value is never thrown away. */
  function instrumentChosen() {
    var picker = document.getElementById("instpick");
    if (!picker) { return; }
    var option = picker.options[picker.selectedIndex];
    if (!option) { return; }

    /* Cleared, not filled: an AUD instrument has nothing to convert, and for a
       foreign one `priceForDate` supplies the rate for the trade's own date. */
    var fx = document.getElementById("fx_rate");
    if (fx && !showFx()) {
      fx.value = "";
      delete fx.dataset.typed;
    }

    var units = document.getElementById("quantity");
    if (units) {
      var filledFor = units.dataset.prefilledFor || "";
      if (filledFor && filledFor === option.value) {
        units.value = units.dataset.prefilledValue || units.value;
      } else if (!units.dataset.typed) {
        units.value = "";
      }
    }
  }

  /* The FX field, for an instrument in another currency: the one picked, or on
     an edit the trade's own. Returns whether it applies. */
  function showFx() {
    var picker = document.getElementById("instpick");
    var form = document.querySelector("form[data-instrument]");
    var option = picker ? picker.options[picker.selectedIndex] : null;
    var currency = picker ? (option && option.dataset.currency) || ""
                          : (form && form.dataset.currency) || "";
    var foreign = !!currency && currency !== (window.reportingCurrency || "AUD");
    var field = document.getElementById("fxfield");
    if (field) { field.hidden = !foreign; }
    return foreign;
  }

  /* Anything typed by hand is protected from the clearing above, and from the
     date-driven refill. Price and FX join units here for that reason: the form
     re-asks the server whenever the date moves, and someone who typed a figure
     off their contract note should not watch it be replaced by a close. */
  document.addEventListener("input", function (event) {
    var id = event.target.id;
    if (id === "quantity" || id === "unit_price" || id === "fx_rate" ||
        id === "brokerage" || event.target.name === "new_currency") {
      event.target.dataset.typed = "1";
    }
    if (id === "quantity" || id === "unit_price" || event.target.name === "new_currency") {
      suggestBrokerage();
    }
  });

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target.id === "instpick") {
      toggleNew(); instrumentChosen(); priceForDate(); suggestBrokerage();
    }
    if (target.id === "tradedate" || target.name === "new_yahoo") { priceForDate(); }
    if (target.id === "settime") { toggleTime(); }
    if (target.dataset && target.dataset.autofill && !switchToListed()) {
      autofill(target.dataset.autofill);
    }
  });

  document.addEventListener("blur", function (event) {
    var target = event.target;
    if (target.dataset && target.dataset.autofill && !switchToListed()) {
      autofill(target.dataset.autofill);
    }
  }, true);   // blur does not bubble; capture is how delegation sees it

  /* Cancel inside a dialog closes it rather than navigating away — the page
     behind is where you already were. */
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-close-dialog]");
    if (!button) { return; }
    var dialog = button.closest("dialog");
    if (dialog) { dialog.close(); }
  });

  /* Pressed once, Save says so and a second press goes nowhere: a save can
     wait on the price provider, and every press used to record a trade. The
     server ignores a repeat too (app/submissions.py); this is the visible half. */
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.matches || !form.matches("form[data-instrument]")) { return; }
    if (form.dataset.saving) { event.preventDefault(); return; }
    form.dataset.saving = "1";
    var save = event.submitter || form.querySelector('.commitbar button[type="submit"]');
    if (save) { save.setAttribute("aria-busy", "true"); }
  });

  function notSaving(form) {
    delete form.dataset.saving;
    form.querySelectorAll('[aria-busy="true"]').forEach(function (button) {
      button.removeAttribute("aria-busy");
    });
  }

  /* Back from the next page, a browser may restore this one as it was left. */
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-instrument]").forEach(notSaving);
  });

  /* Every Record trade starts clean, however the dialog was left last time —
     Cancel, Escape, a click outside. Done as it opens rather than as it
     closes: `close` is queued, and opening is the moment that matters.
     `reset()` puts the markup's values back; the marks this script left
     (what was typed, what the lookup said) go by hand. */
  window.resetTradeForm = function (form) {
    form.reset();
    notSaving(form);
    ["quantity", "unit_price", "fx_rate", "brokerage"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { delete el.dataset.typed; }
    });
    var currency = byName("new_currency", form);
    if (currency) { delete currency.dataset.typed; }
    var status = document.getElementById("new_lookup-status");
    if (status) { status.textContent = ""; }
    var cls = document.getElementById("new_class");
    var shown = document.getElementById("new_class-shown");
    if (cls) { cls.hidden = false; }
    if (shown) { shown.hidden = true; shown.textContent = ""; }
  };

  /* The initial state, for a form already in the page. Re-run when a dialog
     opens, because the form inside it starts from the server's markup. */
  window.syncTradeForm = function () {
    toggleNew();
    toggleTime();
    showFx();
    /* Remember the prefilled quantity before anything can clear it, so
       returning to that instrument can put it back. */
    var units = document.getElementById("quantity");
    if (units && units.dataset.prefilledFor && !units.dataset.prefilledValue) {
      units.dataset.prefilledValue = units.value;
    }
    /* On an EDIT, the price and rate in the fields are recorded values — what
       this trade actually happened at. Treat them as typed, so moving the date
       does not replace somebody's recorded figures with a close. A new trade
       has nothing to protect and gets the date-driven fill. */
    var form = document.querySelector("form[data-instrument]");
    if (form && form.dataset.instrument) {
      ["unit_price", "fx_rate"].forEach(function (id) {
        var el = document.getElementById(id);
        if (el && el.value.trim()) { el.dataset.typed = "1"; }
      });
    }
    /* Reached from a holding's ledger (`/trade/new?ticker=ACME`) the instrument
       is already chosen server-side, so no change event ever fires and the
       price would sit empty. Safe to call unconditionally: it skips typed
       fields, which is every field that matters on an edit. */
    priceForDate();
  };
  window.syncTradeForm();
})();
