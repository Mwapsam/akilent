/* App-wide interactivity: CSRF, toasts, sidebar/drawer state, and AJAX forms.
 * Works alongside (and registers stores on) Alpine. Progressive enhancement:
 * forms without data-ajax keep their normal submit/redirect behaviour, and if
 * JS is disabled everything still works server-side.
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
  window.navFocusLink = function (e, dir) {
    const link = e.target.closest('a[data-nav-link]');
    if (!link) return;
    const group = link.closest('[role="group"]');
    if (!group) return;
    const links = Array.from(group.querySelectorAll('a[data-nav-link]'));
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

  // --- AJAX form submission ---------------------------------------------
  // Opt in with <form data-ajax ...>. Optional attributes:
  //   data-confirm="message"   -> ask the confirm dialog first
  //   data-swap="#sel"         -> replace target's outerHTML with returned HTML
  //   data-append="#sel"       -> append returned HTML into target
  //   data-remove="#sel"       -> remove target on success (e.g. delete)
  //   data-reset               -> reset the form on success
  // Server returns an HTML partial (for swap/append) and/or an `X-Toast`
  // header ("type|message"); or JSON { redirect, toast }.
  async function handleAjaxForm(form, opts) {
    const silent = !!(opts && opts.silent); // auto-poll: swap DOM but stay quiet
    const submitBtn = form.querySelector('[type="submit"]');
    const setBusy = (b) => {
      if (submitBtn) { submitBtn.disabled = b; submitBtn.setAttribute("aria-busy", b ? "true" : "false"); }
      form.classList.toggle("is-submitting", b);
    };
    setBusy(true);
    try {
      const res = await fetch(form.action, {
        method: (form.method || "POST").toUpperCase(),
        headers: { "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": CSRF() },
        body: new FormData(form),
        credentials: "same-origin",
      });

      const toastHeader = res.headers.get("X-Toast");
      const ctype = res.headers.get("Content-Type") || "";

      if (!res.ok) {
        let msg = "Something went wrong.";
        if (ctype.includes("application/json")) {
          const j = await res.json().catch(() => ({}));
          msg = j.error || j.toast || msg;
        } else if (toastHeader) {
          msg = decodeToast(toastHeader).message || msg;
        }
        if (!silent) window.toast("danger", msg);
        return;
      }

      if (ctype.includes("application/json")) {
        const j = await res.json().catch(() => ({}));
        if (j.toast) window.toast(j.toast.type || "success", j.toast.message);
        if (j.redirect) { window.location.href = j.redirect; return; }
      } else {
        const html = await res.text();
        const swap = form.dataset.swap && document.querySelector(form.dataset.swap);
        const append = form.dataset.append && document.querySelector(form.dataset.append);
        if (html.trim() && swap) swap.outerHTML = html;
        else if (html.trim() && append) append.insertAdjacentHTML("beforeend", html);
      }

      if (form.dataset.remove) {
        const el = document.querySelector(form.dataset.remove);
        if (el) el.remove();
      }
      if (form.hasAttribute("data-reset")) form.reset();
      if (toastHeader) {
        const t = decodeToast(toastHeader);
        // While auto-polling, only surface good news (e.g. "verified") — don't
        // nag with "not found yet" every cycle.
        if (!silent || t.type === "success") window.toast(t.type, t.message);
      }
    } catch (e) {
      if (!silent) window.toast("danger", "Network error — please try again.");
    } finally {
      setBusy(false);
    }
  }

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

  // --- AJAX form submission ----------------------------------------------
  document.addEventListener("submit", function (e) {
    const form = e.target.closest("form[data-ajax]");
    if (!form) return;
    e.preventDefault();
    const confirmMsg = form.dataset.confirm;
    if (confirmMsg && window.Alpine && Alpine.store("confirm")) {
      Alpine.store("confirm")
        .ask({ message: confirmMsg, danger: form.hasAttribute("data-confirm-danger"),
               confirmLabel: form.dataset.confirmLabel || "Confirm" })
        .then((ok) => { if (ok) handleAjaxForm(form); });
    } else {
      handleAjaxForm(form);
    }
  });

  // --- DNS auto-poll -----------------------------------------------------
  // Re-check pending domains so they flip to verified on their own once DNS
  // propagates. One global timer; polls only visible, pending, idle check
  // forms — verified cards drop the data-domain-pending marker and stop.
  setInterval(function () {
    if (document.visibilityState && document.visibilityState !== "visible") return;
    document.querySelectorAll("form[data-dns-check][data-auto]").forEach(function (form) {
      if (!form.closest('[data-domain-pending="1"]')) return;
      if (form.classList.contains("is-submitting")) return;
      handleAjaxForm(form, { silent: true });
    });
  }, 20000);
})();
