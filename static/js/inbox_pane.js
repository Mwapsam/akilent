/* Two-pane inbox (≥768px, Phase 5F; it was 1024px): loads a conversation into #conversation-pane-inner instead of
 * navigating the whole page, so switching between conversations keeps the list in view. Below
 * that width there is no pane to load into, so a row's own href is left alone and the existing
 * full-page boosted navigation (apps/core/htmx.py) handles it exactly as it did before this file
 * existed. Does nothing on any other page — akilent.onPage below returns immediately when the
 * markup it looks for isn't there.
 *
 * Also owns what the two-pane view needs to stay coherent:
 *   - Back / Forward: the address bar follows the open conversation (pushState), so popstate
 *     must close or re-open the pane to match it.
 *   - the open row stays highlighted across the list's 8-second poll, which re-renders it.
 *   - the tab title names the open conversation.
 *   - with two panes the columns fill the viewport and scroll on their own.
 *   - the inbox's keys (onKey): J/K open the next or previous conversation as a peek, which is
 *     marked read only once it has stayed open PEEK_READ_MS; R reply, E close (with Undo), A
 *     assign to me, Esc back to the list. Never while typing or with a dialog open.
 *   - opening feels instant: the skeleton shows the row's name and avatar at once, a row is
 *     fetched ahead when hovered (or next after J), and the last few opened are kept a minute
 *     (paneCache), each only while its row in the list still reads the same.
 */
