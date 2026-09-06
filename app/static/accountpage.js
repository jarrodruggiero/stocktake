/* The profile page's sign-in table: each action opens its form in a dialog.

   `showModal()` rather than `show()`, for the same reasons as the trade
   dialog — it takes focus, traps it, closes on Escape and dims the page
   behind. `data-close-dialog` (Cancel) is handled once in tradeform.js. */
(function () {
  "use strict";

  /* Cancel closes rather than navigating away. tradeform.js has the same
     handler, but it is only loaded on the pages with a trade form. */
  document.addEventListener("click", function (event) {
    var close = event.target.closest("[data-close-dialog]");
    if (close) {
      var open = close.closest("dialog");
      if (open) { open.close(); }
      return;
    }
    var trigger = event.target.closest("[data-dialog]");
    if (!trigger) { return; }
    var dialog = document.getElementById(trigger.dataset.dialog);
    if (!dialog) { return; }
    event.preventDefault();
    if (dialog.showModal) {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");   // very old browsers: usable, not modal
    }
    var first = dialog.querySelector("input:not([type=hidden]), button");
    if (first) { first.focus(); }
  });
})();
