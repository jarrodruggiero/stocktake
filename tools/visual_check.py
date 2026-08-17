#!/usr/bin/env python
"""Render every page at a desktop AND a mobile width, and fail on layout faults.

    ../../.venv/bin/python tools/screenshot.py     # writes shots/*.html first
    ../../.venv/bin/python tools/visual_check.py   # then check them

**Why this exists.** Every layout bug so far was found by a person looking at a
page, and the ones that took longest were the ones that only exist at one
width — a calendar cropped to two days, a tab strip that pushed the document
sideways, a dropdown you could read the page through. All of them rendered
perfectly at 1400px, which is the only width anything was ever checked at.

So this is the thing that was missing: **the same pages, at two widths, with
the failure stated as a number rather than as an impression.**

## What it can and cannot catch

It runs a real browser and asks the layout three questions:

  * does the document scroll sideways? (`scrollWidth` > `clientWidth`)
  * if so, which elements cross the edge, and are they inside something that
    is supposed to scroll? A table in a `.tablewrap` is fine — that is the
    design. The same table loose on the page is not.
  * is any text rendered outside the box that is meant to contain it? That is
    the "a long figure in a 170px tile" fault, which does NOT widen the document
    and so slips past a scrollWidth check entirely.

It cannot tell you something is ugly. It tells you something is broken, which
is the part that reaches a real phone instead of being caught here.

Exit status is 1 if any page fails, so it can gate a release.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SHOTS = Path(__file__).resolve().parents[1] / "shots"

# The two widths that matter. 1400 is the layout's design width; 412 is a
# A Pixel-class phone in CSS pixels. Both are
# real viewport widths — see WRAPPER for why that took an iframe to arrange.
WIDTHS = {"desktop": 1400, "mobile": 412}

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FIREFOX = "/Applications/Firefox.app/Contents/MacOS/firefox"

# The probe. Runs in the page and writes its findings into the title, which is
# the one channel both browsers' headless modes will hand back.
PROBE = r"""
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
"""


# The page under test goes in an IFRAME of the wanted width, not in a window of
# it. `--window-size=412,900` is silently clamped: **headless Chrome will not go
# below 500px**, in every headless mode, so for as long as this file has claimed
# to check a Pixel-class phone it has been checking a 500px one. Everything
# between 412 and 500 — the width real phones report — was untested.
# Found 2026-08-11 by measuring `document.documentElement.clientWidth` inside
# the probe instead of trusting the flag.
#
# An iframe gets its own viewport, so media queries and `100vw` resolve to its
# width. Its document cannot write the outer title without
# `--allow-file-access-from-files`, since two `file://` documents are otherwise
# different origins.
WRAPPER = """<!doctype html><html><body style="margin:0">
<iframe id="f" src="%s" style="width:%dpx;height:900px;border:0"></iframe>
<script>
var frame = document.getElementById("f");
var tries = 0;
function collect() {
  var title = "";
  try { title = frame.contentDocument.title; } catch (e) { title = "BLOCKED " + e.message; }
  if (title.indexOf("VC") === 0 || title.indexOf("BLOCKED") === 0 || ++tries > 60) {
    document.title = title || "probe silent";
    return;
  }
  setTimeout(collect, 50);
}
frame.addEventListener("load", collect);
</script></body></html>
"""


def _probe_page(html: Path, width: int, browser: str) -> dict:
    """Render one page at one width and return the probe's findings."""
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        source = html.read_text()
        page.write_text(source.replace("</body>", f"<script>{PROBE}</script></body>"))
        wrapper = Path(tmp) / "wrapper.html"
        wrapper.write_text(WRAPPER % (page.name, width))

        if browser == "chrome":
            out = subprocess.run(
                [CHROME, "--headless", "--disable-gpu", "--dump-dom",
                 "--allow-file-access-from-files",
                 # Room for the frame plus the browser's own minimum.
                 f"--window-size={max(width + 80, 620)},960",
                 "--virtual-time-budget=6000",
                 wrapper.as_uri()],
                capture_output=True, text=True, timeout=90,
            ).stdout
        else:
            # Firefox headless has no --dump-dom, and its --screenshot fires at
            # load — so the probe writes into the title and we read the title
            # out of a saved DOM dump instead. Chrome is the default for that
            # reason; Firefox is available for confirming an engine difference.
            raise SystemExit("firefox mode: use screenshot comparison, not this probe")

        marker = out.find("VC{")
        if marker < 0:
            return {"error": "probe did not run"}
        end = out.find("</title>", marker)
        return json.loads(out[marker + 2:end])


def main() -> int:
    pages = sorted(SHOTS.glob("*.html"))
    # Only the pages the harness itself wrote — not the scratch files a
    # debugging session leaves behind.
    pages = [p for p in pages if p.name[0].isdigit()]
    if not pages:
        print("no shots found — run tools/screenshot.py first")
        return 1

    failures = 0
    for label, width in WIDTHS.items():
        print(f"\n══ {label} ({width}px)")
        for page in pages:
            try:
                report = _probe_page(page, width, "chrome")
            except Exception as exc:                      # noqa: BLE001
                print(f"  ?? {page.name}: {exc}")
                failures += 1
                continue
            if report.get("error"):
                print(f"  ?? {page.name}: {report['error']}")
                failures += 1
                continue
            bad = (report["overflow"] > 0 or report["wide"] or report["spill"]
                   or report.get("panels"))
            mark = "FAIL" if bad else "ok  "
            print(f"  {mark} {page.name}")
            if report["wide"]:
                print(f"        past the edge: {', '.join(report['wide'][:4])}")
            if report["spill"]:
                print(f"        text outside its box: {', '.join(report['spill'][:4])}")
            if report.get("panels"):
                print(f"        popups off screen: {', '.join(report['panels'][:4])}")
            failures += bool(bad)

    print(f"\n{failures} failing page/width combinations" if failures else "\nall pages pass at both widths")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
