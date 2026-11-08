/* App-wide interactivity: CSRF, toasts, sidebar/drawer state, and the bridge
 * between HTMX and this app's own Alpine stores. Works alongside (and
 * registers stores on) Alpine.
 *
 * Background requests are HTMX's job — this file used to carry a hand-rolled
 * engine (`data-ajax`/`data-swap`/`data-append`) that did the same thing less
 * well, and it is gone. Progressive enhancement is unchanged: every hx-post
 * form still carries a real method/action, so it submits and redirects
 * normally with JavaScript off.
 */
(function () {
  "use strict";

  // --- CSRF --------------------------------------------------------------
  function getCookie(name) {
    const m = document.cookie.match("(^|;)\\s*" + name + "\\s*=\\s*([^;]+)");
    return m ? decodeURIComponent(m.pop()) : "";
  }
  // The cookie is HttpOnly in production, so fall back to the token the page carries:
  // <body hx-headers> (base.html), which refreshCsrf() below keeps current.
  const CSRF = function () {
    const cookie = getCookie("csrftoken");
    if (cookie) return cookie;
    try { return JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRFToken"] || ""; }
    catch (_) { return ""; }
  };

  // --- Stale CSRF tokens -------------------------------------------------
  // A page can outlive its CSRF token: signing in issues a new one, and a page the
  // browser restores from its back/forward cache isn't re-requested. refreshCsrf()
  // fetches a current token (the cookie is HttpOnly, so the page can't read it) and
  // puts it in every form field and hx-headers on the page. Tokens that scripts keep
  // in their own config objects are covered by the fetch wrapper below, which swaps
  // the latest token into same-origin requests once a refresh has happened.
  let freshCsrf = "";

  function refreshCsrf() {
    return fetch("/internal/csrf-token/", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.token) return false;
        freshCsrf = data.token;
        document.querySelectorAll('input[name="csrfmiddlewaretoken"]').forEach(function (el) {
          el.value = freshCsrf;
        });
        document.querySelectorAll("[hx-headers]").forEach(function (el) {
          try {
            const headers = JSON.parse(el.getAttribute("hx-headers"));
            if ("X-CSRFToken" in headers) {
              headers["X-CSRFToken"] = freshCsrf;
              el.setAttribute("hx-headers", JSON.stringify(headers));
            }
          } catch (_) {}
        });
        return true;
      })
      .catch(function () { return false; });
  }

  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    if (freshCsrf && init) {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      let sameOrigin = true;
      try { sameOrigin = new URL(url, location.href).origin === location.origin; } catch (_) {}
      if (sameOrigin) {
        if (init.headers instanceof Headers) {
          if (init.headers.has("X-CSRFToken")) init.headers.set("X-CSRFToken", freshCsrf);
        } else if (init.headers && typeof init.headers === "object") {
          Object.keys(init.headers).forEach(function (k) {
            if (k.toLowerCase() === "x-csrftoken") init.headers[k] = freshCsrf;
          });
        }
        if (init.body instanceof FormData && init.body.has("csrfmiddlewaretoken")) {
          init.body.set("csrfmiddlewaretoken", freshCsrf);
        }
      }
    }
    return nativeFetch(input, init);
  };

  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    // A submit that was under way when the user went Back must not stay locked.
    document.querySelectorAll("[data-submit-lock]").forEach(function (b) {
      b.disabled = false;
      b.removeAttribute("data-submit-lock");
    });
    refreshCsrf();
  });

  // A boosted form whose token went stale comes back as a 403 that htmx won't swap.
  // csrf_failure marks it; refresh the token in place, keep what was typed, and say so.
  document.addEventListener("htmx:responseError", function (e) {
    const xhr = e.detail.xhr;
    if (!xhr || xhr.getResponseHeader("X-CSRF-Failed") !== "1") return;
    refreshCsrf().then(function (ok) {
      window.toast(
        ok ? "warning" : "danger",
        ok
          ? "This page had expired, so nothing was saved. Please submit again."
          : "This page has expired. Reload it and try again."
      );
    });
  });

  // Parse an X-Toast header: "type|url-encoded-message", optionally "|url-encoded-undo-path"
  // (accounts.utils.hx_toast). The message and path are encoded, so neither holds a raw "|".
  function decodeToast(header) {
    const parts = header.split("|");
    const type = parts.length === 1 ? "success" : parts[0];
    const dec = function (s) { try { return decodeURIComponent(s || ""); } catch (_) { return s || ""; } };
    const message = parts.length === 1 ? "" : dec(parts[1]);
    return { type: type || "success", message: message, undo: dec(parts[2]) };
  }
  function toastOpts(t) { return t.undo ? { undo: t.undo } : undefined; }

  // --- Toasts ------------------------------------------------------------
  // Backed by an Alpine store (see alpine:init); window.toast is the API:
  //   window.toast(type, message, { timeout, action })
  // How long a toast stays depends on what it says. A failure stays until it is
  // dismissed: at 4.5s an error could vanish before anyone read that their
  // action did not happen. opts.timeout overrides (0 = until dismissed).
  //
  // opts.action = { label, href } or { label, hx: { method, url } }, with optional
  // expires_ms (the button goes away after that) and idempotent (false = it can be
  // pressed once). Only same-origin paths are accepted: a toast can never be made to
  // send a request, or a person, somewhere else.
  const TOAST_TIMEOUTS = { success: 5000, info: 5000, warning: 8000, danger: 0 };
  function safeAction(a) {
    if (!a || typeof a.label !== "string" || !a.label) return null;
    const path = function (u) { return typeof u === "string" && /^\/(?!\/)/.test(u); };
    if (a.href && path(a.href)) return { label: a.label, href: a.href, expires_ms: a.expires_ms || 0 };
    if (a.hx && path(a.hx.url)) {
      const method = String(a.hx.method || "get").toLowerCase();
      if (["get", "post"].indexOf(method) === -1) return null;
      return { label: a.label, hx: { method: method, url: a.hx.url }, idempotent: a.idempotent !== false, expires_ms: a.expires_ms || 0, refresh: !!a.refresh };
    }
    return null;
  }
  let _toastQueue = [];
  // opts.undo = a path: an Undo button that POSTs there once, for 30 seconds, and then
  // refreshes the page (the server's undo, never a client-side timer pretending to be one).
  const UNDO_MS = 30000;
  window.toast = function (type, message, opts) {
    type = type || "info";
    if (opts && opts.undo && !opts.action) {
      opts = Object.assign({}, opts, {
        action: { label: "Undo", hx: { method: "post", url: opts.undo }, idempotent: false, expires_ms: UNDO_MS, refresh: true },
        timeout: opts.timeout !== undefined ? opts.timeout : UNDO_MS,
      });
    }
    const timeout = opts && opts.timeout !== undefined ? opts.timeout
      : (TOAST_TIMEOUTS[type] !== undefined ? TOAST_TIMEOUTS[type] : 5000);
    const t = { type: type, message: message, timeout: timeout, action: safeAction(opts && opts.action) };
    if (window.Alpine && Alpine.store("toasts")) Alpine.store("toasts").push(t);
    else _toastQueue.push(t); // before Alpine boots
  };

  // --- Navigation keyboard shortcuts ------------------------------------
  // Scoped to the whole nav, not to a [role="group"]. Inbox and Dashboard sit
  // above the first group, so group scoping meant arrow keys did nothing at
  // all on the two most-used links in the product.
  window.navFocusLink = function (e, dir) {
    const link = e.target.closest('a[data-nav-link]');
    if (!link) return;
    const scope = link.closest('nav');
    if (!scope) return;
    const links = Array.from(scope.querySelectorAll('a[data-nav-link]'));
    const idx = links.indexOf(link);
    if (idx === -1) return;
    const next = dir === 'up' ? links[idx - 1] : links[idx + 1];
    if (next) { e.preventDefault(); next.focus(); }
  };

  // --- Alpine stores -----------------------------------------------------
  document.addEventListener("alpine:init", function () {
    // Announcing is separate from showing: the message is written once into a
    // visually hidden live region (polite, or assertive for a failure), so a
    // re-render of the visible stack never re-reads anything.
    const _timers = {};
    Alpine.store("toasts", {
      items: [],
      polite: "",
      assertive: "",
      _id: 0,
      push(t) {
        const id = ++this._id;
        const item = Object.assign({ id: id, used: false, expired: false }, t);
        this.items.push(item);
        this.announce(item);
        if (t.timeout) this._schedule(id, t.timeout);
        if (item.action && item.action.expires_ms) {
          setTimeout(() => { const i = this._find(id); if (i) i.expired = true; }, item.action.expires_ms);
        }
      },
      announce(t) {
        const key = t.type === "danger" ? "assertive" : "polite";
        // Clear first, so the same text twice in a row is still read twice.
        this[key] = "";
        setTimeout(() => { this[key] = t.message; }, 50);
      },
      _find(id) { return this.items.find((i) => i.id === id); },
      _schedule(id, ms) {
        _timers[id] = { left: ms, started: Date.now(), handle: setTimeout(() => this.remove(id), ms) };
      },
      // Hovering or focusing a toast stops its clock; leaving restarts it with
      // whatever time it had left.
      pause(id) {
        const tm = _timers[id];
        if (!tm || tm.paused) return;
        clearTimeout(tm.handle);
        tm.left = Math.max(1000, tm.left - (Date.now() - tm.started));
        tm.paused = true;
      },
      resume(id) {
        const tm = _timers[id];
        if (!tm || !tm.paused) return;
        delete _timers[id];
        this._schedule(id, tm.left);
      },
      remove(id) {
        if (_timers[id]) { clearTimeout(_timers[id].handle); delete _timers[id]; }
        this.items = this.items.filter((i) => i.id !== id);
      },
      run(t) {
        const a = t.action;
        if (!a || t.expired || t.used) return;
        if (a.href) { window.location.assign(a.href); return; }
        if (!a.idempotent) t.used = true;
        const post = a.hx.method === "post";
        fetch(a.hx.url, {
          method: post ? "POST" : "GET",
          credentials: "same-origin",
          headers: Object.assign({ "HX-Request": "true" }, post ? { "X-CSRFToken": CSRF() } : {}),
        }).then((r) => {
          if (!r.ok) throw new Error(String(r.status));
          this.remove(t.id);
          const header = r.headers.get("X-Toast");
          if (header) { const d = decodeToast(header); window.toast(d.type, d.message, toastOpts(d)); }
          // An undo changed what the page shows: show it again, in place.
          if (a.refresh && window.akilent && window.akilent.refreshPage) window.akilent.refreshPage();
        }).catch(() => {
          // A request that failed in transit may still have reached the server, so
          // this does not claim nothing changed.
          window.toast("danger", "That didn't go through. Check the page before trying again.");
        });
      },
    });
    _toastQueue.forEach((t) => Alpine.store("toasts").push(t));
    _toastQueue = [];

    Alpine.store("ui", {
      sidebarCollapsed: localStorage.getItem("sidebarCollapsed") === "1",
      drawerOpen: false,
      toggleSidebar() {
        this.sidebarCollapsed = !this.sidebarCollapsed;
        localStorage.setItem("sidebarCollapsed", this.sidebarCollapsed ? "1" : "0");
        // Keep the CSS var (set pre-hydration by the head script) in sync so
        // --sidebar-current-w — and anything else reading it — stays correct.
        if (this.sidebarCollapsed) {
          document.documentElement.setAttribute("data-sidebar-collapsed", "1");
        } else {
          document.documentElement.removeAttribute("data-sidebar-collapsed");
        }
      },
      openDrawer() { this.drawerOpen = true; },
      closeDrawer() { this.drawerOpen = false; },
    });

    // Theme: "light" | "dark" | "system". Persisted to localStorage; the
    // pre-paint script in base.html reads the same key to avoid a flash.
    // "system" removes data-theme so the CSS prefers-color-scheme block wins.
    Alpine.store("theme", {
      value: (function () {
        try { return localStorage.getItem("akilent:theme") || "system"; }
        catch (e) { return "system"; }
      })(),
      init() { this.apply(); },
      set(v) {
        this.value = v;
        try {
          if (v === "system") localStorage.removeItem("akilent:theme");
          else localStorage.setItem("akilent:theme", v);
        } catch (e) {}
        this.apply();
      },
      apply() {
        const el = document.documentElement;
        if (this.value === "light" || this.value === "dark") el.setAttribute("data-theme", this.value);
        else el.removeAttribute("data-theme");
      },
    });
    Alpine.store("theme").init();

    // Confirm dialog: { open, title, message, confirmLabel, danger, _resolve }
    // ask() resolves true/false. With opts.input ({ label, placeholder, value }) it asks for
    // one line of text instead - the app's own dialog in place of window.prompt() - and
    // resolves the text, or null when cancelled.
    Alpine.store("confirm", {
      open: false, title: "", message: "", confirmLabel: "Confirm", cancelLabel: "Cancel", danger: false, _resolve: null,
      input: null, value: "",
      ask(opts) {
        Object.assign(this, { open: true, title: "", danger: false, confirmLabel: "Confirm", cancelLabel: "Cancel", input: null }, opts || {});
        this.value = (this.input && this.input.value) || "";
        return new Promise((res) => (this._resolve = res));
      },
      respond(ok) {
        const asked = !!this.input;
        this.open = false;
        if (this._resolve) this._resolve(asked ? (ok ? this.value : null) : ok);
        this._resolve = null;
      },
    });
  });

  // --- Dialog stack --------------------------------------------------------
  // One stack for every {% modal %}, drawer and the confirm dialog, so that with
  // two open (a confirm over a modal, a modal over a drawer):
  //   - Esc closes only the topmost one; it used to close all of them at once,
  //     because each bound its own handler on window;
  //   - everything behind the topmost is inert (no clicks, no tabbing, hidden
  //     from screen readers), except elements marked data-dialog-exempt: the
  //     toast region has to stay live, or a "Saved" raised while a dialog is
  //     open is never announced;
  //   - focus starts on [autofocus], else the first control in the body, else
  //     the panel itself (never a heading that cannot take focus), and goes
  //     back to whatever opened the dialog - or, if a swap removed that, to the
  //     page heading rather than <body>.
  // x-trap still contains Tab inside the dialog and locks scroll; it is told
  // not to move focus itself (.noautofocus.noreturn) so there is one owner.
  const dialogStack = [];
  const INERT_MARK = "data-dialog-inert";
  const FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
    'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function shown(el) {
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  }
  function syncInert() {
    document.querySelectorAll("[" + INERT_MARK + "]").forEach(function (el) {
      el.inert = false;
      el.removeAttribute(INERT_MARK);
    });
    const top = dialogStack[dialogStack.length - 1];
    if (!top || !top.root.isConnected) return;
    // Walk from the dialog up to <body>, making every sibling on the way inert.
    // Only elements this code marked are ever un-inerted, so an element some
    // other code made inert stays that way.
    for (let node = top.root; node && node !== document.body; node = node.parentElement) {
      const parent = node.parentElement;
      if (!parent) break;
      Array.prototype.forEach.call(parent.children, function (sib) {
        if (sib === node || sib.inert || sib.hasAttribute("data-dialog-exempt")) return;
        if (sib.tagName === "SCRIPT" || sib.tagName === "TEMPLATE" || sib.tagName === "STYLE") return;
        sib.inert = true;
        sib.setAttribute(INERT_MARK, "");
      });
    }
  }
  // preventScroll: move focus without scrolling to it (the overlay opens at its top even when
  // the first control sits below a tall picture, as in the media editor).
  function focusInitial(root, preventScroll) {
    const how = { preventScroll: !!preventScroll };
    const auto = root.querySelector("[autofocus]");
    if (auto && shown(auto)) { auto.focus(); return; }
    const body = root.querySelector("[data-dialog-body]");
    const pick = function (scope) {
      return scope && Array.prototype.find.call(scope.querySelectorAll(FOCUSABLE), shown);
    };
    const first = pick(body) || pick(root);
    if (first) { first.focus(how); return; }
    const panel = root.querySelector("[data-dialog-panel]") || root;
    if (!panel.hasAttribute("tabindex")) panel.setAttribute("tabindex", "-1");
    panel.focus(how);
  }
  function restoreFocus(opener) {
    if (opener && opener.isConnected && opener !== document.body && !opener.closest("[inert]")) {
      opener.focus();
      return;
    }
    const heading = document.querySelector("#main h1") || document.getElementById("main");
    if (!heading) return;
    if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
    heading.focus();
  }

  window.akDialog = {
    // opts.close: how to close this dialog when Esc reaches it.
    open(root, opts) {
      if (!root || dialogStack.some(function (d) { return d.root === root; })) return;
      dialogStack.push({ root: root, close: opts && opts.close, opener: (opts && opts.opener) || document.activeElement });
      syncInert();
      // After Alpine has shown it: focus() on a display:none element does nothing.
      requestAnimationFrame(function () {
        // Without scrolling: a dialog opens at its top, even if its first control is lower down.
        requestAnimationFrame(function () { if (root.isConnected) focusInitial(root, true); });
      });
    },
    close(root) {
      const i = dialogStack.findIndex(function (d) { return d.root === root; });
      if (i === -1) return; // already closed: a second close is a no-op
      const d = dialogStack.splice(i, 1)[0];
      syncInert();
      // Only the topmost dialog hands focus back; closing one underneath (a
      // modal-close for it while a confirm is up) must not pull focus out.
      if (i === dialogStack.length) restoreFocus(d.opener);
    },
    // The element focus returns to when this dialog closes.
    setOpener(root, el) {
      const d = dialogStack.find(function (x) { return x.root === root; });
      if (d) d.opener = el;
    },
    isTop(root) {
      const top = dialogStack[dialogStack.length - 1];
      return !!top && top.root === root;
    },
    get depth() { return dialogStack.length; },
  };

  // Esc closes the topmost dialog only. A control inside it that uses Esc for
  // itself (an open dropdown, a combobox) calls preventDefault and keeps it.
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape" || e.defaultPrevented || !dialogStack.length) return;
    const top = dialogStack[dialogStack.length - 1];
    if (typeof top.close === "function") {
      e.preventDefault();
      top.close();
    }
  });
  // A dialog removed by a swap while open must not leave the page inert.
  document.addEventListener("htmx:afterSettle", function () {
    for (let i = dialogStack.length - 1; i >= 0; i--) {
      if (!dialogStack[i].root.isConnected) dialogStack.splice(i, 1);
    }
    syncInert();
  });

  // --- On-screen keyboard ---------------------------------------------------
  // Chrome on Android shrinks the layout when the keyboard opens (the viewport meta
  // asks for interactive-widget=resizes-content). iOS Safari does not: the keyboard
  // covers the bottom of the layout, which is where the inbox reply box and sticky
  // action bars live. Publish how much is covered as --kb-inset (0 when nothing is),
  // and data-keyboard="open" for rules that hide non-essential sticky chrome.
  (function () {
    const vv = window.visualViewport;
    if (!vv) return;
    let frame = 0;
    function update() {
      frame = 0;
      const covered = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
      // Under ~80px it's browser toolbar movement, not a keyboard.
      const inset = covered > 80 ? covered : 0;
      const root = document.documentElement;
      root.style.setProperty("--kb-inset", inset + "px");
      if (inset) root.setAttribute("data-keyboard", "open");
      else root.removeAttribute("data-keyboard");
    }
    function schedule() { if (!frame) frame = requestAnimationFrame(update); }
    vv.addEventListener("resize", schedule);
    vv.addEventListener("scroll", schedule);
  })();

  // --- Unsaved work ---------------------------------------------------------
  // The shell navigates in place (hx-boost), which never fires beforeunload, so an
  // editor's own beforeunload guard did not cover a click on the sidebar: the
  // composer's work went with the swap, unasked. Anything holding work the
  // server doesn't have registers a check here:
  //   const off = akDirty.register(rootEl, () => this.dirty || this.saving);
  // A boosted navigation is then held while any registered, still-connected
  // check says yes, and the confirm dialog asks first. Leaving the page for real
  // (reload, close, typed URL) is covered by one beforeunload for all of them.
  const dirtyChecks = [];
  let leavingConfirmed = false;
  window.akDirty = {
    register(root, check) {
      const entry = { root: root, check: check };
      dirtyChecks.push(entry);
      return function () {
        const i = dirtyChecks.indexOf(entry);
        if (i !== -1) dirtyChecks.splice(i, 1);
      };
    },
    any() {
      for (let i = dirtyChecks.length - 1; i >= 0; i--) {
        const c = dirtyChecks[i];
        if (!c.root || !c.root.isConnected) { dirtyChecks.splice(i, 1); continue; }
        try { if (c.check()) return true; } catch (_) {}
      }
      return false;
    },
    // A form's values as one comparable string (the CSRF token left out, a file by its
    // name): take one when the page loads, and the form is dirty while it differs.
    formState(form) {
      const fd = new FormData(form);
      fd.delete("csrfmiddlewaretoken");
      return JSON.stringify(Array.from(fd.entries()).map(function (p) {
        return [p[0], typeof p[1] === "string" ? p[1] : (p[1] && p[1].name) || ""];
      }));
    },
  };
  window.addEventListener("beforeunload", function (e) {
    if (leavingConfirmed || !window.akDirty.any()) return;
    e.preventDefault();
    e.returnValue = "";
  });
  document.addEventListener("htmx:beforeRequest", function (e) {
    const d = e.detail;
    if (leavingConfirmed || !d.requestConfig || !d.requestConfig.boosted) return;
    if (d.requestConfig.verb !== "get" || !window.akDirty.any()) return;
    // Hovering a sidebar link preloads its page (htmx-ext-preload, header HX-Preloaded):
    // that only warms a cache and leaves nothing. Only an actual navigation asks.
    const headers = d.requestConfig.headers || {};
    if (headers["HX-Preloaded"] === "true") return;
    const store = window.Alpine && Alpine.store("confirm");
    if (!store) return;
    e.preventDefault();
    const elt = d.elt;
    store.ask({
      title: "Leave without saving?",
      message: "You have changes that haven't been saved. If you leave now, they'll be lost.",
      confirmLabel: "Leave without saving",
      cancelLabel: "Stay on this page",
      danger: true,
    }).then(function (ok) {
      if (!ok) return;
      leavingConfirmed = true;
      if (elt && elt.isConnected && elt.click) elt.click();
      else window.location.href = d.requestConfig.path;
      setTimeout(function () { leavingConfirmed = false; }, 0);
    });
  });

  // A plain form that should not be left half-filled: x-data="dirtyGuard".
  // Dirty = differs from what it held when the page loaded. A save that comes
  // back OK (a real submit, or an htmx request answered 2xx) takes a new
  // snapshot; a failed save leaves it dirty - the work still isn't on the server.
  document.addEventListener("alpine:init", function () {
    Alpine.data("dirtyGuard", function () {
      return {
        _snap: "",
        _submitting: false,
        _off: null,
        serialize() { return window.akDirty.formState(this.$el); },
        get dirty() { return !this._submitting && this.serialize() !== this._snap; },
        init() {
          this._snap = this.serialize();
          this._off = window.akDirty.register(this.$el, () => this.dirty);
          this.$el.addEventListener("submit", () => {
            // A non-htmx submit leaves the page; let it go without a prompt. The nearest
            // hx-boost decides: a form marked hx-boost="false" inside the boosted body
            // is a real page submit.
            const boost = this.$el.closest("[hx-boost]");
            const boosted = !!boost && boost.getAttribute("hx-boost") === "true";
            if (!this.$el.hasAttribute("hx-post") && !boosted) this._submitting = true;
          });
          this.$el.addEventListener("htmx:afterRequest", (e) => {
            if (e.detail.successful) this._snap = this.serialize();
          });
        },
        destroy() { if (this._off) this._off(); },
      };
    });
  });

  // --- One-shot navigations -------------------------------------------------
  // A link or a plain (non-htmx) form that starts something on the server and leaves the
  // page, such as checkout, which creates a payment and goes to the provider. The first
  // click or submit marks it busy and any further one is ignored, so a double click
  // doesn't start it twice. Back from the provider can restore this page from the
  // browser's cache, still busy: pageshow puts it back.
  //   <a data-busy-link class="btn …"><span>Pay</span></a>
  //   <form data-busy-submit …><button type="submit" class="btn …"><span>Submit</span></button></form>
  function markBusy(btn) {
    btn.setAttribute("aria-busy", "true");
    if (!btn.querySelector(":scope > .btn-spinner")) {
      const s = document.createElement("span");
      s.className = "btn-spinner";
      s.setAttribute("aria-hidden", "true");
      btn.prepend(s);
    }
  }
  document.addEventListener("click", function (e) {
    const a = e.target.closest("a[data-busy-link]");
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
    if (a.getAttribute("aria-busy") === "true") { e.preventDefault(); return; }
    markBusy(a);
  });
  document.addEventListener("submit", function (e) {
    const form = e.target.closest("form[data-busy-submit]");
    if (!form) return;
    if (form.dataset.submitting === "1") { e.preventDefault(); return; }
    if (e.defaultPrevented) return;
    form.dataset.submitting = "1";
    form.querySelectorAll("[type=submit]").forEach(function (b) { markBusy(b); b.disabled = true; });
  });
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("a[data-busy-link][aria-busy=true]").forEach(function (a) {
      a.removeAttribute("aria-busy");
      const s = a.querySelector(":scope > .btn-spinner");
      if (s) s.remove();
    });
    document.querySelectorAll("form[data-busy-submit][data-submitting]").forEach(function (f) {
      delete f.dataset.submitting;
      f.querySelectorAll("[type=submit]").forEach(function (b) {
        b.disabled = false;
        b.removeAttribute("aria-busy");
        const s = b.querySelector(":scope > .btn-spinner");
        if (s) s.remove();
      });
    });
  });

  // --- Copy to clipboard -------------------------------------------------
  // Any .copy-btn copies the .copy-value text in its .copy-row.
  function copyText(text, btn) {
    const done = () => {
      const label = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(() => (btn.textContent = label), 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
    } else {
      fallbackCopy(text, done);
    }
  }
  function fallbackCopy(text, done) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); done(); } catch (_) {}
    document.body.removeChild(ta);
  }
  document.addEventListener("click", function (e) {
    const btn = e.target.closest(".copy-btn");
    if (!btn) return;
    const row = btn.closest(".copy-row");
    const el = row && row.querySelector(".copy-value");
    if (el) copyText(el.textContent.trim(), btn);
  });

  // --- HTMX bridge -------------------------------------------------------
  // Elements whose *current* request is an automatic one. Membership is set
  // just before firing and cleared once the toast handlers below have read it.
  const silentRequests = new WeakSet();
  // xhr._akAuto marks one automatic refresh (see "Refreshes nobody asked for" below). It
  // lives on the request, not the element, so a cancelled refresh can't leave the element
  // silent for the next, user-triggered, request.
  function isSilent(el, xhr) {
    return (!!xhr && !!xhr._akAuto) || (!!el && (silentRequests.has(el) || el.hasAttribute("hx-toast-silent")));
  }

  // Three things the hand-rolled data-ajax engine did that HTMX does not do
  // on its own. Keeping the wire format (the X-Toast header) means views stay
  // untouched as their templates move across to hx-*.

  // 1. hx-confirm would otherwise open the browser's native confirm() dialog.
  //    Route it to the same Alpine confirm store the rest of the app uses, so
  //    a destructive HTMX action looks like every other destructive action.
  //    hx-confirm-danger opts into the red styling; hx-confirm-label renames
  //    the button.
  document.addEventListener("htmx:confirm", function (e) {
    const question = e.detail.question;
    if (!question) return; // no hx-confirm on this element — nothing to gate
    const store = window.Alpine && Alpine.store("confirm");
    e.preventDefault();
    // Still starting up: block rather than fall back to the browser's confirm(), the same
    // as data-confirm below. Nothing is sent; a second click a moment later asks properly.
    if (!store) {
      if (window.toast) window.toast("danger", "Still loading. Try again in a moment.");
      return;
    }
    const el = e.detail.elt;
    store
      .ask({
        message: question,
        danger: el.hasAttribute("hx-confirm-danger"),
        confirmLabel: el.getAttribute("hx-confirm-label") || "Confirm",
      })
      .then(function (ok) { if (ok) e.detail.issueRequest(true); });
  });

  // 1b. Plain buttons and forms with data-confirm ask the same modal instead of the browser's
  //     confirm(). data-confirm-danger colours the button red; data-confirm-label renames it.
  //     Nothing is sent unless the user confirms. If the modal is not up yet, the action is
  //     blocked with a toast rather than sent unconfirmed.
  let confirmBypass = false;
  function confirmThen(el, proceed) {
    const store = window.Alpine && Alpine.store("confirm");
    if (!store) {
      window.toast("danger", "Still loading. Try again in a moment.");
      return;
    }
    store
      .ask({
        message: el.getAttribute("data-confirm"),
        danger: el.hasAttribute("data-confirm-danger"),
        confirmLabel: el.getAttribute("data-confirm-label") || "Confirm",
        cancelLabel: el.getAttribute("data-confirm-cancel") || "Cancel",
      })
      .then(function (ok) {
        if (ok) { proceed(); return; }
        // Cancelled: back to where you were, not <body>. A button in a menu that has
        // closed hands focus to the menu's own trigger.
        setTimeout(function () {
          if (!el.isConnected || (document.activeElement && document.activeElement !== document.body)) return;
          const menu = el.closest("details");
          const target = shown(el) ? el : menu && menu.querySelector("summary");
          if (target) target.focus({ preventScroll: true });
        }, 50);
      });
  }
  function proceedUnguarded(fn) {
    confirmBypass = true;
    try { fn(); } finally { confirmBypass = false; }
  }
  // Clicks: ask first, then re-click. The form's own validation runs before the dialog, so a
  // confirmed click can't be stopped by a validation error afterwards.
  document.addEventListener("click", function (e) {
    if (confirmBypass || !e.target.closest) return;
    const btn = e.target.closest("button[data-confirm], input[type=submit][data-confirm]");
    if (!btn) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    const form = btn.form;
    if (form && !btn.formNoValidate && !form.noValidate && !form.checkValidity()) {
      form.reportValidity();
      return;
    }
    confirmThen(btn, function () { proceedUnguarded(function () { btn.click(); }); });
  }, true);
  // Submits that did not come through a confirmed click (requestSubmit, Enter in a field, a
  // script) are asked here, whichever of the submitter or the form carries data-confirm.
  document.addEventListener("submit", function (e) {
    if (confirmBypass) return;
    const form = e.target;
    if (!form.matches) return;
    const submitter = e.submitter;
    const asker = submitter && submitter.hasAttribute("data-confirm") ? submitter
      : form.matches("form[data-confirm]") ? form : null;
    if (!asker) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    confirmThen(asker, function () {
      proceedUnguarded(function () { form.requestSubmit(submitter || undefined); });
    });
  }, true);

  // 2. Surface the X-Toast response header. hx-toast-silent suppresses all but
  //    good news, which is what an auto-poll wants — "not verified yet" every
  //    20s is nagging, "verified" is the thing you are waiting for.
  document.addEventListener("htmx:afterRequest", function (e) {
    const xhr = e.detail.xhr;
    if (!xhr) return;
    const header = xhr.getResponseHeader("X-Toast");
    if (!header) return;
    const t = decodeToast(header);
    // Element-level silence only: an automatic refresh silences its *failures*, never
    // what the server chose to say (the follow-ups-due toast arrives on an SSE refresh).
    if (isSilent(e.detail.elt) && t.type !== "success") return;
    window.toast(t.type, t.message, toastOpts(t));
  });

  // 3. Error-state contract. A 4xx/5xx or a dropped connection is silent in HTMX
  //    by default - it swaps nothing and says nothing, so the button just looks
  //    broken. What we say depends on what failed, and it never claims more than
  //    we know:
  //    - A load (GET) into a region marked data-error-region: that region shows
  //      the error state, with a Try again that repeats the same request. Loads
  //      are safe to repeat. Regions that did not opt in are never touched.
  //    - A save (POST/PUT/PATCH/DELETE): the form and what was typed stay as they
  //      are and a toast says what happened. No automatic retry and no Retry
  //      button - if the connection dropped mid-request the server may already
  //      have acted, so a blind resend could do it twice.
  //    - A response for a region that has since started a newer request, or that
  //      has left the page, is dropped: it would overwrite newer state.
  const regionSeq = new WeakMap();
  function errorRegion(detail) {
    const target = detail.target || detail.elt;
    return target && target.closest ? target.closest("[data-error-region]") : null;
  }
  // Only reads take a turn: a form posted inside the region (assign, tags) must not make
  // the region's own load look superseded, and a mutation's failure is always said.
  document.addEventListener("htmx:beforeRequest", function (e) {
    const region = errorRegion(e.detail);
    if (!region) return;
    const verb = ((e.detail.requestConfig && e.detail.requestConfig.verb) || "get").toLowerCase();
    if (verb !== "get") return;
    const seq = (regionSeq.get(region) || 0) + 1;
    regionSeq.set(region, seq);
    if (e.detail.xhr) e.detail.xhr._akSeq = seq;
  });
  // A mutation whose answer has actually re-rendered the region is newer than any read
  // still out: that read's late failure must not wipe what the mutation just showed. (One
  // still in flight doesn't count, see above; nor does one whose answer was dropped.)
  document.addEventListener("htmx:afterSwap", function (e) {
    const region = errorRegion(e.detail);
    if (!region) return;
    const verb = ((e.detail.requestConfig && e.detail.requestConfig.verb) || "get").toLowerCase();
    if (verb !== "get") regionSeq.set(region, (regionSeq.get(region) || 0) + 1);
  });
  function describeFailure(xhr, kind) {
    const status = xhr ? xhr.status : 0;
    if (kind === "offline" || (!status && !navigator.onLine)) return { kind: "offline" };
    if (kind === "timeout") return { kind: "timeout" };
    if (!status) return { kind: "network" };
    if (status === 403) return { kind: "forbidden" };
    if (status === 404 || status === 410) return { kind: "gone" };
    if (status === 409) return { kind: "conflict" };
    if (status === 429) return { kind: "busy" };
    // A proxy answering for a slow or restarting app: Django may still finish the request.
    if (status === 502 || status === 503 || status === 504) return { kind: "gateway" };
    if (status >= 500) return { kind: "server" };
    return { kind: "client" };
  }
  // Outcomes after which a save may or may not have happened: the request may have reached
  // the server, and requests are not atomic (a 500 can come after the work was committed).
  const MAYBE_SAVED = { timeout: true, network: true, gateway: true, server: true };
  const LOAD_COPY = {
    offline: ["You're offline", "Reconnect and try again. Nothing you entered was lost."],
    timeout: ["This is taking too long", "Akilent didn't answer in time. Nothing you entered was lost."],
    network: ["Couldn't reach Akilent", "Check your connection and try again."],
    forbidden: ["You don't have access to this", "Ask an owner or admin of this business for access."],
    gone: ["This isn't here any more", "It may have been deleted or moved."],
    conflict: ["This changed while you were looking", "Load it again to see the latest."],
    busy: ["Too many requests", "Wait a few seconds, then try again."],
    gateway: ["Akilent isn't answering", "It may be restarting. Try again in a moment."],
    server: ["Couldn't load this", "Something went wrong on our side. Try again in a moment."],
    client: ["Couldn't load this", "Try again, or reload the page."],
  };
  const SAVE_COPY = {
    // Not sent at all: safe to say nothing happened.
    offline: "You're offline, so this wasn't sent. What you entered is still here.",
    // Sent, answer unknown: say exactly that.
    timeout: "No answer from Akilent, so this may or may not have saved. Check before trying again.",
    network: "The connection dropped, so this may or may not have saved. Check before trying again.",
    forbidden: "You don't have permission to do that.",
    gone: "This no longer exists. It may have been deleted.",
    conflict: "Someone else changed this first. Reload to see the latest, then try again.",
    busy: "Too many requests. Wait a few seconds, then try again.",
    gateway: "Akilent didn't answer properly, so this may or may not have saved. Check before trying again.",
    // Not "didn't save": a 500 can come after the change was made.
    server: "Something went wrong on our side, so this may or may not have saved. Check before trying again. What you entered is still here.",
    client: "This didn't go through. Check the form and try again.",
  };
  // One source of truth for "did it go through" (static/js/chat.js uses it for sends).
  // Editors' autosave sends the whole draft, so sending it again is always safe: unlike
  // SAVE_COPY (one-off actions, where a retry could repeat something), every outcome keeps the
  // edits on the page and says how they'll get saved.
  const DRAFT_SAVE_COPY = {
    offline: "You're offline. Your changes are still here and will save when you're back online.",
    forbidden: "You don't have permission to change this.",
    gone: "This no longer exists. It may have been deleted.",
    conflict: "Someone else changed this first. Reload to see the latest.",
    busy: "Too many saves at once. Wait a few seconds, then try again.",
    other: "Couldn't save. Your changes are still here. Try again.",
  };
  function draftSaveMessage(status, data) {
    const kind = describeFailure({ status: status || 0 }).kind;
    if (kind === "client" && data && data.error) return "Couldn't save: " + data.error + ".";
    return DRAFT_SAVE_COPY[kind] || DRAFT_SAVE_COPY.other;
  }
  window.akFailure = { describe: describeFailure, maybeSaved: MAYBE_SAVED, SAVE_COPY: SAVE_COPY, draftSaveMessage: draftSaveMessage };
  // The CSRF token for a fetch() POST.
  window.akCsrf = function () {
    return (document.cookie.match(/(^|;\s*)csrftoken=([^;]+)/) || [])[2] || "";
  };
  function showRegionError(region, detail, failure) {
    const tpl = document.getElementById("ak-error-state");
    if (!tpl) return false;
    const node = tpl.content.firstElementChild.cloneNode(true);
    const copy = LOAD_COPY[failure.kind] || LOAD_COPY.client;
    node.querySelector("[data-error-title]").textContent = region.getAttribute("data-error-title") || copy[0];
    node.querySelector("[data-error-message]").textContent = copy[1];
    const retry = node.querySelector("[data-error-retry]");
    const cfg = detail.requestConfig || {};
    const retryEvent = region.getAttribute("data-error-retry-event");
    if (retry && retryEvent) {
      // The region retries its own way (inbox_pane.js: through openInPane).
      retry.addEventListener("click", function () {
        retry.setAttribute("aria-busy", "true");
        retry.disabled = true;
        region.dispatchEvent(new CustomEvent(retryEvent, { bubbles: false }));
      });
    } else if (retry && cfg.path && window.htmx) {
      retry.addEventListener("click", function () {
        retry.setAttribute("aria-busy", "true");
        retry.disabled = true;
        window.htmx.ajax("GET", cfg.path, {
          target: detail.target || region,
          swap: (detail.elt && detail.elt.getAttribute("hx-swap")) || "innerHTML",
          values: cfg.parameters,
        });
      });
    } else if (retry) {
      retry.remove();
    }
    const swapInto = region.querySelector("[data-error-slot]") || region;
    swapInto.replaceChildren(node);
    // A region that announced itself as loading has stopped: it shows the failure now.
    region.removeAttribute("aria-busy");
    return true;
  }
  function jsonError(xhr) {
    if (!xhr || !/json/i.test(xhr.getResponseHeader("Content-Type") || "")) return "";
    try {
      const body = JSON.parse(xhr.responseText);
      return body && typeof body.error === "string" ? body.error.slice(0, 300) : "";
    } catch (_) { return ""; }
  }
  function handleFailure(e, kind) {
    const detail = e.detail;
    const xhr = detail.xhr;
    if (xhr && xhr.getResponseHeader("X-CSRF-Failed") === "1") return; // see refreshCsrf
    const failure = describeFailure(xhr, kind);
    const verb = ((detail.requestConfig && detail.requestConfig.verb) || "get").toLowerCase();
    const region = errorRegion(detail);
    if (region) {
      if (!region.isConnected) return; // left the page
      if (xhr && xhr._akSeq && xhr._akSeq !== regionSeq.get(region)) return; // superseded
    }
    if (isSilent(detail.elt, xhr)) return;
    // Navigations are handled further down (they fall back to a full page load).
    if (isBoosted(detail) && verb === "get" && kind !== "response") return;
    if (verb === "get") {
      if (region && showRegionError(region, detail, failure)) return;
      if (xhr && xhr.getResponseHeader("X-Toast")) return; // already toasted above
      const c = LOAD_COPY[failure.kind] || LOAD_COPY.client;
      window.toast("danger", c[0] + ". " + c[1]);
      return;
    }
    if (xhr && xhr.getResponseHeader("X-Toast")) return; // the view said what happened
    // A view that answers a refused save with JSON {"error": "..."} knows why ("The
    // workspace owner can't be removed"); say that rather than the generic copy. Only for
    // a refusal (4xx): for a 5xx the outcome is unknown whatever the body says.
    const reason = failure.kind !== "server" && failure.kind !== "gateway" && jsonError(xhr);
    window.toast("danger", reason || SAVE_COPY[failure.kind] || SAVE_COPY.client);
  }
  document.addEventListener("htmx:responseError", function (e) { handleFailure(e, "response"); });
  document.addEventListener("htmx:sendError", function (e) { handleFailure(e, navigator.onLine ? "network" : "offline"); });
  document.addEventListener("htmx:timeout", function (e) { handleFailure(e, "timeout"); });

  // Connection status: one persistent notice while offline, cleared once back.
  // The notice is a toast so it is announced and sits above any open dialog.
  let offlineToastId = null;
  window.addEventListener("offline", function () {
    if (!window.Alpine || !Alpine.store("toasts") || offlineToastId) return;
    window.toast("warning", "You're offline. Changes won't send until you reconnect.", { timeout: 0 });
    const items = Alpine.store("toasts").items;
    offlineToastId = items.length ? items[items.length - 1].id : null;
  });
  window.addEventListener("online", function () {
    if (offlineToastId && window.Alpine) Alpine.store("toasts").remove(offlineToastId);
    offlineToastId = null;
    window.toast("success", "Back online.");
  });

  // Silence lasts one request. A successful poll swaps the element away and
  // takes its entry with it, but a failed one leaves the same element in place
  // — and the next click on it is the user's, which must not be silent. The
  // timeout defers the clear past the toast handlers above, whatever order
  // HTMX fires them in.
  document.addEventListener("htmx:afterRequest", function (e) {
    const el = e.detail.elt;
    if (silentRequests.has(el)) setTimeout(function () { silentRequests.delete(el); }, 0);
    if (el && e.detail.xhr && e.detail.xhr._akAuto) {
      const ok = e.detail.successful;
      // A region that refreshes itself says when it has stopped doing so: the
      // content on screen is what it was at the last good refresh, not live.
      if (ok) el.removeAttribute("data-stale");
      else el.setAttribute("data-stale", "");
      el.dispatchEvent(new CustomEvent(ok ? "ak-refresh-ok" : "ak-refresh-failed", { bubbles: true }));
    }
  });

  // Refreshes nobody asked for fail without a toast. A list refreshing every 8
  // seconds while the server is unreachable raised an error toast each time, and
  // error toasts stay until dismissed. The region is marked data-stale instead and
  // fires ak-refresh-failed (ak-refresh-ok once it recovers), so the page can say
  // "not updating" in place, once.
  // Opt-in, never inferred from htmx internals: an element marked data-auto-refresh
  // declares that its own triggers are background refreshes - its timer or load
  // (htmx sends those with no triggering event), sse:… events, and any custom event
  // names listed in the attribute's value. A trusted click or submit on it is still
  // the user's request and keeps its error toast. htmx.ajax() also sends no event,
  // so code making a user-visible request must not use such an element as source.
  document.addEventListener("htmx:beforeRequest", function (e) {
    const el = e.detail.elt;
    const xhr = e.detail.xhr;
    if (!el || !xhr || !el.hasAttribute || !el.hasAttribute("data-auto-refresh")) return;
    const ev = (e.detail.requestConfig || {}).triggeringEvent;
    const listed = (el.getAttribute("data-auto-refresh") || "").split(/\s+/).filter(Boolean);
    const own = ev == null; // htmx's own trigger (every/poll/load): no event at all
    const pushed = ev instanceof Event && !ev.isTrusted &&
      (ev.type.indexOf("sse:") === 0 || listed.indexOf(ev.type) !== -1);
    if (own || pushed) {
      xhr._akAuto = true;
      // Nobody asked for it, so it never looks busy (app.css .ak-auto-request): the inbox
      // list dimmed for the whole round trip of every 8-second check, even when nothing
      // had changed, and couldn't be clicked meanwhile.
      el.classList.add("ak-auto-request");
    }
  });
  document.addEventListener("htmx:afterRequest", function (e) {
    const el = e.detail.elt;
    if (el && el.classList && e.detail.xhr && e.detail.xhr._akAuto) el.classList.remove("ak-auto-request");
  });

  // A follow-up form (data-followup-form) that went through tells the page, so the nav badge
  // and the inbox's Follow-ups link re-read the shared count at once. In-place navigation
  // only swaps <main>, and live updates may be off, so they'd otherwise wait for their timer.
  // The request is marked as it starts: by the time it ends the form has been swapped away.
  document.addEventListener("htmx:beforeRequest", function (e) {
    const el = e.detail.elt;
    if (el && el.hasAttribute && el.hasAttribute("data-followup-form") && e.detail.xhr) {
      e.detail.xhr._akFollowup = true;
    }
  });
  document.addEventListener("htmx:afterRequest", function (e) {
    if (e.detail.successful && e.detail.xhr && e.detail.xhr._akFollowup) {
      htmx.trigger(document.body, "followup-changed");
    }
  });

  // --- In-place navigation (shell swaps) ---------------------------------
  // <body hx-boost> makes links and forms inside the signed-in shell load the next page into
  // <main> instead of reloading everything. The server half is apps/core/htmx.py: it answers
  // with just the page, or tells HTMX to do a real page load whenever a swap won't do.
  const akilent = (window.akilent = window.akilent || {});
  const mainEl = () => document.getElementById("main");
  const shellName = () => {
    const m = document.head && document.head.querySelector('meta[name="akilent-shell"]');
    return m ? m.content : "";
  };

  // Page lifecycle for scripts that live inside <main>. onPage(fn) runs fn now and after every
  // in-place navigation; whatever fn returns runs just before the page is swapped away.
  const pageHooks = [];
  let pageCleanups = [];
  function runPageHooks() {
    pageHooks.forEach(function (fn) {
      try { const c = fn(mainEl()); if (typeof c === "function") pageCleanups.push(c); }
      catch (err) { console.error(err); }
    });
  }
  // Run a hook at once only if the start-up pass (DOMContentLoaded, below) has already
  // happened. A deferred script calls onPage while readyState is "interactive", before
  // DOMContentLoaded; running it then *and* in the start-up pass left two live copies of
  // the page script (the two-pane inbox opened every conversation twice).
  let pageHooksStarted = false;
  akilent.onPage = function (fn) {
    pageHooks.push(fn);
    if (pageHooksStarted) {
      try { const c = fn(mainEl()); if (typeof c === "function") pageCleanups.push(c); }
      catch (err) { console.error(err); }
    }
  };
  // Alpine.data that works whether Alpine has started yet or not (a swapped-in page loads
  // its script long after alpine:init has fired).
  akilent.alpineData = function (name, factory) {
    if (window.Alpine && window.Alpine.version) window.Alpine.data(name, factory);
    else document.addEventListener("alpine:init", function () { window.Alpine.data(name, factory); });
  };

  // The sidebar's Inbox badge: one hidden element asks (data-badge-source, in the desktop
  // nav, kept across navigations) and every badge shown copies its answer: the desktop and
  // phone navs (data-badge-mirror) and the inbox's "Follow-ups to do" (data-badge-due, from
  // the answer's data-due). One request per poll, whichever nav is on screen.
  function syncBadges() {
    document.querySelectorAll("[data-badge-source]").forEach(function (src) {
      const key = src.getAttribute("data-badge-source");
      const count = src.querySelector(".inbox-tab-count");
      const html = count ? count.outerHTML : "";
      document.querySelectorAll('[data-badge-mirror="' + key + '"]').forEach(function (m) {
        if (m.innerHTML !== html) m.innerHTML = html;
      });
      const due = src.querySelector("[data-due]");
      if (!due) return;
      document.querySelectorAll('[data-badge-due="' + key + '"]').forEach(function (m) {
        m.textContent = due.getAttribute("data-due");
      });
    });
  }
  document.addEventListener("htmx:afterSettle", syncBadges);

  // Navigate in place from script (command palette, toasts). Falls back to a page load.
  akilent.navigate = function (url) {
    const main = mainEl();
    if (!window.htmx || !main || !document.body.hasAttribute("hx-boost")) {
      window.location.href = url;
      return;
    }
    const a = document.createElement("a");
    a.href = url;
    a.hidden = true;
    main.appendChild(a);
    window.htmx.process(a);
    a.click();
  };

  // A page loaded in full because its scripts expect a fresh page keeps its own links and
  // forms as ordinary ones, so a form error never costs the user what they typed.
  function markUnsafeMain() {
    const main = mainEl();
    if (!main || !document.body.hasAttribute("hx-boost")) return;
    const unsafe = Array.prototype.some.call(main.querySelectorAll("script"), function (s) {
      return s.type !== "application/json" && !s.hasAttribute("data-shell-safe");
    });
    if (unsafe) main.setAttribute("hx-boost", "false");
  }

  // Back/forward restore only <main>, so re-mark the active nav link the way
  // apps/core/templatetags/nav.py does (path prefix, or exact; data-nav-exclude carves out
  // sub-sections that have their own link).
  function syncNav() {
    const path = window.location.pathname;
    document.querySelectorAll("a[data-nav-match]").forEach(function (a) {
      const excluded = (a.getAttribute("data-nav-exclude") || "").split(" ").some(function (t) {
        return t && (path === t || path.indexOf(t) === 0);
      });
      const on = !excluded && a.getAttribute("data-nav-match").split(" ").some(function (t) {
        if (!t) return false;
        if (a.hasAttribute("data-nav-exact")) return path === t;
        return path === t || (t !== "/" && path.indexOf(t) === 0);
      });
      a.classList.toggle("nav-link-active", on);
      if (on) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
  }

  // Say where we are after a swap: move focus to the page heading (or <main>) and announce
  // the new title, which is what a screen reader hears on a real page load.
  let liveRegion = null;
  function announce(text) {
    if (!liveRegion) {
      liveRegion = document.createElement("div");
      liveRegion.className = "sr-only";
      liveRegion.setAttribute("aria-live", "polite");
      liveRegion.setAttribute("aria-atomic", "true");
      document.body.appendChild(liveRegion);
    }
    liveRegion.textContent = "";
    setTimeout(function () { liveRegion.textContent = text; }, 50);
  }
  function focusPage() {
    const main = mainEl();
    if (!main) return;
    const invalid = main.querySelector('[aria-invalid="true"], .errorlist');
    // An autofocus field (for example a code entry) takes focus ahead of the heading, as it
    // would on a full page load, so keyboard users land where they need to type.
    const autofocus = main.querySelector("[autofocus]");
    const target = invalid && invalid.matches("input, select, textarea") ? invalid : autofocus || main.querySelector("h1") || main;
    if (target !== main && !target.hasAttribute("tabindex") && !target.matches("input, select, textarea")) {
      target.setAttribute("tabindex", "-1");
    }
    target.focus({ preventScroll: true });
    announce(document.title);
  }

  const isBoosted = (detail) => !!(detail && detail.requestConfig && detail.requestConfig.boosted);
  // htmx-ext-preload (vendor/htmx-ext-preload.js) warms a sidebar link's target on hover by
  // replaying the same boosted request early, marked with this header. It intercepts its own
  // htmx:beforeRequest to send the XHR itself and never lets htmx's normal completion path run —
  // so htmx:afterRequest never fires for it. Progress-bar/aria-busy bookkeeping below counts
  // htmx:beforeSend (a request actually sent, never one cancelled earlier, such as by the
  // unsaved-work prompt) against afterRequest 1:1. Counting on beforeRequest, or counting a
  // preload, increments without a matching decrement and strands the bar and #main's dimmed
  // state. A preload is still excluded by this check, so the count never relies on it.
  const isPreloaded = (detail) =>
    !!(detail && detail.requestConfig && detail.requestConfig.headers && detail.requestConfig.headers["HX-Preloaded"] === "true");

  document.addEventListener("htmx:configRequest", function (e) {
    if (isBoosted(e.detail) || (e.detail.elt && e.detail.elt.closest && e.detail.elt.closest("[hx-boost]"))) {
      e.detail.headers["X-Akilent-Shell"] = shellName();
    }
    if (window.__akilentSessionId) {
      e.detail.headers["X-Browser-Session-Id"] = window.__akilentSessionId;
    }
  });

  document.addEventListener("htmx:beforeSwap", function (e) {
    if (!isBoosted(e.detail)) return;
    const main = mainEl();
    if (!main) return;
    const status = e.detail.xhr.status;
    // A form that failed validation comes back 400/422 with the page to show.
    if (status === 400 || status === 422) { e.detail.shouldSwap = true; e.detail.isError = false; }
    if (!e.detail.shouldSwap) return;
    e.detail.target = main;
    e.detail.swapOverride = "innerHTML show:window:top";
    pageCleanups.forEach(function (c) { try { c(); } catch (err) { console.error(err); } });
    pageCleanups = [];
  });

  // The page's own classes for <main> (base.html, block main_class), which a swap would
  // otherwise leave as the previous page's: the composer's layout depends on them.
  document.addEventListener("htmx:afterSwap", function (e) {
    if (!isBoosted(e.detail)) return;
    const main = mainEl();
    const carrier = main && main.querySelector(":scope > template[data-main-class]");
    if (!carrier) return;
    main.className = carrier.getAttribute("data-main-class");
    carrier.remove();
  });

  // afterSettle only fires once content has actually been swapped in, so isBoosted alone is
  // enough here: a normal 2xx nav and a forced 400/422 re-render (beforeSwap, above) both reach
  // this point, and any other error status never swapped at all. A status check on top of that
  // would (and did) skip focusPage() for exactly the validation-error case it exists for.
  document.addEventListener("htmx:afterSettle", function (e) {
    if (!isBoosted(e.detail)) return;
    syncNav();
    focusPage();
    runPageHooks();
    if (window.Alpine && Alpine.store("ui")) Alpine.store("ui").closeDrawer();
  });

  // View Transitions are for moving between pages (boosted navigation). With
  // globalViewTransitions on, every other swap - the inbox list refreshing itself
  // every few seconds, a conversation opening in the pane, a form re-rendering -
  // also cross-faded the whole screen, which read as the screen flickering.
  document.addEventListener("htmx:beforeTransition", function (e) {
    if (!isBoosted(e.detail)) { e.preventDefault(); return; }
    // Name #main for this one transition only (see .ak-page-vt in assets/app.css); the
    // wrapper below takes the name off again once the transition has finished.
    if (document.startViewTransition) document.documentElement.classList.add("ak-page-vt");
  });
  if (document.startViewTransition && !document.startViewTransition.__ak) {
    const startVT = document.startViewTransition.bind(document);
    let latest = null;
    document.startViewTransition = function () {
      const t = startVT.apply(document, arguments);
      latest = t;
      // Only the newest transition takes the name off: an earlier one, skipped because a
      // second navigation started, finishes while the second is still running.
      const done = function () { if (latest === t) document.documentElement.classList.remove("ak-page-vt"); };
      if (t && t.finished) t.finished.then(done, done); else done();
      // A transition skipped by a newer navigation rejects these with "Transition was skipped":
      // expected, not an error, so don't leave them unhandled in the console.
      const ignore = function () {};
      if (t && t.ready) t.ready.catch(ignore);
      if (t && t.updateCallbackDone) t.updateCallbackDone.catch(ignore);
      return t;
    };
    document.startViewTransition.__ak = true;
  }

  document.addEventListener("htmx:historyRestore", function (e) {
    // The restore response is a whole page; make sure its <title> wins.
    const html = e.detail && e.detail.serverResponse;
    const m = typeof html === "string" && html.match(/<title>([\s\S]*?)<\/title>/i);
    if (m) {
      const t = document.createElement("textarea");
      t.innerHTML = m[1].trim();
      document.title = t.value;
    }
    // Only <main>'s contents are restored: its classes come from the restored page too.
    const cls = typeof html === "string" && html.match(/<main id="main"[^>]*\sclass="([^"]*)"/);
    if (cls && mainEl()) mainEl().className = cls[1];
    syncNav();
    focusPage();
    runPageHooks();
  });

  // A boosted request under ~120ms should feel instant, with nothing shown; past that it needs
  // to read as "loading" rather than "stuck". #main dims (aria-busy) and a thin bar creeps across
  // the top — both driven by CSS in assets/app.css, this just toggles the classes/attribute at
  // the right moments. navBusyCount covers the rare case of two boosted requests overlapping
  // (a background poll landing mid-navigation does not count — it is never boosted).
  let navBusyCount = 0;
  let navProgressTimer = null;
  let navProgressEl = null;
  function navProgressBar() {
    if (!navProgressEl) {
      navProgressEl = document.createElement("div");
      navProgressEl.id = "nav-progress";
      navProgressEl.setAttribute("aria-hidden", "true");
      document.body.appendChild(navProgressEl);
    }
    return navProgressEl;
  }
  // beforeSend, not beforeRequest: a navigation can still be cancelled in beforeRequest
  // (the unsaved-work guard does that when you choose "Stay on this page"), and a
  // cancelled one never reaches afterRequest - the bar stuck halfway and <main> stayed
  // dimmed for good. beforeSend only fires for a request that is actually going out.
  document.addEventListener("htmx:beforeSend", function (e) {
    if (!isBoosted(e.detail) || isPreloaded(e.detail)) return;
    navBusyCount++;
    navProgressTimer = setTimeout(function () {
      const bar = navProgressBar();
      bar.classList.remove("is-done");
      bar.classList.add("is-active");
      const main = mainEl();
      if (main) main.setAttribute("aria-busy", "true");
    }, 120);
  });
  document.addEventListener("htmx:afterRequest", function (e) {
    if (!isBoosted(e.detail) || isPreloaded(e.detail)) return;
    navBusyCount = Math.max(0, navBusyCount - 1);
    if (navBusyCount > 0) return;
    clearTimeout(navProgressTimer);
    const main = mainEl();
    if (main) main.removeAttribute("aria-busy");
    if (navProgressEl && navProgressEl.classList.contains("is-active")) {
      navProgressEl.classList.remove("is-active");
      navProgressEl.classList.add("is-done");
      setTimeout(function () { navProgressEl.classList.remove("is-done"); }, 300);
    }
  });

  // A form's submit buttons are disabled while its htmx request is in flight, so a slow save
  // can't be sent twice. beforeSend runs after htmx has read the form (the clicked button's
  // name and value are already in the request). A form inside an element with its own
  // hx-disabled-elt is left to htmx.
  const lockedSubmits = new WeakMap();
  document.addEventListener("htmx:beforeSend", function (e) {
    const form = e.detail.elt;
    if (!form || form.tagName !== "FORM" || form.closest("[hx-disabled-elt]")) return;
    const buttons = Array.prototype.filter.call(form.querySelectorAll("[type=submit]"), function (b) { return !b.disabled; });
    buttons.forEach(function (b) { b.disabled = true; });
    lockedSubmits.set(e.detail.xhr, buttons);
  });
  document.addEventListener("htmx:afterRequest", function (e) {
    const buttons = e.detail.xhr && lockedSubmits.get(e.detail.xhr);
    if (buttons) buttons.forEach(function (b) { b.disabled = false; });
  });

  // Instant tab feedback (data-instant-tabs, e.g. templates/publishing/_subnav.html). The tab
  // takes the active look as soon as its request goes out (beforeSend, so a navigation the
  // unsaved-work guard cancelled never moves it), and a page slow to arrive shows the tab's
  // data-skeleton under the tabs after 150ms. Anything that ends the navigation without the
  // new page (an error, an abort, Back, a newer navigation) puts the previous tab back; the
  // swapped-in page brings its own server-rendered tabs either way.
  let tabNav = null; // { nav, prev, timer, skeleton, hidden }
  function setActiveTab(nav, link) {
    const on = (nav.dataset.activeClass || "").split(/\s+/).filter(Boolean);
    const off = (nav.dataset.inactiveClass || "").split(/\s+/).filter(Boolean);
    nav.querySelectorAll("a[href]").forEach(function (a) {
      const active = a === link;
      a.classList.remove.apply(a.classList, active ? off : on);
      a.classList.add.apply(a.classList, active ? on : off);
      if (active) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
  }
  function endTabNav(restore) {
    const t = tabNav;
    tabNav = null;
    if (!t) return;
    clearTimeout(t.timer);
    if (t.skeleton) t.skeleton.remove();
    t.hidden.forEach(function (el) { el.hidden = false; });
    if (restore && t.nav && document.contains(t.nav)) setActiveTab(t.nav, t.prev);
  }
  function showTabSkeleton(t, kind) {
    const tpl = document.getElementById("skeleton-" + kind);
    // From tabs: under the tabs. From any other link (t.nav null): the whole of #main.
    const anchor = t.nav || document.getElementById("main");
    if (!tpl || !anchor || !document.contains(anchor)) return;
    let el = t.nav ? t.nav.nextElementSibling : anchor.firstElementChild;
    while (el) {
      if (!el.hidden) { el.hidden = true; t.hidden.push(el); }
      el = el.nextElementSibling;
    }
    t.skeleton = tpl.content.firstElementChild.cloneNode(true);
    if (t.nav) t.nav.after(t.skeleton); else anchor.appendChild(t.skeleton);
  }
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("[data-instant-tabs] a[href]") && window.performance && performance.mark) {
      performance.mark("ak-tab-click");
    }
  }, true);
  document.addEventListener("htmx:beforeSend", function (e) {
    if (!isBoosted(e.detail) || isPreloaded(e.detail)) return;
    endTabNav(true); // a newer navigation replaces the one still in flight
    const elt = e.detail.elt;
    const link = elt && elt.closest && elt.closest("[data-instant-tabs] a[href]");
    if (!link) {
      // A link elsewhere with data-skeleton (a campaign row): the page's shape after 150ms.
      const other = elt && elt.closest && elt.closest("a[href][data-skeleton]");
      if (!other) return;
      const t = { nav: null, prev: null, timer: null, skeleton: null, hidden: [] };
      t.timer = setTimeout(function () { if (tabNav === t) showTabSkeleton(t, other.dataset.skeleton); }, 150);
      tabNav = t;
      return;
    }
    const nav = link.closest("[data-instant-tabs]");
    const prev = nav.querySelector('a[aria-current="page"]');
    if (prev === link) return;
    setActiveTab(nav, link);
    if (window.performance && performance.mark) performance.mark("ak-tab-active");
    const t = { nav: nav, prev: prev, timer: null, skeleton: null, hidden: [] };
    if (link.dataset.skeleton) {
      t.timer = setTimeout(function () { if (tabNav === t) showTabSkeleton(t, link.dataset.skeleton); }, 150);
    }
    tabNav = t;
  });
  document.addEventListener("htmx:beforeSwap", function (e) {
    if (isBoosted(e.detail) && e.detail.shouldSwap) endTabNav(false);
  });
  ["htmx:responseError", "htmx:sendError", "htmx:timeout", "htmx:abort"].forEach(function (name) {
    document.addEventListener(name, function (e) { if (isBoosted(e.detail)) endTabNav(true); });
  });
  window.addEventListener("popstate", function () { endTabNav(true); });

  // --- Overlay ---------------------------------------------------------------
  // One drawer-or-modal over the current page (components/overlay_host.html). A link marked
  // data-overlay-open="drawer|modal" (data-overlay-size="md|lg|xl") opens its own URL here
  // instead of navigating; the view answers with the partial (overlay_or_page in
  // apps/core/htmx.py) and the page underneath stays as it was.
  //   - History: opening pushes the link's URL (a refresh or a shared link gives the real
  //     page); Back, Esc, the backdrop and Close all close it, and closing pops that entry.
  //     The entry carries no htmx key, so htmx's own Back handling ignores it, and the capture
  //     listener below keeps htmx from re-fetching the page when Back lands on it again.
  //   - A navigation started from inside it (a boosted link) first puts the page's URL back
  //     with replaceState, so no stray overlay entry is left behind.
  //   - Forms inside post back into the overlay. A 400/422 re-renders there; a save answers
  //     204 + HX-Trigger overlay-close. Any successful post refreshes the page underneath with
  //     one request, so what it shows (a calendar day, a table row) is never stale.
  //   - In a drawer, J/K (and the arrows in its title row) step through the [data-row] rows
  //     of the list on screen, in the order shown.
  const overlay = { open: false, url: "", base: "", baseState: null, opener: null, pushed: false, rows: [], index: -1 };
  const overlayHost = () => document.getElementById("overlay-host");
  const overlayBody = () => document.getElementById("overlay-body");
  const reducedMotion = () => !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const isOverlayTarget = (detail) => !!(detail && detail.target && detail.target.id === "overlay-body");

  function overlayShape(kind, size) {
    const host = overlayHost();
    host.dataset.kind = kind === "modal" ? "modal" : "drawer";
    host.dataset.size = size || "md";
  }
  const DRAWER_LINK = 'a[data-overlay-open="drawer"][href]';
  const rowLink = (row) => (row.matches(DRAWER_LINK) ? row : row.querySelector(DRAWER_LINK));
  function overlayRows(opener) {
    const row = opener && opener.closest && opener.closest("[data-row]");
    if (!row || !row.closest("#main")) return { rows: [], index: -1 };
    const rows = Array.prototype.filter.call(document.querySelectorAll("#main [data-row]"), function (r) {
      return shown(r) && rowLink(r);
    });
    return { rows: rows, index: rows.indexOf(row) };
  }
  function syncOverlayStep() {
    const host = overlayHost();
    const step = host.querySelector("[data-overlay-step]");
    const many = host.dataset.kind === "drawer" && overlay.rows.length > 1 && overlay.index !== -1;
    step.hidden = !many;
    if (!many) return;
    host.querySelector("[data-overlay-position]").textContent = (overlay.index + 1) + " of " + overlay.rows.length;
    host.querySelector("[data-overlay-prev]").disabled = overlay.index === 0;
    host.querySelector("[data-overlay-next]").disabled = overlay.index === overlay.rows.length - 1;
  }
  function overlayPageview(url) {
    // GA4 counts the history change itself (enhanced measurement); this event is for
    // anything else listening, and for the browser harness.
    window.dispatchEvent(new CustomEvent("akilent:pageview", { detail: { path: url, overlay: true } }));
  }
  function loadOverlay(url) {
    const body = overlayBody();
    htmx.trigger(body, "htmx:abort"); // a newer step through the rows replaces a slower one
    body.setAttribute("aria-busy", "true");
    const pending = setTimeout(function () {
      const tpl = document.getElementById("skeleton-form");
      if (tpl && body.getAttribute("aria-busy") === "true") body.replaceChildren(tpl.content.firstElementChild.cloneNode(true));
    }, 150);
    htmx.ajax("GET", url, { source: body, target: body, swap: "innerHTML" }).finally(function () {
      clearTimeout(pending);
      body.setAttribute("aria-busy", "false");
    });
  }
  function openOverlay(url, opts) {
    const host = overlayHost();
    if (!host || !window.htmx) { window.location.href = url; return; }
    if (!overlay.open) {
      overlay.opener = opts.opener || document.activeElement;
      overlay.base = location.href;
      overlay.baseState = history.state;
      overlay.pushed = false;
      const found = overlayRows(overlay.opener);
      overlay.rows = found.rows;
      overlay.index = found.index;
      overlayShape(opts.kind, opts.size);
      host.querySelector("#overlay-title").textContent = opts.title || "";
      overlayBody().replaceChildren();
      host.hidden = false;
      overlay.open = true;
      document.documentElement.style.overflow = "hidden";
      akDialog.open(host, { close: closeOverlay, opener: overlay.opener });
      if (reducedMotion()) host.classList.add("is-open");
      else requestAnimationFrame(function () { requestAnimationFrame(function () { host.classList.add("is-open"); }); });
    }
    overlay.url = url;
    syncOverlayStep();
    const state = { akOverlay: url, base: overlay.base };
    if (opts.history === "push") {
      // The page's own entry needs htmx's key, or Back from a page loaded at the overlay's
      // URL (a refresh) could never bring this page back.
      if (!history.state) { history.replaceState({ htmx: true }, "", overlay.base); overlay.baseState = history.state; }
      history.pushState(state, "", url);
      overlay.pushed = true;
      overlay.depth = (overlay.depth || 0) + 1; // entries to go back to close it
    }
    else if (opts.history === "replace") history.replaceState(state, "", url);
    overlayPageview(url);
    loadOverlay(url);
  }
  // Hide it without touching history (the caller already has, or will).
  function hideOverlay() {
    if (!overlay.open) return;
    const host = overlayHost();
    const body = overlayBody();
    overlay.open = false;
    overlay.pushed = false;
    overlay.depth = 0;
    htmx.trigger(body, "htmx:abort");
    host.classList.remove("is-open");
    document.documentElement.style.overflow = "";
    akDialog.close(host);
    const done = function () { if (!overlay.open) { host.hidden = true; body.replaceChildren(); } };
    if (reducedMotion()) done(); else setTimeout(done, 200);
  }
  // What Esc, the backdrop, Close and a finished save do: pop the overlay's history entry
  // (popstate, below, then hides it), or just hide it if it never pushed one.
  function closeOverlay() {
    if (!overlay.open) return;
    if (overlay.pushed && history.state && history.state.akOverlay) history.go(-Math.max(1, overlay.depth || 1));
    else hideOverlay();
  }
  function stepOverlay(delta) {
    const i = overlay.index + delta;
    if (!overlay.open || i < 0 || i >= overlay.rows.length) return;
    const row = overlay.rows[i];
    const link = row.isConnected && rowLink(row);
    if (!link) return;
    overlay.index = i;
    overlay.opener = link; // focus goes back to the row you ended on
    akDialog.setOpener(overlayHost(), link);
    openOverlay(link.getAttribute("href"), { history: overlay.pushed ? "replace" : "none" });
    row.scrollIntoView({ block: "nearest" });
  }
  akilent.openOverlay = function (url, opts) {
    openOverlay(url, Object.assign({ history: "push" }, opts || {}));
  };
  akilent.closeOverlay = closeOverlay;

  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const t = e.target;
    if (!t.closest) return;
    if (overlay.open && t.closest("#overlay-host [data-overlay-close]")) { e.preventDefault(); closeOverlay(); return; }
    if (overlay.open && t.closest("#overlay-host [data-overlay-prev]")) { stepOverlay(-1); return; }
    if (overlay.open && t.closest("#overlay-host [data-overlay-next]")) { stepOverlay(1); return; }
    const link = t.closest("a[data-overlay-open][href]");
    if (!link) return;
    // One inside the open overlay (a file's "Used in" posts, its edit) loads in its place, as a
    // new history entry: Back returns to what was there. The grid's J/K order no longer applies.
    if (link.closest("#overlay-host")) {
      if (!overlay.open) return;
      e.preventDefault();
      overlay.rows = [];
      overlay.index = -1;
      openOverlay(link.getAttribute("href"), { history: overlay.pushed ? "push" : "none" });
      return;
    }
    // Captured before htmx's boost sees the click, so the page underneath stays.
    e.preventDefault();
    e.stopPropagation();
    openOverlay(link.getAttribute("href"), {
      kind: link.dataset.overlayOpen, size: link.dataset.overlaySize, opener: link,
      title: link.dataset.overlayTitle || "", history: "push",
    });
  }, true);
  document.addEventListener("keydown", function (e) {
    if (!overlay.open || e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
    if (!akDialog.isTop(overlayHost())) return;
    const t = e.target;
    if (t && (t.isContentEditable || (t.matches && t.matches("input, textarea, select")))) return;
    // ← → too where the content is a picture to step through (the media details).
    const arrows = !!overlayBody().querySelector("[data-overlay-arrows]");
    if (e.key === "j" || e.key === "J" || (arrows && e.key === "ArrowRight")) { e.preventDefault(); stepOverlay(1); }
    else if (e.key === "k" || e.key === "K" || (arrows && e.key === "ArrowLeft")) { e.preventDefault(); stepOverlay(-1); }
  });

  // Live filters (Content Studio app feel, Phase 3): <form method="get" data-live-filter="#results">.
  // Text and search fields apply 300ms after typing stops; selects, radios, checkboxes and
  // dates on change. Only the results region is replaced (the form stays, so focus stays in
  // the search box), and the URL follows with replaceState, so the address is always the
  // real, shareable page. Empty fields are left out of it. A filter chip
  // (a[data-chip-clear="name …"], publishing/_filter_chips.html) clears its fields and
  // applies. Without JavaScript the form's hidden submit and the chips' links still work.
  let liveSeq = 0;
  function liveFilter(form) {
    const sel = form.getAttribute("data-live-filter");
    const results = sel && document.querySelector(sel);
    if (!results || !window.htmx) { form.submit(); return; }
    const params = new URLSearchParams();
    new FormData(form).forEach(function (v, k) { if (typeof v === "string" && v !== "") params.append(k, v); });
    const qs = params.toString();
    const url = (form.getAttribute("action") || location.pathname) + (qs ? "?" + qs : "");
    const seq = ++liveSeq;
    results.setAttribute("aria-busy", "true");
    htmx.ajax("GET", url, { source: results, target: sel, select: sel, swap: "outerHTML" }).then(function () {
      if (seq !== liveSeq) return;
      history.replaceState(history.state, "", url);
      window.dispatchEvent(new CustomEvent("akilent:pageview", { detail: { path: url, filter: true } }));
    }).finally(function () {
      const now = document.querySelector(sel);
      if (now) now.removeAttribute("aria-busy");
    });
  }
  const liveText = 'input[type="search"], input[type="text"], input:not([type])';
  document.addEventListener("input", function (e) {
    const form = e.target.closest && e.target.closest("form[data-live-filter]");
    if (!form || !e.target.matches(liveText)) return;
    clearTimeout(form._liveTimer);
    form._liveTimer = setTimeout(function () { liveFilter(form); }, 300);
  });
  document.addEventListener("change", function (e) {
    const form = e.target.closest && e.target.closest("form[data-live-filter]");
    if (!form || e.target.matches(liveText) || !e.target.name) return;
    liveFilter(form);
  });
  document.addEventListener("submit", function (e) {
    const form = e.target;
    if (!form.matches || !form.matches("form[data-live-filter]")) return;
    // Ahead of htmx's boost: the results region is replaced, not the page.
    e.preventDefault();
    e.stopImmediatePropagation();
    clearTimeout(form._liveTimer);
    liveFilter(form);
  }, true);
  document.addEventListener("click", function (e) {
    const chip = e.target.closest && e.target.closest("a[data-chip-clear]");
    const form = chip && document.querySelector("#main form[data-live-filter]");
    if (!form || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    e.stopPropagation();
    chip.getAttribute("data-chip-clear").split(" ").forEach(function (name) {
      form.querySelectorAll('[name="' + CSS.escape(name) + '"]').forEach(function (f) {
        if (f.type === "radio") f.checked = f.hasAttribute("data-default"); // e.g. Media's "All"
        else if (f.type === "checkbox") f.checked = false;
        else f.value = "";
        // Alpine's x-model listens for these.
        f.dispatchEvent(new Event(f.type === "radio" || f.type === "checkbox" || f.tagName === "SELECT" ? "change" : "input", { bubbles: false }));
      });
    });
    liveFilter(form);
  }, true);

  // Popovers (<details class="pop">, static/css/controls.css) close like menus: a click
  // outside, or Esc (focus back on the trigger). A choice inside closes its own.
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.pop[open]").forEach(function (d) {
      if (!d.contains(e.target)) d.open = false;
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    const open = Array.prototype.slice.call(document.querySelectorAll("details.pop[open]"));
    if (!open.length) return;
    e.preventDefault();
    e.stopImmediatePropagation(); // just the popover, not the drawer or dialog under it
    open.forEach(function (d) { d.open = false; });
    const summary = open[open.length - 1].querySelector("summary");
    if (summary) summary.focus();
  }, true);

  // Page shortcuts (Content Studio app feel, Phase 5), never while typing or with a dialog
  // open: N the page's "new" ([data-shortcut-new], e.g. a post on Content Studio's tabs; a page
  // without one, like the inbox, leaves N alone), / the page's search ([data-search]), J/K the
  // next and previous row ([data-row]; Enter then opens it, as any link), ? lists them in the
  // command palette.
  function modalOpen() {
    return Array.prototype.some.call(document.querySelectorAll('[role="dialog"][aria-modal="true"], [data-dialog-panel]'),
      function (d) { return d.offsetParent !== null || d.getClientRects().length > 0; });
  }
  document.addEventListener("keydown", function (e) {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || overlay.open) return;
    const t = e.target;
    if (t && (t.isContentEditable || (t.matches && t.matches("input, textarea, select")))) return;
    if (modalOpen()) return;
    if (e.key === "?") {
      e.preventDefault();
      window.dispatchEvent(new CustomEvent("palette-open", { detail: { shortcuts: true } }));
    } else if (e.key === "/") {
      const search = document.querySelector("#main [data-search]");
      if (!search || !shown(search)) return;
      e.preventDefault();
      search.focus();
      if (search.select) search.select();
    } else if (e.key === "n" || e.key === "N") {
      const holder = document.querySelector("#main [data-shortcut-new]");
      const url = holder && holder.getAttribute("data-shortcut-new");
      if (!url || location.pathname === url) return;
      e.preventDefault();
      akilent.navigate(url);
    } else if (e.key === "j" || e.key === "k") {
      const rows = Array.prototype.filter.call(document.querySelectorAll("#main [data-row]"), shown);
      if (!rows.length) return;
      e.preventDefault();
      const at = rows.findIndex(function (r) { return r.contains(document.activeElement); });
      const next = rows[Math.min(rows.length - 1, Math.max(0, at === -1 ? 0 : at + (e.key === "j" ? 1 : -1)))];
      const target = next.matches("a[href], button") ? next : next.querySelector("a[href], button, input");
      if (target) { target.focus({ preventScroll: true }); next.scrollIntoView({ block: "nearest" }); }
    }
  });

  // Back and Forward. Capture phase, ahead of htmx's window.onpopstate: landing back on the
  // page under an open overlay just closes it (htmx would re-fetch the whole page).
  window.addEventListener("popstate", function (e) {
    const state = e.state;
    if (overlay.open) {
      if (state && state.akOverlay && state.base === overlay.base) {
        // Still inside: Back from something opened within the overlay shows what was before.
        if (state.akOverlay !== overlay.url) {
          overlay.depth = Math.max(1, (overlay.depth || 1) - 1);
          overlay.url = state.akOverlay;
          syncOverlayStep();
          loadOverlay(state.akOverlay);
        }
        return;
      }
      hideOverlay();
      if (location.href === overlay.base) e.stopImmediatePropagation();
      return;
    }
    if (state && state.akOverlay) {
      e.stopImmediatePropagation();
      // Forward onto an overlay over this same page reopens it; over any other page (the
      // page underneath has changed since), load its real page.
      if (overlay.base && state.base === overlay.base) {
        overlay.base = state.base;
        openOverlay(state.akOverlay, { history: "none" });
        overlay.pushed = true;
      } else {
        window.location.reload();
      }
    }
  }, true);

  // A navigation (boosted link or form) started inside the overlay: the page under it is
  // going away, so close it first and put the page's own URL back in place of the overlay's.
  document.addEventListener("htmx:beforeSend", function (e) {
    if (!overlay.open || !isBoosted(e.detail) || isPreloaded(e.detail)) return;
    if (overlay.pushed) history.replaceState(overlay.baseState, "", overlay.base);
    hideOverlay();
  });
  document.addEventListener("htmx:beforeHistorySave", function () {
    if (overlay.open) hideOverlay();
  });
  // Once another page is showing, an old overlay entry (Forward) belongs to a page that's gone.
  ["htmx:afterSettle", "htmx:historyRestore"].forEach(function (name) {
    document.addEventListener(name, function (e) {
      if (!overlay.open && (name === "htmx:historyRestore" || isBoosted(e.detail))) overlay.base = "";
    });
  });

  // A form inside the overlay posts back into it rather than navigating. hx-boost="false"
  // keeps a form native (one that leaves for another page, like "Edit and fix").
  document.addEventListener("htmx:beforeProcessNode", function (e) {
    const el = e.target;
    if (!el || el.tagName !== "FORM" || !el.closest("#overlay-body")) return;
    if (el.getAttribute("hx-boost") === "false" || el.hasAttribute("hx-post") || el.hasAttribute("hx-get")) return;
    if ((el.getAttribute("method") || "get").toLowerCase() !== "post") return;
    el.setAttribute("hx-post", el.getAttribute("action") || overlay.url);
    el.setAttribute("hx-target", "#overlay-body");
    el.setAttribute("hx-swap", "innerHTML");
  });

  document.addEventListener("htmx:beforeSwap", function (e) {
    if (!isOverlayTarget(e.detail)) return;
    if (!overlay.open) { e.detail.shouldSwap = false; return; }
    const status = e.detail.xhr.status;
    if (status === 400 || status === 422) { e.detail.shouldSwap = true; e.detail.isError = false; }
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    if (!isOverlayTarget(e.detail) || !overlay.open) return;
    const content = overlayBody().querySelector("[data-overlay]");
    if (!content) return;
    overlayShape(content.dataset.overlay, content.dataset.overlaySize);
    overlayHost().querySelector("#overlay-title").textContent = content.dataset.overlayTitle || "";
    syncOverlayStep();
    overlayBody().scrollTop = 0;
    // A frame later, once Alpine has set the new content up: before that, controls it disables
    // (the media editor's, until its picture is ready) still look enabled, and focus put on one
    // of them fell to <body> when Alpine disabled it.
    requestAnimationFrame(function () {
      if (!overlay.open || !content.isConnected) return;
      const invalid = content.querySelector('[aria-invalid="true"]');
      if (invalid && invalid.focus) invalid.focus({ preventScroll: true });
      else focusInitial(overlayHost(), true);
      overlayBody().scrollTop = 0;
    });
  });
  document.addEventListener("overlay-close", function () { closeOverlay(); });
  // A control disabled while it has focus drops focus to <body>, outside the open overlay (and
  // out of reach of its Esc and Tab handling): put it on Close instead.
  document.addEventListener("focusout", function (e) {
    if (!overlay.open || !e.target.closest || !e.target.closest("#overlay-host") || e.relatedTarget) return;
    setTimeout(function () {
      if (!overlay.open || document.activeElement !== document.body || !akDialog.isTop(overlayHost())) return;
      const close = overlayHost().querySelector(".ak-overlay-head [data-overlay-close]");
      if (close) close.focus({ preventScroll: true });
    }, 0);
  });

  // One request refreshes the page under the overlay after a save: its [data-cs-region]
  // parts if it marks any, else everything in <main>. Focus that was on a link inside goes
  // to the same link in the new copy.
  let refreshSeq = 0;
  function refreshUnderOverlay() {
    const main = mainEl();
    // A page holding unsaved work (the composer) is never replaced under you.
    if (!main || !window.htmx || main.querySelector('[data-overlay-refresh="off"]')) return;
    const url = overlay.open ? overlay.base : location.href;
    const seq = ++refreshSeq;
    fetch(url, { credentials: "same-origin", cache: "no-store", headers: { "X-Akilent-Refresh": "1" } })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (!html || seq !== refreshSeq || !main.isConnected) return;
        const doc = new DOMParser().parseFromString(html, "text/html");
        const fresh = doc.getElementById("main");
        if (!fresh) return;
        const active = document.activeElement;
        const href = active && main.contains(active) && active.getAttribute && active.getAttribute("href");
        const regions = main.querySelectorAll("[data-cs-region][id]");
        const scrollY = window.scrollY;
        if (regions.length) {
          regions.forEach(function (el) {
            const next = doc.getElementById(el.id);
            if (next) htmx.swap(el, next.outerHTML, { swapStyle: "outerHTML" });
          });
        } else {
          pageCleanups.forEach(function (c) { try { c(); } catch (err) { console.error(err); } });
          pageCleanups = [];
          htmx.swap(main, fresh.innerHTML, { swapStyle: "innerHTML" });
          runPageHooks();
        }
        window.scrollTo(0, scrollY);
        if (overlay.open) {
          const found = overlayRows(main.querySelector('a[data-overlay-open][href="' + CSS.escape(overlay.url) + '"]'));
          overlay.rows = found.rows;
          overlay.index = found.index;
          syncOverlayStep();
        }
        if (href) {
          const again = main.querySelector('a[href="' + CSS.escape(href) + '"]');
          if (again && !overlay.open) again.focus({ preventScroll: true });
        }
      })
      .catch(function () {});
  }
  // The same refresh after a toast's Undo (above), so the page shows what the undo changed.
  akilent.refreshPage = refreshUnderOverlay;
  document.addEventListener("htmx:afterRequest", function (e) {
    const verb = ((e.detail.requestConfig && e.detail.requestConfig.verb) || "get").toLowerCase();
    if (verb === "get" || !e.detail.successful || !e.detail.elt || !e.detail.elt.closest) return;
    if (!e.detail.elt.closest("#overlay-host")) return;
    refreshUnderOverlay();
  });
  // For a save made by a script rather than a form (the media editor's upload).
  akilent.overlaySaved = function (message) {
    if (message) window.toast("success", message);
    refreshUnderOverlay();
    closeOverlay();
  };

  // Never strand a navigation: if the in-place request can't complete, load the page. Why it
  // happened is kept (sessionStorage "akilent:hard-nav", and the console), so a reload nobody
  // asked for can be explained afterwards.
  function hardNavigate(e, reason) {
    const path = e.detail.requestConfig.path;
    const why = { reason: reason, path: path, from: location.pathname, status: e.detail.xhr ? e.detail.xhr.status : null,
      online: navigator.onLine, at: new Date().toISOString() };
    try { sessionStorage.setItem("akilent:hard-nav", JSON.stringify(why)); } catch (err) { /* private mode */ }
    console.warn("In-place navigation failed, loading the page instead:", why);
    window.location.href = path;
  }
  document.addEventListener("htmx:sendError", function (e) {
    if (isBoosted(e.detail) && e.detail.requestConfig.verb === "get") hardNavigate(e, "send-error");
  });
  document.addEventListener("htmx:timeout", function (e) {
    if (isBoosted(e.detail) && e.detail.requestConfig.verb === "get") hardNavigate(e, "timeout");
  });

  document.addEventListener("DOMContentLoaded", function () {
    if (window.htmx) {
      // Back/forward always re-fetch from the server: an Alpine page restored from a DOM
      // snapshot can come back half-initialised.
      window.htmx.config.historyCacheSize = 0;
      window.htmx.config.refreshOnHistoryMiss = false;
      window.htmx.config.timeout = 20000;
      // The View Transitions API itself checks prefers-reduced-motion for its default
      // cross-fade, but ours is a custom keyframe (assets/app.css) that bypasses that default,
      // so it needs its own check — otherwise a reduced-motion user still gets the animation.
      const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      window.htmx.config.globalViewTransitions = !reduceMotion;
    }
    markUnsafeMain();
    pageHooksStarted = true;
    runPageHooks();
  });

  // --- DNS auto-poll -----------------------------------------------------
  // Re-check pending domains so they flip to verified on their own once DNS
  // propagates. One global timer; polls only visible, pending, idle check
  // forms — verified cards drop the data-domain-pending marker and stop.
  //
  // The form carries hx-post/hx-target already, so the poll just fires it.
  // The poll marks the form silent for that one request only: the same form
  // clicked by hand should still say "not in DNS yet", while a poll running
  // every 20 seconds should keep that to itself.
  setInterval(function () {
    if (document.visibilityState && document.visibilityState !== "visible") return;
    if (!window.htmx) return;
    document.querySelectorAll("form[data-dns-check]").forEach(function (form) {
      if (!form.closest('[data-domain-pending="1"]')) return;
      if (form.classList.contains("htmx-request")) return;
      silentRequests.add(form);
      window.htmx.trigger(form, "submit");
    });
  }, 20000);

  // A verified domain isn't polled, so a card whose last check is old
  // (data-dns-stale) quietly re-checks itself once when the page opens -- DNS
  // records can be edited or deleted long after setup. The swapped-in card has
  // a fresh check time, so this never repeats.
  akilent.onPage(function (root) {
    if (!window.htmx || !root) return;
    root.querySelectorAll('[data-dns-stale="1"] form[data-dns-check]').forEach(function (form) {
      if (form.classList.contains("htmx-request")) return;
      silentRequests.add(form);
      window.htmx.trigger(form, "submit");
    });
  });
})();
