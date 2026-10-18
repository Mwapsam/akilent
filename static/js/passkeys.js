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
    if (!el) throw new Error("This page is missing its security token. Refresh and try again.");
    return el.value;
  }
  // Network failures become a readable message instead of the browser's "Failed to fetch".
  async function post(url, body) {
    // Build the request first: its own errors are not network errors.
    var init = {
      method: "POST",
      credentials: "same-origin",
      headers: { "Accept": "application/json", "Content-Type": "application/json", "X-CSRFToken": csrf() },
      body: body ? JSON.stringify(body) : "{}"
    };
    try {
      return await fetch(url, init);
    } catch (e) {
      throw new Error("Could not reach the server. Check your connection and try again.");
    }
  }
  // A non-JSON body (for example a proxy error page) becomes null, not a SyntaxError.
  function readJson(res) {
    return res.json().catch(function () { return null; });
  }

  // Turn a browser or server failure into a sentence a user can act on.
  function explain(err, fallback) {
    // NotAllowedError also covers a timeout or a key that is not on this device, so the
    // message names those causes rather than assuming the user cancelled.
    if (err && err.name === "NotAllowedError") return new Error("We couldn't use that passkey. It may not be on this device, the prompt may have timed out, or it was cancelled. Try again, or sign in another way.");
    if (err && err.name === "InvalidStateError") return new Error("This key is already on your account.");
    if (err && err.name === "NotSupportedError") return new Error("This browser can't use passkeys. Try another browser or device.");
    if (err && err.name === "AbortError") return new Error("The request was stopped. Start again when you're ready.");
    if (err && err.name === "SecurityError") return new Error("Passkeys need a secure (https) connection and the right site address. Open Akilent from its normal address and try again.");
    return new Error(fallback);
  }

  function requirePasskeySupport() {
    if (!window.isSecureContext) {
      throw new Error("Passkeys need a secure (https) connection. Open Akilent from its normal address.");
    }
    if (!window.PublicKeyCredential || !navigator.credentials) {
      throw new Error("This browser doesn't support passkeys. Try another browser or device.");
    }
  }

  // Server errors are shown when they carry a message; otherwise the fallback is used.
  async function serverError(res, fallback) {
    var body = (await readJson(res)) || {};
    return new Error((typeof body === "object" && body.error) || fallback);
  }

  // Optional control: { signal: AbortSignal } lets the page cancel the browser prompt.
  window.passkeyRegister = async function (optionsUrl, finishUrl, name, reauth, control) {
    requirePasskeySupport();
    var res = await post(optionsUrl, reauth || {});
    if (!res.ok) throw await serverError(res, "Could not start. Check the details and try again.");
    var opts = await readJson(res);
    if (!opts || !opts.challenge || !opts.user || !opts.user.id) {
      throw new Error("Could not start. Check the details and try again.");
    }
    opts.challenge = b64urlToBuf(opts.challenge);
    opts.user.id = b64urlToBuf(opts.user.id);
    (opts.excludeCredentials || []).forEach(function (c) { c.id = b64urlToBuf(c.id); });
    var cred;
    try {
      var createOpts = { publicKey: opts };
      if (control && control.signal) createOpts.signal = control.signal;
      cred = await navigator.credentials.create(createOpts);
    } catch (err) {
      // Diagnostic only: the error name and browser context. Never the challenge or key data.
      console.error("Passkey registration failed", {
        name: err && err.name,
        secureContext: window.isSecureContext
      });
      throw explain(err, "The key could not be added. Try again.");
    }
    if (!cred || cred.type !== "public-key") throw new Error("Unexpected response from your device. Try again.");
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
    if (!done.ok) throw await serverError(done, "The key could not be saved. Try again.");
    return true;
  };

  window.passkeySignIn = async function (optionsUrl, finishUrl, control) {
    requirePasskeySupport();
    var res = await post(optionsUrl);
    if (!res.ok) throw await serverError(res, "Could not start sign-in.");
    var opts = await readJson(res);
    if (!opts || !opts.challenge) throw new Error("Could not start sign-in. Try again.");
    opts.challenge = b64urlToBuf(opts.challenge);
    (opts.allowCredentials || []).forEach(function (c) { c.id = b64urlToBuf(c.id); });
    var cred;
    try {
      var getOpts = { publicKey: opts };
      if (control && control.signal) getOpts.signal = control.signal;
      cred = await navigator.credentials.get(getOpts);
    } catch (err) {
      throw explain(err, "That key did not sign in. Try again.");
    }
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
    if (!done.ok) throw await serverError(done, "That key did not sign in. Try again.");
    // The server has already signed the user in, so a bad redirect must not show an error.
    // Fall back to the dashboard instead.
    var body = await readJson(done);
    var target = null;
    try {
      if (body && typeof body.redirect === "string" && body.redirect) {
        target = new URL(body.redirect, location.origin);
      }
    } catch (e) { target = null; }
    if (!target || target.origin !== location.origin) {
      window.location.href = "/dashboard/";
      return;
    }
    window.location.href = target.pathname + target.search + target.hash;
  };
})();
