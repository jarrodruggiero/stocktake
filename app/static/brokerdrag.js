/* Assign a field to a column by dragging a chip onto it — or by tapping.
 *
 * The selects in the fallback table stay the source of truth: this only sets
 * their value and fires `change`, so the form submits identically whether the
 * mapping was dragged, tapped, or chosen from the list with JavaScript off.
 *
 * Tap is not a lesser path. HTML drag-and-drop does not fire on touch at all,
 * and the layout is checked at 412px, so a drag-only mapper would be a mapper
 * that does not work on a phone.
 */
(function () {
  "use strict";
  var form = document.getElementById("mapform");
  var sheet = document.querySelector(".sheet");
  var tray = document.getElementById("fieldtray");
  if (!form || !sheet || !tray) return;

  /* Revealed only now: without JavaScript the tray would be a row of buttons
     that do nothing, so the select list is what a plain browser gets. */
  tray.hidden = false;
  var fallback = document.getElementById("mapperfallback");
  if (fallback) fallback.open = false;

  var armed = null;   /* the chip waiting for somewhere to land */

  function selectFor(field) {
    return form.querySelector('.colpick[data-field="' + field + '"]');
  }

  function assign(field, header) {
    var select = selectFor(field);
    if (!select) return;
    /* A column is claimed by at most one field. Two fields reading the same
       column parses without complaint and produces trades whose price is their
       brokerage — so taking a column takes it from whoever had it. */
    if (header) {
      tray.querySelectorAll(".fieldchip").forEach(function (other) {
        if (other.dataset.field === field) return;
        var s = selectFor(other.dataset.field);
        if (s && s.value === header) { s.value = ""; s.dispatchEvent(new Event("change", { bubbles: true })); }
      });
    }
    select.value = header || "";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    paint();
  }

  /* Which column each field currently owns, read back off the selects so this
     survives a server round trip without a second copy of the state. */
  function paint() {
    var taken = {};
    tray.querySelectorAll(".fieldchip").forEach(function (chip) {
      var select = selectFor(chip.dataset.field);
      var header = select ? select.value : "";
      chip.classList.toggle("assigned", !!header);
      var clear = tray.querySelector('.chipclear[data-field="' + chip.dataset.field + '"]');
      if (clear) clear.hidden = !header;
      if (header) taken[header] = chip.dataset.field;
    });
    sheet.querySelectorAll("th[data-header]").forEach(function (th) {
      var field = taken[th.dataset.header] || "";
      th.dataset.assigned = field;
      var tag = th.querySelector(".coltag");
      if (tag) tag.remove();
      if (field) {
        var span = document.createElement("span");
        span.className = "coltag";
        span.textContent = field;
        th.appendChild(span);
      }
    });
  }

  function arm(chip) {
    if (armed === chip) { disarm(); return; }
    disarm();
    armed = chip;
    chip.classList.add("armed");
    sheet.classList.add("targeting");
  }

  function disarm() {
    if (armed) armed.classList.remove("armed");
    armed = null;
    sheet.classList.remove("targeting");
  }

  tray.addEventListener("click", function (e) {
    var clear = e.target.closest(".chipclear");
    if (clear) {
      e.preventDefault();
      assign(clear.dataset.field, "");
      disarm();
      return;
    }
    var chip = e.target.closest(".fieldchip");
    if (!chip) return;
    e.preventDefault();
    /* Arming, even when already assigned: tapping a chip is how you MOVE it to
       another column, and a mis-tap then costs nothing. The × clears. */
    arm(chip);
  });

  sheet.addEventListener("click", function (e) {
    var th = e.target.closest("th[data-header]");
    if (!th || !armed) return;
    assign(armed.dataset.field, th.dataset.header);
    disarm();
  });

  if ("draggable" in document.createElement("div")) {
    tray.addEventListener("dragstart", function (e) {
      var chip = e.target.closest(".fieldchip");
      if (!chip) return;
      e.dataTransfer.setData("text/plain", chip.dataset.field);
      e.dataTransfer.effectAllowed = "copy";
      sheet.classList.add("targeting");
    });
    tray.addEventListener("dragend", disarm);

    sheet.addEventListener("dragover", function (e) {
      var th = e.target.closest("th[data-header]");
      if (!th) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      th.classList.add("dropping");
    });
    sheet.addEventListener("dragleave", function (e) {
      var th = e.target.closest("th[data-header]");
      if (th) th.classList.remove("dropping");
    });
    sheet.addEventListener("drop", function (e) {
      var th = e.target.closest("th[data-header]");
      if (!th) return;
      e.preventDefault();
      th.classList.remove("dropping");
      assign(e.dataTransfer.getData("text/plain"), th.dataset.header);
      disarm();
    });
  }

  /* ── Trade types: drop one onto a cell, and every cell saying that word
        takes it. The selects in the fallback table remain the state. ── */
  var typetray = document.getElementById("typetray");

  /* Words every broker agrees on. They need no mapping, so outlining them as
     unmapped would say the whole column needs attention when two words do.
     Kept in step with brokercsv.BUILTIN_ACTIONS by a test. */
  var BUILTIN = { buy: "buy", b: "buy", sell: "sell", s: "sell" };

  function typeSelect(word) {
    return form.querySelector('.typepick[data-word="' + (window.CSS && CSS.escape
      ? CSS.escape(word) : word) + '"]');
  }

  function actionHeader() {
    var select = selectFor("action");
    return select ? select.value : "";
  }

  function setType(word, kind) {
    var select = typeSelect(word);
    if (!select) return;
    select.value = kind || "";
    paintTypes();
  }

  /* Every cell in the action column wears what its word maps to, so the answer
     is visible on the data rather than only in a list underneath it. */
  function paintTypes() {
    var header = actionHeader();
    sheet.querySelectorAll("td[data-col]").forEach(function (td) {
      td.classList.remove("typed", "untyped");
      var tag = td.querySelector(".typetag");
      if (tag) tag.remove();
      if (!header || td.dataset.col !== header) return;
      var word = td.textContent.trim();
      var select = typeSelect(word);
      if (!select) return;
      var builtin = BUILTIN[word.toLowerCase()];
      var kind = select.value || builtin || "";
      td.classList.add(kind ? "typed" : "untyped");
      if (kind) {
        var span = document.createElement("span");
        span.className = "typetag" + (select.value ? "" : " typetag-builtin");
        span.textContent = kind;
        td.appendChild(span);
      }
    });
  }

  if (typetray) {
    var armedType = null;
    typetray.addEventListener("click", function (e) {
      var chip = e.target.closest(".typechip");
      if (!chip) return;
      e.preventDefault();
      if (armedType) armedType.classList.remove("armed");
      armedType = armedType === chip ? null : chip;
      if (armedType) armedType.classList.add("armed");
      sheet.classList.toggle("targeting-cells", !!armedType);
    });
    sheet.addEventListener("click", function (e) {
      var td = e.target.closest("td[data-col]");
      if (!td || !armedType || td.dataset.col !== actionHeader()) return;
      setType(td.textContent.trim(), armedType.dataset.kind);
      armedType.classList.remove("armed");
      armedType = null;
      sheet.classList.remove("targeting-cells");
    });
    if ("draggable" in document.createElement("div")) {
      typetray.addEventListener("dragstart", function (e) {
        var chip = e.target.closest(".typechip");
        if (!chip) return;
        e.dataTransfer.setData("text/plain", "type:" + chip.dataset.kind);
        e.dataTransfer.effectAllowed = "copy";
        sheet.classList.add("targeting-cells");
      });
      typetray.addEventListener("dragend", function () {
        sheet.classList.remove("targeting-cells");
      });
      sheet.addEventListener("dragover", function (e) {
        var td = e.target.closest("td[data-col]");
        if (td && td.dataset.col === actionHeader()) e.preventDefault();
      });
      sheet.addEventListener("drop", function (e) {
        var td = e.target.closest("td[data-col]");
        if (!td || td.dataset.col !== actionHeader()) return;
        var payload = e.dataTransfer.getData("text/plain");
        if (payload.indexOf("type:") !== 0) return;
        e.preventDefault();
        setType(td.textContent.trim(), payload.slice(5));
        sheet.classList.remove("targeting-cells");
      });
    }
  }

  form.addEventListener("change", function (e) {
    if (!e.target.classList) return;
    if (e.target.classList.contains("colpick")) { paint(); paintTypes(); }
    if (e.target.classList.contains("typepick")) paintTypes();
  });
  paint();
  paintTypes();
})();
