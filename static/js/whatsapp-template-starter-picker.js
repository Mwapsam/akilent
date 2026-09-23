/*
 * "Start with a template" picker on the WhatsApp create-template form.
 * Two levels: category cards (plus a trailing "Start from scratch" card), then
 * the scenarios within a category ("Payment reminder", "Payment received",
 * ...). Selecting one prefills the form below — the owner still reviews,
 * edits and submits it themselves; nothing here talks to the server. Reads
 * starter data from #wa-starter-picker's data-starters attribute
 * (apps.whatsapp.starter_templates.STARTER_CATEGORIES).
 */
(function () {
  "use strict";

  var SCRATCH_ICON_PATH = "M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.456-2.456L14.25 6l1.035-.259a3.375 3.375 0 0 0 2.456-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 0 0-2.456 2.456Z";
  var CATEGORY_ICON_PATHS = {
    "message-circle": "M12 20.25c4.97 0 9-3.694 9-8.25s-4.03-8.25-9-8.25S3 7.444 3 12c0 2.104.859 4.023 2.273 5.48.432.447.74 1.04.586 1.641a4.483 4.483 0 0 1-.923 1.785A5.969 5.969 0 0 0 6 21c1.282 0 2.47-.402 3.445-1.087.81.22 1.668.337 2.555.337Z",
    building: "M3.75 21h16.5M4.5 3h15M5.25 3v18m13.5-18v18M9 6.75h1.5m-1.5 3h1.5m-1.5 3h1.5m3-6H15m-1.5 3H15m-1.5 3H15M9 21v-3.375c0-.621.504-1.125 1.125-1.125h3.75c.621 0 1.125.504 1.125 1.125V21",
    card: "M2.25 8.25h19.5M2.25 9h19.5m-16.5 5.25h6m-6 2.25h3M3.75 5.25h16.5a1.5 1.5 0 0 1 1.5 1.5v10.5a1.5 1.5 0 0 1-1.5 1.5H3.75a1.5 1.5 0 0 1-1.5-1.5V6.75a1.5 1.5 0 0 1 1.5-1.5Z",
    send: "M6 12 3.269 3.126A59.768 59.768 0 0 1 21.485 12 59.77 59.77 0 0 1 3.27 20.876L5.999 12Zm0 0h7.5",
    clock: "M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
    workflow: "M3.75 6a2.25 2.25 0 1 1 4.5 0 2.25 2.25 0 0 1-4.5 0ZM15.75 18a2.25 2.25 0 1 1 4.5 0 2.25 2.25 0 0 1-4.5 0ZM6 8.25v3.75a2.25 2.25 0 0 0 2.25 2.25H15m0 0-2.25-2.25M15 14.25l-2.25 2.25",
    users: "M15 19.128a9.38 9.38 0 0 0 2.625.372 9.337 9.337 0 0 0 4.121-.952 4.125 4.125 0 0 0-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 0 1 8.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0 1 11.964-3.07M12 6.375a3.375 3.375 0 1 1-6.75 0 3.375 3.375 0 0 1 6.75 0Zm8.25 2.25a2.625 2.625 0 1 1-5.25 0 2.625 2.625 0 0 1 5.25 0Z",
    sparkles: SCRATCH_ICON_PATH,
    check: "m4.5 12.75 6 6 9-13.5",
  };

  function icon(path) {
    return '<svg class="w-5 h-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke-width="1.7" stroke="currentColor" aria-hidden="true">' +
      '<path stroke-linecap="round" stroke-linejoin="round" d="' + path + '"/></svg>';
  }

  function init() {
    var picker = document.getElementById("wa-starter-picker");
    var gridEl = document.getElementById("wa-starter-grid");
    var templatesSection = document.getElementById("wa-starter-templates");
    var templateListEl = document.getElementById("wa-starter-template-list");
    var backBtn = document.getElementById("wa-starter-back");
    var formSection = document.getElementById("wa-template-form");
    if (!picker || !gridEl) return;

    var categories;
    try {
      categories = JSON.parse(picker.getAttribute("data-starters") || "[]");
    } catch (e) {
      return;
    }

    var nameEl = document.getElementById("tpl-name");
    var categoryEl = document.getElementById("tpl-category");
    var bodyEl = document.getElementById("tpl-body");
    var variableCardsEl = document.getElementById("variable-cards");

    function showForm() {
      formSection.hidden = false;
      formSection.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function applyStarter(starter) {
      nameEl.value = starter.slug || "";
      if (categoryEl) categoryEl.value = starter.meta_category || "";
      bodyEl.value = starter.body || "";

      var preview = window.WhatsAppTemplatePreview;
      if (preview) {
        preview.setHeaderMode(false);
        preview.setFooter("");
        preview.setButton((starter.buttons || [])[0]);
        // Cards are derived purely from the body's {{n}} tokens — apply each
        // starter variable's label/example/source onto the card at that
        // position, so a starter and manual typing share one code path for
        // "which variable cards exist".
        preview.reconcileVariableCards();
        (starter.variables || []).forEach(function (v, i) {
          var card = variableCardsEl.children[i];
          if (!card) return;
          var labelInput = card.querySelector('[name="variable_label"]');
          var exampleInput = card.querySelector('[name="variable_example"]');
          if (labelInput) labelInput.value = v.label || "";
          if (exampleInput) exampleInput.value = v.example || "";
          if (v.source) preview.setVariableSource(card.dataset.n, v.source);
        });
        preview.refreshButtonVariablePicker();
        preview.render();
      }
      showForm();
    }

    function useScratch() {
      nameEl.value = "";
      bodyEl.value = "";
      var preview = window.WhatsAppTemplatePreview;
      if (preview) {
        preview.setHeaderMode(false);
        preview.setFooter("");
        preview.setButton(null);
        preview.reconcileVariableCards();
        preview.render();
      }
      showForm();
    }

    function renderGrid() {
      gridEl.innerHTML = "";
      categories.forEach(function (cat) {
        var card = document.createElement("button");
        card.type = "button";
        card.className = "card card-pad card-hover text-left flex items-start gap-3";
        card.innerHTML =
          icon(CATEGORY_ICON_PATHS[cat.icon] || CATEGORY_ICON_PATHS.workflow) +
          '<span class="min-w-0">' +
            '<span class="text-sm font-semibold text-ink block">' + cat.label + '</span>' +
            '<span class="text-xs text-gray-500 block mt-0.5">' + cat.blurb + '</span>' +
          '</span>';
        card.addEventListener("click", function () { renderTemplates(cat); });
        gridEl.appendChild(card);
      });

      var scratchCard = document.createElement("button");
      scratchCard.type = "button";
      scratchCard.className = "card card-pad card-hover text-left flex items-start gap-3";
      scratchCard.innerHTML =
        icon(SCRATCH_ICON_PATH) +
        '<span class="min-w-0">' +
          '<span class="text-sm font-semibold text-ink block">Start from scratch</span>' +
          '<span class="text-xs text-gray-500 block mt-0.5">Build your own template</span>' +
        '</span>';
      scratchCard.addEventListener("click", useScratch);
      gridEl.appendChild(scratchCard);
    }

    function renderTemplates(cat) {
      gridEl.hidden = true;
      templatesSection.classList.remove("hidden");
      templateListEl.innerHTML = "";
      cat.templates.forEach(function (tpl) {
        var row = document.createElement("div");
        row.className = "card card-pad flex items-center justify-between gap-3";
        var info = document.createElement("div");
        info.innerHTML =
          '<p class="text-sm font-medium text-ink">' + tpl.name + "</p>" +
          '<p class="text-xs text-gray-500">' + tpl.use_case + "</p>";
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-secondary btn-sm shrink-0";
        btn.textContent = "Use template";
        btn.addEventListener("click", function () { applyStarter(tpl); });
        row.appendChild(info);
        row.appendChild(btn);
        templateListEl.appendChild(row);
      });
    }

    if (backBtn) {
      backBtn.addEventListener("click", function () {
        gridEl.hidden = false;
        templatesSection.classList.add("hidden");
      });
    }

    renderGrid();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
