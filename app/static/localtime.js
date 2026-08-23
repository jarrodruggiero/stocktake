/* Show trade times on this device's clock.
 *
 * Progressive enhancement, deliberately: the server renders the exchange's own
 * time, and this rewrites it only when the account asked for local. With
 * JavaScript off you still get a correct time, just not yours.
 *
 * Done here rather than on the server because the browser knows where it
 * actually is. A configured timezone is wrong the moment you travel, and the
 * conversion needs the trade's DATE to pick the right offset — an ASX trade in
 * January and one in July are an hour apart.
 */
(function () {
  if (document.body.dataset.times !== "local") return;

  var fmt = new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit" });

  document.querySelectorAll("time[data-market]").forEach(function (el) {
    var stamp = el.getAttribute("datetime");
    if (!stamp) return;
    var when = new Date(stamp);
    if (isNaN(when)) return;

    var local = fmt.format(when);
    if (local === el.textContent.trim()) return;  // same clock, nothing to say

    el.textContent = local;
    /* Name the zone, because a time that silently differs from the exchange's
       is worse than no time at all. */
    var zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    el.title = el.getAttribute("data-market") + " " + zone;
  });
})();
