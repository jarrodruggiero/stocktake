/* A button that opens a dialog, and the dialog's Cancel.

   `data-opens="<id>"` names the dialog. showModal() rather than show(): it
   takes focus, traps it, closes on Escape and dims the page behind. Each
   such dialog also has a route of its own for a browser without scripting. */
(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    var opener = event.target.closest("[data-opens]");
    if (opener) {
      var dialog = document.getElementById(opener.dataset.opens);
      if (dialog && dialog.showModal) { dialog.showModal(); }
      return;
    }
    var close = event.target.closest("[data-close-dialog]");
    if (close && close.closest("dialog")) { close.closest("dialog").close(); }
  });
})();
