/* Imports & exports: the dialogs on this page.

   Three of them — Manage templates, and the two mappers. The mappers are in
   dialogs so each panel carries ONE file input: an upload and a mapper input
   sitting together is the sort of thing you read twice to use once.

   Same `showModal()` shape as the trade and plan dialogs. `data-close-dialog`
   is handled once in tradeform.js, which this page also loads. */
(function () {
  "use strict";

  /* The templates dialog is rendered with a plain `open` attribute after an
     install or a removal (so the result is visible without script), which
     shows it NON-modally — in the page's flow, below the exports, rather than
     over them. Close first so every way in ends up the same modal. */
  function show(dialog) {
    if (dialog.hasAttribute("open") && dialog.showModal) { dialog.close(); }
    if (dialog.showModal) {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
  }

  function wire(buttonId, dialogId) {
    var open = document.getElementById(buttonId);
    var dialog = document.getElementById(dialogId);
    if (!open || !dialog) { return; }

    open.addEventListener("click", function () {
      show(dialog);
      var first = dialog.querySelector('input[type="file"]');
      if (first) { first.focus(); }
    });
  }

  wire("opentemplates", "templatedialog");
  wire("openbrokermap", "brokermapdialog");
  wire("openstatementmap", "statementmapdialog");

  var templates = document.getElementById("templatedialog");
  if (templates && templates.hasAttribute("open")) {
    show(templates);

    /* The outcome is reported once. Left in the address bar, a reload would
       reopen the dialog and say "Removed mufg.yaml" again as if it had just
       happened. */
    if (window.URL && window.history && history.replaceState) {
      var url = new URL(window.location.href);
      ["installed", "removed", "error", "templates"].forEach(function (key) {
        url.searchParams.delete(key);
      });
      history.replaceState(null, "", url.pathname + url.search + url.hash);
    }
  }

  /* Likewise when the button opens it again: a result still sitting in it is
     about something done earlier, not something just done. On the button and
     not on `close`, because the `show()` above closes it too, and `close`
     fires asynchronously — the message would vanish as it appeared. */
  var button = document.getElementById("opentemplates");
  if (templates && button) {
    button.addEventListener("click", function () {
      templates.querySelectorAll("p.panel.ok, p.panel.error").forEach(function (p) {
        p.remove();
      });
    });
  }
})();
