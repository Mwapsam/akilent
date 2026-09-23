/*
 * Live chat-bubble preview for the WhatsApp create-template form, plus the
 * "insert Akilent data field" picker on each variable row and the button's
 * "use a message variable" picker. Pure client-side: WhatsApp template
 * bodies are plain text with {{n}} substitution (no HTML/Django-template
 * rendering like the email builder's preview), so no server round-trip is
 * needed here.
 *
 * "Insert Akilent data field" only autofills a variable row's label/example
 * text at creation time — it does not bind the template to that field for
 * sending. Runtime substitution still happens via the campaign's own
 * variable_mapping (apps.whatsapp.campaigns), which has no UI yet.
 *
 * A button's {{n}} isn't an independent placeholder — it must reference one
 * of the message's own variables (apps.whatsapp.template_builder
 * ._validate_buttons), so the button doesn't get its own data-field picker;
 * it gets a "use a message variable" picker built from the current variable
 * rows instead.
 */
(function () {
  "use strict";

  function debounce(fn, wait) {
    var timer;
    return function () {
      var args = arguments;
      var ctx = this;
      clearTimeout(timer);
      timer = setTimeout(function () {
        fn.apply(ctx, args);
      }, wait);
    };
  }

  function init() {
    var form = document.getElementById("wa-template-form");
    if (!form) return;

    var headerEl = document.getElementById("tpl-header");
    var bodyEl = document.getElementById("tpl-body");
    var footerEl = document.getElementById("tpl-footer");
    var buttonTextEl = document.getElementById("tpl-button-text");
    var buttonUrlEl = document.getElementById("tpl-button-url");
    var buttonExampleEl = document.getElementById("tpl-button-example");
    var buttonVariablePicker = document.getElementById("tpl-button-variable-picker");
    var variableRows = document.getElementById("variable-rows");

    var previewHeader = document.getElementById("wa-preview-header");
    var previewBody = document.getElementById("wa-preview-body");
    var previewFooter = document.getElementById("wa-preview-footer");
    var previewButton = document.getElementById("wa-preview-button");

    var dataFields = { groups: [] };
    try {
      dataFields = JSON.parse((variableRows && variableRows.getAttribute("data-fields")) || "{}");
      if (!dataFields.groups) dataFields.groups = [];
    } catch (e) {
      dataFields = { groups: [] };
    }

    function populateDataFieldPicker(select) {
      if (!select || select.dataset.populated) return;
      select.dataset.populated = "1";
      dataFields.groups.forEach(function (group) {
        var optgroup = document.createElement("optgroup");
        optgroup.label = group.label;
        group.fields.forEach(function (field) {
          var opt = document.createElement("option");
          opt.value = group.key + "." + field.key;
          opt.textContent = field.label;
          opt.dataset.label = field.label;
          opt.dataset.sample = field.sample;
          optgroup.appendChild(opt);
        });
        select.appendChild(optgroup);
      });
      select.addEventListener("change", function () {
        var opt = select.selectedOptions[0];
        if (!opt || !opt.value) return;
        var row = select.closest("div");
        var labelInput = row.querySelector('[name="variable_label"]');
        var exampleInput = row.querySelector('[name="variable_example"]');
        if (labelInput) labelInput.value = opt.dataset.label;
        if (exampleInput) exampleInput.value = opt.dataset.sample;
        refreshButtonVariablePicker();
        renderPreview();
      });
    }

    function variableLabelsAndExamples() {
      if (!variableRows) return [];
      var labels = variableRows.querySelectorAll('[name="variable_label"]');
      var examples = variableRows.querySelectorAll('[name="variable_example"]');
      return Array.prototype.map.call(labels, function (input, i) {
        return { label: input.value, example: examples[i] ? examples[i].value : "" };
      });
    }

    function refreshButtonVariablePicker() {
      if (!buttonVariablePicker) return;
      var selected = buttonVariablePicker.value;
      buttonVariablePicker.innerHTML = '<option value="">Use a message variable…</option>';
      variableLabelsAndExamples().forEach(function (v, i) {
        var opt = document.createElement("option");
        var n = i + 1;
        opt.value = String(n);
        opt.textContent = "{{" + n + "}} " + (v.label || "Variable " + n);
        opt.dataset.example = v.example;
        buttonVariablePicker.appendChild(opt);
      });
      buttonVariablePicker.value = selected;
    }

    function substitute(text, examples) {
      return (text || "").replace(/\{\{(\d+)\}\}/g, function (match, n) {
        var value = examples[parseInt(n, 10) - 1];
        return value ? value : match;
      });
    }

    function toggleButtonExampleField() {
      if (!buttonUrlEl || !buttonExampleEl) return;
      var needsExample = /\{\{\d+\}\}/.test(buttonUrlEl.value);
      buttonExampleEl.classList.toggle("hidden", !needsExample);
    }

    function renderPreview() {
      var examples = variableLabelsAndExamples().map(function (v) { return v.example; });

      if (headerEl && previewHeader) previewHeader.textContent = headerEl.value;
      if (bodyEl && previewBody) previewBody.textContent = substitute(bodyEl.value, examples);
      if (footerEl && previewFooter) previewFooter.textContent = footerEl.value;

      var buttonText = buttonTextEl ? buttonTextEl.value.trim() : "";
      if (previewButton) {
        if (buttonText) {
          previewButton.textContent = "🔗 " + buttonText;
          previewButton.classList.remove("hidden");
        } else {
          previewButton.textContent = "";
          previewButton.classList.add("hidden");
        }
      }
    }

    var debouncedRender = debounce(renderPreview, 150);
    var debouncedRefreshButtonPicker = debounce(refreshButtonVariablePicker, 150);

    [headerEl, bodyEl, footerEl, buttonTextEl, buttonUrlEl].forEach(function (el) {
      if (el) el.addEventListener("input", debouncedRender);
    });
    if (buttonUrlEl) buttonUrlEl.addEventListener("input", toggleButtonExampleField);
    if (buttonVariablePicker) {
      buttonVariablePicker.addEventListener("change", function () {
        var opt = buttonVariablePicker.selectedOptions[0];
        if (!opt || !opt.value || !buttonUrlEl) return;
        buttonUrlEl.value = buttonUrlEl.value.replace(/\{\{\d+\}\}/, "").replace(/\/?$/, "/") + "{{" + opt.value + "}}";
        if (buttonExampleEl) buttonExampleEl.value = opt.dataset.example || "";
        toggleButtonExampleField();
        renderPreview();
      });
    }
    if (variableRows) {
      variableRows.addEventListener("input", function () {
        debouncedRender();
        debouncedRefreshButtonPicker();
      });
      Array.prototype.forEach.call(
        variableRows.querySelectorAll('[data-role="data-field-picker"]'),
        populateDataFieldPicker
      );
    }

    toggleButtonExampleField();
    refreshButtonVariablePicker();
    renderPreview();

    window.WhatsAppTemplatePreview = {
      populateDataFieldPicker: populateDataFieldPicker,
      refreshButtonVariablePicker: refreshButtonVariablePicker,
      render: renderPreview,
    };
  }

  document.addEventListener("DOMContentLoaded", init);
})();
