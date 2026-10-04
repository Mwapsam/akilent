/**
 * Session tracker — Tier-2 observability.
 *
 * Responsibilities:
 *  1. Generate / restore a pseudonymous browser session ID (sess_…).
 *  2. Expose window.__akilentSessionId so app.js can inject X-Browser-Session-Id.
 *  3. Maintain a request history ring buffer so UX events can name the nearest
 *     backend request (rage-click → 500 correlation).
 *  4. Detect rage clicks, dead clicks, rapid navigation, and JS errors, then
 *     POST structured events to /internal/ux-event/.
 *
 * Load this script BEFORE app.js so the session ID is available when HTMX
 * registers its configRequest handler.
 */
(function () {
  "use strict";

  // ─── Session ID ───────────────────────────────────────────────────────────

  var SESSION_KEY = "akilent:bsid";
  var SESSION_MAX_AGE_MS = 4 * 60 * 60 * 1000; // 4 hours

  function getOrCreateSessionId() {
    try {
      var stored = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null");
      if (stored && typeof stored.id === "string" && stored.id.startsWith("sess_")) {
        if (Date.now() - (stored.ts || 0) < SESSION_MAX_AGE_MS) {
          return stored.id;
        }
      }
    } catch (_) {}
    var id = "sess_" + generateUUID();
    try {
      sessionStorage.setItem(SESSION_KEY, JSON.stringify({ id: id, ts: Date.now() }));
    } catch (_) {}
    return id;
  }

  function generateUUID() {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID().replace(/-/g, "");
    }
    // Fallback for environments without crypto.randomUUID
    return "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx".replace(/x/g, function () {
      return ((Math.random() * 16) | 0).toString(16);
    });
  }

  window.__akilentSessionId = getOrCreateSessionId();

  // ─── Request history ring buffer ─────────────────────────────────────────
  // Maintains the last 20 requests so UX events can correlate against the
  // nearest backend request rather than just the absolute last one.

  var MAX_HISTORY = 20;
  window.__akilentRequests = [];

  function nearestRequestId(nowMs) {
    var best = null;
    var bestDelta = Infinity;
    for (var i = 0; i < window.__akilentRequests.length; i++) {
      var r = window.__akilentRequests[i];
      if (!r.completedAt) continue;
      var delta = Math.abs(nowMs - r.completedAt);
      if (delta < bestDelta) { bestDelta = delta; best = r.requestId; }
    }
    return best || "";
  }

  document.addEventListener("htmx:beforeRequest", function (e) {
    var cfg = e.detail && e.detail.requestConfig;
    window.__akilentRequests.push({
      startedAt: Date.now(),
      method: (cfg && cfg.verb || "").toUpperCase(),
      url: (cfg && cfg.path) || "",
      requestId: "",
      status: null,
      completedAt: null,
    });
    if (window.__akilentRequests.length > MAX_HISTORY) {
      window.__akilentRequests.shift();
    }
  });

  document.addEventListener("htmx:afterRequest", function (e) {
    var xhr = e.detail && e.detail.xhr;
    var rid = (xhr && xhr.getResponseHeader && xhr.getResponseHeader("X-Request-Id")) || "";
    var status = (xhr && xhr.status) || 0;
    var last = window.__akilentRequests[window.__akilentRequests.length - 1];
    if (last) {
      last.requestId = rid;
      last.status = status;
      last.completedAt = Date.now();
    }
  });

  // ─── UX event POST ────────────────────────────────────────────────────────

  var UX_EVENT_URL = "/internal/ux-event/";

  function getCsrfToken() {
    var name = "csrftoken";
    var cookies = document.cookie.split(";");
    for (var i = 0; i < cookies.length; i++) {
      var c = cookies[i].trim();
      if (c.startsWith(name + "=")) return decodeURIComponent(c.slice(name.length + 1));
    }
    var el = document.querySelector("[name=csrfmiddlewaretoken]");
    return el ? el.value : "";
  }

  function postUxEvent(payload) {
    var sid = window.__akilentSessionId;
    if (!sid) return;
    var body = Object.assign({ session_id: sid, request_id: nearestRequestId(Date.now()) }, payload);
    try {
      fetch(UX_EVENT_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": getCsrfToken(),
          "X-Browser-Session-Id": sid,
        },
        body: JSON.stringify(body),
        keepalive: true,
      }).catch(function () {});
    } catch (_) {}
  }

  // ─── Target sanitisation ─────────────────────────────────────────────────
  // Only safe, semantic attributes — never [value=…] or [data-email=…].

  var SAFE_ATTRS = ["data-action", "data-testid", "name", "type", "aria-label", "id"];

  function sanitizeTarget(el) {
    if (!el || !el.tagName) return "";
    var sel = el.tagName.toLowerCase();
    for (var i = 0; i < SAFE_ATTRS.length; i++) {
      var attr = SAFE_ATTRS[i];
      if (attr === "id" && el.id) { sel += "#" + el.id; continue; }
      if (el.hasAttribute && el.hasAttribute(attr)) {
        sel += "[" + attr + "=\"" + el.getAttribute(attr) + "\"]";
      }
    }
    return sel.slice(0, 255);
  }

  // ─── Rage-click detection ────────────────────────────────────────────────

  var RAGE_WINDOW_MS = 800;
  var RAGE_RADIUS_PX = 30;
  var RAGE_MIN_CLICKS = 3;

  var _rageState = {}; // key: sanitizeTarget(el) → { count, firstAt, x, y }

  function isExcludedTarget(el) {
    if (!el) return true;
    // Pagination links
    var rel = el.getAttribute && el.getAttribute("rel");
    if (rel === "prev" || rel === "next") return true;
    // Explicitly opted out
    if (el.closest && el.closest("[data-no-rage]")) return true;
    // Range inputs and sliders
    if (el.tagName === "INPUT" && el.type === "range") return true;
    return false;
  }

  document.addEventListener("click", function (e) {
    // Ignore native double-clicks
    if (e.detail >= 2) return;
    // Ignore active text selection
    try { if (window.getSelection && window.getSelection().toString()) return; } catch (_) {}
    // Ignore stylus
    if (e.pointerType === "pen") return;

    var el = e.target;
    if (isExcludedTarget(el)) return;

    var key = sanitizeTarget(el);
    var now = Date.now();
    var state = _rageState[key];

    if (!state || (now - state.firstAt) > RAGE_WINDOW_MS) {
      _rageState[key] = { count: 1, firstAt: now, x: e.clientX, y: e.clientY };
      return;
    }

    var dx = e.clientX - state.x;
    var dy = e.clientY - state.y;
    if (Math.sqrt(dx * dx + dy * dy) > RAGE_RADIUS_PX) {
      _rageState[key] = { count: 1, firstAt: now, x: e.clientX, y: e.clientY };
      return;
    }

    state.count++;
    if (state.count >= RAGE_MIN_CLICKS) {
      var duration = now - state.firstAt;
      postUxEvent({
        type: "rage_click",
        page: location.pathname,
        target: key,
        click_count: state.count,
        duration_ms: duration,
      });
      // Reset so we don't fire repeatedly for the same cluster
      delete _rageState[key];
    }
  }, true);

  // ─── Dead-click detection ────────────────────────────────────────────────
  // Conservative: only fires when target is interactive AND no observable
  // feedback occurs within 600ms (DOM change, focus change, URL change,
  // HTMX request, aria-expanded / disabled change, loading class).

  var DEAD_CLICK_WAIT_MS = 600;
  var INTERACTIVE_SELECTOR = "button, a[href], [role=button], [tabindex], input[type=submit], input[type=button]";

  var _lastHtmxRequestAt = 0;
  document.addEventListener("htmx:beforeRequest", function () {
    _lastHtmxRequestAt = Date.now();
  });

  document.addEventListener("click", function (e) {
    var el = e.target;
    if (!el || !el.closest) return;
    var interactive = el.closest(INTERACTIVE_SELECTOR);
    if (!interactive) return;

    var key = sanitizeTarget(interactive);
    var clickAt = Date.now();
    var urlBefore = location.href;
    var ariaExpandedBefore = interactive.getAttribute("aria-expanded");
    var disabledBefore = interactive.hasAttribute("disabled");

    var mutated = false;
    var observer = new MutationObserver(function () { mutated = true; });
    observer.observe(interactive.closest("[class]") || document.body, {
      childList: true, subtree: true, attributes: true,
    });

    setTimeout(function () {
      observer.disconnect();

      // Check all escape hatches
      if (mutated) return;
      if (location.href !== urlBefore) return;
      if (interactive.getAttribute("aria-expanded") !== ariaExpandedBefore) return;
      if (interactive.hasAttribute("disabled") !== disabledBefore) return;
      if (interactive.classList.contains("htmx-request")) return;
      if ((Date.now() - _lastHtmxRequestAt) < DEAD_CLICK_WAIT_MS * 2) return;
      if (document.activeElement && document.activeElement !== document.body &&
          document.activeElement !== interactive) return;

      postUxEvent({
        type: "dead_click",
        page: location.pathname,
        target: key,
      });
    }, DEAD_CLICK_WAIT_MS);
  }, true);

  // ─── Rapid navigation ────────────────────────────────────────────────────

  var RAPID_NAV_WINDOW_MS = 5000;
  var RAPID_NAV_MIN = 3;
  var _navTimestamps = [];

  document.addEventListener("htmx:afterSettle", function () {
    var now = Date.now();
    _navTimestamps.push(now);
    if (_navTimestamps.length > RAPID_NAV_MIN) _navTimestamps.shift();
    if (
      _navTimestamps.length >= RAPID_NAV_MIN &&
      (now - _navTimestamps[0]) < RAPID_NAV_WINDOW_MS
    ) {
      postUxEvent({
        type: "rapid_navigation",
        page: location.pathname,
        target: "",
        click_count: _navTimestamps.length,
        duration_ms: now - _navTimestamps[0],
      });
      _navTimestamps = [];
    }
  });

  // ─── JS error capture ────────────────────────────────────────────────────

  window.addEventListener("error", function (e) {
    postUxEvent({
      type: "js_error",
      page: location.pathname,
      target: e.filename || "",
      data: {
        message: (e.message || "").slice(0, 2048),
        stack: (e.error && e.error.stack ? e.error.stack : "").slice(0, 8192),
        line: e.lineno || 0,
        col: e.colno || 0,
      },
    });
  });

  window.addEventListener("unhandledrejection", function (e) {
    var message = "";
    try { message = String(e.reason || "").slice(0, 2048); } catch (_) {}
    postUxEvent({
      type: "js_error",
      page: location.pathname,
      target: "",
      data: { message: message },
    });
  });
})();
