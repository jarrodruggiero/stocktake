/* The theme designer's two-faced control.
 *
 * A wheel and a hex box are one setting shown two ways, and each has to write
 * to the other or they drift apart while you are looking at them. The TEXT box
 * is the one that submits — `<input type="color">` cannot express "no value",
 * and "leave it blank for the default" is the behaviour that keeps a partial
 * choice from becoming an all-or-nothing switch.
 */
(function () {
  "use strict";

  function normalise(value) {
    var v = (value || "").trim();
    if (/^[0-9a-fA-F]{3}$|^[0-9a-fA-F]{6}$/.test(v)) { v = "#" + v; }  // paste without the #
    if (!/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(v)) { return null; }
    if (v.length === 4) {
      v = "#" + v[1] + v[1] + v[2] + v[2] + v[3] + v[3];
    }
    return v.toLowerCase();
  }

  document.querySelectorAll(".swatchwheel").forEach(function (wheel) {
    var hex = document.getElementById(wheel.dataset.for);
    if (!hex) { return; }

    // Wheel -> box. `input` rather than `change` so dragging updates live.
    wheel.addEventListener("input", function () { hex.value = wheel.value; });

    // Box -> wheel, but only once what is typed is a colour: repainting the
    // wheel from a half-typed "#a2" would make it flicker through nonsense.
    hex.addEventListener("input", function () {
      var v = normalise(hex.value);
      if (v) { wheel.value = v; }
    });

    // Tidy the box on the way out: "ABC" becomes "#aabbcc", which is what gets
    // stored, so what you see is what was saved.
    hex.addEventListener("blur", function () {
      var v = normalise(hex.value);
      if (v) { hex.value = v; }
    });
  });
})();
