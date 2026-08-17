/* The idle warning: blur the page, count down, offer a way back.
 *
 * The server is the authority on when a session dies. This file only decides
 * when to *warn*, and it re-syncs from /session/status rather than trusting its
 * own arithmetic — a laptop that slept, a clock that drifted, or a second tab
 * where you were busy would all make a purely local timer lie.
 *
 * Three things worth knowing before changing this:
 *
 *   1. /session/status is in auth.SLIDING_EXEMPT, so polling it does NOT keep
 *      the session alive. That is deliberate and load-bearing: if asking "how
 *      long have I got" reset the answer, a tab left open would never expire.
 *
 *   2. /session/keepalive is the one endpoint that does slide the window,
 *      because a button press is a person.
 *
 *   3. Activity is shared across tabs through localStorage. Typing in one tab
 *      must stop another from warning, or the app nags you about a session you
 *      are actively using.
 */
(function () {
  "use strict";

  const body = document.body;
  const overlay = document.getElementById("idlewarn");
  if (!overlay) return; // logged out — nothing to warn about

  const countdownEl = document.getElementById("idlewarn-countdown");
  const stayBtn = document.getElementById("idlewarn-stay");
  const outBtn = document.getElementById("idlewarn-out");
  const logoutForm = document.getElementById("idlewarn-logout");
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;

  const WARN_AT = Number(body.dataset.idleWarning || 120);
  const ACTIVITY_KEY = "pf:lastActivity";
  /* Poll often enough that the countdown is honest, rarely enough that it is
     not a load: every 30s normally, every 5s once the warning is up. */
  const POLL_IDLE = 30000;
  const POLL_WARNING = 5000;
  /* Only tell the server about activity this often, however much you type. */
  const ACTIVITY_THROTTLE = 60000;

  let secondsLeft = null;
  let warning = false;
  let pollTimer = null;
  let tickTimer = null;
  let lastReported = 0;

  const now = () => Date.now();

  /* ---------------------------------------------------------------- */
  /* Talking to the server                                             */
  /* ---------------------------------------------------------------- */

  async function poll() {
    try {
      const resp = await fetch("/session/status", {
        headers: { accept: "application/json" },
        cache: "no-store",
      });
      if (!resp.ok) return;
      const data = await resp.json();
      if (!data.authenticated) return expire();
      secondsLeft = data.seconds_left;
      if (secondsLeft <= WARN_AT) show();
      else hide();
    } catch (e) {
      /* Offline or the server is restarting. Keep the local countdown going
         rather than logging someone out over a dropped packet — the server
         will refuse the next real request anyway if the session is gone. */
    }
  }

  async function keepalive() {
    try {
      const resp = await fetch("/session/keepalive", {
        method: "POST",
        headers: { "X-CSRF-Token": csrf, accept: "application/json" },
      });
      if (!resp.ok) return expire();
      const data = await resp.json();
      secondsLeft = data.seconds_left;
      hide();
    } catch (e) {
      /* Leave the overlay up: we could not confirm, so we must not pretend. */
    }
  }

  /* ---------------------------------------------------------------- */
  /* The overlay                                                       */
  /* ---------------------------------------------------------------- */

  function show() {
    if (warning) return;
    warning = true;
    overlay.hidden = false;
    /* The same class the money-blur has always used, so charts, tables and
       notes all obscure themselves the way they already know how. */
    body.classList.add("hide-values");
    window.dispatchEvent(new Event("values-visibility-changed"));
    stayBtn.focus();
    schedule(POLL_WARNING);
  }

  function hide() {
    if (!warning) return;
    warning = false;
    overlay.hidden = true;
    body.classList.remove("hide-values");
    window.dispatchEvent(new Event("values-visibility-changed"));
    schedule(POLL_IDLE);
  }

  function expire() {
    /* Post the real logout so the row is destroyed, then land somewhere that
       explains itself. Never leave a dead-session page sitting there showing
       balances. */
    clearInterval(pollTimer);
    clearInterval(tickTimer);
    logoutForm.action = "/logout?next=" + encodeURIComponent("/login?timeout=1");
    logoutForm.submit();
  }

  function render() {
    if (secondsLeft === null) return;
    const s = Math.max(0, Math.floor(secondsLeft));
    countdownEl.textContent =
      Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  function tick() {
    if (secondsLeft === null) return;
    secondsLeft -= 1;
    if (secondsLeft <= WARN_AT) show();
    if (secondsLeft <= 0) return expire();
    if (warning) render();
  }

  function schedule(every) {
    clearInterval(pollTimer);
    pollTimer = setInterval(poll, every);
  }

  /* ---------------------------------------------------------------- */
  /* Activity                                                          */
  /* ---------------------------------------------------------------- */

  function onActivity() {
    /* Shared across tabs: being busy in one window must not let another warn
       you about the session you are using right now. */
    try {
      localStorage.setItem(ACTIVITY_KEY, String(now()));
    } catch (e) {
      /* Private browsing with storage denied — fall back to this tab only. */
    }
    maybeReport();
  }

  function maybeReport() {
    /* Real activity should extend the session, but not with a request per
       keystroke. One call a minute is enough to keep a working session alive,
       and the warning is what catches anything this misses. */
    if (warning) return; // while warning, only the button counts
    if (now() - lastReported < ACTIVITY_THROTTLE) return;
    lastReported = now();
    keepalive();
  }

  for (const evt of ["click", "keydown", "scroll", "pointerdown"]) {
    window.addEventListener(evt, onActivity, { passive: true });
  }

  window.addEventListener("storage", (e) => {
    /* Another tab saw activity. Trust it and re-check with the server. */
    if (e.key === ACTIVITY_KEY && warning) poll();
  });

  document.addEventListener("visibilitychange", () => {
    /* Coming back to a backgrounded tab: browsers throttle timers there, so
       the local count is probably wrong. Ask. */
    if (!document.hidden) poll();
  });

  stayBtn.addEventListener("click", keepalive);
  outBtn.addEventListener("click", () => logoutForm.submit());

  tickTimer = setInterval(tick, 1000);
  schedule(POLL_IDLE);
  poll();
})();
