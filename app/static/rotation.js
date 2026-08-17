/* The DCA rotation, as chips you arrange.
 *
 * ## The textarea is still the field
 *
 * Nothing here posts anything. `#tickers` is the form control, exactly as it
 * was, and every change writes back to it — so the server keeps its one
 * parser, the no-JavaScript path is the plain comma-separated box, and a
 * failed submit re-renders from a value the server already understands.
 *
 * ## Why clicking has to work, not just dragging
 *
 * HTML5 drag-and-drop does not exist on touch devices and is close to
 * unusable by keyboard. So drag is the pleasant way and never the only way:
 * every palette chip is a button that appends, and every chip in the rotation
 * carries move and remove buttons. Drag is layered on top — the same shape as
 * the holdings column editor, where the arrows are real controls and dragging
 * is the enhancement.
 *
 * ## Duplicates are the point
 *
 * A ticker appearing three times is bought three times as often. So the
 * palette ADDS rather than toggles, nothing is deduplicated, and a chip's
 * identity is its position in the list — never its ticker.
 */
(function () {
  "use strict";

  var field = document.getElementById("tickers");
  var panel = document.getElementById("rotation");
  var list = document.getElementById("rotorder");
  var toggleWrap = document.querySelector(".rottoggle");
  var toggle = document.getElementById("rotastext");
  var empty = document.querySelector(".rotempty");
  var count = document.querySelector(".rotcount");
  if (!field || !panel || !list) { return; }

  var still = window.matchMedia("(prefers-reduced-motion: reduce)");

  /* Scripting is on, so the chips become the interface and the textarea steps
     back. It stays in the DOM — hiding it is a display change, not a removal,
     so it still submits. */
  panel.hidden = false;
  if (toggleWrap) { toggleWrap.hidden = false; }
  field.hidden = true;

  /* `required` has to come OFF once the field is hidden.
     A hidden control that fails validation blocks the submit and then cannot
     be focused to explain why — Chrome logs "not focusable" to the console and
     the button appears to do nothing, which is the worst of both. The
     attribute stays in the markup for the no-JavaScript path; the check below
     replaces it, and says so somewhere visible. */
  field.required = false;

  var error = document.createElement("p");
  error.className = "panel error roterror";
  error.hidden = true;
  error.textContent = "Add at least one ticker to the rotation.";
  panel.parentNode.insertBefore(error, panel.nextSibling);

  var form = field.form;
  if (form) {
    form.addEventListener("submit", function (e) {
      if (list.children.length) { return; }
      e.preventDefault();
      error.hidden = false;
      var first = document.querySelector(".rotsource .add");
      if (first) { first.focus(); }
    });
  }

  function parse(text) {
    return text.replace(/\n/g, ",").split(",")
      .map(function (t) { return t.trim().toUpperCase(); })
      .filter(Boolean);
  }

  function tickers() {
    return Array.prototype.map.call(
      list.querySelectorAll("li[data-ticker]"),
      function (li) { return li.dataset.ticker; }
    );
  }

  /* One direction: chips -> field. The reverse only happens on an explicit
     text edit, which keeps the two from writing over each other. */
  function sync() {
    field.value = tickers().join(", ");
    var n = list.children.length;
    if (empty) { empty.hidden = n > 0; }
    if (count) {
      count.textContent = n
        ? n + (n === 1 ? " buy in the cycle" : " buys in the cycle")
        : "";
    }
    // Clears the moment they add something, rather than on the next submit.
    if (n) { error.hidden = true; }
    refreshPreview();
    Array.prototype.forEach.call(list.querySelectorAll("li"), function (li, i) {
      var up = li.querySelector("[data-move='up']");
      var down = li.querySelector("[data-move='down']");
      if (up) { up.disabled = i === 0; }
      if (down) { down.disabled = i === list.children.length - 1; }
      var pos = li.querySelector(".rotpos");
      if (pos) { pos.textContent = i + 1; }
    });
  }

  /* ── the live preview ─────────────────────────────────────────────────
     Asks the server what the draft would produce. Deliberately NOT computed
     here: `plans.project` already does this arithmetic for the real schedule,
     and a copy in JavaScript is a copy that can disagree — which is exactly
     what a preview must never do. */
  var rows = document.getElementById("previewrows");
  var previewEmpty = document.querySelector(".previewempty");
  var note = document.getElementById("previewnote");
  var noteText = note ? note.textContent : "";
  var inflight = null;
  var pending = null;

  function renderPreview(data) {
    if (!rows) { return; }
    rows.textContent = "";
    var today = document.getElementById("preview").dataset.today || "";
    (data.rows || []).forEach(function (row) {
      var tr = document.createElement("tr");
      if (row.date <= today) { tr.className = "due"; }   // ISO dates sort as text
      var date = document.createElement("td");
      date.textContent = row.date;
      var ticker = document.createElement("td");
      ticker.textContent = row.ticker;
      tr.appendChild(date);
      tr.appendChild(ticker);
      rows.appendChild(tr);
    });
    if (previewEmpty) { previewEmpty.hidden = (data.rows || []).length > 0; }
    if (note) {
      note.textContent = data.why || noteText;
      note.classList.toggle("down", Boolean(data.why));
    }
  }

  function refreshPreview() {
    if (!form || !rows) { return; }
    clearTimeout(pending);
    /* Debounced: dragging a chip fires sync() on every reorder, and a request
       per frame would be both wasteful and out of order. */
    pending = setTimeout(function () {
      var body = new FormData();
      body.append("_csrf", (form.querySelector('[name="_csrf"]') || {}).value || "");
      body.append("interval_days", (form.querySelector('[name="interval_days"]') || {}).value || "");
      body.append("start_date", (form.querySelector('[name="start_date"]') || {}).value || "");
      body.append("tickers", field.value);
      // The last request wins: without this a slow early response can land
      // after a fast later one and show dates for a rotation you have moved on
      // from.
      if (inflight) { inflight.abort(); }
      inflight = new AbortController();
      fetch("/schedule/preview", { method: "POST", body: body, signal: inflight.signal })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) { if (data) { renderPreview(data); } })
        .catch(function () { /* aborted, or offline: the table simply stands */ });
    }, 250);
  }

  ["interval_days", "start_date"].forEach(function (name) {
    var input = form && form.querySelector('[name="' + name + '"]');
    if (input) { input.addEventListener("input", refreshPreview); }
  });

  function chip(ticker) {
    var li = document.createElement("li");
    li.className = "rotitem";
    li.dataset.ticker = ticker;
    li.innerHTML =
      '<span class="rotpos" aria-hidden="true"></span>' +
      '<span class="rotname"></span>' +
      '<span class="rotacts">' +
      '<button type="button" data-move="up" aria-label="Move ' + ticker + ' earlier">↑</button>' +
      '<button type="button" data-move="down" aria-label="Move ' + ticker + ' later">↓</button>' +
      '<button type="button" data-remove aria-label="Remove ' + ticker + ' from the rotation">×</button>' +
      "</span>";
    // textContent, not innerHTML: a ticker is user data and reaches this from
    // the database.
    li.querySelector(".rotname").textContent = ticker;
    return li;
  }

  function render(from) {
    list.textContent = "";
    parse(from).forEach(function (t) { list.appendChild(chip(t)); });
    sync();
  }

  /* ── adding, and dragging ────────────────────────────────────────────

     One mechanism for both, driven by pointer events rather than HTML5
     drag-and-drop, because the native API cannot do either of the two things
     that make this feel like moving an object:

       * the thing you picked up stays under the cursor — the native drag
         image is a translucent snapshot the browser positions itself; and
       * the others move aside to make room, because a real placeholder is
         inserted at the index you are hovering and the neighbours animate to
         their new places around it.

     Pointer events also cover touch, where HTML5 drag simply does not exist,
     and let a press that never moves fall through to a plain click. */

  var drag = null;   // { clone, placeholder, source, mode, ticker, offsetX, offsetY }

  function floatingCopy(el, ticker) {
    var box = el.getBoundingClientRect();
    var clone = el.cloneNode(true);
    clone.className = "rotchip rotfloat";
    clone.textContent = ticker;
    clone.style.width = box.width + "px";
    clone.style.height = box.height + "px";
    document.body.appendChild(clone);
    return clone;
  }

  function placeholderFor(height) {
    var li = document.createElement("li");
    li.className = "rotplace";
    li.style.height = height + "px";
    return li;
  }

  function moveClone(x, y) {
    drag.clone.style.transform =
      "translate(" + (x - drag.offsetX) + "px, " + (y - drag.offsetY) + "px)";
  }

  /* Where the placeholder belongs for a pointer at `y`: before the first item
     whose middle the pointer is above. Measured against real items only, so
     the placeholder never tries to position itself relative to itself. */
  function slotFor(y) {
    var items = list.querySelectorAll("li:not(.rotplace)");
    for (var i = 0; i < items.length; i++) {
      var box = items[i].getBoundingClientRect();
      if (y < box.top + box.height / 2) { return items[i]; }
    }
    return null;
  }

  function overList(x, y) {
    var box = list.getBoundingClientRect();
    var pad = 40;   // forgiving: aiming at a thin list is fiddly
    return x >= box.left - pad && x <= box.right + pad &&
           y >= box.top - pad && y <= box.bottom + pad;
  }

  function placeAt(y) {
    var before = slotFor(y);
    if (before === drag.placeholder) { return; }
    if (before && before.previousElementSibling === drag.placeholder) { return; }
    reflow(function () {
      if (before) { list.insertBefore(drag.placeholder, before); }
      else { list.appendChild(drag.placeholder); }
    });
  }

  function begin(source, ticker, mode, event) {
    var box = source.getBoundingClientRect();
    drag = {
      source: source, ticker: ticker, mode: mode,
      offsetX: event.clientX - box.left,
      offsetY: event.clientY - box.top,
      clone: floatingCopy(source, ticker),
      placeholder: placeholderFor(mode === "move" ? box.height : 34),
      moved: false,
    };
    moveClone(event.clientX, event.clientY);
    document.body.classList.add("rotdragging");
    if (mode === "move") {
      // The original leaves the layout; the placeholder is now its seat.
      list.insertBefore(drag.placeholder, source);
      source.hidden = true;
    }
  }

  function finish(commit) {
    if (!drag) { return; }
    var placeholder = drag.placeholder;
    var seated = placeholder.parentNode === list;
    drag.clone.remove();
    document.body.classList.remove("rotdragging");
    if (drag.mode === "move") {
      drag.source.hidden = false;
      if (commit && seated) { list.insertBefore(drag.source, placeholder); }
    } else if (commit && seated) {
      var li = chip(drag.ticker);
      list.insertBefore(li, placeholder);
      flash(li);
    }
    if (placeholder.parentNode) { placeholder.remove(); }
    drag = null;
    sync();
  }

  function onMove(e) {
    if (!drag) { return; }
    drag.moved = true;
    e.preventDefault();
    moveClone(e.clientX, e.clientY);
    if (overList(e.clientX, e.clientY)) {
      placeAt(e.clientY);
    } else if (drag.placeholder.parentNode && drag.mode === "add") {
      // Dragged back out: close the gap rather than promising a drop.
      reflow(function () { drag.placeholder.remove(); });
    }
  }

  function onUp() { finish(true); }

  document.addEventListener("pointermove", onMove);
  document.addEventListener("pointerup", onUp);
  document.addEventListener("pointercancel", function () { finish(false); });

  /* A press on a palette chip may become a drag; if it never moves, the
     button's own click handler adds to the end, which is what makes this
     keyboard- and touch-friendly without a second code path. */
  document.querySelectorAll(".rotsource .add").forEach(function (button) {
    button.addEventListener("click", function () {
      if (drag) { return; }             // the drag already placed it
      var li = chip(button.dataset.ticker);
      list.appendChild(li);
      sync();
      flash(li);
    });
    button.addEventListener("pointerdown", function (e) {
      if (e.button !== 0 && e.pointerType === "mouse") { return; }
      begin(button, button.dataset.ticker, "add", e);
    });
  });

  list.addEventListener("pointerdown", function (e) {
    if (e.target.closest("button")) { return; }   // the move/remove controls
    var li = e.target.closest("li.rotitem");
    if (!li) { return; }
    if (e.button !== 0 && e.pointerType === "mouse") { return; }
    begin(li, li.dataset.ticker, "move", e);
  });

  function flash(li) {
    if (still.matches || typeof li.animate !== "function") { return; }
    li.animate(
      [{ transform: "scale(0.9)", opacity: 0 }, { transform: "none", opacity: 1 }],
      { duration: 160, easing: "cubic-bezier(0.2, 0, 0, 1)" }
    );
  }

  /* ── the buttons on a chip ───────────────────────────────────────────── */

  list.addEventListener("click", function (e) {
    var li = e.target.closest("li");
    if (!li) { return; }
    if (e.target.closest("[data-remove]")) {
      reflow(function () { li.remove(); });
      sync();
      return;
    }
    var move = e.target.closest("[data-move]");
    if (!move) { return; }
    var flip = move.dataset.move === "up" ? li.previousElementSibling : li.nextElementSibling;
    if (!flip) { return; }
    reflow(function () {
      if (move.dataset.move === "up") { list.insertBefore(li, flip); }
      else { list.insertBefore(flip, li); }
    });
    sync();
    // Keep the keyboard on the button that just moved, or a run of presses
    // walks off the control.
    li.querySelector("[data-move='" + move.dataset.move + "']").focus();
  });

  /* Measure, mutate, invert, play — the same FLIP the charts grid uses. This
     is what makes the neighbours slide aside instead of jumping. */
  function reflow(mutate) {
    var items = Array.prototype.slice.call(list.children);
    if (still.matches || typeof Element.prototype.animate !== "function") {
      mutate();
      return;
    }
    var before = items.map(function (el) { return el.getBoundingClientRect(); });
    mutate();
    items.forEach(function (el, i) {
      if (!el.isConnected || el.hidden || (drag && el === drag.placeholder)) { return; }
      var last = el.getBoundingClientRect();
      var dx = before[i].left - last.left;
      var dy = before[i].top - last.top;
      if (!dx && !dy) { return; }
      el.animate(
        [{ transform: "translate(" + dx + "px, " + dy + "px)" }, { transform: "none" }],
        { duration: 180, easing: "cubic-bezier(0.2, 0, 0, 1)" }
      );
    });
  }

  /* ── back to text ────────────────────────────────────────────────────── */

  if (toggle) {
    toggle.addEventListener("click", function () {
      var showing = !field.hidden;
      field.hidden = showing;
      toggle.textContent = showing ? "Edit as text" : "Done editing";
      if (!showing) { field.focus(); }
      else { render(field.value); }     // text wins on the way back
    });
    // Typing in the box rebuilds the chips, so the two never disagree.
    field.addEventListener("change", function () { render(field.value); });
  }

  render(field.value);
})();
