/*
 * WhatsApp create-template page: live chat-bubble preview, the variable
 * "Personalize" cards (auto-generated from {{n}} tokens typed in the body),
 * the Contact/Order/Payment command-menu data-field picker, and progressive
 * disclosure for header/footer/button. Pure client-side — WhatsApp template
 * bodies are plain text with {{n}} substitution (no HTML/Django-template
 * rendering like the email builder's preview), so no server round-trip is
 * needed for any of this.
 *
 * Variable cards are the single source of truth for "which variables exist"
 * and it lives in the DOM (each card's own label/example inputs + a
 * data-source attribute), not a parallel JS object — reconcileVariableCards()
 * only adds/removes cards for numbers that appeared/disappeared in the body,
 * moving (not recreating) surviving cards, so a keystroke never clobbers an
 * already-filled-in card. The frontend mirrors template_builder.py's
 * sequential-numbering rule (extractVariableNumbers/isSequential) so a
 * non-sequential body (e.g. {{1}} and {{3}} with no {{2}}) shows an inline
 * warning instead of the UI silently inventing or renumbering a variable —
 * "insert Akilent data field" only autofills a card's label/example text at
 * creation time, it does not bind the template to that field for sending.
 * Runtime substitution still happens via the campaign's own variable_mapping
 * (apps.whatsapp.campaigns), which has no UI yet.
 *
 * A button's {{n}} isn't an independent placeholder — it must reference one
 * of the message's own variables (apps.whatsapp.template_builder
 * ._validate_buttons), so the button gets a "use a message variable" select
 * built from the current cards, not its own data-field picker.
 *
 * The picker's "+ Create custom field…" opens a small shared panel that
 * POSTs to apps.contacts.views.create_custom_field (the field belongs to
 * Contact, not to WhatsApp) and, on success, is immediately searchable in
 * every picker on the page — no page reload needed.
 *
 * Anything user-controlled (custom field labels, examples, body text) is
 * HTML-escaped before it touches innerHTML.
 */
