/* Removing a member: confirm first, and when they are the last one, offer to
   delete the portfolio instead.

   One dialog for the page rather than one per row — the button carries the
   name and whether this is the last member, and the dialog reads them. With
   scripting off the row's own form posts as before and the server applies the
   same rules, so nothing here is the only thing standing between a click and
   a deletion. */
(function () {
  "use strict";

  var dialog = document.getElementById("removedialog");
  var form = document.getElementById("removeform");
  if (!dialog || !form) { return; }

  var name = document.getElementById("removename");
  var plain = document.getElementById("removeplain");
  var warning = document.getElementById("removelast");
  var offer = document.getElementById("removedelete");
  var tick = document.getElementById("deletetick");
  var confirm = document.getElementById("removeconfirm");

  function paint() {
    /* Ticking answers the warning, so it goes: leaving both up would ask the
       question and refuse it at the same time. */
    var deleting = tick.checked;
    warning.hidden = deleting;
    plain.hidden = true;
    confirm.textContent = deleting ? "Delete portfolio" : "Remove";
  }

  document.addEventListener("click", function (event) {
    var close = event.target.closest("[data-close-dialog]");
    if (close && close.closest("dialog")) { close.closest("dialog").close(); return; }

    var button = event.target.closest("[data-remove]");
    if (!button) { return; }
    event.preventDefault();

    form.action = button.closest("form").action;
    name.textContent = button.dataset.remove;
    tick.checked = false;

    var last = button.dataset.last === "1";
    warning.hidden = !last;
    offer.hidden = !last;
    plain.hidden = last;
    confirm.textContent = "Remove";

    if (dialog.showModal) { dialog.showModal(); } else { dialog.setAttribute("open", ""); }
  });

  tick.addEventListener("change", paint);
})();
