// WebAuthn ceremonies for passkeys and security keys. No external libraries.
(function () {
  function b64urlToBuf(s) {
    var b = s.replace(/-/g, "+").replace(/_/g, "/");
    while (b.length % 4) b += "=";
    var bin = atob(b), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }
  function bufToB64url(buf) {
    var bytes = new Uint8Array(buf), bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function csrf() {
    var el = document.querySelector("[name=csrfmiddlewaretoken]");
    return el ? el.value : "";
  }
  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
      body: body ? JSON.stringify(body) : "{}"
    });
  }

  window.passkeyRegister = async function (optionsUrl, finishUrl, name, reauth) {
    var res = await post(optionsUrl, reauth || {});
    if (res.status === 400) throw new Error("Check your password and code, then try again.");
    if (!res.ok) throw new Error("Could not start registration.");
    var opts = await res.json();
    opts.challenge = b64urlToBuf(opts.challenge);
    opts.user.id = b64urlToBuf(opts.user.id);
    (opts.excludeCredentials || []).forEach(function (c) { c.id = b64urlToBuf(c.id); });
    var cred = await navigator.credentials.create({ publicKey: opts });
    var payload = {
      id: cred.id, rawId: bufToB64url(cred.rawId), type: cred.type,
      response: {
        attestationObject: bufToB64url(cred.response.attestationObject),
        clientDataJSON: bufToB64url(cred.response.clientDataJSON),
        transports: cred.response.getTransports ? cred.response.getTransports() : []
      },
      clientExtensionResults: cred.getClientExtensionResults()
    };
    var done = await post(finishUrl, { credential: payload, name: name || "" });
    if (!done.ok) throw new Error("The key could not be saved. Try again.");
    return true;
  };

  window.passkeySignIn = async function (optionsUrl, finishUrl) {
    var res = await post(optionsUrl);
    if (!res.ok) throw new Error("Could not start sign-in.");
    var opts = await res.json();
    opts.challenge = b64urlToBuf(opts.challenge);
    (opts.allowCredentials || []).forEach(function (c) { c.id = b64urlToBuf(c.id); });
    var cred = await navigator.credentials.get({ publicKey: opts });
    var payload = {
      id: cred.id, rawId: bufToB64url(cred.rawId), type: cred.type,
      response: {
        authenticatorData: bufToB64url(cred.response.authenticatorData),
        clientDataJSON: bufToB64url(cred.response.clientDataJSON),
        signature: bufToB64url(cred.response.signature),
        userHandle: cred.response.userHandle ? bufToB64url(cred.response.userHandle) : null
      },
      clientExtensionResults: cred.getClientExtensionResults()
    };
    var done = await post(finishUrl, { credential: payload });
    if (!done.ok) throw new Error("That key did not sign in. Try again.");
    var body = await done.json();
    window.location.href = body.redirect;
  };
})();
