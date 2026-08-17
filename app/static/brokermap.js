/* Show what is actually in the column somebody just picked.

   Nothing here is required. The selects are named (`col_date`, `col_ticker`,
   …) and submit on their own, so the mapper works with JavaScript off — this
   file only refreshes the sample values shown beside each dropdown, because
   picking the right column is much easier when you can see what is in it. */
(function () {
  "use strict";
  var form = document.getElementById("mapform");
  if (!form) { return; }

  var picks = Array.prototype.slice.call(form.querySelectorAll(".colpick"));
  if (!picks.length) { return; }

  /* The file's rows, read off the table the server already rendered, so there
     is no second copy of the data and nothing to fetch. */
  var table = document.querySelector("section table");
  var headers = [];
  var rows = [];
  if (table) {
    table.querySelectorAll("thead th").forEach(function (th) {
      headers.push(th.textContent.trim());
    });
    table.querySelectorAll("tbody tr").forEach(function (tr) {
      var cells = [];
      tr.querySelectorAll("td").forEach(function (td) { cells.push(td.textContent.trim()); });
      rows.push(cells);
    });
  }

  function samplesFor(header) {
    var at = headers.indexOf(header);
    if (at < 0) { return ""; }
    return rows.map(function (r) { return r[at] || ""; }).join(" · ");
  }

  function sync() {
    picks.forEach(function (select) {
      var cell = form.querySelector('.samples[data-field="' + select.dataset.field + '"]');
      if (cell) { cell.textContent = select.value ? samplesFor(select.value) : ""; }
    });
  }

  picks.forEach(function (select) { select.addEventListener("change", sync); });
  sync();
})();

/* Reveal the custom date-format box when its checkbox is ticked. The server
   renders the box open when it was in use, so with JavaScript off the tick and
   the field are both there — just always visible. */
(function () {
  "use strict";
  var box = document.getElementById("customdatebox");
  var tick = document.getElementById("customdate");
  if (!box || !tick) { return; }
  var picker = document.querySelector('select[name="date_format"]');
  var sync = function () {
    box.hidden = !tick.checked;
    if (picker) { picker.disabled = tick.checked; }
  };
  tick.addEventListener("change", sync);
  sync();
})();
