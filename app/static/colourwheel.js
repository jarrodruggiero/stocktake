/* A colour wheel, ours rather than the operating system's.
 *
 * `<input type="color">` opens whatever the platform supplies — the Windows
 * picker on Windows, the Android one on a phone. Three different experiences
 * of the same setting, none of which we can style, and two of which look
 * nothing like the rest of the app. So this draws the wheel.
 *
 * ## What the wheel shows
 *
 * Hue around the circumference, saturation along the radius, drawn once into a
 * canvas at device resolution. Value (brightness) is a separate slider,
 * because a disc cannot show three dimensions and brightness is the one people
 * reach for last.
 *
 * ## The hex box is still the field
 *
 * The wheel writes to it and it writes back to the wheel; the FORM only ever
 * submits the text. That is what keeps "leave it empty for the default"
 * expressible — a wheel has no way to say "nothing".
 *
 * ## Accessibility
 *
 * A canvas is invisible to a screen reader and unreachable by keyboard, so the
 * wheel is `aria-hidden` and the hex box carries the label and the value. The
 * wheel is an enhancement on top of a text field, not a replacement for one —
 * which is also why it degrades to nothing if the canvas fails.
 */
(function () {
  "use strict";

  var SIZE = 176;          // CSS pixels; the canvas is drawn at dpr multiples
  var open = null;         // the one popover on screen, if any

  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

  function hsvToRgb(h, s, v) {
    var c = v * s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), m = v - c;
    var r = 0, g = 0, b = 0;
    if (h < 60) { r = c; g = x; }
    else if (h < 120) { r = x; g = c; }
    else if (h < 180) { g = c; b = x; }
    else if (h < 240) { g = x; b = c; }
    else if (h < 300) { r = x; b = c; }
    else { r = c; b = x; }
    return [Math.round((r + m) * 255), Math.round((g + m) * 255), Math.round((b + m) * 255)];
  }

  function rgbToHsv(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    var max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
    var h = 0;
    if (d) {
      if (max === r) { h = 60 * (((g - b) / d) % 6); }
      else if (max === g) { h = 60 * ((b - r) / d + 2); }
      else { h = 60 * ((r - g) / d + 4); }
    }
    if (h < 0) { h += 360; }
    return [h, max ? d / max : 0, max];
  }

  function toHex(rgb) {
    return "#" + rgb.map(function (v) {
      return clamp(v, 0, 255).toString(16).padStart(2, "0");
    }).join("");
  }

  function parseHex(value) {
    var v = (value || "").trim().replace(/^#/, "");
    if (v.length === 3) { v = v[0] + v[0] + v[1] + v[1] + v[2] + v[2]; }
    if (!/^[0-9a-fA-F]{6}$/.test(v)) { return null; }
    return [parseInt(v.slice(0, 2), 16), parseInt(v.slice(2, 4), 16), parseInt(v.slice(4, 6), 16)];
  }

  /* Drawn per pixel rather than with conic + radial gradients stacked, because
     the stacked version disagrees between engines about where the hues land —
     and a wheel whose red is in a different place on someone else's machine is
     worse than one that takes a few milliseconds to paint. Cached per value,
     so dragging the brightness slider is the only thing that repaints. */
  function paint(canvas, value) {
    var dpr = window.devicePixelRatio || 1;
    var px = Math.round(SIZE * dpr);
    canvas.width = px;
    canvas.height = px;
    var ctx = canvas.getContext("2d");
    var image = ctx.createImageData(px, px);
    var data = image.data;
    var r0 = px / 2;
    for (var y = 0; y < px; y++) {
      for (var x = 0; x < px; x++) {
        var dx = x - r0, dy = y - r0;
        var dist = Math.sqrt(dx * dx + dy * dy);
        var i = (y * px + x) * 4;
        if (dist > r0) { data[i + 3] = 0; continue; }
        var hue = (Math.atan2(dy, dx) * 180 / Math.PI + 360) % 360;
        var rgb = hsvToRgb(hue, clamp(dist / r0, 0, 1), value);
        data[i] = rgb[0]; data[i + 1] = rgb[1]; data[i + 2] = rgb[2];
        // Feather the last pixel so the disc edge is not a staircase.
        data[i + 3] = dist > r0 - 1 ? Math.round(255 * (r0 - dist)) : 255;
      }
    }
    ctx.putImageData(image, 0, 0);
  }

  function build(field, swatch) {
    var pop = document.createElement("div");
    pop.className = "cwheel panel";
    pop.innerHTML =
      '<canvas class="cwheel-disc" width="176" height="176" aria-hidden="true"></canvas>' +
      '<div class="cwheel-marker" aria-hidden="true"></div>' +
      '<label class="cwheel-value">Brightness' +
      '<input type="range" min="0" max="100" value="100" class="cwheel-slider"></label>' +
      '<div class="cwheel-foot">' +
      '<span class="cwheel-preview" aria-hidden="true"></span>' +
      '<button type="button" class="secondary cwheel-clear">Use default</button>' +
      '<button type="button" class="cwheel-done">Done</button>' +
      "</div>";

    var canvas = pop.querySelector(".cwheel-disc");
    var marker = pop.querySelector(".cwheel-marker");
    var slider = pop.querySelector(".cwheel-slider");
    var preview = pop.querySelector(".cwheel-preview");
    var hsv = [0, 0, 1];

    function reflect(hex) {
      preview.style.background = hex;
      swatch.style.background = hex;
    }

    function place() {
      var rad = (hsv[0] * Math.PI) / 180;
      var r = (SIZE / 2) * hsv[1];
      marker.style.left = (SIZE / 2 + Math.cos(rad) * r) + "px";
      marker.style.top = (SIZE / 2 + Math.sin(rad) * r) + "px";
    }

    function push() {
      var hex = toHex(hsvToRgb(hsv[0], hsv[1], hsv[2]));
      field.value = hex;
      field.dispatchEvent(new Event("input", { bubbles: true }));
      reflect(hex);
      place();
    }

    function pull() {
      /* With nothing chosen, the marker sits on the DEFAULT rather than at the
         centre. Somebody nudging a colour slightly needs to see where it
         currently sits: a marker parked in the middle says the colour is grey,
         which is not what the app is drawing, and gives nothing to nudge from.

         The field stays empty either way. Showing where a colour is and
         claiming it has been set are different things, and only moving the
         wheel does the second. */
      var rgb = parseHex(field.value) || parseHex(field.placeholder);
      if (rgb) {
        hsv = rgbToHsv(rgb[0], rgb[1], rgb[2]);
        slider.value = Math.round(hsv[2] * 100);
        reflect(toHex(rgb));
      }
      paint(canvas, hsv[2] || 1);
      place();
    }

    function fromPointer(e) {
      var box = canvas.getBoundingClientRect();
      var dx = e.clientX - box.left - SIZE / 2;
      var dy = e.clientY - box.top - SIZE / 2;
      var dist = Math.sqrt(dx * dx + dy * dy);
      hsv[0] = (Math.atan2(dy, dx) * 180 / Math.PI + 360) % 360;
      hsv[1] = clamp(dist / (SIZE / 2), 0, 1);
      push();
    }

    var dragging = false;
    canvas.addEventListener("pointerdown", function (e) {
      dragging = true;
      canvas.setPointerCapture(e.pointerId);
      fromPointer(e);
    });
    canvas.addEventListener("pointermove", function (e) { if (dragging) { fromPointer(e); } });
    canvas.addEventListener("pointerup", function () { dragging = false; });

    slider.addEventListener("input", function () {
      hsv[2] = slider.value / 100;
      paint(canvas, hsv[2]);
      push();
    });

    pop.querySelector(".cwheel-clear").addEventListener("click", function () {
      field.value = "";
      field.dispatchEvent(new Event("input", { bubbles: true }));
      pull();     // marker back onto the default, not left where it was dragged
    });
    pop.querySelector(".cwheel-done").addEventListener("click", close);

    pop.pull = pull;
    return pop;
  }

  function close() {
    if (open) { open.remove(); open = null; }
    document.removeEventListener("pointerdown", outside, true);
    document.removeEventListener("keydown", onKey, true);
  }

  function outside(e) {
    if (open && !open.contains(e.target) && !e.target.closest(".swatchwheel")) { close(); }
  }

  function onKey(e) { if (e.key === "Escape") { close(); } }

  document.querySelectorAll(".swatchwheel").forEach(function (swatch) {
    var field = document.getElementById(swatch.dataset.for);
    if (!field) { return; }

    // The stored colour is what the button shows, so the row reads at a glance
    // without opening anything.
    var initial = parseHex(field.value);
    swatch.style.background = initial ? toHex(initial) : (field.placeholder || "#888888");

    swatch.addEventListener("click", function () {
      if (open && open.dataset.token === field.id) { close(); return; }
      close();
      var pop = build(field, swatch);
      pop.dataset.token = field.id;
      swatch.parentNode.appendChild(pop);
      open = pop;
      pop.pull();
      // Registered late and captured, so the click that opened it does not
      // immediately close it again.
      setTimeout(function () {
        document.addEventListener("pointerdown", outside, true);
        document.addEventListener("keydown", onKey, true);
      }, 0);
    });

    // Typing in the box keeps the button's colour honest even with the wheel
    // shut.
    field.addEventListener("input", function () {
      var rgb = parseHex(field.value);
      swatch.style.background = rgb ? toHex(rgb) : (field.placeholder || "#888888");
    });
  });
})();
