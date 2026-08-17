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
  }

  function toggleTime() {
    var check = document.getElementById("settime");
    var field = document.getElementById("timefield");
    if (check && field) { field.hidden = !check.checked; }
  }

  /* Ask the server what the ticker is as soon as there is something to look
     up, and fill only the fields still empty — anything typed by hand is
     kept, because overwriting someone's correction is worse than not helping. */
  function autofill(prefix) {
    var ticker = byName(prefix + "ticker");
    var exchange = byName(prefix + "exchange");
    if (!ticker || !ticker.value.trim()) { return; }
    var status = document.getElementById(prefix + "lookup-status");
    if (status) { status.textContent = "looking up…"; }

    var url = "/holdings/lookup?ticker=" + encodeURIComponent(ticker.value) +
              "&exchange=" + encodeURIComponent(exchange ? exchange.value : "ASX");
    fetch(url)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) { if (status) { status.textContent = ""; } return; }
        var fill = function (name, value) {
          var el = byName(prefix + name);
          if (el && !el.value.trim() && value) { el.value = value; }
        };
        fill("name", d.name);
        fill("currency", d.currency);
        fill("yahoo", d.symbol);
        fill("yahoo_symbol", d.symbol);
        if (status) {
          status.textContent = d.found
            ? "found: " + (d.name || d.symbol) + (d.currency ? " (" + d.currency + ")" : "")
            : "no match for " + d.symbol + " — check the ticker and exchange";
        }
      })
      .catch(function () { if (status) { status.textContent = ""; } });
  }

  /* Picking an instrument fills in what the app knows about it: the latest
     close, and — only for a foreign one — the exchange rate. Both come off the
     selected <option>, which the server already populated, so there is no
     round trip.

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

    var price = document.getElementById("unit_price");
    if (price && option.dataset.price) { price.value = option.dataset.price; }

    var currency = option.dataset.currency || "";
    var field = document.getElementById("fxfield");
    var fx = document.getElementById("fx_rate");
    var foreign = currency && currency !== (window.reportingCurrency || "AUD");
    if (field) { field.hidden = !foreign; }
    if (fx) { fx.value = foreign ? (option.dataset.fx || "") : ""; }

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

  /* Anything typed by hand is protected from the clearing above. */
  document.addEventListener("input", function (event) {
    if (event.target.id === "quantity") { event.target.dataset.typed = "1"; }
  });

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target.id === "instpick") { toggleNew(); instrumentChosen(); }
    if (target.id === "settime") { toggleTime(); }
    if (target.dataset && target.dataset.autofill) { autofill(target.dataset.autofill); }
  });

  document.addEventListener("blur", function (event) {
    var target = event.target;
    if (target.dataset && target.dataset.autofill) { autofill(target.dataset.autofill); }
  }, true);   // blur does not bubble; capture is how delegation sees it

  /* Cancel inside a dialog closes it rather than navigating away — the page
     behind is where you already were. */
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-close-dialog]");
    if (!button) { return; }
    var dialog = button.closest("dialog");
    if (dialog) { dialog.close(); }
  });

  /* The initial state, for a form already in the page. Re-run when a dialog
     opens, because the form inside it starts from the server's markup. */
  window.syncTradeForm = function () {
    toggleNew();
    toggleTime();
    /* Remember the prefilled quantity before anything can clear it, so
       returning to that instrument can put it back. */
    var units = document.getElementById("quantity");
    if (units && units.dataset.prefilledFor && !units.dataset.prefilledValue) {
      units.dataset.prefilledValue = units.value;
    }
  };
  window.syncTradeForm();
})();
