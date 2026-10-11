/* A form with `data-confirm` asks first, in its own words. Declined,
   nothing is sent; with scripting off the form posts as it always did, and
   the server's rules are the same either way. */
(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    var words = event.target.dataset && event.target.dataset.confirm;
    if (words && !window.confirm(words)) { event.preventDefault(); }
  });
})();
