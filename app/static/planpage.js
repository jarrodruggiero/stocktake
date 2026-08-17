/* The DCA schedule page: opening the plan editor.

   Same shape as `dashboard.js` — `showModal()` for focus handling, Escape and
   the backdrop. `data-close-dialog` (Cancel) is handled once in tradeform.js,
   which every page carrying a dialog already loads. */
(function () {
  "use strict";

  var open = document.getElementById("openplan");
  var dialog = document.getElementById("plandialog");
  if (!open || !dialog) { return; }

  open.addEventListener("click", function () {
    /* `?edit=1` renders the dialog with a plain `open` attribute, which shows
       it NON-modally: no backdrop, no focus trap. Close and reopen so the two
       ways in behave identically. */
    if (dialog.hasAttribute("open") && dialog.showModal) {
      dialog.close();
    }
    if (dialog.showModal) {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    var first = dialog.querySelector('[name="name"]');
    if (first) { first.focus(); }
  });
})();
