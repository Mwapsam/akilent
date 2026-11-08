/*
 * Tab-switching + copy-to-clipboard for `.code-tabs` groups. Ported from
 * templates/docs/_layout.html's docs-site code-tabs behavior (same
 * .code-tabs / [data-tab] / [data-panel] / .copy-btn selector contract),
 * generalized so it can run inside the dashboard on any page that renders a
 * .code-tabs group — e.g. a modal that's added to the DOM after page load.
 */
(function () {
  "use strict";

  var copyIcon =
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5v-2a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 3.5v5A1.5 1.5 0 0 0 4 10h1.5"/></svg>';
  var checkIcon =
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5"/></svg>';

  function initTabs(group) {
    if (group.dataset.tabsInit) return;
    group.dataset.tabsInit = "1";
    var buttons = group.querySelectorAll(".tab-bar button[data-tab]");
    buttons.forEach(function (btn) {
      btn.setAttribute("aria-selected", btn.classList.contains("active") ? "true" : "false");
      btn.addEventListener("click", function () {
        buttons.forEach(function (b) {
          b.classList.remove("active");
          b.setAttribute("aria-selected", "false");
        });
        group.querySelectorAll("[data-panel]").forEach(function (p) {
          p.hidden = true;
        });
        btn.classList.add("active");
        btn.setAttribute("aria-selected", "true");
        var panel = group.querySelector('[data-panel="' + btn.dataset.tab + '"]');
        if (panel) panel.hidden = false;
      });
    });
  }

  function initCopy(group) {
    if (!navigator.clipboard || group.dataset.copyInit) return;
    group.dataset.copyInit = "1";
    var bar = group.querySelector(".tab-bar");
    if (!bar) return;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.setAttribute("aria-label", "Copy code");
    btn.innerHTML = copyIcon;
    btn.addEventListener("click", function () {
      var visible = group.querySelector("[data-panel]:not([hidden])");
      var text = visible ? visible.innerText : "";
      navigator.clipboard.writeText(text).then(function () {
        btn.classList.add("copied");
        btn.innerHTML = checkIcon + "<span>Copied</span>";
        setTimeout(function () {
          btn.classList.remove("copied");
          btn.innerHTML = copyIcon;
        }, 1500);
      });
    });
    bar.appendChild(btn);
  }

  function initAll(root) {
    (root || document).querySelectorAll(".code-tabs").forEach(function (group) {
      initTabs(group);
      initCopy(group);
    });
  }

  // Loaded with the shell (templates/base.html), so also after in-place navigations;
  // initTabs/initCopy skip groups they've already set up.
  if (window.akilent && window.akilent.onPage) {
    window.akilent.onPage(function () { initAll(document); });
  } else {
    document.addEventListener("DOMContentLoaded", function () { initAll(document); });
  }

  window.CodeTabs = { init: initAll };
})();
