/*
 * "Start with a template" picker on the WhatsApp create-template form.
 * Two levels: category cards, then the scenarios within a category ("Payment
 * reminder", "Payment received", ...). Selecting one prefills the form below
 * — the owner still reviews, edits and submits it themselves; nothing here
 * talks to the server. Reads starter data from #wa-starter-picker's
 * data-starters attribute (apps.whatsapp.starter_templates.STARTER_CATEGORIES).
 */
(function () {
  "use strict";

  function init() {
    var picker = document.getElementById("wa-starter-picker");
    var categoriesEl = document.getElementById("wa-starter-categories");
    var templatesSection = document.getElementById("wa-starter-templates");
    var templateListEl = document.getElementById("wa-starter-template-list");
    var backBtn = document.getElementById("wa-starter-back");
    var scratchBtn = document.getElementById("wa-start-scratch");
    var formSection = document.getElementById("wa-template-form");
    if (!picker || !categoriesEl) return;

    var categories;
    try {
      categories = JSON.parse(picker.getAttribute("data-starters") || "[]");
    } catch (e) {
      return;
    }

    var nameEl = document.getElementById("tpl-name");
    var categoryEl = document.getElementById("tpl-category");
    var headerEl = document.getElementById("tpl-header");
    var bodyEl = document.getElementById("tpl-body");
    var footerEl = document.getElementById("tpl-footer");
    var buttonTextEl = document.getElementById("tpl-button-text");
    var buttonUrlEl = document.getElementById("tpl-button-url");
    var buttonExampleEl = document.getElementById("tpl-button-example");
    var variableRows = document.getElementById("variable-rows");

    function setButtonFields(button) {
      if (buttonTextEl) buttonTextEl.value = (button && button.text) || "";
      if (buttonUrlEl) buttonUrlEl.value = (button && button.url) || "";
      if (buttonExampleEl) buttonExampleEl.value = (button && button.example) || "";
      if (buttonUrlEl) buttonUrlEl.dispatchEvent(new Event("input"));
    }

    function showForm() {
      formSection.hidden = false;
      formSection.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function clearVariableRows() {
      variableRows.innerHTML = "";
    }

    function useStarter(starter) {
      nameEl.value = starter.slug;
      if (categoryEl) categoryEl.value = starter.meta_category;
      headerEl.value = "";
      bodyEl.value = starter.body;
      footerEl.value = "";
      setButtonFields((starter.buttons || [])[0]);
      clearVariableRows();
      (starter.variables || []).forEach(function (v) {
        window.addVariableRow(v.label, v.example);
      });
      if (!(starter.variables || []).length) window.addVariableRow();
      // Reflect each variable's known source ("contact.first_name" etc.) in
      // its row's picker, so a starter visibly teaches the Akilent data
      // model instead of just dropping in static example text.
      var rows = variableRows.children;
      (starter.variables || []).forEach(function (v, i) {
        if (!v.source || !rows[i]) return;
        var picker = rows[i].querySelector('[data-role="data-field-picker"]');
        if (picker) picker.value = v.source;
      });
      showForm();
      if (window.WhatsAppTemplatePreview) window.WhatsAppTemplatePreview.render();
    }

    function renderCategories() {
      categoriesEl.innerHTML = "";
      categories.forEach(function (cat) {
        var card = document.createElement("button");
        card.type = "button";
        card.className = "card card-pad card-hover text-left flex items-start gap-3";
        card.innerHTML =
          '<span class="text-sm font-semibold text-ink block">' + cat.label + "</span>" +
          '<span class="text-xs text-gray-500 block mt-0.5">' + cat.blurb + "</span>";
        card.addEventListener("click", function () {
          renderTemplates(cat);
        });
        categoriesEl.appendChild(card);
      });
    }

    function renderTemplates(cat) {
      categoriesEl.parentElement.querySelector("h2").textContent = cat.label;
      categoriesEl.hidden = true;
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
        btn.addEventListener("click", function () {
          useStarter(tpl);
        });
        row.appendChild(info);
        row.appendChild(btn);
        templateListEl.appendChild(row);
      });
    }

    if (backBtn) {
      backBtn.addEventListener("click", function () {
        categoriesEl.hidden = false;
        templatesSection.classList.add("hidden");
      });
    }
    if (scratchBtn) {
      scratchBtn.addEventListener("click", function () {
        clearVariableRows();
        window.addVariableRow();
        setButtonFields(null);
        showForm();
        if (window.WhatsAppTemplatePreview) window.WhatsAppTemplatePreview.render();
      });
    }

    renderCategories();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
