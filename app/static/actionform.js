/* The split or capital change form: only the chosen kind's fields show.
 *
 * Every kind's fields are in the markup, marked `data-kinds`, so with no
 * script they all show and the server reads the ones it needs. Hidden ones
 * are disabled too, so a value typed before switching kinds is not sent.
 */
(function () {
  "use strict";

  var form = document.querySelector(".actionform");
  if (!form) { return; }
  var kind = form.querySelector('[name="kind"]');

  function show() {
    form.querySelectorAll("[data-kinds]").forEach(function (part) {
      var on = part.dataset.kinds.split(" ").indexOf(kind.value) !== -1;
      part.hidden = !on;
      part.querySelectorAll("input, select").forEach(function (field) { field.disabled = !on; });
    });
  }

  kind.addEventListener("change", show);
  show();
})();
