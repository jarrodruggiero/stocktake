/* The probe. Runs inside the page under test and writes its findings
   into the document title, which is the one channel headless Chrome
   will hand back through --dump-dom. Read by tests/test_visual.py. */
document.addEventListener("DOMContentLoaded", function () {
  var doc = document.documentElement;
  var report = { overflow: doc.scrollWidth - doc.clientWidth,
                 wide: [], spill: [], panels: [] };

  /* An element crossing the viewport edge is only a fault if nothing between
     it and the page is a scroll container. `.tablewrap` exists precisely so a
     twelve-column table can scroll on its own; that is a design decision, not
     a bug, and a check that cannot tell the two apart gets switched off. */
  function scrollableAncestor(el) {
    for (var p = el.parentElement; p; p = p.parentElement) {
      var o = getComputedStyle(p).overflowX;
      if (o === "auto" || o === "scroll" || o === "hidden") return true;
    }
    return false;
  }

  document.querySelectorAll("body *").forEach(function (el) {
    var r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > doc.clientWidth + 1 && !scrollableAncestor(el)) {
      report.wide.push(el.tagName.toLowerCase() +
        (el.className && typeof el.className === "string"
          ? "." + el.className.split(" ")[0] : "") +
        "@" + Math.round(r.right));
    }
  });

  /* Text outside its own box. A 29px number in a 170px tile does not widen the
     document — the tile clips or the glyphs simply hang out — so the overflow
     check above never sees it. Compare each leaf's scroll width against its
     client width instead. */
  document.querySelectorAll(".tile, .m-tile, .chartcard, .panel, button, .fytabs a")
    .forEach(function (el) {
      if (el.scrollWidth > el.clientWidth + 2 &&
          getComputedStyle(el).overflowX === "visible") {
        report.spill.push(el.tagName.toLowerCase() +
          (el.className && typeof el.className === "string"
            ? "." + el.className.split(" ")[0] : "") +
          " content=" + el.scrollWidth + " box=" + el.clientWidth);
      }
    });

  /* ── the floating panels ──────────────────────────────────────────────
     Everything above measures the page as it FIRST renders, which means
     every <details class="menu"> is shut and has no geometry at all. So the
     checks could not see the feed-status popup opening 284px off the left
     edge of a phone: the panel anchors `right: 0` to a control that sits at
     the far LEFT of the page, and 20rem of panel then extends backwards past
     the window. Reported from a phone — which is the
     failure this whole harness exists to stop.

     Two reasons it slipped through, and both are fixed here rather than by
     remembering to look: the panels were never opened, and the overflow test
     only ever asked about the RIGHT edge (`r.right > clientWidth`). A panel
     that runs off the left is just as unreadable and does not widen the
     document, so nothing else would ever notice it.

     Clipping is checked too. A panel is position:absolute inside its control,
     so an ancestor that scrolls (`.tablewrap`) crops it — the holdings page
     puts a "why can't I remove this" panel inside exactly that. Off-screen
     and cropped are different faults with the same symptom. */
  var opened = document.querySelectorAll("details.menu");
  opened.forEach(function (m) { m.open = true; });

  /* Measured a tick later, ON PURPOSE. Setting `.open` from script QUEUES the
     `toggle` event rather than dispatching it, and topbar.js does its
     keep-it-on-screen placement in that handler — so measuring on this turn of
     the loop would report the unplaced geometry and the check would fail
     against a page that is actually correct in a browser. */
  setTimeout(function () { measure_panels(); }, 60);

  function measure_panels() {
  opened.forEach(function (menu) {
    var panel = menu.querySelector("summary ~ *");
    if (!panel) { return; }
    var r = panel.getBoundingClientRect();
    if (r.width === 0) { return; }
    var name = (menu.className || "").split(" ").filter(function (c) {
      return c !== "menu";
    })[0] || "menu";
    if (r.left < -1) {
      report.panels.push(name + " starts at " + Math.round(r.left));
    } else if (r.right > doc.clientWidth + 1) {
      report.panels.push(name + " ends at " + Math.round(r.right) +
                         " (window " + doc.clientWidth + ")");
    }
    /* A `fixed` panel has left the flow, so no ancestor's overflow can crop
       it — that is exactly how topbar.js gets the holdings table's panel out
       of `.tablewrap`. Testing containment against an ancestor it is no longer
       laid out inside reports every escape as the fault it just fixed. */
    if (getComputedStyle(panel).position === "fixed") { return; }
    for (var p = panel.parentElement; p && p !== document.body; p = p.parentElement) {
      var style = getComputedStyle(p);
      if (style.overflowX === "visible" && style.overflowY === "visible") { continue; }
      var box = p.getBoundingClientRect();
      if (r.left < box.left - 1 || r.right > box.right + 1 || r.bottom > box.bottom + 1) {
        report.panels.push(name + " is cropped by ." +
                           (p.className || p.tagName).split(" ")[0]);
      }
      break;
    }
  });

  opened.forEach(function (m) { m.open = false; });
  document.title = "VC" + JSON.stringify(report);
  }

  /* Nothing to open: report immediately rather than waiting on a timer that
     has no work to do. */
  if (!opened.length) { document.title = "VC" + JSON.stringify(report); }
});
