/*
 * Live chat-bubble preview for the WhatsApp create-template form, plus the
 * "insert from contact field" picker on each variable row. Pure client-side:
 * WhatsApp template bodies are plain text with {{n}} substitution (no HTML/
 * Django-template rendering like the email builder's preview), so no server
 * round-trip is needed here.
 *
 * "Insert from contact field" only autofills a variable row's label/example
 * text at creation time — it does not bind the template to that field for
 * sending. Runtime substitution still happens via the campaign's own
 * variable_mapping (apps.whatsapp.campaigns), which has no UI yet.
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
    var variableRows = document.getElementById("variable-rows");

    var previewHeader = document.getElementById("wa-preview-header");
    var previewBody = document.getElementById("wa-preview-body");
    var previewFooter = document.getElementById("wa-preview-footer");
    var previewButton = document.getElementById("wa-preview-button");

    var contactFields = { built_in: [], custom: [] };
    try {
      contactFields = JSON.parse((variableRows && variableRows.getAttribute("data-contact-fields")) || "{}");
      if (!contactFields.built_in) contactFields.built_in = [];
      if (!contactFields.custom) contactFields.custom = [];
    } catch (e) {
      contactFields = { built_in: [], custom: [] };
    }

    function populateContactPicker(select) {
      if (!select || select.dataset.populated) return;
      select.dataset.populated = "1";
      contactFields.built_in.concat(contactFields.custom).forEach(function (field) {
        var opt = document.createElement("option");
        opt.value = field.key;
        opt.textContent = field.label;
        opt.dataset.label = field.label;
        opt.dataset.sample = field.sample;
        select.appendChild(opt);
      });
      select.addEventListener("change", function () {
        var opt = select.selectedOptions[0];
        if (!opt || !opt.value) return;
        var row = select.closest("div");
        var labelInput = row.querySelector('[name="variable_label"]');
        var exampleInput = row.querySelector('[name="variable_example"]');
        if (labelInput) labelInput.value = opt.dataset.label;
        if (exampleInput) exampleInput.value = opt.dataset.sample;
        renderPreview();
      });
    }

    function variableExamples() {
      if (!variableRows) return [];
      return Array.prototype.map.call(
        variableRows.querySelectorAll('[name="variable_example"]'),
        function (input) { return input.value; }
      );
    }

    function substitute(text, examples) {
      return (text || "").replace(/\{\{(\d+)\}\}/g, function (match, n) {
        var value = examples[parseInt(n, 10) - 1];
        return value ? value : match;
      });
    }

    function toggleButtonExampleField() {
      if (!buttonUrlEl || !buttonExampleEl) return;
      var needsExample = /\{\{1\}\}/.test(buttonUrlEl.value);
      buttonExampleEl.classList.toggle("hidden", !needsExample);
    }

    function renderPreview() {
      var examples = variableExamples();

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

    [headerEl, bodyEl, footerEl, buttonTextEl, buttonUrlEl].forEach(function (el) {
      if (el) el.addEventListener("input", debouncedRender);
    });
    if (buttonUrlEl) buttonUrlEl.addEventListener("input", toggleButtonExampleField);
    if (variableRows) {
      variableRows.addEventListener("input", debouncedRender);
      Array.prototype.forEach.call(
        variableRows.querySelectorAll('[data-role="contact-field-picker"]'),
        populateContactPicker
      );
    }

    toggleButtonExampleField();
    renderPreview();

    window.WhatsAppTemplatePreview = {
      populateContactPicker: populateContactPicker,
      render: renderPreview,
    };
  }

  document.addEventListener("DOMContentLoaded", init);
})();
