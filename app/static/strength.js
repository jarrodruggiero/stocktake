/* How strong a new password is: a bar and one word, guidance only.

   zxcvbn estimates how many guesses a password would take from what people
   actually choose — words, names, dates, keyboard runs, the app's own name and
   the person's email — rather than counting character types. It is 800 KB, so
   it loads on the first keystroke, not with the page. It never stops a submit:
   the only rule is the length, which the form and the server hold. */
(function () {
  "use strict";

  var WORDS = ["Very weak", "Weak", "Fair", "Strong", "Very strong"];
  var loading = null;

  function load(src) {
    if (window.zxcvbn) { return Promise.resolve(window.zxcvbn); }
    if (!loading) {
      loading = new Promise(function (resolve, reject) {
        var script = document.createElement("script");
        script.src = src;
        script.onload = function () { resolve(window.zxcvbn); };
        script.onerror = reject;
        document.head.appendChild(script);
      });
    }
    return loading;
  }

  function show(meter, field) {
    if (!field.value) { meter.hidden = true; return; }
    var words = ["stocktake"].concat((meter.dataset.words || "").split(/\s+/));
    ["name", "email"].forEach(function (name) {
      var el = field.form.querySelector('[name="' + name + '"]');
      if (el && el.value) { words.push(el.value); }
    });
    load(meter.dataset.zxcvbn).then(function (zxcvbn) {
      if (!field.value) { meter.hidden = true; return; }
      var score = zxcvbn(field.value, words.filter(Boolean)).score;
      meter.dataset.score = score;
      meter.querySelector(".strengthword").textContent = WORDS[score];
      meter.hidden = false;
    }).catch(function () { meter.hidden = true; });
  }

  document.addEventListener("input", function (event) {
    var field = event.target;
    if (field.name !== "password" || !field.form) { return; }
    var meter = field.form.querySelector(".strength");
    if (meter) { show(meter, field); }
  });
})();
