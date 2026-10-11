/* A form with `data-confirm` asks first, in its own words; so does a button
   with one, which asks for itself, as "Save and restart" does beside a plain
   "Save settings" in the same form.

   The words are an attribute the template escapes, never part of a script,
   so a name in them is only ever text. Declined, nothing is sent; with
   scripting off the form posts as it always did, and the server's rules are
   the same either way. */
(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    var button = event.submitter;
    var asker = button && button.dataset.confirm ? button : event.target;
    var words = asker.dataset && asker.dataset.confirm;
    if (words && !window.confirm(words)) { event.preventDefault(); }
  });
})();
