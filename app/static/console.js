/* Tails the Settings page console.

   The panel is rendered server-side with its opening window; this appends what
   arrives after it. Appending rather than replacing is the point — the lines
   you were first shown stay where they are, so the console reads like a log
   and not like a dashboard that refreshes under you.

   Two behaviours that matter more than they sound:

   * **Only scroll if you are already at the bottom.** Yanking the view to the
     end while somebody is reading history is how a live console becomes
     unusable.
   * **Back off when nothing is happening.** A quiet install should not be
     asked every two seconds for the rest of the day. */
(function () {
  "use strict";

  var box = document.getElementById("console");
  var follow = document.getElementById("consolefollow");
  if (!box) { return; }

  var wait = 4000;
  var CEILING = 30000;

  function atBottom() {
    return box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  }

  function append(lines) {
    var wasAtBottom = atBottom();
    lines.forEach(function (line) {
      var row = document.createElement("div");
      row.className = "cline lvl-" + line.level.toLowerCase();
      // textContent throughout: a log line can quote a filename or a ticker,
      // and those arrive from outside.
      [["ctime", line.when], ["clevel", line.level],
       ["cname", line.logger], ["cmsg", line.message]].forEach(function (pair) {
        var span = document.createElement("span");
        span.className = pair[0];
        span.textContent = pair[1];
        row.appendChild(span);
        row.appendChild(document.createTextNode(" "));
      });
      box.appendChild(row);
      box.dataset.seq = line.seq;
    });
    if (wasAtBottom && follow && follow.checked) { box.scrollTop = box.scrollHeight; }
  }

  function poll() {
    fetch("/admin/logs.json?after=" + encodeURIComponent(box.dataset.seq || 0),
          { headers: { accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data && data.lines && data.lines.length) {
          append(data.lines);
          wait = 4000;            // something happened: pay attention again
        } else {
          wait = Math.min(Math.round(wait * 1.5), CEILING);
        }
        setTimeout(poll, wait);
      })
      .catch(function () {
        // Signed out, or the pod restarted. Keep trying, slowly.
        wait = Math.min(Math.round(wait * 2), CEILING);
        setTimeout(poll, wait);
      });
  }

  box.scrollTop = box.scrollHeight;
  setTimeout(poll, wait);
})();
