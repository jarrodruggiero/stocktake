/* Opening the move dialog on the edit-trade page.

   `showModal()` rather than `show()`, for the four things a hand-rolled modal
   gets wrong: it takes focus, traps it, closes on Escape and dims the page
   behind. Same as the Record trade dialog — see dashboard.js.

   Cancel is handled in tradeform.js, which closes whatever dialog a
   `[data-close-dialog]` button sits in and is loaded on this page already. */
(function () {
  "use strict";

  var dialog = document.getElementById("movedialog");
  var button = document.getElementById("openmove");
  if (!dialog || !button) { return; }

  button.addEventListener("click", function () {
    if (dialog.showModal) {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");   // very old browsers: usable, not modal
    }
    var first = dialog.querySelector('[name="target"]');
    if (first) { first.focus(); }
  });
})();
