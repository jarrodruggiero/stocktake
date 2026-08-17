/* The statement template designer's interaction, shared by both ways of
   pointing at a value.

   There are two designers — words in extracted text, and boxes over a rendered
   page — and they differ only in what you click. Everything after the click is
   identical, so duplicating it would mean two implementations of the one thing
   that must not disagree: a wrong mapping produces a template that reads the
   wrong number on somebody else's statement.

   The inference is deliberately NOT here. It lives in `docformats.infer_field`,
   where it can be tested; this file carries clicks to it and answers back.

   ## The rule this all follows

   The designer never refuses a mapping it cannot work out — that reads as the
   tool already knowing the answer, and it cannot: a statement nobody has seen
   is exactly what this is for. So:

     * a click always maps;
     * the label is EDITABLE, and typing one re-reads it against the document,
       which is the way out of a value in a table column that has no label to
       its left;
     * a discrepancy between the value's type and the field's is shown as a
       note that clears when it is fixed, never as a refusal;
     * a written date highlights all three of its words, because clicking the
       month and clicking the day must do the same thing. */
(function () {
  "use strict";

  window.startDesigner = function (options) {
    var csrf = document.querySelector('meta[name="csrf-token"]').content;
    var text = document.getElementById("doctext-source").value;
    var hint = document.getElementById("hint");
    var surface = document.getElementById(options.surface);
    var mapping = {};
    var active = null;

    if (!surface) { return; }

    function setHint(message) { if (hint) { hint.textContent = message; } }

    function itemsAt(startIndex, span) {
      /* Every element covered by one value. A written date is three words, and
         highlighting only the one that was clicked leaves somebody wondering
         why the other two did nothing. */
      var out = [];
      for (var i = startIndex; i < startIndex + span; i++) {
        var el = surface.querySelector(options.item + '[data-index="' + i + '"]');
        if (el) { out.push(el); }
      }
      return out;
    }

    function clearMarks() {
      surface.querySelectorAll(options.item).forEach(function (t) {
        t.setAttribute("aria-pressed", "false");
      });
    }

    function noteOn(li, message, kind) {
      var note = li.querySelector(".fieldnote");
      if (!note) { return; }
      note.textContent = message || "";
      note.className = "fieldnote" + (message ? " " + (kind || "warn") : "");
    }

    function clearField(li) {
      /* Back to untouched: the mapping entry goes, the label box closes, the
         note clears. Editing a template is mostly re-mapping, so this has to
         leave no trace of the previous answer. */
      delete mapping[li.dataset.field];
      document.getElementById("mapping").value = JSON.stringify(mapping);
      li.classList.remove("mapped", "active");
      li.querySelector(".state").textContent = "not set";
      var box = li.querySelector(".labeledit");
      var input = li.querySelector(".labelinput");
      if (box) { box.hidden = true; }
      if (input) { input.value = ""; }
      var clear = li.querySelector(".clearfield");
      if (clear) { clear.hidden = true; }
      noteOn(li, "");
      if (active === li) { active = null; }
      clearMarks();
      enableButtons();
      setHint("Cleared. Pick a field, then click its value.");
    }

    function record(li, found) {
      var field = li.dataset.field;
      mapping[field] = { after: found.labels, type: found.type };
      document.getElementById("mapping").value = JSON.stringify(mapping);
      li.classList.add("mapped");
      li.querySelector(".state").textContent =
        found.value === null ? "no value read" : "reads " + found.value;

      /* The label box appears when the app could not work one out, and stays
         available afterwards so a wrong guess can be corrected. */
      var box = li.querySelector(".labeledit");
      var input = li.querySelector(".labelinput");
      if (box && input) {
        box.hidden = false;
        input.value = (found.labels && found.labels[0]) || "";
      }
      var clear = li.querySelector(".clearfield");
      if (clear) { clear.hidden = false; }
      noteOn(li, found.mismatch || found.warning || "",
             found.mismatch ? "warn" : "sub");
      enableButtons();
    }

    function enableButtons() {
      var empty = Object.keys(mapping).length === 0;
      ["download", "install", "contribute"].forEach(function (id) {
        var button = document.getElementById(id);
        if (button) { button.disabled = empty; }
      });
    }

    function selectField(li) {
      document.querySelectorAll("#fields li").forEach(function (n) {
        n.classList.remove("active");
      });
      li.classList.add("active");
      active = li;
      setHint("Now click the value for “" +
              li.querySelector(".pick").textContent + "”.");
    }

    document.querySelectorAll("#fields li").forEach(function (li) {
      li.querySelector(".pick").addEventListener("click", function () { selectField(li); });

      var clear = li.querySelector(".clearfield");
      if (clear) {
        clear.addEventListener("click", function () { clearField(li); });
      }

      /* Typing a label re-reads it against the document. A label is only worth
         something if it finds the value, and the only way to know is to try. */
      var input = li.querySelector(".labelinput");
      if (input) {
        input.addEventListener("change", async function () {
          var field = li.dataset.field;
          var current = mapping[field] || { type: li.dataset.expect || "text" };
          var body = new URLSearchParams({
            text: text, label: input.value, type: current.type, _csrf: csrf,
          });
          var resp = await fetch("/imports-exports/statement/design/check", {
            method: "POST", body: body, headers: { "X-CSRF-Token": csrf },
          });
          if (!resp.ok) { return noteOn(li, "could not check that label", "warn"); }
          var reading = await resp.json();
          mapping[field] = { after: [input.value], type: current.type };
          document.getElementById("mapping").value = JSON.stringify(mapping);
          li.classList.add("mapped");
          li.querySelector(".state").textContent =
            reading.value === null ? "no value read" : "reads " + reading.value;
          var clearButton = li.querySelector(".clearfield");
          if (clearButton) { clearButton.hidden = false; }
          noteOn(li, reading.problem || "", reading.problem ? "warn" : "sub");
          enableButtons();
        });
      }
    });

    surface.addEventListener("click", async function (event) {
      var target = event.target.closest(options.item);
      if (!target) { return; }
      if (!active) { return setHint("Pick a field on the right first."); }

      var body = new URLSearchParams({
        text: text, index: target.dataset.index, _csrf: csrf,
        expect: active.dataset.expect || "",
      });
      var resp = await fetch("/imports-exports/statement/design/infer", {
        method: "POST", body: body, headers: { "X-CSRF-Token": csrf },
      });
      if (!resp.ok) { return setHint("Could not work that out — try another value."); }
      var found = await resp.json();
      /* `problem` now means only "that is not a word in this document". */
      if (found.problem) { return setHint(found.problem); }

      clearMarks();
      itemsAt(found.starts_at, found.span).forEach(function (el) {
        el.setAttribute("aria-pressed", "true");
      });

      record(active, found);
      setHint(found.mismatch ||
              (found.needs_label
                ? "Mapped. Type the words that come before it to make it findable."
                : "Set. " + Object.keys(mapping).length + " field(s) mapped."));
    });

    /* Opening an existing template shows where its fields already point,
       rather than a blank slate. */
    var start = document.getElementById("startmapping");
    if (start) {
      try { mapping = JSON.parse(start.textContent) || {}; } catch (e) { mapping = {}; }
      Object.keys(mapping).forEach(function (field) {
        var li = document.querySelector('#fields li[data-field="' + field + '"]');
        if (!li) { return; }
        li.classList.add("mapped");
        li.querySelector(".state").textContent = "from the template";
        var existing = li.querySelector(".clearfield");
        if (existing) { existing.hidden = false; }
        var box = li.querySelector(".labeledit");
        var input = li.querySelector(".labelinput");
        if (box && input) {
          box.hidden = false;
          input.value = (mapping[field].after || [])[0] || "";
        }
      });
      if (Object.keys(mapping).length) {
        document.getElementById("mapping").value = JSON.stringify(mapping);
        enableButtons();
      }
    }
  };
})();
