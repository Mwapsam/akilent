/* Two-pane inbox (≥1024px): loads a conversation into #conversation-pane-inner instead of
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
 *   - at ≥1024px the two columns fill the viewport and scroll on their own.
 */
(function () {
  "use strict";

  var WIDE = "(min-width: 1024px)";

  function isWide() {
    return window.matchMedia(WIDE).matches;
  }

  akilent.onPage(function () {
    var split = document.getElementById("inbox-split");
    var list = document.getElementById("inbox-body");
    var pane = document.getElementById("conversation-pane-inner");
    if (!split || !list || !pane) return; // not the inbox list page

    var inboxTitle = document.title;
    var inboxUrl = window.location.href;

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
      loadSeq++;
      paneEpoch++;
      window.htmx.trigger(pane, "htmx:abort");
      delete split.dataset.open;
      split.classList.remove("has-conversation", "is-loading");
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
    function openInPane(url, push) {
      var mine = ++loadSeq;
      paneEpoch++; // and again when it swaps in (onBeforeSwap): forms sent meanwhile are A's
      lastLoad = { url: url, push: push };
      split.dataset.open = url;
      selectRow(rowFor(url));
      split.classList.add("has-conversation", "is-loading");
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
      window.htmx
        .ajax("GET", url, { target: "#conversation-pane-inner", swap: "innerHTML", source: pane, headers: paneHeaders })
        .then(function () {
          stopListening();
          if (mine !== loadSeq) return; // a newer conversation was asked for meanwhile
          if (failed) throw new Error("pane did not load");
          // Answered, but not with a conversation (a sign-in page, an unexpected redirect):
          // the full page shows whatever it is.
          if (!pane.querySelector("[data-title]")) { window.location.href = url; return; }
          split.classList.remove("is-loading");
          if (push) history.pushState({ akilentPane: url }, "", url);
          setTitle();
          focusPaneHeading();
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
      else paneEpoch++; // the pane is about to show this conversation
    }

    // The pane's error state asks for a retry through data-error-retry-event (app.js) rather
    // than repeating the raw request, so a retry goes through openInPane: latest-click-wins,
    // history and title included.
    function onRetry() { if (lastLoad) openInPane(lastLoad.url, lastLoad.push); }

    // The list poll re-renders every row; keep the open conversation highlighted.
    function onAfterSwap(e) {
      if (e.detail && e.detail.target === list && split.dataset.open) selectRow(rowFor(split.dataset.open));
    }

    // At ≥1024px the split fills the viewport below it, so each column scrolls on its own
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

    document.addEventListener("click", onClickCapture, true);
    window.addEventListener("popstate", onPopState);
    document.addEventListener("htmx:afterSwap", onAfterSwap);
    document.addEventListener("htmx:beforeSwap", onBeforeSwap);
    document.addEventListener("htmx:beforeRequest", onBeforeRequest);
    pane.addEventListener("inbox-pane-retry", onRetry);
    window.addEventListener("resize", fit, { passive: true });
    return function cleanup() {
      document.removeEventListener("click", onClickCapture, true);
      window.removeEventListener("popstate", onPopState);
      document.removeEventListener("htmx:afterSwap", onAfterSwap);
      document.removeEventListener("htmx:beforeSwap", onBeforeSwap);
      document.removeEventListener("htmx:beforeRequest", onBeforeRequest);
      pane.removeEventListener("inbox-pane-retry", onRetry);
      window.removeEventListener("resize", fit);
      document.title = inboxTitle;
    };
  });
})();