(function () {
  "use strict";

  var ICON_PATHS = {
    user: "M15.75 6a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0ZM4.501 20.118a7.5 7.5 0 0 1 14.998 0A17.933 17.933 0 0 1 12 21.75c-2.676 0-5.216-.584-7.499-1.632Z",
    sliders: "M10.5 6h9.75M10.5 6a1.5 1.5 0 1 1-3 0m3 0a1.5 1.5 0 1 0-3 0M3.75 6H7.5m3 12h9.75m-9.75 0a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m-3.75 0H7.5m9-6h3.75m-3.75 0a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m-9.75 0h9.75",
    building: "M3.75 21h16.5M4.5 3h15M5.25 3v18m13.5-18v18M9 6.75h1.5m-1.5 3h1.5m-1.5 3h1.5m3-6H15m-1.5 3H15m-1.5 3H15M9 21v-3.375c0-.621.504-1.125 1.125-1.125h3.75c.621 0 1.125.504 1.125 1.125V21",
    card: "M2.25 8.25h19.5M2.25 9h19.5m-16.5 5.25h6m-6 2.25h3M3.75 5.25h16.5a1.5 1.5 0 0 1 1.5 1.5v10.5a1.5 1.5 0 0 1-1.5 1.5H3.75a1.5 1.5 0 0 1-1.5-1.5V6.75a1.5 1.5 0 0 1 1.5-1.5Z",
  };

  function icon(name) {
    var d = ICON_PATHS[name] || ICON_PATHS.sliders;
    return '<svg class="w-4 h-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke-width="1.7" stroke="currentColor" aria-hidden="true">' +
      '<path stroke-linecap="round" stroke-linejoin="round" d="' + d + '"/></svg>';
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // WhatsApp's inline formatting: *bold*, _italic_, ~strike~, ```mono```.
  // Markers only count at word boundaries, so URLs and snake_case survive.
  var FMT_BEFORE = "(^|[\\s(\\[])";
  var FMT_AFTER = "(?=$|[\\s.,!?:;)\\]])";
  var RE_BOLD = new RegExp(FMT_BEFORE + "\\*([^*\\n]+?)\\*" + FMT_AFTER, "g");
  var RE_ITALIC = new RegExp(FMT_BEFORE + "_([^_\\n]+?)_" + FMT_AFTER, "g");
  var RE_STRIKE = new RegExp(FMT_BEFORE + "~([^~\\n]+?)~" + FMT_AFTER, "g");

  function formatWhatsApp(text) {
    return escapeHtml(text)
      .replace(/```([\s\S]+?)```/g, "<code>$1</code>")
      .replace(RE_BOLD, "$1<strong>$2</strong>")
      .replace(RE_ITALIC, "$1<em>$2</em>")
      .replace(RE_STRIKE, "$1<s>$2</s>")
      // Variables with no example yet show as a chip so they're easy to spot.
      .replace(/\{\{(\d+)\}\}/g, '<span class="wa-var">{{$1}}</span>');
  }

  function debounce(fn, wait) {
    var timer;
    return function () {
      var args = arguments;
      var ctx = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(ctx, args); }, wait);
    };
  }

  function init() {
    var builder = document.getElementById("wa-builder");
    var form = document.getElementById("wa-form");
    if (!builder || !form) return;

    var headerModeNone = document.getElementById("tpl-header-mode-none");
    var headerModeText = document.getElementById("tpl-header-mode-text");
    var headerModeRadios = Array.prototype.slice.call(document.querySelectorAll('input[name="header_format"]'));
    var headerEl = document.getElementById("tpl-header");
    var headerMediaField = document.getElementById("wa-header-media-field");
    var headerMediaFile = document.getElementById("tpl-header-media-file");
    var headerMediaAssetId = document.getElementById("tpl-header-media-asset-id");
    var headerMediaLabel = document.getElementById("wa-header-media-label");
    var headerMediaStatus = document.getElementById("wa-header-media-status");
    var headerMediaError = document.getElementById("wa-header-media-error");
    var bodyEl = document.getElementById("tpl-body");
    var footerEl = document.getElementById("tpl-footer");
    var footerAddBtn = document.getElementById("wa-footer-add");
    var footerRemoveBtn = document.getElementById("wa-footer-remove");
    var footerField = document.getElementById("wa-footer-field");
    var buttonAddBtn = document.getElementById("wa-button-add");
    var buttonRemoveBtn = document.getElementById("wa-button-remove");
    var buttonField = document.getElementById("wa-button-field");
    var buttonTypeEl = document.getElementById("tpl-button-type");
    var buttonUrlFields = document.getElementById("wa-button-url-fields");
    var buttonPhoneFields = document.getElementById("wa-button-phone-fields");
    var buttonCodeFields = document.getElementById("wa-button-code-fields");
    var buttonPhoneNumberEl = document.getElementById("tpl-button-phone-number");
    var buttonCodeExampleEl = document.getElementById("tpl-button-code-example");
    var buttonTextEl = document.getElementById("tpl-button-text");
    var buttonUrlEl = document.getElementById("tpl-button-url");
    var buttonExampleEl = document.getElementById("tpl-button-example");
    var buttonVariablePicker = document.getElementById("tpl-button-variable-picker");
    var variableCardsEl = document.getElementById("variable-cards");
    var noVariablesHint = document.getElementById("wa-no-variables-hint");
    var variableWarningEl = document.getElementById("wa-variable-warning");
    var submitBtn = document.getElementById("wa-submit-btn");

    var previewHeader = document.getElementById("wa-preview-header");
    var previewBody = document.getElementById("wa-preview-body");
    var previewFooter = document.getElementById("wa-preview-footer");
    var previewButton = document.getElementById("wa-preview-button");

    var mediaUploadUrl = builder.getAttribute("data-media-upload-url") || "";
    var MEDIA_ACCEPT = { image: "image/jpeg,image/png", video: "video/mp4", document: "application/pdf" };

    var dataFields = { groups: [] };
    try {
      dataFields = JSON.parse(builder.getAttribute("data-fields") || "{}");
      if (!dataFields.groups) dataFields.groups = [];
    } catch (e) {
      dataFields = { groups: [] };
    }

    var initialVariables = [];
    try {
      initialVariables = JSON.parse(builder.getAttribute("data-initial-variables") || "[]") || [];
    } catch (e) {
      initialVariables = [];
    }

    // ------------------------------------------------------------------
    // Variables: extraction, sequencing, and card reconciliation
    // ------------------------------------------------------------------

    function extractVariableNumbers(body) {
      var matches = (body || "").match(/\{\{(\d+)\}\}/g) || [];
      return matches.map(function (m) { return parseInt(m.replace(/\D/g, ""), 10); });
    }

    function distinctSorted(numbers) {
      var seen = {};
      var out = [];
      numbers.forEach(function (n) {
        if (!seen[n]) { seen[n] = true; out.push(n); }
      });
      out.sort(function (a, b) { return a - b; });
      return out;
    }

    function isSequential(numbers) {
      for (var i = 0; i < numbers.length; i++) {
        if (numbers[i] !== i + 1) return false;
      }
      return true;
    }

    function updateVariableWarning(rawNumbers) {
      if (!variableWarningEl) return;
      var valid = isSequential(rawNumbers);
      if (valid) {
        variableWarningEl.classList.add("hidden");
        variableWarningEl.textContent = "";
        if (submitBtn) submitBtn.disabled = false;
        return;
      }
      var expectedNext = 1;
      for (var i = 0; i < rawNumbers.length; i++) {
        if (rawNumbers[i] !== expectedNext) break;
        expectedNext++;
      }
      variableWarningEl.textContent =
        "Variables must be numbered in order starting at {{1}} — {{" + expectedNext + "}} is missing.";
      variableWarningEl.classList.remove("hidden");
      if (submitBtn) submitBtn.disabled = true;
    }

    function buildVariableCard(n) {
      var card = document.createElement("div");
      card.className = "wa-varcard space-y-3";
      card.dataset.n = String(n);
      card.dataset.source = "";
      card.innerHTML =
        '<div class="flex items-center gap-2">' +
          '<span class="wa-var-chip">{{' + n + '}}</span>' +
          '<input class="input flex-1" name="variable_label" placeholder="Label, e.g. Customer name" aria-label="Label for variable ' + n + '" />' +
        '</div>' +
        '<div class="grid sm:grid-cols-2 gap-2">' +
          '<div>' +
            '<span class="text-xs text-gray-500 block mb-1">Akilent data</span>' +
            '<div data-role="picker-mount"></div>' +
          '</div>' +
          '<div>' +
            '<label class="text-xs text-gray-500 block mb-1" for="wa-var-example-' + n + '">Example value</label>' +
            '<input class="input" id="wa-var-example-' + n + '" name="variable_example" placeholder="e.g. Ada" />' +
          '</div>' +
        '</div>' +
        '<p class="text-xs text-gray-400">Meta uses the example to review your template. It isn’t sent to customers.</p>';

      var labelInput = card.querySelector('[name="variable_label"]');
      var exampleInput = card.querySelector('[name="variable_example"]');
      var mount = card.querySelector('[data-role="picker-mount"]');

      card._picker = createDataFieldPicker(mount, {
        getValue: function () { return card.dataset.source; },
        onSelect: function (value, meta) {
          card.dataset.source = value;
          if (meta) {
            labelInput.value = meta.label;
            exampleInput.value = meta.sample;
          }
          refreshButtonVariablePicker();
          renderPreview();
        },
      });

      labelInput.addEventListener("input", renderPreview);
      exampleInput.addEventListener("input", debounce(function () {
        refreshButtonVariablePicker();
        renderPreview();
      }, 150));

      return card;
    }

    function renderVariableCards(present) {
      var existing = {};
      Array.prototype.forEach.call(variableCardsEl.children, function (card) {
        existing[card.dataset.n] = card;
      });
      // Remove cards for numbers no longer present.
      Object.keys(existing).forEach(function (n) {
        if (present.indexOf(parseInt(n, 10)) === -1) existing[n].remove();
      });
      // Create cards for newly-seen numbers, then reorder (append moves an
      // existing node rather than recreating it, preserving its state).
      present.forEach(function (n) {
        var card = variableCardsEl.querySelector('[data-n="' + n + '"]');
        if (!card) card = buildVariableCard(n);
        variableCardsEl.appendChild(card);
      });
      if (noVariablesHint) noVariablesHint.classList.toggle("hidden", present.length > 0);
    }

    function applyInitialVariables() {
      // Populate freshly-created cards from the server's error-repost state
      // (label, example) by position — best-effort only; source isn't
      // persisted server-side so it can't be restored here.
      initialVariables.forEach(function (pair, i) {
        var card = variableCardsEl.children[i];
        if (!card) return;
        var labelInput = card.querySelector('[name="variable_label"]');
        var exampleInput = card.querySelector('[name="variable_example"]');
        if (labelInput && !labelInput.value) labelInput.value = pair[0] || "";
        if (exampleInput && !exampleInput.value) exampleInput.value = pair[1] || "";
      });
    }

    function reconcileVariableCards() {
      var raw = extractVariableNumbers(bodyEl.value);
      var present = distinctSorted(raw);
      renderVariableCards(present);
      updateVariableWarning(raw);
      refreshButtonVariablePicker();
      renderPreview();
    }

    // ------------------------------------------------------------------
    // Command-menu data-field picker
    // ------------------------------------------------------------------

    function fieldByValue(value) {
      if (!value) return null;
      for (var i = 0; i < dataFields.groups.length; i++) {
        var g = dataFields.groups[i];
        for (var j = 0; j < g.fields.length; j++) {
          if ((g.prefix || g.key) + "." + g.fields[j].key === value) {
            return { group: g, field: g.fields[j] };
          }
        }
      }
      return null;
    }

    function createDataFieldPicker(mount, opts) {
      var wrap = document.createElement("div");
      wrap.className = "relative";
      mount.appendChild(wrap);

      function renderChip() {
        var value = opts.getValue();
        var found = fieldByValue(value);
        wrap.innerHTML =
          '<button type="button" class="input w-full flex items-center gap-2 text-left" data-role="chip">' +
            icon(found ? found.group.icon : "sliders") +
            '<span class="flex-1 truncate' + (found ? "" : " text-gray-400") + '">' +
              (found ? escapeHtml(found.group.label) + " → " + escapeHtml(found.field.label) : "Choose a field…") +
            '</span>' +
            (found ? '<span data-role="clear" class="text-gray-400 hover:text-gray-600 px-1" aria-label="Clear field">×</span>' : '') +
          '</button>';
        wrap.querySelector('[data-role="chip"]').addEventListener("click", function (e) {
          if (e.target.closest('[data-role="clear"]')) {
            opts.onSelect("", null);
            renderChip();
            return;
          }
          renderSearch();
        });
      }

      function renderSearch() {
        wrap.innerHTML =
          '<input type="text" class="input w-full" placeholder="Search fields…" data-role="search" autocomplete="off" />' +
          '<div class="absolute z-20 mt-1 w-full min-w-[16rem] bg-white border border-gray-200 rounded-lg shadow-lg max-h-64 overflow-y-auto" data-role="panel"></div>';
        var searchInput = wrap.querySelector('[data-role="search"]');
        var panel = wrap.querySelector('[data-role="panel"]');

        function focusSibling(row, dir) {
          var buttons = Array.prototype.filter.call(panel.children, function (el) { return el.tagName === "BUTTON"; });
          var idx = buttons.indexOf(row);
          var next = buttons[idx + dir];
          if (next) next.focus();
          else if (dir < 0) searchInput.focus();
        }

        function renderPanel(query) {
          var q = (query || "").toLowerCase();
          panel.innerHTML = "";
          var anyMatch = false;
          dataFields.groups.forEach(function (g) {
            var matches = g.fields.filter(function (f) { return f.label.toLowerCase().indexOf(q) !== -1; });
            if (!matches.length) return;
            anyMatch = true;
            var heading = document.createElement("div");
            heading.className = "px-3 pt-2 pb-1 text-xs font-semibold text-gray-500 flex items-center gap-1.5";
            heading.innerHTML = icon(g.icon) + "<span>" + escapeHtml(g.label) + "</span>";
            panel.appendChild(heading);
            matches.forEach(function (f) {
              var row = document.createElement("button");
              row.type = "button";
              row.className = "w-full text-left px-3 py-1.5 hover:bg-gray-50 focus:bg-gray-50 focus:outline-none text-sm";
              row.innerHTML = '<span class="block text-ink">' + escapeHtml(f.label) + '</span><span class="block text-xs text-gray-400">' + escapeHtml(f.sample) + '</span>';
              row.addEventListener("click", function () {
                opts.onSelect((g.prefix || g.key) + "." + f.key, { label: f.label, sample: f.sample });
                renderChip();
              });
              row.addEventListener("keydown", function (e) {
                if (e.key === "Escape") { renderChip(); }
                else if (e.key === "ArrowDown") { e.preventDefault(); focusSibling(row, 1); }
                else if (e.key === "ArrowUp") { e.preventDefault(); focusSibling(row, -1); }
              });
              panel.appendChild(row);
            });
          });
          if (!anyMatch) {
            var empty = document.createElement("p");
            empty.className = "px-3 py-2 text-sm text-gray-500";
            empty.textContent = "No fields match. Create a custom field below.";
            panel.appendChild(empty);
          }
          if (dataFields.create_custom_field_url) {
            var createRow = document.createElement("button");
            createRow.type = "button";
            createRow.className = "w-full text-left px-3 py-2 border-t border-gray-100 text-sm font-medium text-blue-600 hover:bg-gray-50";
            createRow.textContent = "+ Create custom field…";
            createRow.addEventListener("click", function () {
              openCustomFieldPanel(function (created) {
                opts.onSelect("contact." + created.key, { label: created.label, sample: created.sample });
                renderChip();
              });
            });
            panel.appendChild(createRow);
          }
        }

        renderPanel("");
        searchInput.focus();
        searchInput.addEventListener("input", function () { renderPanel(searchInput.value); });
        searchInput.addEventListener("keydown", function (e) {
          if (e.key === "Escape") { renderChip(); }
          else if (e.key === "ArrowDown") { e.preventDefault(); focusSibling(null, 1); }
        });

        function onDocClick(e) {
          if (!wrap.contains(e.target)) {
            document.removeEventListener("mousedown", onDocClick);
            renderChip();
          }
        }
        document.addEventListener("mousedown", onDocClick);
      }

      renderChip();
      return { refresh: renderChip };
    }

    // ------------------------------------------------------------------
    // "Create custom field" — shared panel, callback-driven so any picker
    // instance can trigger it.
    // ------------------------------------------------------------------

    var customFieldPanel = document.getElementById("wa-custom-field-panel");
    var customFieldLabelEl = document.getElementById("wa-custom-field-label");
    var customFieldTypeEl = document.getElementById("wa-custom-field-type");
    var customFieldErrorEl = document.getElementById("wa-custom-field-error");
    var customFieldCreateBtn = document.getElementById("wa-custom-field-create");
    var customFieldCancelBtn = document.getElementById("wa-custom-field-cancel");
    var pendingCreateCallback = null;

    function openCustomFieldPanel(onCreated) {
      if (!customFieldPanel) return;
      pendingCreateCallback = onCreated;
      if (customFieldErrorEl) customFieldErrorEl.classList.add("hidden");
      customFieldPanel.classList.remove("hidden");
      customFieldPanel.scrollIntoView({ block: "nearest" });
      if (customFieldLabelEl) { customFieldLabelEl.value = ""; customFieldLabelEl.focus(); }
    }

    function closeCustomFieldPanel() {
      if (!customFieldPanel) return;
      customFieldPanel.classList.add("hidden");
      pendingCreateCallback = null;
    }

    if (customFieldCancelBtn) customFieldCancelBtn.addEventListener("click", closeCustomFieldPanel);
    if (customFieldCreateBtn) {
      customFieldCreateBtn.addEventListener("click", function () {
        var label = (customFieldLabelEl && customFieldLabelEl.value || "").trim();
        if (!label) {
          if (customFieldErrorEl) {
            customFieldErrorEl.textContent = "Give the field a name.";
            customFieldErrorEl.classList.remove("hidden");
          }
          return;
        }
        var csrfInput = form.querySelector('[name="csrfmiddlewaretoken"]');
        customFieldCreateBtn.disabled = true;
        fetch(dataFields.create_custom_field_url, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": csrfInput ? csrfInput.value : "",
            "X-Requested-With": "XMLHttpRequest",
          },
          body: JSON.stringify({ label: label, type: customFieldTypeEl ? customFieldTypeEl.value : "string" }),
        })
          .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
          .then(function (result) {
            if (!result.ok) {
              if (customFieldErrorEl) {
                customFieldErrorEl.textContent = result.data.error || "Couldn't create the field.";
                customFieldErrorEl.classList.remove("hidden");
              }
              return;
            }
            var group = dataFields.groups.filter(function (g) { return g.key === "contact_custom"; })[0];
            if (group) group.fields.push(result.data);
            var callback = pendingCreateCallback;
            closeCustomFieldPanel();
            if (callback) callback(result.data);
          })
          .catch(function () {
            if (customFieldErrorEl) {
              customFieldErrorEl.textContent = "Couldn't reach the server. Check your connection and try again.";
              customFieldErrorEl.classList.remove("hidden");
            }
          })
          .then(function () { customFieldCreateBtn.disabled = false; });
      });
    }

    // ------------------------------------------------------------------
    // Progressive disclosure: header / footer / button
    // ------------------------------------------------------------------

    function currentHeaderFormat() {
      var checked = headerModeRadios.filter(function (r) { return r.checked; })[0];
      return checked ? checked.value : "none";
    }

    var headerMediaObjectUrl = null;

    function resetHeaderMediaField() {
      if (headerMediaFile) headerMediaFile.value = "";
      if (headerMediaAssetId) headerMediaAssetId.value = "";
      if (headerMediaLabel) headerMediaLabel.textContent = "Drag and drop, or choose a file";
      if (headerMediaStatus) headerMediaStatus.textContent = "";
      if (headerMediaError) headerMediaError.classList.add("hidden");
      if (headerMediaObjectUrl) {
        URL.revokeObjectURL(headerMediaObjectUrl);
        headerMediaObjectUrl = null;
      }
    }

    function updateHeaderVisibility() {
      var format = currentHeaderFormat();
      var showText = format === "text";
      var showMedia = format === "image" || format === "video" || format === "document";
      headerEl.classList.toggle("hidden", !showText);
      if (!showText) headerEl.value = "";
      if (headerMediaField) headerMediaField.classList.toggle("hidden", !showMedia);
      if (!showMedia) {
        resetHeaderMediaField();
      } else if (headerMediaFile) {
        headerMediaFile.accept = MEDIA_ACCEPT[format] || "";
      }
      renderPreview();
    }
    headerModeRadios.forEach(function (radio) {
      radio.addEventListener("change", function () {
        updateHeaderVisibility();
        if (radio === headerModeText) headerEl.focus();
      });
    });

    function uploadHeaderMedia(file) {
      var format = currentHeaderFormat();
      if (!mediaUploadUrl || !file) return;
      if (headerMediaError) headerMediaError.classList.add("hidden");
      if (headerMediaStatus) headerMediaStatus.textContent = "Uploading…";
      if (headerMediaFile) headerMediaFile.disabled = true;

      // Preview immediately from the local file — no need to wait on the
      // network round-trip (or Meta's upload) just to show what was picked.
      if (headerMediaObjectUrl) URL.revokeObjectURL(headerMediaObjectUrl);
      headerMediaObjectUrl = (format === "image" || format === "video") ? URL.createObjectURL(file) : null;
      if (headerMediaLabel) headerMediaLabel.textContent = file.name;
      renderPreview();

      var csrfInput = form.querySelector('[name="csrfmiddlewaretoken"]');
      var formData = new FormData();
      formData.append("header_format", format);
      formData.append("file", file);

      fetch(mediaUploadUrl, {
        method: "POST",
        headers: csrfInput ? { "X-CSRFToken": csrfInput.value } : {},
        body: formData,
      })
        .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
        .then(function (result) {
          if (!result.ok) throw new Error(result.data.error || "Upload failed.");
          if (headerMediaAssetId) headerMediaAssetId.value = result.data.asset_id;
          if (headerMediaStatus) headerMediaStatus.textContent = "Uploaded";
        })
        .catch(function (err) {
          resetHeaderMediaField();
          if (headerMediaError) {
            headerMediaError.textContent = err.message || "Couldn't upload this file.";
            headerMediaError.classList.remove("hidden");
          }
          renderPreview();
        })
        .then(function () {
          if (headerMediaFile) headerMediaFile.disabled = false;
        });
    }
    if (headerMediaFile) {
      headerMediaFile.addEventListener("change", function () {
        var file = headerMediaFile.files[0];
        if (file) uploadHeaderMedia(file);
      });
    }

    if (footerAddBtn) {
      footerAddBtn.addEventListener("click", function () {
        footerAddBtn.classList.add("hidden");
        footerField.classList.remove("hidden");
        footerEl.focus();
      });
    }
    if (footerRemoveBtn) {
      footerRemoveBtn.addEventListener("click", function () {
        footerEl.value = "";
        footerField.classList.add("hidden");
        footerAddBtn.classList.remove("hidden");
        footerAddBtn.focus();
        renderPreview();
      });
    }

    function updateButtonTypeVisibility() {
      var type = buttonTypeEl ? buttonTypeEl.value : "url";
      if (buttonUrlFields) buttonUrlFields.classList.toggle("hidden", type !== "url");
      if (buttonPhoneFields) buttonPhoneFields.classList.toggle("hidden", type !== "phone_number" && type !== "voice_call");
      if (buttonCodeFields) buttonCodeFields.classList.toggle("hidden", type !== "copy_code");
    }
    if (buttonTypeEl) buttonTypeEl.addEventListener("change", function () { updateButtonTypeVisibility(); renderPreview(); });
    updateButtonTypeVisibility();

    if (buttonAddBtn) {
      buttonAddBtn.addEventListener("click", function () {
        buttonAddBtn.classList.add("hidden");
        buttonField.classList.remove("hidden");
        buttonTextEl.focus();
      });
    }
    if (buttonRemoveBtn) {
      buttonRemoveBtn.addEventListener("click", function () {
        buttonTextEl.value = "";
        buttonUrlEl.value = "";
        buttonExampleEl.value = "";
        if (buttonPhoneNumberEl) buttonPhoneNumberEl.value = "";
        if (buttonCodeExampleEl) buttonCodeExampleEl.value = "";
        if (buttonTypeEl) buttonTypeEl.value = "url";
        updateButtonTypeVisibility();
        toggleButtonExampleField();
        buttonField.classList.add("hidden");
        buttonAddBtn.classList.remove("hidden");
        buttonAddBtn.focus();
        renderPreview();
      });
    }

    // ------------------------------------------------------------------
    // Button's "use a message variable" picker
    // ------------------------------------------------------------------

    function refreshButtonVariablePicker() {
      if (!buttonVariablePicker) return;
      var selected = buttonVariablePicker.value;
      buttonVariablePicker.innerHTML = '<option value="">Use a message variable…</option>';
      Array.prototype.forEach.call(variableCardsEl.children, function (card) {
        var n = card.dataset.n;
        var labelInput = card.querySelector('[name="variable_label"]');
        var exampleInput = card.querySelector('[name="variable_example"]');
        var opt = document.createElement("option");
        opt.value = n;
        opt.textContent = "{{" + n + "}} " + ((labelInput && labelInput.value) || "Variable " + n);
        opt.dataset.example = (exampleInput && exampleInput.value) || "";
        buttonVariablePicker.appendChild(opt);
      });
      buttonVariablePicker.value = selected;
      buttonVariablePicker.disabled = variableCardsEl.children.length === 0;
    }

    function toggleButtonExampleField() {
      if (!buttonUrlEl || !buttonExampleEl) return;
      var needsExample = /\{\{\d+\}\}/.test(buttonUrlEl.value);
      buttonExampleEl.classList.toggle("hidden", !needsExample);
    }

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

    // ------------------------------------------------------------------
    // Preview
    // ------------------------------------------------------------------

    function variableExampleMap() {
      var map = {};
      Array.prototype.forEach.call(variableCardsEl.children, function (card) {
        var exampleInput = card.querySelector('[name="variable_example"]');
        map[card.dataset.n] = exampleInput ? exampleInput.value : "";
      });
      return map;
    }

    function substitute(text, map) {
      return (text || "").replace(/\{\{(\d+)\}\}/g, function (match, n) {
        var value = map[n];
        return value ? value : match;
      });
    }

    function renderPreview() {
      var map = variableExampleMap();
      if (previewHeader) {
        var format = currentHeaderFormat();
        if (format === "text") {
          previewHeader.textContent = headerEl.value;
        } else if (format === "image" && headerMediaObjectUrl) {
          previewHeader.innerHTML = "";
          var img = document.createElement("img");
          img.src = headerMediaObjectUrl;
          img.className = "w-full rounded-md mb-2 max-h-56 object-cover";
          img.alt = "";
          previewHeader.appendChild(img);
        } else if (format === "video" && headerMediaObjectUrl) {
          previewHeader.innerHTML = "";
          var video = document.createElement("video");
          video.src = headerMediaObjectUrl;
          video.controls = true;
          video.className = "w-full rounded-md mb-2 max-h-56";
          previewHeader.appendChild(video);
        } else if (format === "image" || format === "video" || format === "document") {
          var name = headerMediaLabel ? headerMediaLabel.textContent : "";
          previewHeader.textContent = name && name !== "Drag and drop, or choose a file"
            ? "[" + format + "] " + name
            : "[" + format + " header]";
        } else {
          previewHeader.textContent = "";
        }
      }
      if (previewBody) {
        var body = substitute(bodyEl.value, map);
        previewBody.innerHTML = body.trim()
          ? formatWhatsApp(body)
          : '<span class="text-gray-400">Your message appears here.</span>';
      }
      if (previewFooter) previewFooter.textContent = footerField.classList.contains("hidden") ? "" : footerEl.value;

      var buttonText = (!buttonField.classList.contains("hidden") && buttonTextEl.value.trim()) || "";
      if (previewButton) {
        // The link icon is drawn in CSS (#wa-preview-button::before).
        previewButton.textContent = buttonText;
        previewButton.classList.toggle("hidden", !buttonText);
      }
    }

    // ------------------------------------------------------------------
    // Wiring
    // ------------------------------------------------------------------

    // Preview updates on every keystroke; card reconciliation stays debounced
    // so typing "{{1" doesn't create and destroy cards mid-token.
    bodyEl.addEventListener("input", renderPreview);
    bodyEl.addEventListener("input", debounce(reconcileVariableCards, 150));
    if (headerEl) headerEl.addEventListener("input", renderPreview);
    if (footerEl) footerEl.addEventListener("input", renderPreview);
    if (buttonTextEl) buttonTextEl.addEventListener("input", renderPreview);
    if (buttonPhoneNumberEl) buttonPhoneNumberEl.addEventListener("input", renderPreview);
    if (buttonCodeExampleEl) buttonCodeExampleEl.addEventListener("input", renderPreview);

    reconcileVariableCards();
    applyInitialVariables();
    refreshButtonVariablePicker();
    renderPreview();

    window.WhatsAppTemplatePreview = {
      reconcileVariableCards: reconcileVariableCards,
      refreshButtonVariablePicker: refreshButtonVariablePicker,
      render: renderPreview,
      setHeaderMode: function (hasHeader) {
        if (headerModeNone) headerModeNone.checked = !hasHeader;
        if (headerModeText) headerModeText.checked = hasHeader;
        updateHeaderVisibility();
      },
      setFooter: function (value) {
        footerEl.value = value || "";
        if (value) {
          footerAddBtn.classList.add("hidden");
          footerField.classList.remove("hidden");
        } else {
          footerField.classList.add("hidden");
          footerAddBtn.classList.remove("hidden");
        }
      },
      setButton: function (button) {
        if (buttonTypeEl) buttonTypeEl.value = "url";
        updateButtonTypeVisibility();
        buttonTextEl.value = (button && button.text) || "";
        buttonUrlEl.value = (button && button.url) || "";
        buttonExampleEl.value = (button && button.example) || "";
        toggleButtonExampleField();
        if (button && (button.text || button.url)) {
          buttonAddBtn.classList.add("hidden");
          buttonField.classList.remove("hidden");
        } else {
          buttonField.classList.add("hidden");
          buttonAddBtn.classList.remove("hidden");
        }
      },
      setVariableSource: function (n, source) {
        var card = variableCardsEl.querySelector('[data-n="' + n + '"]');
        if (!card) return;
        card.dataset.source = source || "";
        if (card._picker) card._picker.refresh();
      },
    };
  }

  // A script-execution-deferring proxy (e.g. Cloudflare Rocket Loader) can
  // delay this file's actual execution until after DOMContentLoaded has
  // already fired — listening for it unconditionally would then never call
  // init(). Run immediately if the DOM is already parsed.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();