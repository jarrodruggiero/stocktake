/* The Portfolio page's own interactions.

   Only the dialog for now. `showModal()` rather than `show()`: it takes focus,
   traps it, closes on Escape and dims the page behind — the four things a
   hand-rolled modal gets wrong, and the reason the holdings page's add-an-
   instrument dialog is built the same way. */
(function () {
  "use strict";

  var dialog = document.getElementById("tradedialog");
  if (!dialog) { return; }

  function openTrade() {
    if (dialog.showModal) {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");   // very old browsers: not modal, but usable
    }
    /* The form is server-rendered, so its conditional fields start from
       markup. Re-sync them each time rather than once at load: a failed
       submit reopens with different values. */
    if (window.syncTradeForm) { window.syncTradeForm(); }
    var first = dialog.querySelector("#instpick");
    if (first) { first.focus(); }
  }

  /* Two buttons, one dialog: the header's Record trade, and the one on the
     first-run empty state. The empty state only exists before anything has
     been recorded, so the two are never on the page together — but wiring both
     by id costs nothing and means the empty state's primary action is not a
     dead button if that ever changes. */
  ["opentrade", "opentrade-empty"].forEach(function (id) {
    var button = document.getElementById(id);
    if (button) { button.addEventListener("click", openTrade); }
  });
})();
