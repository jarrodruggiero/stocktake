/* The theme designer's tabs, one per mode, the mode in use open first.
 *
 * Both lists are in the one form, so either mode's colours can be changed
 * without switching the page to it. With no script the tab strip stays hidden
 * and both lists show, each under its name.
 */
(function () {
  "use strict";

  var strip = document.querySelector(".modetabs");
  if (!strip) { return; }
  var tabs = Array.prototype.slice.call(strip.querySelectorAll('[role="tab"]'));

  function select(tab, focus) {
    tabs.forEach(function (each) {
      var on = each === tab;
      each.setAttribute("aria-selected", on ? "true" : "false");
      each.tabIndex = on ? 0 : -1;
      document.getElementById(each.getAttribute("aria-controls")).hidden = !on;
    });
    if (focus) { tab.focus(); }
  }

  tabs.forEach(function (tab, i) {
    var panel = document.getElementById(tab.getAttribute("aria-controls"));
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", tab.id);
    tab.addEventListener("click", function () { select(tab, false); });
    // Arrows move between tabs and Home and End go to either end, as a tab
    // strip does everywhere else.
    tab.addEventListener("keydown", function (e) {
      var next = {ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1}[e.key];
      if (next === undefined) { return; }
      e.preventDefault();
      select(tabs[(next + tabs.length) % tabs.length], true);
    });
  });

  // The person's choice, or for "auto" what the device is showing now.
  var theme = document.body.dataset.theme;
  var mode = theme === "light" || theme === "dark" ? theme
    : (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  strip.closest("form").classList.add("tabbed");
  strip.hidden = false;
  select(document.getElementById("tab-" + mode) || tabs[0], false);
})();
