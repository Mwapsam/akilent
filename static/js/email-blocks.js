/*
 * The block editor for email templates (template_blocks.html, apps.email.blocks).
 *
 * The email is a list of sections (`doc.blocks`). This file edits that list and draws an
 * approximation of it on the canvas; the server builds the real HTML from the list on every
 * save, preview and test, so what's sent never depends on this drawing. Loaded as a plain
 * script before Alpine starts (base.html loads Alpine with defer), so `emailBlocks` exists when
 * the page's x-data asks for it.
 */
(function () {
  "use strict";

  var ICONS = {
    heading: '<svg viewBox="0 0 24 24"><path d="M6 4v16M18 4v16M6 12h12"/></svg>',
    text: '<svg viewBox="0 0 24 24"><path d="M4 6h16M4 10h16M4 14h10M4 18h13"/></svg>',
    image: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/></svg>',
    button: '<svg viewBox="0 0 24 24"><rect x="3" y="7" width="18" height="10" rx="5"/><path d="M9 12h6"/></svg>',
    columns: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="8" height="16" rx="1.5"/><rect x="13" y="4" width="8" height="16" rx="1.5"/></svg>',
    divider: '<svg viewBox="0 0 24 24"><path d="M3 12h18"/><path d="M8 7h8M8 17h8" opacity=".4"/></svg>',
    spacer: '<svg viewBox="0 0 24 24"><path d="M12 4v16M8 8l4-4 4 4M8 16l4 4 4-4"/></svg>',
    footer: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 15h18M7 18h6"/></svg>',
    html: '<svg viewBox="0 0 24 24"><path d="m8 8-4 4 4 4M16 8l4 4-4 4M14 5l-4 14"/></svg>',
  };

  var TYPES = [
    { type: "heading", name: "Heading", desc: "A title or greeting" },
    { type: "text", name: "Text", desc: "Paragraphs of words" },
    { type: "image", name: "Image", desc: "A photo or logo" },
    { type: "button", name: "Button", desc: "A link people tap" },
    { type: "columns", name: "Two columns", desc: "Side by side, stacked on phones" },
    { type: "divider", name: "Divider", desc: "A thin line" },
    { type: "spacer", name: "Space", desc: "Room between sections" },
    { type: "footer", name: "Footer", desc: "Small print at the bottom" },
    { type: "html", name: "HTML", desc: "Your own code (advanced)" },
  ].map(function (t) { return Object.assign({ icon: ICONS[t.type] }, t); });

  var FONTS = {
    sans: "Arial,Helvetica,sans-serif",
    serif: "Georgia,'Times New Roman',serif",
    rounded: "'Trebuchet MS',Verdana,sans-serif",
    mono: "'Courier New',Courier,monospace",
  };
  var HEADING_SIZES = { small: 18, medium: 22, large: 28 };
  var TAG = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*\}\}/g;
  var ONLY_TAG = /^\{\{\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*\s*\}\}$/;
  var URL_OK = /^(https?:\/\/[^\s"'<>]+|mailto:[^\s"'<>]+|tel:[+0-9 ()-]+)$/i;
  var HISTORY_LIMIT = 100;
  var AUTOSAVE_MS = 3000;

  function uid() {
    return "b" + Math.random().toString(16).slice(2, 12);
  }

  function copy(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // Escaped words as the canvas shows them: blanks as chips, **bold**, line breaks.
  function words(text) {
    return esc(text)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(TAG, function (_, name) { return '<span class="eb-chip">' + name + "</span>"; })
      .replace(/\n/g, "<br>");
  }

  function paragraphs(text) {
    return String(text || "").split(/\n\s*\n/).filter(function (p) { return p.trim(); });
  }

  function isMobile() {
    return window.matchMedia("(max-width: 1023px)").matches;
  }

  function flatten(obj, prefix, out) {
    out = out || [];
    Object.keys(obj || {}).forEach(function (k) {
      var key = prefix ? prefix + "." + k : k;
      var v = obj[k];
      if (v && typeof v === "object" && !Array.isArray(v)) flatten(v, key, out);
      else out.push({ key: key, value: v == null ? "" : String(v) });
    });
    return out;
  }

  function unflatten(rows) {
    var out = {};
    rows.forEach(function (row) {
      var key = String(row.key || "").trim();
      if (!key) return;
      var parts = key.split(".");
      var node = out;
      for (var i = 0; i < parts.length - 1; i++) {
        if (typeof node[parts[i]] !== "object" || node[parts[i]] === null) node[parts[i]] = {};
        node = node[parts[i]];
      }
      node[parts[parts.length - 1]] = row.value;
    });
    return out;
  }

  function csrf(config) {
    if (config.csrfToken) return config.csrfToken;
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function toast(kind, message) {
    if (window.toast) window.toast(kind, message);
  }

  function newBlock(type, style) {
    var b = { id: uid(), type: type };
    switch (type) {
      case "heading": return Object.assign(b, { text: "A clear heading", size: "medium", align: "left" });
      case "text": return Object.assign(b, { text: "Write your message here.", align: "left" });
      case "image": return Object.assign(b, { src: "", alt: "", href: "", width: 100, align: "center" });
      case "button": return Object.assign(b, { label: "Find out more", href: "", align: "left", color: "", text_color: "", full_width: false });
      case "divider": return Object.assign(b, { color: "" });
      case "spacer": return Object.assign(b, { height: 24 });
      case "columns":
        return Object.assign(b, {
          columns: [0, 1].map(function (i) {
            return {
              image: { src: "", alt: "" },
              heading: i ? "Second item" : "First item",
              text: "A short description.",
              button: { label: "", href: "" },
            };
          }),
        });
      case "footer": return Object.assign(b, { text: "{{ company_name }}", align: "center" });
      case "html": return Object.assign(b, { html: "<p>Your HTML here</p>" });
    }
    return b;
  }

  window.emailBlocks = function (config) {
    var doc = copy(config.doc);
    return {
      config: config,
      types: TYPES,
      doc: doc,
      name: config.name || "",
      subject: config.subject || "",
      samples: flatten(config.sampleVariables || {}),
      selectedId: null,
      device: "desktop",
      sheet: "",
      error: "",
      saving: false,
      saveQueued: false,
      lastSaved: "",
      savedAt: null,
      saveFailed: "",
      leaving: false,
      past: [],
      future: [],
      current: "",
      dirty: false,
      lastField: null,
      dropAt: null,
      dragId: null,
      dragType: null,
      previewOpen: false,
      previewLoading: false,
      previewDevice: "desktop",
      previewData: {},
      testing: false,
      picker: { open: false, target: null, assets: [], loaded: false, loading: false, uploading: false, error: "" },
      zoom: 1,
      _commitTimer: null,
      _saveTimer: null,

      init: function () {
        this.current = this.serialize();
        this.lastSaved = this.current;
        var self = this;
        ["doc", "name", "subject", "samples"].forEach(function (prop) {
          self.$watch(prop, function () { self.changed(); });
        });
        // "Desktop" shows the real 600px layout, scaled down when the canvas is narrower, so two
        // columns sit side by side as they will in the inbox instead of stacking early.
        var canvas = this.$refs.canvas;
        if (canvas && window.ResizeObserver) {
          new ResizeObserver(function () {
            if (isMobile()) { self.zoom = 1; return; } // phones: the email just fills the width
            var style = getComputedStyle(canvas);
            var room = canvas.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
            self.zoom = Math.max(0.5, Math.min(1, room / 648));
          }).observe(canvas);
        }
      },

      // ---- state, history, saving ---------------------------------------------------------
      serialize: function () {
        return JSON.stringify({ doc: this.doc, name: this.name, subject: this.subject, samples: this.samples });
      },

      changed: function () {
        var self = this;
        this.dirty = this.serialize() !== this.lastSaved;
        clearTimeout(this._commitTimer);
        this._commitTimer = setTimeout(function () { self.commit(); }, 350);
        clearTimeout(this._saveTimer);
        if (this.dirty) this._saveTimer = setTimeout(function () { self.save(false); }, AUTOSAVE_MS);
      },

      commit: function () {
        clearTimeout(this._commitTimer);
        var now = this.serialize();
        if (now === this.current) return;
        this.past.push(this.current);
        if (this.past.length > HISTORY_LIMIT) this.past.shift();
        this.current = now;
        this.future = [];
      },

      restore: function (snapshot) {
        var s = JSON.parse(snapshot);
        this.current = snapshot;
        this.doc = s.doc;
        this.name = s.name;
        this.subject = s.subject;
        this.samples = s.samples;
        if (this.selectedId && !this.find(this.selectedId)) this.selectedId = null;
      },

      undo: function () {
        this.commit();
        if (!this.past.length) return;
        this.future.push(this.current);
        this.restore(this.past.pop());
      },

      redo: function () {
        this.commit();
        if (!this.future.length) return;
        this.past.push(this.current);
        this.restore(this.future.pop());
      },

      sampleObject: function () {
        return unflatten(this.samples);
      },

      save: function (explicit) {
        var self = this;
        clearTimeout(this._saveTimer);
        if (this.saving) {
          this.saveQueued = explicit || this.saveQueued === "explicit" ? "explicit" : "auto";
          return Promise.resolve();
        }
        var sent = this.serialize();
        if (!explicit && sent === this.lastSaved) return Promise.resolve();
        this.saving = true;
        var form = new FormData();
        form.append("csrfmiddlewaretoken", csrf(this.config));
        form.append("name", this.name.trim() || "Untitled email");
        form.append("subject", this.subject);
        form.append("builder_mode", "blocks");
        form.append("content_blocks", JSON.stringify(this.doc));
        form.append("sample_variables", JSON.stringify(this.sampleObject()));
        form.append(explicit ? "snapshot" : "autosave", "1");
        return fetch(this.config.saveUrl, {
          method: "POST", body: form, credentials: "same-origin",
          headers: { "X-Requested-With": "XMLHttpRequest", Accept: "application/json", "X-CSRFToken": csrf(this.config) },
        })
          .then(function (res) {
            return res.json().catch(function () { return {}; }).then(function (data) { return { ok: res.ok, data: data }; });
          })
          .then(function (r) {
            if (r.ok && r.data.saved !== false) {
              self.lastSaved = sent;
              self.savedAt = new Date();
              self.saveFailed = "";
              if (explicit) { self.error = ""; toast("success", "Saved."); }
            } else {
              self.saveFailed = (r.data && r.data.error) || "The email couldn't be saved.";
              if (explicit) self.error = self.saveFailed;
            }
          })
          .catch(function () {
            self.saveFailed = "Couldn't reach Akilent. Your changes are still here; try again.";
            if (explicit) self.error = self.saveFailed;
          })
          .finally(function () {
            self.saving = false;
            self.dirty = self.serialize() !== self.lastSaved;
            if (self.saveQueued) {
              var again = self.saveQueued === "explicit";
              self.saveQueued = false;
              if (self.dirty || again) self.save(again);
            }
          });
      },

      statusText: function () {
        if (this.saving) return "Saving…";
        if (this.saveFailed) return "Not saved";
        if (this.dirty) return "Unsaved changes";
        if (this.savedAt) return "Saved " + this.savedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        return "All changes saved";
      },

      statusTone: function () {
        if (this.saveFailed) return "bad";
        if (this.dirty || this.saving) return "busy";
        return "ok";
      },

      onLeave: function (e) {
        if ((this.dirty || this.saving) && !this.leaving) {
          e.preventDefault();
          e.returnValue = "";
        }
      },

      onKey: function (e) {
        var mod = e.ctrlKey || e.metaKey;
        var key = (e.key || "").toLowerCase();
        if (mod && key === "s") { e.preventDefault(); this.save(true); return; }
        var t = e.target;
        var typing = t && (t.isContentEditable || /^(input|textarea|select)$/i.test(t.tagName));
        if (typing || this.previewOpen || this.picker.open) return;
        if (mod && key === "z") { e.preventDefault(); if (e.shiftKey) this.redo(); else this.undo(); return; }
        if (mod && key === "y") { e.preventDefault(); this.redo(); return; }
        if (!this.selectedId) return;
        if (key === "delete" || key === "backspace") { e.preventDefault(); this.remove(this.selectedId); }
        else if (key === "escape") { this.select(null); }
        else if (e.altKey && key === "arrowup") { e.preventDefault(); this.move(this.selectedId, -1); }
        else if (e.altKey && key === "arrowdown") { e.preventDefault(); this.move(this.selectedId, 1); }
      },

      // ---- sections ------------------------------------------------------------------------
      find: function (id) {
        return this.doc.blocks.find(function (b) { return b.id === id; }) || null;
      },

      get selected() {
        return this.selectedId ? this.find(this.selectedId) : null;
      },

      selectedIndex: function () {
        var id = this.selectedId;
        return this.doc.blocks.findIndex(function (b) { return b.id === id; });
      },

      select: function (id) {
        this.selectedId = id;
        if (id && isMobile()) this.sheet = "edit";
        if (id) {
          var self = this;
          this.$nextTick(function () {
            var el = self.$root.querySelector('.eb-block.is-selected');
            if (el && !isMobile()) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
          });
        }
      },

      showEmailPanel: function () {
        this.selectedId = null;
        if (isMobile()) this.sheet = "edit";
      },

      add: function (type, index) {
        var block = newBlock(type, this.doc.style);
        var at = index;
        if (at == null) {
          var sel = this.selectedIndex();
          var blocks = this.doc.blocks;
          var last = blocks[blocks.length - 1];
          // Nothing selected: add at the end, but keep the footer last.
          at = sel >= 0 ? sel + 1 : (last && last.type === "footer" && type !== "footer" ? blocks.length - 1 : blocks.length);
        }
        this.doc.blocks.splice(at, 0, block);
        this.commit();
        this.select(block.id);
        return block;
      },

      move: function (id, dir) {
        var i = this.doc.blocks.findIndex(function (b) { return b.id === id; });
        var j = i + dir;
        if (i < 0 || j < 0 || j >= this.doc.blocks.length) return;
        var block = this.doc.blocks.splice(i, 1)[0];
        this.doc.blocks.splice(j, 0, block);
        this.commit();
      },

      duplicate: function (id) {
        var i = this.doc.blocks.findIndex(function (b) { return b.id === id; });
        if (i < 0) return;
        var clone = copy(this.doc.blocks[i]);
        clone.id = uid();
        this.doc.blocks.splice(i + 1, 0, clone);
        this.commit();
        this.select(clone.id);
      },

      remove: function (id) {
        var i = this.doc.blocks.findIndex(function (b) { return b.id === id; });
        if (i < 0) return;
        this.doc.blocks.splice(i, 1);
        this.commit();
        var next = this.doc.blocks[i] || this.doc.blocks[i - 1];
        this.selectedId = next ? next.id : null;
        if (!next) this.sheet = "";
        toast("info", "Section deleted. Press Undo to bring it back.");
      },

      typeName: function (type) {
        var t = TYPES.find(function (x) { return x.type === type; });
        return t ? t.name : type;
      },

      label: function (b) {
        var text = b.text || b.label || b.alt || "";
        text = String(text).replace(/\s+/g, " ").trim();
        return this.typeName(b.type) + (text ? ": " + text.slice(0, 60) : "");
      },

      // ---- drag and drop (desktop) ---------------------------------------------------------
      dragNew: function (e, type) {
        this.dragType = type;
        this.dragId = null;
        e.dataTransfer.effectAllowed = "copy";
        e.dataTransfer.setData("text/plain", "new:" + type);
      },

      dragExisting: function (e, id) {
        this.dragId = id;
        this.dragType = null;
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", "move:" + id);
        var block = e.target.closest(".eb-block");
        if (block && e.dataTransfer.setDragImage) e.dataTransfer.setDragImage(block, 24, 24);
      },

      dragOverBlock: function (e, i) {
        var rect = e.currentTarget.getBoundingClientRect();
        this.dropAt = e.clientY < rect.top + rect.height / 2 ? i : i + 1;
      },

      dragOverCanvas: function (e) {
        if (this.dropAt == null || !this.doc.blocks.length) this.dropAt = this.doc.blocks.length;
      },

      drop: function (e) {
        var at = this.dropAt == null ? this.doc.blocks.length : this.dropAt;
        var data = (e.dataTransfer && e.dataTransfer.getData("text/plain")) || "";
        if (data.indexOf("new:") === 0 || this.dragType) {
          this.add(this.dragType || data.slice(4), at);
        } else if (data.indexOf("move:") === 0 || this.dragId) {
          var id = this.dragId || data.slice(5);
          var from = this.doc.blocks.findIndex(function (b) { return b.id === id; });
          if (from >= 0) {
            var block = this.doc.blocks.splice(from, 1)[0];
            if (from < at) at -= 1;
            this.doc.blocks.splice(at, 0, block);
            this.commit();
            this.select(id);
          }
        }
        this.dragEnd();
      },

      dragEnd: function () {
        this.dropAt = null;
        this.dragId = null;
        this.dragType = null;
      },

      // ---- drawing the canvas ---------------------------------------------------------------
      pageStyle: function () {
        var zoom = this.device === "desktop" ? this.zoom : 1;
        return "background:" + this.doc.style.background + ";zoom:" + zoom.toFixed(3) + ";";
      },

      emailStyle: function () {
        var s = this.doc.style;
        return [
          "background:" + s.content_background,
          "color:" + s.text_color,
          "font-family:" + (FONTS[s.font] || FONTS.sans),
          "border-radius:" + (s.rounded ? "12px" : "0"),
        ].join(";");
      },

      buttonHtml: function (label, opts) {
        var s = this.doc.style;
        opts = opts || {};
        return '<div style="text-align:' + (opts.align || "left") + ';margin:0 0 20px;">' +
          '<span style="display:' + (opts.full ? "block" : "inline-block") + ";background:" + (opts.color || s.accent) +
          ";color:" + (opts.textColor || s.button_text) + ";font-weight:600;font-size:15px;line-height:1.2;" +
          "padding:12px 24px;border-radius:" + (s.rounded ? "8px" : "0") + ';text-align:center;">' +
          words(label) + "</span></div>";
      },

      imageHtml: function (src, alt, width, align) {
        if (!src) {
          return '<div class="eb-placeholder">' + ICONS.image + "<span>Choose an image</span></div>";
        }
        return '<div style="text-align:' + (align || "center") + ';margin:0 0 20px;line-height:0;">' +
          '<img src="' + esc(src) + '" alt="' + esc(alt) + '" style="display:inline-block;width:' + (width || 100) +
          "%;height:auto;border-radius:" + (this.doc.style.rounded ? "8px" : "0") + ';">' + "</div>";
      },

      preview: function (b) {
        var self = this;
        var muted = function (text) { return '<span class="eb-muted">' + text + "</span>"; };
        switch (b.type) {
          case "heading":
            return '<h2 style="margin:0 0 16px;font-size:' + (HEADING_SIZES[b.size] || 22) +
              "px;line-height:1.25;font-weight:700;text-align:" + b.align + ';">' +
              (b.text ? words(b.text) : muted("Heading")) + "</h2>";
          case "text":
            if (!b.text || !b.text.trim()) return '<p style="margin:0 0 16px;">' + muted("Write something…") + "</p>";
            return paragraphs(b.text).map(function (p) {
              return '<p style="margin:0 0 16px;font-size:15px;line-height:1.6;text-align:' + b.align + ';">' + words(p) + "</p>";
            }).join("");
          case "image":
            return this.imageHtml(b.src, b.alt, b.width, b.align);
          case "button":
            return this.buttonHtml(b.label || "Button", { align: b.align, color: b.color, textColor: b.text_color, full: b.full_width });
          case "divider":
            return '<hr style="border:0;border-top:1px solid ' + (b.color || "#E8EDF3") + ';margin:8px 0 24px;">';
          case "spacer":
            return '<div class="eb-spacer" style="height:' + b.height + 'px;"></div>';
          case "columns":
            return '<div class="eb-cols">' + b.columns.map(function (c) {
              var html = "";
              if (c.image && c.image.src) html += self.imageHtml(c.image.src, c.image.alt, 100, "center");
              if (c.heading) html += '<h3 style="margin:0 0 8px;font-size:17px;line-height:1.3;font-weight:700;">' + words(c.heading) + "</h3>";
              paragraphs(c.text).forEach(function (p) {
                html += '<p style="margin:0 0 12px;font-size:14px;line-height:1.6;">' + words(p) + "</p>";
              });
              if (c.button && c.button.label) html += self.buttonHtml(c.button.label);
              return '<div class="eb-col">' + (html || muted("Empty column")) + "</div>";
            }).join("") + "</div>";
          case "footer":
            return '<div style="margin:24px 0 0;padding-top:16px;border-top:1px solid #E8EDF3;font-size:12px;line-height:1.6;' +
              "color:#657089;text-align:" + b.align + ';">' +
              (paragraphs(b.text).map(function (p) { return '<p style="margin:0 0 8px;">' + words(p) + "</p>"; }).join("") || muted("Footer")) +
              "</div>";
          case "html":
            return "";
        }
        return "";
      },

      fitFrame: function (frame) {
        try {
          var docEl = frame.contentDocument && frame.contentDocument.documentElement;
          if (docEl) frame.style.height = Math.max(40, docEl.scrollHeight) + "px";
        } catch (e) { /* not same-origin: keep the default height */ }
      },

      // ---- blanks, links -----------------------------------------------------------------------
      blankNames: function () {
        var names = this.samples.map(function (r) { return String(r.key || "").trim(); }).filter(Boolean);
        var json = JSON.stringify(this.doc.blocks) + " " + this.subject + " " + this.doc.preheader;
        var m;
        TAG.lastIndex = 0;
        while ((m = TAG.exec(json))) names.push(m[1]);
        ["first_name", "company_name"].forEach(function (n) { names.push(n); });
        return names.filter(function (n, i) { return names.indexOf(n) === i; });
      },

      rememberField: function (e) {
        if (e.target && e.target.hasAttribute && e.target.hasAttribute("data-blanks")) this.lastField = e.target;
      },

      insertBlank: function (name, from) {
        var field = (from && from.closest(".eb-field") && from.closest(".eb-field").querySelector("[data-blanks]")) || this.lastField;
        if (!field) return;
        var tag = "{{ " + name + " }}";
        if (field.getAttribute("inputmode") === "url") {
          field.value = tag; // a link is either an address or exactly one blank
        } else {
          var start = field.selectionStart == null ? field.value.length : field.selectionStart;
          var end = field.selectionEnd == null ? start : field.selectionEnd;
          field.value = field.value.slice(0, start) + tag + field.value.slice(end);
          var caret = start + tag.length;
          field.focus();
          try { field.setSelectionRange(caret, caret); } catch (e) { /* not a text field */ }
        }
        field.dispatchEvent(new Event("input", { bubbles: true }));
        var known = this.samples.some(function (r) { return r.key === name; });
        if (!known && name.indexOf(".") === -1) {
          this.samples.push({ key: name, value: /(url|link)$/.test(name) ? "https://example.com" : name.replace(/_/g, " ") });
        }
      },

      newBlank: function (from) {
        var raw = window.prompt("Name the blank, for example order_number (letters, numbers and _):", "");
        if (!raw) return;
        var name = raw.trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
        if (!/^[a-z_][a-z0-9_]*$/.test(name)) { toast("danger", "Use letters, numbers and _ only, starting with a letter."); return; }
        this.insertBlank(name, from);
      },

      linkProblem: function (href) {
        href = String(href || "").trim();
        if (!href) return "Add a link, or a blank like {{ link }}.";
        if (ONLY_TAG.test(href) || href.charAt(0) === "/") return "";
        if (/^www\./i.test(href)) return "";
        if (!URL_OK.test(href)) return "Start the link with https:// (or use a blank).";
        return "";
      },

      plainText: function () {
        return this.doc.blocks.map(function (b) {
          if (b.type === "columns") return b.columns.map(function (c) { return [c.heading, c.text].join("\n"); }).join("\n");
          return b.text || b.label || "";
        }).filter(Boolean).join("\n\n").slice(0, 20000);
      },

      // ---- preview, test ----------------------------------------------------------------------
      post: function (url, body) {
        return fetch(url, {
          method: "POST", credentials: "same-origin", body: JSON.stringify(body),
          headers: {
            "Content-Type": "application/json", Accept: "application/json",
            "X-CSRFToken": csrf(this.config), "X-Requested-With": "XMLHttpRequest",
          },
        }).then(function (res) {
          return res.json().catch(function () { return {}; }).then(function (data) { return { ok: res.ok, data: data }; });
        });
      },

      openPreview: function () {
        var self = this;
        this.previewOpen = true;
        this.previewLoading = true;
        this.previewData = {};
        this.post(this.config.previewUrl, { subject: this.subject, blocks: this.doc, variables: this.sampleObject() })
          .then(function (r) {
            self.previewData = r.ok
              ? { subject: r.data.subject, html: r.data.html, missing: r.data.missing_variables || [] }
              : { error: (r.data && r.data.error) || "The preview couldn't be built." };
          })
          .catch(function () { self.previewData = { error: "Couldn't reach Akilent. Try again." }; })
          .finally(function () { self.previewLoading = false; });
      },

      sendTest: function () {
        var self = this;
        if (this.testing) return;
        this.testing = true;
        this.post(this.config.sendTestUrl, { subject: this.subject, blocks: this.doc, variables: this.sampleObject() })
          .then(function (r) {
            if (r.ok) toast("success", "Test email on its way. Check your inbox in a minute.");
            else toast("danger", (r.data && r.data.error) || "The test couldn't be sent.");
          })
          .catch(function () { toast("danger", "Couldn't reach Akilent. Try again."); })
          .finally(function () { self.testing = false; });
      },

      // ---- images ------------------------------------------------------------------------------
      pickImage: function (target) {
        var self = this;
        this.picker.target = target;
        this.picker.error = "";
        this.picker.open = true;
        if (this.picker.loaded) return;
        this.picker.loading = true;
        fetch(this.config.assetsUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
          .then(function (res) { return res.json(); })
          .then(function (data) { self.picker.assets = data.assets || []; self.picker.loaded = true; })
          .catch(function () { self.picker.error = "Your images couldn't be loaded. You can still upload one."; })
          .finally(function () { self.picker.loading = false; });
      },

      chooseImage: function (url) {
        if (this.picker.target) this.picker.target.src = url;
        this.picker.open = false;
      },

      upload: function (e) {
        var self = this;
        var file = e.target.files && e.target.files[0];
        if (!file) return;
        this.picker.uploading = true;
        this.picker.error = "";
        var form = new FormData();
        form.append("files", file);
        fetch(this.config.uploadUrl, {
          method: "POST", body: form, credentials: "same-origin",
          headers: { "X-CSRFToken": csrf(this.config), Accept: "application/json" },
        })
          .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
          .then(function (r) {
            var url = r.ok && r.data.data && r.data.data[0];
            if (!url) { self.picker.error = (r.data && r.data.error) || "That image couldn't be uploaded."; return; }
            self.picker.assets.unshift({ url: url, name: file.name });
            self.chooseImage(url);
          })
          .catch(function () { self.picker.error = "That image couldn't be uploaded. Try again."; })
          .finally(function () { self.picker.uploading = false; e.target.value = ""; });
      },
    };
  };
})();
