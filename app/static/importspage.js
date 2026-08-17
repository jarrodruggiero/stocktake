/* Imports & exports: the dialogs on this page.

   Three of them — Manage templates, and the two mappers. The mappers are in
   dialogs so each panel carries ONE file input: an upload and a mapper input
   sitting together is the sort of thing you read twice to use once.

   Same `showModal()` shape as the trade and plan dialogs. `data-close-dialog`
   is handled once in tradeform.js, which this page also loads. */
(function () {
  "use strict";

  function wire(buttonId, dialogId) {
    var open = document.getElementById(buttonId);
    var dialog = document.getElementById(dialogId);
    if (!open || !dialog) { return; }

    open.addEventListener("click", function () {
      /* The templates dialog is rendered with a plain `open` attribute after
         an install (so the result is visible without script), which shows it
         NON-modally. Close first so both ways in behave the same. */
      if (dialog.hasAttribute("open") && dialog.showModal) { dialog.close(); }
      if (dialog.showModal) {
        dialog.showModal();
      } else {
        dialog.setAttribute("open", "");
      }
      var first = dialog.querySelector('input[type="file"]');
      if (first) { first.focus(); }
    });
  }

  wire("opentemplates", "templatedialog");
  wire("openbrokermap", "brokermapdialog");
  wire("openstatementmap", "statementmapdialog");
})();
