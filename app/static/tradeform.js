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
        /* Now that the symbol is known, the price for the date can be. Here
           rather than beside the ticker's own handler because the symbol
           arrives with this response — asking any earlier has nothing to ask
           about. */
        if (d.found) { priceForDate(); }
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
    var status = document.getElementById("price-status");
    var url = "/holdings/price?" + query +
              "&date=" + encodeURIComponent(date.value);
    fetch(url)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) { return; }
        if (price && !price.dataset.typed) { price.value = d.price || ""; }
        if (fx && !fx.dataset.typed) { fx.value = d.fx || ""; }
        if (!status) { return; }
        if (!d.price) {
          status.textContent = "no close stored for that date — enter it yourself";
        } else if (d.as_at !== date.value) {
          /* Say which day it came from rather than implying the market traded
             on a Saturday. */
          status.textContent = "the " + d.as_at + " close";
        } else {
          status.textContent = "";
        }
      })
      .catch(function () { /* leave whatever is in the fields */ });
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

    var currency = option.dataset.currency || "";
    var field = document.getElementById("fxfield");
    var fx = document.getElementById("fx_rate");
    var foreign = currency && currency !== (window.reportingCurrency || "AUD");
    if (field) { field.hidden = !foreign; }
    /* Cleared, not filled: an AUD instrument has nothing to convert, and for a
       foreign one `priceForDate` supplies the rate for the trade's own date. */
    if (fx && !foreign) { fx.value = ""; }

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

  /* Anything typed by hand is protected from the clearing above, and from the
     date-driven refill. Price and FX join units here for that reason: the form
     re-asks the server whenever the date moves, and someone who typed a figure
     off their contract note should not watch it be replaced by a close. */
  document.addEventListener("input", function (event) {
    var id = event.target.id;
    if (id === "quantity" || id === "unit_price" || id === "fx_rate") {
      event.target.dataset.typed = "1";
    }
  });

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target.id === "instpick") { toggleNew(); instrumentChosen(); priceForDate(); }
    if (target.id === "tradedate") { priceForDate(); }
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
