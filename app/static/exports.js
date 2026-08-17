/* Show the "a CSV holds one sheet" notice only when it applies.
 *
 * A static file rather than an inline block: the CSP still carries
 * 'unsafe-inline' for ~7 older handlers, and the aim is to stop adding to that
 * debt rather than grow it. Nothing here is page-specific enough to need it.
 *
 * The notice is rendered visible-by-default in the HTML and hidden here, so
 * with JavaScript off it simply shows — a warning that fails open is the right
 * way round. The server refuses the combination regardless.
 */
(function () {
  "use strict";
  var notice = document.getElementById("csv-single-sheet");
  if (!notice) { return; }
  var form = notice.closest("form");
  var report = form && form.querySelector('select[name="report"]');
  var format = form && form.querySelector('select[name="fmt"]');
  if (!report || !format) { return; }

  var sync = function () {
    notice.hidden = !(report.value === "all" && format.value === "csv");
  };
  report.addEventListener("change", sync);
  format.addEventListener("change", sync);
  sync();
})();
