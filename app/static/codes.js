/* "Copy all" for the recovery codes: every code, one per line.

   The clipboard API exists only over HTTPS, and plenty of installs are plain
   HTTP on a home network, so without it the codes go through a hidden,
   selected textarea instead. If neither works the codes are still on screen,
   selectable, which is how they were copied before. */
(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy-codes]");
    if (!button) { return; }
    var codes = Array.prototype.map.call(
      document.querySelectorAll(".codes code"),
      function (code) { return code.textContent.trim(); }).join("\n");
    var label = button.textContent;

    function copied() {
      button.textContent = "Copied";
      setTimeout(function () { button.textContent = label; }, 2000);
    }

    function fallback() {
      var box = document.createElement("textarea");
      box.value = codes;
      box.setAttribute("readonly", "");
      box.style.position = "fixed";
      box.style.opacity = "0";
      document.body.appendChild(box);
      box.focus();
      box.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(box);
      button.focus();
      if (ok) { copied(); }
    }

    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(codes).then(copied, fallback);
    } else {
      fallback();
    }
  });
})();
