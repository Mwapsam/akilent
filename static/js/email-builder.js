/*
 * Glue code wiring the vendored GrapesJS + preset-newsletter bundles into a
 * template's builder canvas. No bundler — this is a plain script tag, reading
 * its config from the container element's data attributes.
 *
 * Expects a container element: <div id="email-builder" data-config='{...}'>
 * Config shape:
 *   {
 *     html: string,            // seed html_body
 *     projectData: object,     // seed content_blocks (GrapesJS project data), may be {}
 *     mergeTags: string[],     // sample_variables keys
 *     saveUrl: string,         // POST target (template_edit)
 *     previewUrl: string,      // POST target (template_preview draft render)
 *     uploadUrl: string,       // POST target (template_asset_upload)
 *     csrfToken: string,
 *     name: string,
 *     subject: string,
 *     sampleVariables: object,
 *   }
 *
 * Name/subject/sample-variables are edited in the shared settings panel
 * (#tpl-name/#tpl-subject/#tpl-sample-variables, see _template_settings.html)
 * rather than in this config — config only seeds their initial values.
 */
(function () {
  "use strict";

  function insertTagAtCursor(editor, tag) {
    var text = "{{ " + tag + " }}";
    var selected = editor.getSelected();
    var rte = editor.RichTextEditor && selected && selected.view && selected.view.rte;
    if (rte && rte.el && document.activeElement && rte.el.contains(document.activeElement)) {
      document.execCommand("insertText", false, text);
      return;
    }
    // No active RTE selection — append a new text block to the canvas so the
    // tag isn't lost; the user can drag it into place.
    editor.addComponents({ type: "text", content: text });
  }

  function buildMergeTagPanel(editor, tags) {
    if (!tags || !tags.length) return;
    var panelId = "merge-tags-panel";
    editor.Panels.addPanel({ id: panelId, visible: true, buttons: [] });
    tags.forEach(function (tag) {
      editor.Panels.addButton(panelId, {
        id: "tag-" + tag,
        className: "gjs-merge-tag-btn",
        label: tag,
        command: {
          run: function (ed) {
            insertTagAtCursor(ed, tag);
          },
        },
      });
    });

    // Add a typeahead search box that filters the tag buttons — the panel
    // itself only supports button widgets, so the filter box is injected
    // directly into the rendered panel's DOM. (Deliberately not unified with
    // merge-tag-autocomplete.js's dropdown — that pattern fights GrapesJS's
    // own Panels/RTE lifecycle for marginal UX gain.)
    if (tags.length > 6) {
      var panel = editor.Panels.getPanel(panelId);
      var panelEl = panel && panel.view && panel.view.el;
      if (panelEl) {
        var search = document.createElement("input");
        search.type = "text";
        search.placeholder = "Filter merge tags…";
        search.className = "gjs-merge-tag-search";
        search.style.cssText =
          "font-size:0.75rem;padding:2px 6px;border:1px solid #d1d5db;border-radius:4px;margin-right:6px;width:140px;";
        search.addEventListener("input", function () {
          var q = search.value.toLowerCase();
          panelEl.querySelectorAll(".gjs-merge-tag-btn").forEach(function (btn) {
            var label = (btn.getAttribute("title") || btn.textContent || "").toLowerCase();
            btn.style.display = label.indexOf(q) === -1 ? "none" : "";
          });
        });
        panelEl.insertBefore(search, panelEl.firstChild);
      }
    }
  }

  function stripTags(html) {
    var div = document.createElement("div");
    div.innerHTML = html;
    return (div.textContent || div.innerText || "").replace(/\s+\n/g, "\n").trim();
  }

  function csrfHeaders(token) {
    return { "X-CSRFToken": token };
  }

  function readSampleVariables(config) {
    var el = document.getElementById("tpl-sample-variables");
    if (!el) return config.sampleVariables || {};
    try {
      return JSON.parse(el.value || "{}");
    } catch (e) {
      return config.sampleVariables || {};
    }
  }

  function initBuilder(container) {
    var config = JSON.parse(container.getAttribute("data-config") || "{}");
    var newsletterPreset = window["grapesjs-preset-newsletter"];

    var editor = grapesjs.init({
      container: container,
      height: "100%",
      fromElement: false,
      storageManager: false,
      plugins: newsletterPreset ? [newsletterPreset] : [],
      pluginsOpts: newsletterPreset
        ? {
            "grapesjs-preset-newsletter": {
              modalTitleImport: "Import HTML",
            },
          }
        : {},
      assetManager: {
        uploadName: "files",
        upload: config.uploadUrl,
        headers: csrfHeaders(config.csrfToken),
        autoAdd: true,
      },
      // Grouped, task-oriented sectors in place of GrapesJS's generic
      // General/Dimension/Typography/Decorations/Extra defaults. "Buttons"
      // is deliberately not its own sector — button styling is just
      // Colors/Typography/Spacing applied to the selected component.
      styleManager: {
        sectors: [
          {
            name: "Layout",
            open: true,
            properties: ["display", "position", "top", "right", "bottom", "left", "width", "height", "max-width", "min-height", "float"],
          },
          {
            name: "Typography",
            open: false,
            properties: ["font-family", "font-size", "font-weight", "letter-spacing", "color", "line-height", "text-align", "text-decoration", "text-shadow"],
          },
          {
            name: "Colors",
            open: false,
            properties: ["background-color", "border-color", "border-style", "border-width", "border-radius", "box-shadow"],
          },
          {
            name: "Images",
            open: false,
            properties: ["background", "background-image", "background-repeat", "background-position", "background-size"],
          },
          {
            name: "Spacing",
            open: false,
            properties: ["margin", "padding"],
          },
        ],
      },
    });

    // Group preset-newsletter's flat block list into named categories —
    // must run after grapesjs.init() resolves, since the plugin registers
    // its own blocks during its own init and a pre-init blockManager.blocks
    // override would race it.
    var BLOCK_CATEGORIES = {
      sect100: "Layout",
      sect50: "Layout",
      sect30: "Layout",
      sect37: "Layout",
      text: "Content",
      "text-sect": "Content",
      quote: "Content",
      image: "Content",
      button: "Content",
      link: "Content",
      "link-block": "Content",
      divider: "Content",
      "grid-items": "Content",
      "list-items": "Content",
    };
    editor.BlockManager.getAll().forEach(function (block) {
      var category = BLOCK_CATEGORIES[block.get("id")];
      if (category) block.set("category", category);
    });

    // Akilent components as first-class blocks. Dropping one inserts the
    // {% component "Name" ... %} tag, which apps.email.template_components
    // expands at render time (built-ins need no {% load %}).
    var AKILENT_BLOCKS = [
      {
        id: "akilent-header",
        label: "Akilent Header",
        content: '{% component "AkilentHeader" title="Your title" %}',
      },
      {
        id: "akilent-button",
        label: "Akilent Button",
        content: '{% component "AkilentButton" href="https://example.com" label="Click me" %}',
      },
      {
        id: "akilent-invoice",
        label: "Akilent Invoice",
        content: '{% component "AkilentInvoice" number="INV-001" amount="$0.00" %}',
      },
      {
        id: "akilent-footer",
        label: "Akilent Footer",
        content: '{% component "AkilentFooter" company="Your company" %}',
      },
    ];
    AKILENT_BLOCKS.forEach(function (b) {
      editor.BlockManager.add(b.id, {
        label: b.label,
        category: "Akilent",
        content: b.content,
        attributes: { class: "gjs-block-akilent" },
      });
    });

    // Desktop / mobile / dark-mode preview toggle on the editor canvas.
    editor.DeviceManager.add({ id: "mobile", name: "Mobile", width: "375px" });
    var panelButtons = [
      { id: "dev-desktop", label: "Desktop", command: function () { editor.setDevice("Desktop"); } },
      { id: "dev-mobile", label: "Mobile", command: function () { editor.setDevice("Mobile"); } },
      {
        id: "dev-dark",
        label: "Dark",
        command: function () {
          var frame = editor.Canvas.getFrameEl();
          if (frame && frame.contentDocument && frame.contentDocument.body) {
            frame.contentDocument.body.classList.toggle("akilent-dark-preview");
          }
        },
      },
    ];
    panelButtons.forEach(function (b) {
      editor.Panels.addButton("options", {
        id: b.id,
        className: "gjs-pn-btn",
        label: b.label,
        command: b.command,
        togglable: false,
      });
    });

    editor.on("canvas:frame:load", function (opts) {
      var doc = (opts && opts.window && opts.window.document) || editor.Canvas.getDocument();
      if (!doc || doc.getElementById("akilent-dark-style")) return;
      var style = doc.createElement("style");
      style.id = "akilent-dark-style";
      style.textContent =
        "body.akilent-dark-preview{background:#0f172a!important;color:#e2e8f0!important;}" +
        "body.akilent-dark-preview *{border-color:#334155!important;}";
      doc.head.appendChild(style);
    });

    if (config.projectData && Object.keys(config.projectData).length) {
      editor.loadProjectData(config.projectData);
    } else if (config.html) {
      editor.setComponents(config.html);
    }

    buildMergeTagPanel(editor, config.mergeTags || []);

    var saveBtn = document.getElementById("email-builder-save");
    var statusEl = document.getElementById("email-builder-status");
    var nameEl = document.getElementById("tpl-name");
    var subjectEl = document.getElementById("tpl-subject");

    function currentHtml() {
      var css = editor.getCss();
      var html = editor.getHtml();
      return css ? "<style>" + css + "</style>" + html : html;
    }

    function currentSubject() {
      return (subjectEl && subjectEl.value) || config.subject || "";
    }

    function save() {
      var html = currentHtml();
      var form = new FormData();
      form.append("csrfmiddlewaretoken", config.csrfToken);
      form.append("name", (nameEl && nameEl.value) || config.name || "");
      form.append("subject", currentSubject());
      form.append("html_body", html);
      form.append("text_body", stripTags(html));
      form.append("content_blocks", JSON.stringify(editor.getProjectData()));
      form.append("sample_variables", JSON.stringify(readSampleVariables(config)));
      form.append("builder_mode", "blocks");

      return fetch(config.saveUrl, { method: "POST", body: form })
        .then(function (res) {
          if (statusEl) {
            statusEl.textContent = res.ok ? "Saved." : "Save failed.";
          }
          if (!res.ok && window.toast) {
            window.toast("danger", "Save failed (" + res.status + "). Your changes are still in the editor — try again.");
          }
          return res;
        })
        .catch(function () {
          if (statusEl) statusEl.textContent = "Save failed.";
          if (window.toast) {
            window.toast("danger", "Save failed — check your connection and try again.");
          }
        });
    }

    // GrapesJS's asset manager only console.error()s on a failed upload by
    // default (invalid image, plan limit, etc.) — surface it to the user via
    // the app's toast system instead of leaving it silent.
    editor.on("asset:upload:error", function (errText) {
      var message = "Image upload failed.";
      try {
        var parsed = typeof errText === "string" ? JSON.parse(errText) : errText;
        if (parsed && parsed.error) message = parsed.error;
      } catch (e) {
        if (typeof errText === "string" && errText) message = errText;
      }
      if (window.toast) window.toast("danger", message);
    });

    var refreshPreview = window.TemplatePreview.debounce(function () {
      window.TemplatePreview.refresh(config.previewUrl, config.csrfToken, {
        subject: currentSubject(),
        html_body: currentHtml(),
        text_body: stripTags(currentHtml()),
        variables: readSampleVariables(config),
      });
    }, 400);

    editor.on("update", refreshPreview);
    if (subjectEl) subjectEl.addEventListener("input", refreshPreview);
    var sampleVarsEl = document.getElementById("tpl-sample-variables");
    if (sampleVarsEl) sampleVarsEl.addEventListener("input", refreshPreview);

    if (saveBtn) saveBtn.addEventListener("click", save);

    // Initial paint so the preview pane isn't blank before the first edit.
    refreshPreview();

    // Shared draft shape with window.RawEditor.buildDraft() (raw-editor.js)
    // so callers like send-test.js don't need to branch on mode themselves.
    editor.getDraft = function () {
      var html = currentHtml();
      return {
        subject: currentSubject(),
        text_body: stripTags(html),
        html_body: html,
        variables: readSampleVariables(config),
      };
    };

    window.EmailBuilderEditor = editor;
    return editor;
  }

  document.addEventListener("DOMContentLoaded", function () {
    var container = document.getElementById("email-builder");
    if (container && window.grapesjs) {
      initBuilder(container);
    }
  });
})();
