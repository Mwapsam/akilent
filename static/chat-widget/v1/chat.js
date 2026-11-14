/*!
 * Akilent Chat Widget v1
 * Usage: copy the snippet from Chatbots → your bot → Install. It looks like
 *   <script src="https://<akilent static host>/chat-widget/v1/chat.js" data-chatbot="pk_chat_xxx" async></script>
 *
 * Zero framework dependencies. The widget runs on the business's own website,
 * so it never calls its page's origin: init goes to Akilent's site (SITE_URL is
 * read from the script's data-api attribute, else derived from the script URL),
 * and later calls use the api_origin the init response returns.
 */
(function () {
  "use strict";

  // ── Bootstrap ─────────────────────────────────────────────────────────────

  var script = document.currentScript ||
    document.querySelector('script[data-chatbot]');

  if (!script) return;

  var PUBLIC_KEY = script.getAttribute("data-chatbot");
  if (!PUBLIC_KEY) return;

  // Where Akilent's API lives. The snippet names it in data-api (the script itself may be
  // served from a static/CDN host); otherwise the script's own origin is used.
  var API_BASE = (function () {
    var explicit = script.getAttribute("data-api");
    try {
      return new URL(explicit || script.src, window.location.href).origin;
    } catch (_) {
      return null;
    }
  })();
  if (!API_BASE) return;
  var STORAGE_KEY = "ak_chat_" + PUBLIC_KEY;

  // ── State ──────────────────────────────────────────────────────────────────

  var state = {
    sessionKey: null,
    apiOrigin: null,
    config: null,
    open: false,
    turnCount: 0,
    identified: false,
  };

  // The session kept from an earlier page of this visit. init() offers it back to the server,
  // which resumes it (with the chat so far) or starts a new one.
  var savedSessionKey = null;
  try {
    var saved = sessionStorage.getItem(STORAGE_KEY);
    if (saved) savedSessionKey = JSON.parse(saved).sessionKey || null;
  } catch (_) {}

  // ── Init ───────────────────────────────────────────────────────────────────

  function init() {
    startSession(savedSessionKey, function (data) {
      render();
      var past = data.messages || [];
      for (var i = 0; i < past.length; i++) {
        if (past[i].from === "visitor") appendVisitorMsg(past[i].text);
        else appendBotMsg(past[i].text);
      }
    });
  }

  function startSession(resumeKey, done) {
    var body = { public_key: PUBLIC_KEY };
    if (resumeKey) body.session_key = resumeKey;
    post("/api/chat/init/", body, function (err, data) {
      if (err || !data || !data.session_key) return;
      state.sessionKey = data.session_key;
      state.apiOrigin = data.api_origin || API_BASE;
      state.config = data.config;
      try {
        sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ sessionKey: state.sessionKey }));
      } catch (_) {}
      done(data);
    }, true /* init always goes to API_BASE */);
  }

  // ── API helpers ────────────────────────────────────────────────────────────

  function post(path, body, cb, useScriptOrigin) {
    var origin = useScriptOrigin ? API_BASE : (state.apiOrigin || API_BASE);
    var url = origin + path;

    var xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.onload = function () {
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status >= 400 || !data) cb({ status: xhr.status, error: (data && data.error) || "error" }, null);
      else cb(null, data);
    };
    // Status 0: offline, blocked, or this site isn't on the chatbot's domain list.
    xhr.onerror = function () { cb({ status: 0, error: "network error" }, null); };
    xhr.send(JSON.stringify(body));
  }

  function sendMessage(text, cb) {
    post(
      "/api/chat/message/",
      { session_key: state.sessionKey, message: text },
      cb
    );
  }

  function identify(fields, cb) {
    post(
      "/api/chat/identify/",
      Object.assign({ session_key: state.sessionKey }, fields),
      cb
    );
  }

  // ── DOM ────────────────────────────────────────────────────────────────────

  var widget, panel, log, input, sendBtn, identifyForm;

  function render() {
    var cfg = state.config || {};
    var color = cfg.primary_color || "#1a56db";
    var position = cfg.position === "bottom_left" ? "left:24px" : "right:24px";

    // Launcher button
    widget = el("div", {
      id: "ak-chat-launcher",
      style: [
        "position:fixed;bottom:24px;" + position,
        "width:56px;height:56px;border-radius:50%",
        "background:" + color,
        "cursor:pointer;display:flex;align-items:center;justify-content:center",
        "box-shadow:0 4px 12px rgba(0,0,0,.2);z-index:9998",
        "transition:transform .15s",
      ].join(";"),
    });
    widget.innerHTML = svgIcon();
    widget.addEventListener("click", toggle);

    // Chat panel
    panel = el("div", {
      id: "ak-chat-panel",
      "hx-boost": "false",
      "x-ignore": "",
      style: [
        "position:fixed;bottom:92px;" + position,
        "width:340px;max-height:520px",
        "background:#fff;color:#111;border-radius:16px",
        "box-shadow:0 8px 32px rgba(0,0,0,.18);z-index:9999",
        "display:none;flex-direction:column;overflow:hidden",
        "font-family:system-ui,sans-serif;font-size:14px",
      ].join(";"),
    });

    // Header
    var header = el("div", {
      style: "background:" + color + ";color:#fff;padding:14px 16px;font-weight:600;font-size:15px",
    });
    header.textContent = cfg.name || "Chat";

    // Message log
    log = el("div", {
      style: "flex:1;overflow-y:auto;padding:12px 14px;display:flex;flex-direction:column;gap:8px",
    });

    // Welcome message
    if (cfg.welcome_message) {
      appendBotMsg(cfg.welcome_message);
    }

    // Input row
    var inputRow = el("div", {
      style: "display:flex;gap:8px;padding:10px 12px;border-top:1px solid #eee;color:#111;background:#f9f9f9",
    });
    input = el("input", {
      type: "text",
      placeholder: "Type a message…",
      style: "flex:1;border:1px solid #ddd;border-radius:8px;padding:6px 10px;outline:none;font-size:13px;color:#111;background:#fff",
    });
    sendBtn = el("button", {
      style: [
        "background:" + color + ";color:#fff;border:none;border-radius:8px",
        "padding:6px 14px;cursor:pointer;font-size:13px",
      ].join(";"),
    });
    sendBtn.textContent = "Send";
    sendBtn.addEventListener("click", onSend);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") onSend();
    });
    inputRow.appendChild(input);
    inputRow.appendChild(sendBtn);

    // Identify form (shown after handoff suggestion or can be triggered by visitor)
    identifyForm = buildIdentifyForm(color);

    panel.appendChild(header);
    panel.appendChild(log);
    panel.appendChild(identifyForm);
    panel.appendChild(inputRow);

    document.body.appendChild(widget);
    document.body.appendChild(panel);
  }

  function toggle() {
    state.open = !state.open;
    panel.style.display = state.open ? "flex" : "none";
    if (state.open) input.focus();
  }

  function onSend() {
    var text = input.value.trim();
    if (!text || !state.sessionKey) return;
    input.value = "";
    appendVisitorMsg(text);
    setLoading(true);
    sendMessage(text, function (err, data) {
      setLoading(false);
      if (err) {
        if (err.status === 410) {
          // The chat went quiet for a day and expired: start a fresh one and resend.
          startSession(null, function () {
            appendBotMsg("Let's pick this up again.");
            input.value = text;
          });
        } else if (err.status === 429) {
          appendBotMsg("You're sending messages very quickly. Please wait a moment and try again.");
          input.value = text;
        } else if (err.status === 0) {
          appendBotMsg("We couldn't reach the chat. Check your connection and try again.");
          input.value = text;
        } else {
          appendBotMsg("Something went wrong. Please try again.");
        }
        return;
      }
      state.turnCount++;
      appendBotMsg(data.reply);
      if (data.handoff_suggested && !state.identifyFormShown) {
        showHandoffPrompt();
      }
    });
  }

  function showHandoffPrompt() {
    state.identifyFormShown = true;
    var prompt = el("div", {
      style: "padding:10px 14px;background:#f9f9f9;border-top:1px solid #eee;font-size:13px;color:#555",
    });
    prompt.textContent = "Would you like to be connected with a team member?";
    var btn = el("button", {
      style: "margin-top:6px;background:#1a56db;color:#fff;border:none;border-radius:6px;padding:5px 12px;cursor:pointer;font-size:12px",
    });
    btn.textContent = "Connect me";
    btn.addEventListener("click", function () {
      prompt.remove();
      identifyForm.style.display = "block";
    });
    prompt.appendChild(btn);
    log.appendChild(prompt);
    log.scrollTop = log.scrollHeight;
  }

  function buildIdentifyForm(color) {
    var form = el("div", {
      style: "display:none;padding:10px 14px;border-top:1px solid #eee;background:#f9fafb",
    });
    var nameInput = el("input", { type: "text", placeholder: "Your name", style: inputStyle() });
    var emailInput = el("input", { type: "email", placeholder: "Your email", style: inputStyle() });
    var btn = el("button", {
      style: "margin-top:6px;background:" + color + ";color:#fff;border:none;border-radius:8px;padding:6px 14px;cursor:pointer;font-size:13px;width:100%",
    });
    btn.textContent = "Submit";
    btn.addEventListener("click", function () {
      var name = nameInput.value.trim();
      var email = emailInput.value.trim();
      if (!email) return;
      identify({ name: name, email: email }, function (err, data) {
        if (!err && data.identified) {
          state.identified = true;
          form.style.display = "none";
          appendBotMsg("Thanks! A team member will follow up with you shortly.");
        }
      });
    });
    form.appendChild(nameInput);
    form.appendChild(emailInput);
    form.appendChild(btn);
    return form;
  }

  function appendBotMsg(text) {
    var msg = el("div", {
      style: [
        "max-width:80%;padding:8px 12px;border-radius:12px 12px 12px 4px",
        "background:#f1f1f1;color:#111;align-self:flex-start;line-height:1.4",
      ].join(";"),
    });
    msg.textContent = text;
    log.appendChild(msg);
    log.scrollTop = log.scrollHeight;
  }

  function appendVisitorMsg(text) {
    var color = (state.config && state.config.primary_color) || "#1a56db";
    var msg = el("div", {
      style: [
        "max-width:80%;padding:8px 12px;border-radius:12px 12px 4px 12px",
        "background:" + color + ";color:#fff;align-self:flex-end;line-height:1.4",
      ].join(";"),
    });
    msg.textContent = text;
    log.appendChild(msg);
    log.scrollTop = log.scrollHeight;
  }

  function setLoading(on) {
    sendBtn.disabled = on;
    sendBtn.textContent = on ? "…" : "Send";
  }

  // ── Utility ────────────────────────────────────────────────────────────────

  function el(tag, attrs) {
    var e = document.createElement(tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  function inputStyle() {
    return "width:100%;box-sizing:border-box;border:1px solid #ddd;border-radius:8px;padding:6px 10px;font-size:13px;margin-bottom:6px;outline:none;color:#111;background:#fff";
  }

  function svgIcon() {
    return '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" fill="none" viewBox="0 0 24 24" stroke="white" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"/></svg>';
  }

  // ── Start ──────────────────────────────────────────────────────────────────

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
