/* Behaviour for the first-run wizard.
 *
 * Three unrelated jobs in one file because they belong to one flow and each is
 * a dozen lines. Every one of them is an enhancement: the wizard works with
 * JavaScript off — both sets of database fields are visible, the timezone is a
 * text box with a datalist, and the restart page tells you where to go.
 */
(function () {
  "use strict";

  /* ---- database step: show the fields for the chosen kind --------------- */
  var kinds = document.querySelectorAll('input[name="kind"][data-shows]');
  if (kinds.length) {
    var show = function () {
      kinds.forEach(function (radio) {
        var panel = document.getElementById(radio.dataset.shows);
        if (panel) { panel.hidden = !radio.checked; }
      });
    };
    kinds.forEach(function (radio) { radio.addEventListener("change", show); });
    show();
  }

  /* ---- portfolio step: the browser already knows the timezone ----------- */
  /* Intl needs no permission prompt, unlike geolocation — it is the zone the
     operating system is already set to, which is the answer we want anyway. */
  var detect = document.getElementById("tzdetect");
  if (detect) {
    detect.addEventListener("click", function () {
      var found = null;
      try {
        found = Intl.DateTimeFormat().resolvedOptions().timeZone;
      } catch (e) {
        found = null;
      }
      var note = document.getElementById("tzfound");
      var input = document.getElementById("tzinput");
      if (found && input) {
        input.value = found;
        if (note) {
          note.textContent = "Your browser says " + found + ".";
          note.hidden = false;
        }
      } else if (note) {
        note.textContent = "Your browser would not say. Choose one from the list.";
        note.hidden = false;
      }
    });
  }

  /* ---- restart page: wait for it to come back --------------------------- */
  var card = document.getElementById("restarting");
  if (card) {
    var target = card.dataset.target || "/";
    var state = document.getElementById("restart-state");
    var wentDown = false;
    var say = function (text) { if (state) { state.textContent = text; } };

    /* Polls THIS origin, not the target, and deliberately so: the target may
       be a different host, and a cross-origin request is blocked by the
       page's own connect-src policy long before CORS gets a say. The signal
       is still real — the process serving this page is the process that has
       to come back, so "gone, then answering again" is exactly the event we
       are waiting for. */
    /* Four stages, not two, and the extra one is the point: `/healthz` says the
       process is answering, `/readyz` says the database is migrated and open.
       Between them is where a bad restart actually gets stuck — the process
       comes back, migrations fail, and it answers health checks forever while
       never becoming usable.

       Reporting only "waiting" through that would leave somebody watching a
       spinner with no idea which half had failed. Usually all of this flicks
       past unread; the case it exists for is the one where it does not. */
    var poll = function () {
      fetch("/healthz", { cache: "no-store" })
        .then(function (resp) {
          if (!resp.ok) { throw new Error("not answering"); }
          if (!wentDown) {
            say("Waiting for the application to stop…");
            return;
          }
          /* Up again. Ready is a second question. */
          return fetch("/readyz", { cache: "no-store" }).then(function (ready) {
            if (ready.ok) {
              say("Ready — taking you there.");
              window.location.href = target;
            } else {
              say("Started. Preparing the database…");
            }
          });
        })
        .catch(function () {
          wentDown = true;
          say("Stopped. Waiting for it to start again…");
        });
    };

    setInterval(poll, 1000);
    poll();
  }
})();
