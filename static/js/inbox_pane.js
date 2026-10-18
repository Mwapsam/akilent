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
      delete split.dataset.open;
      split.classList.remove("has-conversation", "is-loading");
      pane.innerHTML = "";
      selectRow(null);
      document.title = inboxTitle;
    }

    // Loads ``url`` into the pane. ``push`` adds a history entry (a click); a popstate
    // re-open must not, or Back would bounce between the same two entries.
    function openInPane(url, push) {
      split.dataset.open = url;
      selectRow(rowFor(url));
      split.classList.add("has-conversation", "is-loading");
      var failed = false;
      function onError(e) {
        if (e.detail && e.detail.target === pane) failed = true;
      }
      document.addEventListener("htmx:responseError", onError);
      document.addEventListener("htmx:sendError", onError);
      window.htmx
        .ajax("GET", url, { target: "#conversation-pane-inner", swap: "innerHTML", source: document.body })
        .then(function () {
          document.removeEventListener("htmx:responseError", onError);
          document.removeEventListener("htmx:sendError", onError);
          if (failed || !pane.querySelector("[data-title]")) throw new Error("pane did not load");
          split.classList.remove("is-loading");
          if (push) history.pushState({ akilentPane: url }, "", url);
          setTitle();
          focusPaneHeading();
        })
        .catch(function () {
          // The pane couldn't load (network error, a 404 for a deleted conversation, …) —
          // fall back to a real navigation rather than leaving the user on an empty pane.
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
      if (a.getAttribute("aria-current") === "true") return; // already open in the pane
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
    window.addEventListener("resize", fit, { passive: true });
    return function cleanup() {
      document.removeEventListener("click", onClickCapture, true);
      window.removeEventListener("popstate", onPopState);
      document.removeEventListener("htmx:afterSwap", onAfterSwap);
      window.removeEventListener("resize", fit);
      document.title = inboxTitle;
    };
  });
})();
