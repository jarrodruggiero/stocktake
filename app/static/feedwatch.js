/* Whether somebody is in the middle of something a reload would throw away.
   Both watchers below ask it before reloading. */
var feedwatchBusy = (function () {
  "use strict";

  /* Reloading rather than patching the numbers in place: handlers are bound at
     load, so swapping the table out from under them would leave a page that
     looks right and no longer works. What a reload must never do is throw
     away what somebody was in the middle of. */
  return function busy() {
    /* Status menus are deliberately NOT counted: their whole content is what
       this reload updates, so treating them as "in use" would mean the thing
       you opened to watch is the thing that stops it moving. Every other menu
       is a choice somebody is part-way through. */
    if (document.querySelector("dialog[open], details.menu[open]:not(.statusmenu)")) {
      return true;
    }
    /* A closed dialog's fields are not in use, focused or not: focus can stay
       on one after the dialog closes, and opening it starts the form afresh. */
    var shut = "dialog:not([open])";
    var active = document.activeElement;
    if (active && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName)
        && !active.closest(shut)) { return true; }
    var fields = document.querySelectorAll("form input, form textarea, form select");
    for (var i = 0; i < fields.length; i++) {
      var el = fields[i];
      if (el.type === "hidden" || el.type === "submit" || el.disabled) { continue; }
      if (el.closest(shut)) { continue; }
      if (el.tagName === "SELECT") {
        /* Two traps, and either one makes this watcher a no-op.
           `defaultValue` on a select is `undefined` and never equals its
           value. And comparing `selected` to `defaultSelected` per option
           fails on the common case of a select whose markup names no
           `selected` option: the browser picks the first, so option 0 reads
           selected=true / defaultSelected=false on a page nobody has touched.
           The default is the first option marked selected, or option 0. */
        var def = 0;
        for (var j = 0; j < el.options.length; j++) {
          if (el.options[j].defaultSelected) { def = j; break; }
        }
        if (el.selectedIndex !== def) { return true; }
      } else if (el.type === "checkbox" || el.type === "radio") {
        if (el.checked !== el.defaultChecked) { return true; }
      } else if (el.value !== el.defaultValue) { return true; }
    }
    return window.getSelection && String(window.getSelection()).length > 0;
  };
})();

/* Watches the price feed while it is running.
 *
 * It ASKS rather than reloading on a timer, and reloads exactly once: when the
 * feed stops running. A blind `setTimeout(reload)` fires whether or not
 * anything has changed and throws away whatever you were reading — a sort
 * order, a half-filled Record trade dialog — every few seconds until the feed
 * happens to finish, and then waits until nothing on the page is in use.
 * Until then it shows a progress bar rather than nothing, because a refresh
 * that takes fifteen seconds with no sign of life reads as a broken button.
 */
(function () {
  "use strict";

  var body = document.body;
  if (!body || body.dataset.feedRunning !== "1") { return; }

  var bar = document.createElement("div");
  bar.className = "feedprogress";
  bar.setAttribute("role", "status");
  bar.setAttribute("aria-label", "Refreshing prices");
  body.appendChild(bar);

  /* Backs off: most refreshes finish in a couple of seconds, and a feed that
     is going to take a minute should not be asked thirty times. */
  var wait = 1500;
  var ceiling = 8000;
  var attempts = 0;

  function poll() {
    attempts++;
    fetch("/feed/status", { headers: { accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (state) {
        if (!state) { return schedule(); }
        if (state.running) { return schedule(); }
        /* Finished. Reload so the prices, totals and the chart all come from
           one render rather than being patched in three places — but not
           under a half-typed trade: it waits until nothing is in use. */
        reloadWhenFree();
      })
      .catch(function () {
        /* Offline, or the pod restarted mid-refresh. Keep asking: the page is
           still truthful, it just is not moving. */
        if (attempts < 40) { schedule(); } else { bar.remove(); }
      });
  }

  function reloadWhenFree() {
    if (feedwatchBusy()) { setTimeout(reloadWhenFree, 2000); return; }
    window.location.reload();
  }

  function schedule() {
    if (attempts > 60) { bar.remove(); return; }   // give up rather than poll forever
    wait = Math.min(Math.round(wait * 1.25), ceiling);
    setTimeout(poll, wait);
  }

  setTimeout(poll, wait);
})();

/* Watches for LIVE prices arriving, which is a different job from the above.
 *
 * The quote poll writes new prices into the database every few minutes, and a
 * page rendered before that has no way to know: the numbers on screen were
 * right when they were drawn and quietly are not any more. Reloading by hand
 * was the only way to see them, which makes a working refresh look broken.
 *
 * Runs on every page that renders the feed panel, not only during a refresh —
 * so `/feed/status` had to become sliding-exempt first, or a dashboard left
 * open would keep its own session alive for ever and the idle timeout would
 * be decorative.
 */
(function () {
  "use strict";

  var body = document.body;
  if (!body || !("quoted" in body.dataset)) { return; }

  var drawnWith = body.dataset.quoted;
  var EVERY = 60000;

  function look() {
    fetch("/feed/status", { headers: { accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (state) {
        if (state && state.quoted && state.quoted !== drawnWith && !feedwatchBusy()) {
          window.location.reload();
        }
      })
      .catch(function () { /* offline: the page is stale, not wrong */ });
  }

  setInterval(look, EVERY);
})();
