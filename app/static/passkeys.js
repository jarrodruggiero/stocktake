/* The browser half of both WebAuthn ceremonies.
 *
 * Each is: ask the server for options, hand them to the authenticator, post
 * back what it signed. The only real work here is base64url — WebAuthn speaks
 * ArrayBuffers and JSON does not, so every buffer crosses the wire encoded and
 * has to be decoded again on the way in.
 *
 * There is no no-JavaScript path, and that is not an omission: the ceremony IS
 * `navigator.credentials`, so a browser without it cannot hold a passkey at
 * all. Both pages say so rather than showing a button that does nothing.
 */
(function () {
  "use strict";

  function fromB64(value) {
    var padded = value.replace(/-/g, "+").replace(/_/g, "/");
    while (padded.length % 4) padded += "=";
    var raw = atob(padded);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes.buffer;
  }

  function toB64(buffer) {
    var bytes = new Uint8Array(buffer), raw = "";
    for (var i = 0; i < bytes.length; i++) raw += String.fromCharCode(bytes[i]);
    return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  /* Only the fields WebAuthn defines as buffers are decoded. Walking the whole
     object and guessing by shape would eventually decode a name. */
  function decodeOptions(options) {
    options.challenge = fromB64(options.challenge);
    if (options.user) options.user.id = fromB64(options.user.id);
    ["excludeCredentials", "allowCredentials"].forEach(function (key) {
      (options[key] || []).forEach(function (descriptor) {
        descriptor.id = fromB64(descriptor.id);
      });
    });
    return options;
  }

  function encodeCredential(credential) {
    var r = credential.response;
    var out = {
      id: credential.id,
      rawId: toB64(credential.rawId),
      type: credential.type,
      response: {clientDataJSON: toB64(r.clientDataJSON)},
    };
    if (r.attestationObject) out.response.attestationObject = toB64(r.attestationObject);
    if (r.authenticatorData) out.response.authenticatorData = toB64(r.authenticatorData);
    if (r.signature) out.response.signature = toB64(r.signature);
    if (r.userHandle) out.response.userHandle = toB64(r.userHandle);
    return JSON.stringify(out);
  }

  function post(url, data) {
    var body = new FormData();
    Object.keys(data).forEach(function (k) { body.append(k, data[k]); });
    return fetch(url, {method: "POST", body: body, credentials: "same-origin"});
  }

  /* A cancelled prompt is not an error worth shouting about: pressing Escape
     is a decision, and it arrives as the same exception a real failure does. */
  function isCancellation(err) {
    return err && (err.name === "NotAllowedError" || err.name === "AbortError");
  }

  function supported() {
    return !!(window.PublicKeyCredential && navigator.credentials);
  }

  /* ── Enrolling, on the account's passkey page ── */
  var addForm = document.getElementById("addpasskey");
  if (addForm) {
    var unsupported = document.getElementById("nopasskeys");
    if (!supported() && unsupported) {
      unsupported.hidden = false;
      addForm.hidden = true;
    }
    addForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var button = addForm.querySelector("button");
      var problem = document.getElementById("passkeyerror");
      var csrf = addForm.querySelector('[name="_csrf"]').value;
      button.disabled = true;
      post("/profile/passkeys/options", {_csrf: csrf})
        .then(function (r) {
          if (!r.ok) throw new Error("This app cannot offer passkeys right now.");
          return r.json();
        })
        .then(function (data) {
          return navigator.credentials.create({
            publicKey: decodeOptions(data.options),
          }).then(function (credential) {
            /* A form POST rather than fetch: the answer is a redirect to a
               page with a flash message, and letting the browser follow it
               keeps the back button honest. */
            var form = document.createElement("form");
            form.method = "post";
            form.action = "/profile/passkeys";
            [["_csrf", csrf], ["token", data.token],
             ["credential", encodeCredential(credential)],
             ["name", addForm.querySelector('[name="name"]').value]
            ].forEach(function (pair) {
              var input = document.createElement("input");
              input.type = "hidden";
              input.name = pair[0];
              input.value = pair[1];
              form.appendChild(input);
            });
            document.body.appendChild(form);
            form.submit();
          });
        })
        .catch(function (err) {
          button.disabled = false;
          if (isCancellation(err)) return;
          if (problem) {
            problem.textContent = err.message || "That passkey could not be added.";
            problem.hidden = false;
          }
        });
    });
  }

  /* ── Signing in, on the login page ── */
  var signIn = document.getElementById("passkeysignin");
  if (signIn) {
    if (!supported()) signIn.hidden = true;
    signIn.addEventListener("click", function (e) {
      e.preventDefault();
      var problem = document.getElementById("passkeyerror");
      var csrf = signIn.dataset.csrf;
      signIn.disabled = true;
      post("/login/passkey/options", {_csrf: csrf})
        .then(function (r) {
          if (!r.ok) throw new Error("This app cannot offer passkeys right now.");
          return r.json();
        })
        .then(function (data) {
          return navigator.credentials.get({
            publicKey: decodeOptions(data.options),
          }).then(function (credential) {
            return post("/login/passkey", {
              _csrf: csrf, token: data.token,
              credential: encodeCredential(credential),
            });
          });
        })
        .then(function (r) {
          return r.json().then(function (body) {
            if (!r.ok) throw new Error(body.error || "That passkey was refused.");
            window.location = body.next || "/";
          });
        })
        .catch(function (err) {
          signIn.disabled = false;
          if (isCancellation(err)) return;
          if (problem) {
            problem.textContent = err.message || "That passkey was refused.";
            problem.hidden = false;
          }
        });
    });
  }
})();
