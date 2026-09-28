/* Two-pane inbox (≥1024px): loads a conversation into #conversation-pane-inner instead of
 * navigating the whole page, so switching between conversations keeps the list in view. Below
 * that width there is no pane to load into, so a row's own href is left alone and the existing
 * full-page boosted navigation (apps/core/htmx.py) handles it exactly as it did before this file
 * existed. Does nothing on any other page — akilent.onPage below returns immediately when the
 * markup it looks for isn't there.
 */
(function () {
  "use strict";

  function isWide() {
    return window.matchMedia("(min-width: 1024px)").matches;
  }

  akilent.onPage(function () {
    var split = document.getElementById("inbox-split");
    var list = document.getElementById("inbox-body");
    var pane = document.getElementById("conversation-pane-inner");
    if (!split || !list || !pane) return; // not the inbox list page

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

    // A capture-phase listener on document runs before htmx's own (bubble-phase) click
    // handling, so stopPropagation here keeps htmx from also boosting this same click into a
    // full-page navigation — the two would otherwise both fire for one click.
    function onClickCapture(e) {
      if (!isWide()) return; // let the row's plain href navigate the whole page instead
      var a = e.target.closest(".inbox-row");
      if (!a || !list.contains(a)) return;
      e.preventDefault();
      e.stopPropagation();
      if (a.getAttribute("aria-current") === "true") return; // already open in the pane

      selectRow(a);
      split.classList.add("has-conversation", "is-loading");
      window.htmx
        .ajax("GET", a.href, { target: "#conversation-pane-inner", swap: "innerHTML", source: document.body })
        .then(function () {
          split.classList.remove("is-loading");
          history.pushState({ akilentPane: a.href }, "", a.href);
          focusPaneHeading();
        })
        .catch(function () {
          // The pane couldn't load (network error, etc.) — fall back to a real navigation
          // rather than leaving the user stuck looking at an empty pane.
          window.location.href = a.href;
        });
    }

    document.addEventListener("click", onClickCapture, true);
    return function cleanup() {
      document.removeEventListener("click", onClickCapture, true);
    };
  });
})();
