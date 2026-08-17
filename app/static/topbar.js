/* Top-bar behaviour: the portfolio switcher, and closing the account menu.

   Behaviour lives here rather than in an inline `onchange`, because every
   inline handler is what holds `'unsafe-inline'` open in the CSP. Moving them
   out is a security change, not tidying — see `main.security_headers`. */
(function () {
  "use strict";

  /* Switching portfolio submits; "+ New portfolio…" is not a portfolio, so it
     navigates instead and puts the select back where it was — otherwise the
     box would sit there naming a portfolio you are not in. */
  var pf = document.getElementById("pfswitch");
  if (pf) {
    pf.addEventListener("change", function () {
      if (pf.value === "new") {
        pf.value = pf.dataset.current || "";
        location.href = "/admin#new";
        return;
      }
      pf.form.submit();
    });
  }

  /* A <details> menu stays open until you toggle it again, which is fine for
     the column chooser sitting inside the page and wrong for a menu floating
     over it: you click a page behind it and the menu is still there. Close on
     an outside click and on Escape.

     Both are progressive — with scripting off the menu still opens and closes
     from its own summary, which is why it is a <details> in the first place.

     ## Why <details> and not the popover API

     `<button popovertarget>` + `[popover]` is the obvious choice for these
     floating panels, for the free Esc/light-dismiss/top-layer behaviour. It
     does not work here: a `[popover]` lives in the top layer, so it is
     positioned against the VIEWPORT, and pinning it to the control that opened
     it needs CSS anchor positioning — which Firefox
     does not ship yet. A menu that lands in the corner of the screen instead
     of under its button is worse than one that needs twelve lines of script.

     So: one pattern, `details.menu`, everywhere — the account menu, the feed
     status, and anything later. Every panel that floats gets this behaviour by
     wearing the class, and none of them reimplement it. */
  var menus = document.querySelectorAll("details.menu");
  if (!menus.length) { return; }

  /* ── Keeping an open panel on the screen ────────────────────────────────
     Every one of these panels is `position: absolute` anchored `right: 0` to
     its own control, so it opens LEFTWARDS. That is right for a control on
     the right of the page and catastrophic for one on the left: the feed tick
     sits ~20px in from the edge, so its 20rem panel started 286px off the left
     of a 412px phone and could not be read at all. Reported from a phone
     2026-08-11; `tools/visual_check.py` now opens every menu and measures both
     edges, which is what turns this from something you notice into something
     that fails.

     Why measure at runtime rather than write more CSS: which way a panel must
     open depends on where its control lands after the page has laid out, and
     that changes with the viewport, the text, and the length of a portfolio
     name. CSS can cap the WIDTH — it does, see style.css — but only the
     browser knows where the thing ended up. The two `.tip.left` / `.tip.right`
     modifier classes were the previous answer to this, and they are a person
     guessing per instance.

     Progressive, like the rest of this file: with scripting off the panels
     open exactly where the stylesheet puts them, which is where they open
     today. Nothing here is load-bearing for reading the page. */
  var EDGE = 8;   // breathing room between a panel and the window edge

  /* The ancestor that would crop this panel, or null. A panel is absolutely
     positioned inside its control, so a scrolling box anywhere above it clips
     the panel to itself — the holdings table's "why can't I remove this"
     explanation lives inside `.tablewrap`, and was being cut off as well as
     pushed off. */
  function cropper(panel, rect) {
    for (var p = panel.parentElement; p && p !== document.body; p = p.parentElement) {
      var style = getComputedStyle(p);
      if (style.overflowX === "visible" && style.overflowY === "visible") { continue; }
      var box = p.getBoundingClientRect();
      return (rect.left < box.left - 1 || rect.right > box.right + 1 ||
              rect.bottom > box.bottom + 1) ? p : null;
    }
    return null;
  }

  function place(menu) {
    var panel = menu.querySelector("summary ~ *");
    if (!panel) { return; }
    /* Always start from the stylesheet's own answer, so reopening at a
       different width does not inherit the last one's nudge. */
    panel.style.position = "";
    panel.style.top = panel.style.left = panel.style.right = "";
    panel.style.transform = "";
    if (!menu.open) { return; }

    var rect = panel.getBoundingClientRect();
    if (rect.width === 0) { return; }

    /* Cropped: leave the flow entirely and pin it under its own control.
       `fixed` is not the default because with scripting off it would land in
       the corner of the window rather than under the control that opened it —
       the same objection that kept these off the popover API. */
    if (cropper(panel, rect)) {
      var summary = menu.querySelector("summary").getBoundingClientRect();
      panel.style.position = "fixed";
      panel.style.right = "auto";
      panel.style.top = Math.round(summary.bottom + 5) + "px";
      panel.style.left = Math.round(summary.right - panel.offsetWidth) + "px";
      rect = panel.getBoundingClientRect();
    }

    var window_width = document.documentElement.clientWidth;
    var shift = 0;
    if (rect.left < EDGE) { shift = EDGE - rect.left; }
    else if (rect.right > window_width - EDGE) {
      shift = (window_width - EDGE) - rect.right;
    }
    if (shift) { panel.style.transform = "translateX(" + Math.round(shift) + "px)"; }
  }

  menus.forEach(function (menu) {
    menu.addEventListener("toggle", function () { place(menu); });
    if (menu.open) { place(menu); }
  });

  /* A `fixed` panel is pinned to the WINDOW, so it has to be re-placed when
     the page moves under it; a nudged one has to be re-measured when the
     window changes size. Same call answers both. */
  function replace_open() {
    menus.forEach(function (menu) { if (menu.open) { place(menu); } });
  }
  window.addEventListener("resize", replace_open);
  window.addEventListener("scroll", replace_open, true);

  document.addEventListener("click", function (event) {
    menus.forEach(function (menu) {
      if (menu.open && !menu.contains(event.target)) { menu.open = false; }
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") { return; }
    menus.forEach(function (menu) {
      if (!menu.open) { return; }
      menu.open = false;
      /* Focus goes back to the control that opened it, or it lands at the top
         of the document and a keyboard user has to tab back in. */
      var summary = menu.querySelector("summary");
      if (summary) { summary.focus(); }
    });
  });
})();
