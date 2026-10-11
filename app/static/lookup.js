/* Asking the server what a ticker is, for both forms that add an instrument:
   Record trade's "Something not listed" fields and Manage holdings' Add
   dialog. One copy, because two had drifted apart.

   `lookupInstrument(prefix, form)` reads the fields named <prefix>ticker and
   <prefix>exchange and fills the rest, in that form only: Manage holdings'
   rows have fields of the same names, and a lookup that searched the page
   filled the first row's instead of the dialog's. Anything typed by hand is kept,
   because overwriting someone's correction is worse than not helping. What
   the lookup itself filled in is replaced by its next answer: the exchange
   often changes after the ticker, and the first answer, the ASX guess, is
   then wrong. Only the newest question's answer is used, because the first
   can come back last and put GOOG.AX back on a NASDAQ stock.

   The answer then goes out as an `instrument-answer` event, for what one
   form does with it and the other does not. */
(function () {
  "use strict";

  var asked = {};

  window.lookupInstrument = function (prefix, form) {
    var field = function (prefix, name) {
      return (form || document).querySelector('[name="' + prefix + name + '"]');
    };
    var ticker = field(prefix, "ticker");
    var exchange = field(prefix, "exchange");
    if (!ticker || !ticker.value.trim()) { return; }
    var status = document.getElementById(prefix + "lookup-status");
    if (status) { status.textContent = "looking up…"; }
    var mine = asked[prefix] = (asked[prefix] || 0) + 1;

    fetch("/holdings/lookup?ticker=" + encodeURIComponent(ticker.value) +
          "&exchange=" + encodeURIComponent(exchange ? exchange.value : "ASX"))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (asked[prefix] !== mine) { return; }
        if (!d) { if (status) { status.textContent = ""; } return; }
        var fill = function (name, value) {
          var el = field(prefix, name);
          if (!el || (el.value.trim() && el.value !== el.dataset.filled)) { return; }
          el.value = value || "";
          el.dataset.filled = el.value;
        };
        fill("name", d.name);
        /* Not `fill`: a field that starts at AUD was never empty, so a USD
           stock stayed AUD. Replaced unless somebody typed it, and back to
           where it started when the answer has none. */
        var currency = field(prefix, "currency");
        if (currency && !currency.dataset.typed) {
          currency.value = d.currency || currency.defaultValue;
        }
        fill("yahoo", d.symbol);
        fill("yahoo_symbol", d.symbol);
        if (status) {
          status.textContent = d.off ? "not looked up: the price feed is off"
            : d.found
            ? "found: " + (d.name || d.symbol) + (d.currency ? " (" + d.currency + ")" : "")
            : "no match for " + d.symbol + " — check the ticker and exchange";
        }
        document.dispatchEvent(new CustomEvent("instrument-answer",
                                               {detail: {prefix: prefix, answer: d}}));
      })
      .catch(function () { if (status) { status.textContent = ""; } });
  };

  /* A currency typed by hand is kept from then on. */
  document.addEventListener("input", function (event) {
    if (/currency$/.test(event.target.name || "")) { event.target.dataset.typed = "1"; }
  });
})();