(function () {
  "use strict";

  var WIDE = "(min-width: 768px)";

  function isWide() {
    return window.matchMedia(WIDE).matches;
  }

  akilent.onPage(function () {
    var split = document.getElementById("inbox-split");
    var list = document.getElementById("inbox-body");
    var pane = document.getElementById("conversation-pane-inner");
    if (!split || !list || !pane) return; // not the inbox list page

    // What closing the pane goes back to. Taken from the page, not the address bar: a
    // conversation's own address loaded as a full page renders the inbox with it open.
    var inboxTitle = split.getAttribute("data-inbox-title") || document.title;
    var inboxUrl = new URL(split.getAttribute("data-inbox-url") || window.location.href, window.location.href).href;

    function rowFor(url) {
      if (!url) return null;
      var path = new URL(url, window.location.href).pathname;
      var rows = list.querySelectorAll(".inbox-row");
      for (var i = 0; i < rows.length; i++) {
        if (new URL(rows[i].href).pathname === path) return rows[i];
      }
      return null;
    }

    function selectRow(a) {
      list.querySelectorAll(".inbox-row").forEach(function (el) {
        if (el === a) el.setAttribute("aria-current", "true");
        else el.removeAttribute("aria-current");
      });
    }

    function focusPaneHeading() {
      var h1 = pane.querySelector("h1");
      if (!h1) return;
      if (!h1.hasAttribute("tabindex")) h1.setAttribute("tabindex", "-1");
      h1.focus({ preventScroll: true });
    }

    function setTitle() {
      var root = pane.querySelector("[data-title]");
      document.title = root ? root.getAttribute("data-title") + " — Inbox" : inboxTitle;
    }

    function closePane() {
      // A load still in flight must not land in a closed pane or push a history entry.
      cancelRead();
      loadSeq++;
      paneEpoch++;
      window.htmx.trigger(pane, "htmx:abort");
      delete split.dataset.open;
      split.classList.remove("has-conversation", "is-loading");
      fit();
      pane.innerHTML = "";
      selectRow(null);
      document.title = inboxTitle;
    }

    // Loads ``url`` into the pane. ``push`` adds a history entry (a click); a popstate
    // re-open must not, or Back would bounce between the same two entries.
    // Only the latest call finishes the job: clicking B while A is still loading must
    // not leave A's title, history entry or focus behind once A's answer arrives.
    var loadSeq = 0;
    // Bumped whenever the pane starts showing a different conversation (or closes). A form
    // inside the pane (assign, tags, snooze) is stamped with it when sent, so its answer
    // can't land over the next conversation once the person has moved on.
    var paneEpoch = 0;
    var lastLoad = null; // what the pane's Try again repeats
    function pathOf(u) { return new URL(u, window.location.href).pathname; }
    // Pane loads carry this header (set below); nothing else sends it. Forms inside the pane
    // (assign, tags, snooze) also target the pane and must always swap. htmx fires the swap
    // events on the target, so the event's element can't tell the two apart.
    var PANE_HEADER = "X-Inbox-Pane-Load";
    var paneHeaders = {};
    paneHeaders[PANE_HEADER] = "1";
    function isPaneLoad(detail) {
      var cfg = detail && detail.requestConfig;
      return !!(cfg && cfg.headers && cfg.headers[PANE_HEADER]);
    }
    // A peek (J/K) is marked read only after it has stayed open this long, so stepping past
    // conversations doesn't clear their unread; a click marks read at once, as before.
    var PEEK_READ_MS = 1500;
    var readTimer = null;
    function cancelRead() { clearTimeout(readTimer); readTimer = null; }
    function markReadLater(url, mine, delay) {
      readTimer = setTimeout(function () {
        readTimer = null;
        if (mine !== loadSeq || split.dataset.open !== url) return; // moved on meanwhile
        var token = "";
        try { token = JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRFToken"] || ""; } catch (e) { /* none */ }
        fetch(pathOf(url), {
          method: "POST",
          credentials: "same-origin",
          headers: { "X-CSRFToken": token, "X-Requested-With": "XMLHttpRequest", "Content-Type": "application/x-www-form-urlencoded" },
          body: "action=mark_read",
        }).then(function (r) { if (r.ok) window.htmx.trigger(document.body, "inbox-refresh"); }).catch(function () { /* stays unread */ });
      }, delay === undefined ? PEEK_READ_MS : delay);
    }

    // Panes fetched ahead or opened lately, by path. An entry is used only while its row in
    // the list reads the same as when it was fetched: the 8-second poll re-renders a row whose
    // status, assignee, last message or "Not sent" changed, so a stale pane is never shown.
    // New messages after it are the thread's own poll's job (chat.js, from lastId), as on any
    // open. Every fetch ahead is a peek, so it never marks anything read.
    var CACHE_MS = 60000;
    var CACHE_MAX = 5;
    var PREFETCH_HOVER_MS = 150;
    var paneCache = new Map();
    var prefetching = null; // one at a time, so hovering down the list can't queue a burst
    var hoverTimer = null;
    function rowKey(url) {
      var r = rowFor(url);
      // Reading it marks it read; that alone doesn't make the pane stale.
      return r ? r.textContent.replace("Unread.", "").replace(/\s+/g, " ").trim() : null;
    }
    function remember(url, html) {
      var key = rowKey(url);
      // Only a conversation: never a sign-in page or an error state.
      if (key === null || !html || html.indexOf("data-title") === -1) return;
      var path = pathOf(url);
      paneCache.delete(path);
      paneCache.set(path, { html: html, at: Date.now(), key: key });
      while (paneCache.size > CACHE_MAX) paneCache.delete(paneCache.keys().next().value);
    }
    function cached(url) {
      var hit = paneCache.get(pathOf(url));
      if (!hit) return null;
      if (Date.now() - hit.at > CACHE_MS || hit.key !== rowKey(url)) { paneCache.delete(pathOf(url)); return null; }
      return hit.html;
    }
    function prefetch(url) {
      if (!url || prefetching || cached(url) || split.dataset.open === url) return;
      if (navigator.connection && navigator.connection.saveData) return;
      var u = new URL(url, window.location.href);
      u.searchParams.set("peek", "1");
      var path = pathOf(url);
      prefetching = path;
      fetch(u.href, {
        credentials: "same-origin",
        headers: { "HX-Request": "true", "HX-Target": "conversation-pane-inner", "X-Inbox-Prefetch": "1" },
      }).then(function (r) {
        // A sign-in page or an error is not worth keeping: the real open will say what it is.
        return r.ok && !r.redirected ? r.text() : null;
      }).then(function (html) {
        remember(url, html);
      }).catch(function () { /* the real open fetches it */ }).then(function () {
        if (prefetching === path) prefetching = null;
      });
    }
    function onHover(e) {
      var a = e.target.closest && e.target.closest(".inbox-row");
      if (!a || !list.contains(a) || !isWide()) return;
      clearTimeout(hoverTimer);
      hoverTimer = setTimeout(function () { prefetch(a.href); }, PREFETCH_HOVER_MS);
    }
    function onHoverOut(e) {
      var a = e.target.closest && e.target.closest(".inbox-row");
      if (a && !a.contains(e.relatedTarget)) clearTimeout(hoverTimer);
    }
    // The skeleton names the conversation being opened at once, from its row.
    function fillSkeleton(url) {
      var row = rowFor(url);
      var name = split.querySelector("[data-skel-name]");
      var avatar = split.querySelector("[data-skel-avatar]");
      if (!name || !avatar) return;
      var rowAvatar = row && row.querySelector(".inbox-avatar");
      name.textContent = row ? row.getAttribute("data-title") || "" : "";
      avatar.innerHTML = rowAvatar ? rowAvatar.innerHTML : "";
      split.classList.toggle("skel-named", !!row);
    }

    // ``push``: true adds a history entry (a click), "replace" swaps the current one (J/K
    // stepping through the list), false leaves history alone (a popstate re-open).
    // ``peek``: opened by J/K; it isn't marked read on opening (see markReadLater), and focus
    // stays on its row so the next J/K carries on from there.
    function openInPane(url, push, peek) {
      cancelRead();
      var mine = ++loadSeq;
      paneEpoch++; // and again when it swaps in (onBeforeSwap): forms sent meanwhile are A's
      lastLoad = { url: url, push: push };
      split.dataset.open = url;
      selectRow(rowFor(url));
      split.classList.add("has-conversation", "is-loading");
      fit(); // the inbox header is shorter while a conversation is open
      var wasUnread = !!(rowFor(url) && rowFor(url).classList.contains("is-unread"));
      // What a shown pane needs, however it arrived.
      function shown() {
        split.classList.remove("is-loading");
        if (push === "replace") history.replaceState({ akilentPane: url }, "", url);
        else if (push) history.pushState({ akilentPane: url }, "", url);
        setTitle();
        if (peek) { if (wasUnread) markReadLater(url, mine); }
        else focusPaneHeading();
      }
      var html = cached(url);
      if (html) {
        window.htmx.trigger(pane, "htmx:abort");
        paneEpoch++;
        window.htmx.swap(pane, html, { swapStyle: "innerHTML" });
        // Kept from a peek, so a click still has to mark it read, at once as always.
        if (!peek && wasUnread) markReadLater(url, mine, 0);
        shown();
        return;
      }
      fillSkeleton(url);
      var failed = false;
      var status = 0;
      // Only this load's own failure: a form inside the pane (assign, tags, snooze) also
      // targets the pane, and its error must not be read as the conversation failing to open.
      function onError(e) {
        var cfg = e.detail && e.detail.requestConfig;
        if (isPaneLoad(e.detail) && cfg && pathOf(cfg.path) === pathOf(url)) {
          failed = true;
          status = e.detail.xhr ? e.detail.xhr.status : 0;
        }
      }
      function stopListening() {
        document.removeEventListener("htmx:responseError", onError);
        document.removeEventListener("htmx:sendError", onError);
        document.removeEventListener("htmx:timeout", onError);
      }
      document.addEventListener("htmx:responseError", onError);
      document.addEventListener("htmx:sendError", onError);
      document.addEventListener("htmx:timeout", onError);
      // htmx queues a second request on a source element behind the first and resolves its
      // promise at once, so clicking B then C opened B. The pane is the source, and any load
      // still in flight on it is cancelled first; onBeforeSwap below also drops a late answer
      // that is not for the conversation asked for last.
      // app.js never runs a View Transition for a pane load (it isn't boosted), so the swap
      // has happened by the time the promise resolves and the pane can be read straight away.
      window.htmx.trigger(pane, "htmx:abort");
      var requestUrl = url;
      if (peek) { var u = new URL(url, window.location.href); u.searchParams.set("peek", "1"); requestUrl = u.href; }
      window.htmx
        .ajax("GET", requestUrl, { target: "#conversation-pane-inner", swap: "innerHTML", source: pane, headers: paneHeaders })
        .then(function () {
          stopListening();
          if (mine !== loadSeq) return; // a newer conversation was asked for meanwhile
          if (failed) throw new Error("pane did not load");
          // Answered, but not with a conversation (a sign-in page, an unexpected redirect):
          // the full page shows whatever it is.
          if (!pane.querySelector("[data-title]")) { window.location.href = url; return; }
          shown();
        })
        .catch(function () {
          stopListening();
          if (mine !== loadSeq) return;
          split.classList.remove("is-loading");
          // Rejected without an error of its own: cancelled (htmx:abort), not failed.
          if (!failed) return;
          // Lost connection, a timeout or a server error: the pane (a data-error-region)
          // now says so, with Try again (which comes back as onRetry). Leaving the page
          // instead would land an offline user on the browser's own error page and lose the
          // list beside it.
          if (failed && status !== 403 && status !== 404 && status !== 410 && pane.querySelector("[data-error-state]")) return;
          // A conversation that is gone or not visible, or anything else unexpected: the
          // full page says why better than the pane can.
          window.location.href = url;
        });
    }

    // A capture-phase listener on document runs before htmx's own (bubble-phase) click
    // handling, so stopPropagation here keeps htmx from also boosting this same click into a
    // full-page navigation — the two would otherwise both fire for one click.
    function onClickCapture(e) {
      if (!isWide()) return; // let the row's plain href navigate the whole page instead
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; // new tab etc.
      var a = e.target.closest(".inbox-row");
      if (!a || !list.contains(a)) return;
      e.preventDefault();
      e.stopPropagation();
      // Already open and showing: nothing to do. A row whose load failed (the pane shows the
      // error state) or is still loading opens again, rather than ignoring the click.
      if (a.getAttribute("aria-current") === "true" && pane.querySelector("[data-title]") &&
          !split.classList.contains("is-loading")) return;
      openInPane(a.href, true);
    }

    // Back / Forward between the inbox and the conversations opened from it.
    function onPopState(e) {
      if (!document.body.contains(split)) return; // htmx restored a different page
      var url = window.location.href;
      if (e.state && e.state.akilentPane && isWide()) {
        if (split.dataset.open !== url) openInPane(url, false);
      } else if (new URL(url).pathname === new URL(inboxUrl).pathname) {
        closePane();
      }
    }

    // A late answer for a conversation that is no longer the one asked for never replaces
    // the newer one (race safety: the last click wins, whatever order the answers arrive).
    // Also: nothing lands once the pane has been closed (Back to the inbox).
    // A form's answer from a conversation that has since been replaced or closed is dropped
    // the same way (what it did on the server still happened; only the stale view is not shown).
    function inPane(el) { return !!el && (el === pane || pane.contains(el)); }
    function onBeforeRequest(e) {
      if (!isPaneLoad(e.detail) && inPane(e.detail.target) && e.detail.xhr) e.detail.xhr._akPaneEpoch = paneEpoch;
      var verb = e.detail.requestConfig && e.detail.requestConfig.verb;
      if (verb && verb !== "get" && split.dataset.open && (inPane(e.detail.elt) || inPane(e.detail.target))) {
        paneCache.delete(pathOf(split.dataset.open));
      }
    }
    function onBeforeSwap(e) {
      var cfg = e.detail && e.detail.requestConfig;
      if (!cfg) return;
      if (!isPaneLoad(e.detail)) {
        var xhr = e.detail.xhr;
        // Also while another conversation is loading: the pane is about to be replaced, and
        // if that load fails, its error must be what the pane shows, not the old form's answer.
        if (xhr && xhr._akPaneEpoch !== undefined &&
            (xhr._akPaneEpoch !== paneEpoch || split.classList.contains("is-loading"))) e.detail.shouldSwap = false;
        return;
      }
      if (!split.dataset.open || pathOf(cfg.path) !== pathOf(split.dataset.open)) e.detail.shouldSwap = false;
      else {
        paneEpoch++; // the pane is about to show this conversation
        if (e.detail.xhr && e.detail.xhr.status === 200) remember(split.dataset.open, e.detail.serverResponse);
      }
    }

    // The pane's error state asks for a retry through data-error-retry-event (app.js) rather
    // than repeating the raw request, so a retry goes through openInPane: latest-click-wins,
    // history and title included.
    function onRetry() { if (lastLoad) openInPane(lastLoad.url, lastLoad.push); }

    // A row whose content changed on a refresh (a new message, a status, an assignee) is
    // highlighted for a moment (.is-changed), so the eye finds it (Phase 5E). Read before the
    // swap, compared after; never on a search's swap, which fades in as a whole (.is-fresh).
    var before = null;
    // Its words, without its times: "5m" becoming "6m" isn't news.
    function rowText(r) {
      var c = r.cloneNode(true);
      c.querySelectorAll("time").forEach(function (t) { t.remove(); });
      return c.textContent.replace(/\s+/g, " ");
    }
    function rowTexts() {
      var map = {};
      list.querySelectorAll(".inbox-row").forEach(function (r) { map[pathOf(r.href)] = rowText(r); });
      return map;
    }
    // The row that had focus before the poll re-rendered the list, by path, so focus (and
    // with it J/K and Enter) stays on the same conversation even if rows moved.
    var focusedPath = null;
    function onBeforeListSwap(e) {
      before = e.detail && e.detail.target === list && !list.classList.contains("is-fresh") ? rowTexts() : null;
      var f = document.activeElement && document.activeElement.closest ? document.activeElement.closest(".inbox-row") : null;
      focusedPath = e.detail && e.detail.target === list && f && list.contains(f) ? pathOf(f.href) : null;
    }

    // The list poll re-renders every row; keep the open conversation highlighted.
    function onAfterSwap(e) {
      if (!e.detail || e.detail.target !== list) return;
      if (split.dataset.open) selectRow(rowFor(split.dataset.open));
      if (focusedPath) {
        var again = rowFor(focusedPath);
        if (again && document.activeElement === document.body) again.focus({ preventScroll: true });
        focusedPath = null;
      }
      if (!before) return;
      var was = before;
      before = null;
      list.querySelectorAll(".inbox-row").forEach(function (r) {
        var path = pathOf(r.href);
        if (path in was && was[path] === rowText(r)) return;
        if (!(path in was) && Object.keys(was).length === 0) return;
        r.classList.add("is-changed");
      });
    }

    // With two panes the split fills the viewport below it, so each column scrolls on its own
    // instead of the page scrolling both. Measured, like chat.js `_fit`, because what sits
    // above it (header, banners) varies.
    function fit() {
      if (!isWide()) { split.style.removeProperty("--inbox-top"); return; }
      var main = document.getElementById("main");
      var pad = main ? parseFloat(getComputedStyle(main).paddingBottom) || 0 : 0;
      var top = split.getBoundingClientRect().top + window.scrollY;
      split.style.setProperty("--inbox-top", Math.round(top + pad) + "px");
    }
    requestAnimationFrame(fit);

    // Opened as a full page with a conversation already in the pane (a reload, a link):
    // mark its row and record the history entry the way a click would have, so Back,
    // Forward and the next click behave the same as after opening it from the list.
    if (split.dataset.open && pane.querySelector("[data-title]")) {
      split.dataset.open = window.location.href;
      selectRow(rowFor(window.location.href));
      history.replaceState(Object.assign({}, history.state, { akilentPane: window.location.href }), "", window.location.href);
    }

    // The inbox's keys. Capture phase, so they're handled here before app.js's page shortcuts
    // (which skip a key already handled): there J/K only move focus between rows, which is
    // still what they do below 768px, where there's no pane to open into.
    function typing(t) {
      return !!t && (t.isContentEditable || (t.matches && t.matches("input, textarea, select")));
    }
    // Any dialog or menu showing, modal or not (the template picker opens over the thread):
    // its own Esc closes it, not the conversation.
    function dialogOpen() {
      return Array.prototype.some.call(document.querySelectorAll('[role="dialog"], [role="menu"]'),
        function (d) { return d.offsetParent !== null || d.getClientRects().length > 0; });
    }
    function chatData() {
      var cv = pane.querySelector(".cv");
      return cv && window.Alpine ? window.Alpine.$data(cv) : null;
    }
    // A pane action as if from its own form: answered with the pane again (and a toast, with
    // Undo for a close), stamped like any form in the pane so it can't land on the next one.
    function paneAction(values) {
      var url = split.dataset.open;
      if (!url) return;
      window.htmx.ajax("POST", pathOf(url), { target: "#conversation-pane-inner", swap: "innerHTML",
        source: pane.querySelector(".cv") || pane, values: values });
    }
    function onKey(e) {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
      if (!document.body.contains(split) || !isWide() || typing(e.target) || dialogOpen()) return;
      var key = e.key;
      var open = !!split.dataset.open && !!pane.querySelector("[data-title]");
      if (key === "j" || key === "k") {
        var rows = Array.prototype.filter.call(list.querySelectorAll(".inbox-row"), function (r) { return r.offsetParent !== null; });
        if (!rows.length) return;
        e.preventDefault();
        // From the focused row (what the person sees as "here"), else the open one.
        var focused = document.activeElement && document.activeElement.closest ? document.activeElement.closest(".inbox-row") : null;
        var at = focused ? rows.indexOf(focused) : -1;
        if (at === -1) at = rows.findIndex(function (r) { return r.getAttribute("aria-current") === "true"; });
        var next = rows[Math.min(rows.length - 1, Math.max(0, at === -1 ? 0 : at + (key === "j" ? 1 : -1)))];
        next.focus({ preventScroll: true });
        next.scrollIntoView({ block: "nearest" });
        if (next.getAttribute("aria-current") === "true" && open) return; // already showing it
        openInPane(next.href, split.dataset.open ? "replace" : true, true);
        var after = rows[rows.indexOf(next) + (key === "j" ? 1 : -1)];
        if (after) setTimeout(function () { prefetch(after.href); }, PREFETCH_HOVER_MS);
        return;
      }
      if (key === "Enter") {
        // Enter opens the row that has focus, never the one open before (rows are links,
        // but the list poll may have re-rendered them between the keys).
        var row = document.activeElement && document.activeElement.closest ? document.activeElement.closest(".inbox-row") : null;
        if (!row || !list.contains(row)) return;
        e.preventDefault();
        e.stopPropagation();
        if (row.getAttribute("aria-current") === "true" && open && !split.classList.contains("is-loading")) {
          focusPaneHeading();
          return;
        }
        openInPane(row.href, true);
        return;
      }
      if (!open) return;
      var chat = chatData();
      if (key === "r") {
        var box = pane.querySelector(".cv-composer textarea");
        if (!box || box.offsetParent === null || box.disabled) return;
        e.preventDefault();
        box.focus();
      } else if (key === "e") {
        if (!chat || !chat.open) return; // already closed
        e.preventDefault();
        paneAction({ action: "close" });
      } else if (key === "a") {
        var me = pane.querySelector("[data-me]");
        if (!me) return;
        e.preventDefault();
        paneAction({ action: "assign", assignee: me.getAttribute("data-me") });
      } else if (key === "Escape") {
        e.preventDefault();
        var row = rowFor(split.dataset.open);
        history.pushState(null, "", inboxUrl);
        closePane();
        if (row) row.focus({ preventScroll: true });
      }
    }

    document.addEventListener("click", onClickCapture, true);
    document.addEventListener("keydown", onKey, true);
    list.addEventListener("mouseover", onHover);
    list.addEventListener("mouseout", onHoverOut);
    window.addEventListener("popstate", onPopState);
    document.addEventListener("htmx:afterSwap", onAfterSwap);
    document.addEventListener("htmx:beforeSwap", onBeforeSwap);
    document.addEventListener("htmx:beforeSwap", onBeforeListSwap);
    document.addEventListener("htmx:beforeRequest", onBeforeRequest);
    pane.addEventListener("inbox-pane-retry", onRetry);
    window.addEventListener("resize", fit, { passive: true });
    return function cleanup() {
      document.removeEventListener("click", onClickCapture, true);
      document.removeEventListener("keydown", onKey, true);
      list.removeEventListener("mouseover", onHover);
      list.removeEventListener("mouseout", onHoverOut);
      clearTimeout(hoverTimer);
      paneCache.clear();
      cancelRead();
      window.removeEventListener("popstate", onPopState);
      document.removeEventListener("htmx:afterSwap", onAfterSwap);
      document.removeEventListener("htmx:beforeSwap", onBeforeSwap);
      document.removeEventListener("htmx:beforeSwap", onBeforeListSwap);
      document.removeEventListener("htmx:beforeRequest", onBeforeRequest);
      pane.removeEventListener("inbox-pane-retry", onRetry);
      window.removeEventListener("resize", fit);
      document.title = inboxTitle;
    };
  });
})();
