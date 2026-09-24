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
  const CSRF = () => getCookie("csrftoken");

  // Parse an X-Toast header: "type|url-encoded-message".
  function decodeToast(header) {
    const i = header.indexOf("|");
    const type = i === -1 ? "success" : header.slice(0, i);
    let message = i === -1 ? "" : header.slice(i + 1);
    try { message = decodeURIComponent(message); } catch (_) {}
    return { type: type || "success", message };
  }

  // --- Toasts ------------------------------------------------------------
  // Backed by an Alpine store (see alpine:init); window.toast is the API.
  let _toastQueue = [];
  window.toast = function (type, message, opts) {
    const t = { type: type || "info", message: message, timeout: (opts && opts.timeout) || 4500 };
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
    Alpine.store("toasts", {
      items: [],
      _id: 0,
      push(t) {
        const id = ++this._id;
        this.items.push(Object.assign({ id }, t));
        if (t.timeout) setTimeout(() => this.remove(id), t.timeout);
      },
      remove(id) {
        this.items = this.items.filter((i) => i.id !== id);
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
    Alpine.store("confirm", {
      open: false, title: "", message: "", confirmLabel: "Confirm", danger: false, _resolve: null,
      ask(opts) {
        Object.assign(this, { open: true, danger: false, confirmLabel: "Confirm" }, opts || {});
        return new Promise((res) => (this._resolve = res));
      },
      respond(ok) { this.open = false; if (this._resolve) this._resolve(ok); this._resolve = null; },
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
  function isSilent(el) {
    return !!el && (silentRequests.has(el) || el.hasAttribute("hx-toast-silent"));
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
    if (!store) return; // Alpine not up yet: let HTMX fall back to confirm()
    e.preventDefault();
    const el = e.detail.elt;
    store
      .ask({
        message: question,
        danger: el.hasAttribute("hx-confirm-danger"),
        confirmLabel: el.getAttribute("hx-confirm-label") || "Confirm",
      })
      .then(function (ok) { if (ok) e.detail.issueRequest(true); });
  });

  // 2. Surface the X-Toast response header. hx-toast-silent suppresses all but
  //    good news, which is what an auto-poll wants — "not verified yet" every
  //    20s is nagging, "verified" is the thing you are waiting for.
  document.addEventListener("htmx:afterRequest", function (e) {
    const xhr = e.detail.xhr;
    if (!xhr) return;
    const header = xhr.getResponseHeader("X-Toast");
    if (!header) return;
    const t = decodeToast(header);
    if (isSilent(e.detail.elt) && t.type !== "success") return;
    window.toast(t.type, t.message);
  });

  // 3. A 4xx/5xx or a dropped connection is silent in HTMX by default — it
  //    swaps nothing and says nothing, so the button just looks broken.
  document.addEventListener("htmx:responseError", function (e) {
    if (isSilent(e.detail.elt)) return;
    if (e.detail.xhr && e.detail.xhr.getResponseHeader("X-Toast")) return; // already toasted above
    window.toast("danger", "Something went wrong.");
  });
  document.addEventListener("htmx:sendError", function (e) {
    if (isSilent(e.detail.elt)) return;
    window.toast("danger", "Network error — please try again.");
  });

  // Silence lasts one request. A successful poll swaps the element away and
  // takes its entry with it, but a failed one leaves the same element in place
  // — and the next click on it is the user's, which must not be silent. The
  // timeout defers the clear past the toast handlers above, whatever order
  // HTMX fires them in.
  document.addEventListener("htmx:afterRequest", function (e) {
    const el = e.detail.elt;
    if (silentRequests.has(el)) setTimeout(function () { silentRequests.delete(el); }, 0);
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
})();
