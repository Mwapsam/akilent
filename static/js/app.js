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
  akilent.onPage = function (fn) {
    pageHooks.push(fn);
    if (document.readyState !== "loading") {
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
  // apps/core/templatetags/nav.py does (path prefix, or exact).
  function syncNav() {
    const path = window.location.pathname;
    document.querySelectorAll("a[data-nav-match]").forEach(function (a) {
      const on = a.getAttribute("data-nav-match").split(" ").some(function (t) {
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
    const target = invalid && invalid.matches("input, select, textarea") ? invalid : main.querySelector("h1") || main;
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
  // so htmx:afterRequest never fires for it. Progress-bar/aria-busy bookkeping below counts
  // beforeRequest against afterRequest 1:1; treating a preload as boosted there would increment
  // on hover and never decrement, permanently stranding the bar and #main's dimmed state.
  const isPreloaded = (detail) =>
    !!(detail && detail.requestConfig && detail.requestConfig.headers && detail.requestConfig.headers["HX-Preloaded"] === "true");

  document.addEventListener("htmx:configRequest", function (e) {
    if (isBoosted(e.detail) || (e.detail.elt && e.detail.elt.closest && e.detail.elt.closest("[hx-boost]"))) {
      e.detail.headers["X-Akilent-Shell"] = shellName();
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

  document.addEventListener("htmx:historyRestore", function (e) {
    // The restore response is a whole page; make sure its <title> wins.
    const html = e.detail && e.detail.serverResponse;
    const m = typeof html === "string" && html.match(/<title>([\s\S]*?)<\/title>/i);
    if (m) {
      const t = document.createElement("textarea");
      t.innerHTML = m[1].trim();
      document.title = t.value;
    }
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
  document.addEventListener("htmx:beforeRequest", function (e) {
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

  // Never strand a navigation: if the in-place request can't complete, load the page.
  document.addEventListener("htmx:sendError", function (e) {
    if (isBoosted(e.detail) && e.detail.requestConfig.verb === "get") {
      window.location.href = e.detail.requestConfig.path;
    }
  });
  document.addEventListener("htmx:timeout", function (e) {
    if (isBoosted(e.detail) && e.detail.requestConfig.verb === "get") {
      window.location.href = e.detail.requestConfig.path;
    }
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
