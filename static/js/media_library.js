/*
 * Media library (templates/publishing/media_library.html, media_edit.html).
 *
 * mediaLibrary(): upload several files at once (the button, or dropped anywhere on the
 * page), through the same endpoints as the composer (straight to S3 when direct upload is
 * on, otherwise through the server), each shown as a card with its progress until the grid
 * is refreshed in place; tick files (Shift for a range) for the selection bar; and search
 * and add Pexels stock photos. The library records each upload itself.
 *
 * mediaEditor(): crop and rotate with Cropper.js at a channel's shape, add one line of
 * text, and save the result as a new picture. The original is never changed.
 */
(function () {
  "use strict";

  function csrf() {
    var el = document.querySelector("[name=csrfmiddlewaretoken]");
    return el ? el.value : "";
  }

  function config(id) {
    var el = document.getElementById(id);
    return el ? JSON.parse(el.textContent) : {};
  }

  async function postForm(url, fields) {
    var fd = new FormData();
    Object.keys(fields).forEach(function (k) { fd.append(k, fields[k]); });
    fd.append("csrfmiddlewaretoken", csrf());
    var r = await fetch(url, { method: "POST", body: fd });
    var data = {};
    try { data = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok || data.error) throw new Error(data.error || "Failed (" + r.status + ").");
    return data;
  }

  function send(url, fd, onProgress) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.upload.onprogress = function (e) { if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100)); };
      xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 300) {
          var data = {};
          try { data = JSON.parse(xhr.responseText || "{}"); } catch (e) { /* S3 sends no body */ }
          resolve(data);
        } else {
          var msg = "Upload failed (" + xhr.status + ").";
          try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) { /* not JSON */ }
          reject(new Error(msg));
        }
      };
      xhr.onerror = function () { reject(new Error("Network error. Try again when you're back online.")); };
      xhr.open("POST", url);
      xhr.send(fd);
    });
  }

  var ACCEPTED = /^(image\/(jpeg|png|webp|gif)|video\/(mp4|quicktime))$/;
  var HASH_MAX = 25 * 1024 * 1024;

  // A file's SHA-256 (hex), for spotting one already in the library; "" for large files or
  // where the browser can't (it's only a hint, the server records its own on upload).
  async function sha256Of(file) {
    if (!window.crypto || !crypto.subtle || file.size > HASH_MAX) return "";
    try {
      var digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
      return Array.prototype.map.call(new Uint8Array(digest), function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
    } catch (e) {
      return "";
    }
  }
  var uploadSeq = 0;

  // The grid (#media-results) again, in place: after uploads, a stock photo, or anything else
  // that adds files. Falls back to a reload without htmx.
  function refreshResults() {
    if (!window.htmx || !document.getElementById("media-results")) { window.location.reload(); return Promise.resolve(); }
    return window.htmx.ajax("GET", window.location.pathname + window.location.search,
      { target: "#media-results", select: "#media-results", swap: "outerHTML" });
  }

  window.mediaLibrary = function mediaLibrary() {
    var cfg = config("media-library-config");
    // Grid or list: both are on the page, so switching is instant (it used to reload the
    // results). The choice is remembered in this browser; ?view= in a link still wins.
    var fromUrl = new URLSearchParams(window.location.search).get("view");
    var saved = null;
    try { saved = window.localStorage.getItem("akilent:media:view"); } catch (e) { /* private mode */ }
    return {
      view: fromUrl === "list" || fromUrl === "grid" ? fromUrl : (saved === "list" ? "list" : "grid"),
      setView: function (v) {
        this.view = v;
        try { window.localStorage.setItem("akilent:media:view", v); } catch (e) { /* private mode */ }
      },
      uploads: [],
      busy: false,
      dragging: false,
      dragDepth: 0,
      selected: [],
      lastPicked: null,
      stockQuery: "",
      stockPhotos: [],
      stockError: "",
      stockLoading: false,
      adding: null,

      // Tick or untick one file; with Shift, every file between it and the last one picked
      // takes the same state, in the order they're shown.
      toggle: function (e, id) {
        var on = e.target.checked;
        var ids = [id];
        if (e.shiftKey && this.lastPicked !== null) {
          var order = Array.prototype.map.call(document.querySelectorAll("#media-results [data-asset]"),
            function (el) { return Number(el.getAttribute("data-asset")); });
          var a = order.indexOf(this.lastPicked);
          var b = order.indexOf(id);
          if (a !== -1 && b !== -1) ids = order.slice(Math.min(a, b), Math.max(a, b) + 1);
        }
        var set = this.selected.filter(function (x) { return ids.indexOf(x) === -1; });
        this.selected = on ? set.concat(ids) : set;
        this.lastPicked = id;
      },

      // Files dragged anywhere over the page. dragenter/dragleave fire for every element
      // crossed, so a depth count says when the drag has really left the window.
      hasFiles: function (e) {
        return !!(e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") !== -1);
      },
      dragEnter: function (e) {
        if (!this.hasFiles(e) || document.getElementById("overlay-host") && !document.getElementById("overlay-host").hidden) return;
        e.preventDefault();
        this.dragDepth++;
        this.dragging = true;
      },
      dragOver: function (e) {
        if (!this.hasFiles(e) || !this.dragging) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
      },
      dragLeave: function (e) {
        if (!this.dragging) return;
        this.dragDepth = Math.max(0, this.dragDepth - 1);
        if (!this.dragDepth) this.dragging = false;
      },
      drop: function (e) {
        if (!this.dragging) return;
        e.preventDefault();
        this.dragging = false;
        this.dragDepth = 0;
        if (e.dataTransfer && e.dataTransfer.files.length) this.onFiles(e.dataTransfer.files);
      },

      onFiles: async function (files) {
        var self = this;
        this.busy = true;
        Array.prototype.forEach.call(files, function (f) {
          var ok = ACCEPTED.test(f.type);
          self.uploads.push({
            key: ++uploadSeq, name: f.name, progress: 0, done: false, file: f,
            sha: "", duplicate: null, decide: null, skipped: false,
            error: ok ? "" : "Not a picture or video this library takes (JPEG, PNG, WebP, GIF, MP4 or MOV).",
            preview: ok && f.type.indexOf("image/") === 0 && window.URL ? URL.createObjectURL(f) : "",
          });
        });
        for (var i = 0; i < this.uploads.length; i++) {
          var u = this.uploads[i];
          if (u.done || u.error || u.skipped) continue;
          try {
            // Already in the library? Ask before adding a copy (the card shows Keep both / Skip).
            u.sha = await sha256Of(u.file);
            var match = await this.lookSimilar(u);
            if (match) {
              u.duplicate = match;
              var choice = await new Promise(function (resolve) { u.decide = resolve; });
              u.duplicate = null;
              if (choice === "skip") { u.skipped = true; continue; }
            }
            await this.uploadOne(u);
            u.done = true;
            u.progress = 100;
          } catch (e) {
            u.error = e.message;
          }
        }
        this.busy = false;
        if (this.uploads.some(function (u) { return u.done; })) await refreshResults();
        // Finished and skipped ones go (the finished are in the grid now); failed ones stay, with why.
        this.uploads.filter(function (u) { return (u.done || u.skipped) && u.preview; }).forEach(function (u) { URL.revokeObjectURL(u.preview); });
        this.uploads = this.uploads.filter(function (u) { return !u.done && !u.skipped; });
      },

      lookSimilar: async function (u) {
        if (!cfg.similar) return null;
        var q = "?name=" + encodeURIComponent(u.name) + "&size=" + u.file.size + (u.sha ? "&sha256=" + u.sha : "");
        try {
          var r = await fetch(cfg.similar + q, { headers: { Accept: "application/json" } });
          var data = r.ok ? await r.json() : {};
          return data.match || null;
        } catch (e) {
          return null; // the check is a courtesy: never block an upload on it
        }
      },

      uploadOne: async function (u) {
        var f = u.file;
        var progress = function (p) { u.progress = Math.min(99, p); };
        if (!cfg.direct) {
          var fd = new FormData();
          fd.append("file", f);
          fd.append("csrfmiddlewaretoken", csrf());
          return send(cfg.upload, fd, progress);
        }
        var started = await postForm(cfg.upload_start, { name: f.name, content_type: f.type, size: f.size, sha256: u.sha || "" });
        var s3 = new FormData();
        Object.keys(started.fields).forEach(function (k) { s3.append(k, started.fields[k]); });
        s3.append("file", f);
        await send(started.url, s3, progress);
        return postForm(cfg.upload_confirm, { token: started.token });
      },

      searchStock: async function () {
        this.stockError = "";
        this.stockLoading = true;
        try {
          var r = await fetch(cfg.stock_search + "?q=" + encodeURIComponent(this.stockQuery), { headers: { Accept: "application/json" } });
          var data = await r.json();
          if (!r.ok || data.error) throw new Error(data.error || "Search failed.");
          this.stockPhotos = data.photos || [];
          if (!this.stockPhotos.length) this.stockError = "No photos found. Try other words.";
        } catch (e) {
          this.stockError = e.message;
        } finally {
          this.stockLoading = false;
        }
      },

      addStock: async function (photo) {
        this.adding = photo.id;
        this.stockError = "";
        try {
          await postForm(cfg.stock_import, { photo: photo.id });
          await refreshResults();
          if (window.toast) window.toast("success", "Added to the library.");
        } catch (e) {
          this.stockError = e.message;
        } finally {
          this.adding = null;
        }
      },
    };
  };

  window.mediaEditor = function mediaEditor() {
    var cfg = config("media-editor-config");
    return {
      cropper: null,
      ratio: NaN,
      text: "",
      textPos: "bottom",
      textColor: "#ffffff",
      saving: false,
      previewOnly: false,
      error: "",
      saved: null,

      init: function () {
        var img = this.$refs.image;
        var self = this;
        var start = function () {
          // No wheel zoom: over a picture this tall it caught the wheel, and the modal
          // couldn't be scrolled from there.
          self.cropper = new window.Cropper(img, { viewMode: 1, autoCropArea: 1, background: false, zoomOnWheel: false });
        };
        // A picture that failed to load is "complete" too, with no size: usually the media
        // server not allowing it to be read back (CORS), which editing needs.
        // Then show it anyway, loaded without CORS: a preview only (Cropper is never started
        // on it, since a picture the page can't read back can't be saved).
        var fail = function () {
          img.removeEventListener("load", start);
          self.previewOnly = true;
          self.error = "This picture couldn't be loaded for editing, so it's shown as a " +
            "preview only. Close and try again in a moment. Your files and posts aren't affected.";
          // A new element: the same one, with crossorigin removed, kept its failed load.
          var preview = new Image();
          preview.alt = img.alt;
          preview.className = img.className;
          preview.style.cssText = img.style.cssText;
          preview.setAttribute("data-media-preview", "");
          preview.src = img.getAttribute("src");
          img.replaceWith(preview);
        };
        if (img.complete) {
          if (img.naturalWidth) start(); else fail();
        } else {
          img.addEventListener("load", start, { once: true });
          img.addEventListener("error", fail, { once: true });
        }
      },

      setRatio: function (r) {
        this.ratio = r;
        if (this.cropper) this.cropper.setAspectRatio(r);
      },

      rotate: function (deg) { if (this.cropper) this.cropper.rotate(deg); },

      // The cropped picture, with the text line drawn on it. One line, one size: a
      // caption for the picture, not a design tool.
      render: function () {
        var canvas = this.cropper.getCroppedCanvas({ maxWidth: 4096, maxHeight: 4096, imageSmoothingQuality: "high" });
        var text = (this.text || "").trim();
        if (!text) return canvas;
        var ctx = canvas.getContext("2d");
        var size = Math.max(16, Math.round(canvas.width / 18));
        ctx.font = "600 " + size + "px system-ui, -apple-system, Segoe UI, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = this.textPos === "top" ? "top" : "bottom";
        var pad = Math.round(size * 0.8);
        var y = this.textPos === "top" ? pad : canvas.height - pad;
        ctx.lineWidth = Math.max(2, Math.round(size / 8));
        ctx.strokeStyle = this.textColor === "#ffffff" ? "rgba(0,0,0,0.6)" : "rgba(255,255,255,0.6)";
        ctx.fillStyle = this.textColor;
        ctx.strokeText(text, canvas.width / 2, y, canvas.width - pad * 2);
        ctx.fillText(text, canvas.width / 2, y, canvas.width - pad * 2);
        return canvas;
      },

      save: function () {
        var self = this;
        if (!this.cropper) return;
        this.saving = true;
        this.error = "";
        var canvas;
        try {
          canvas = this.render();
        } catch (e) {
          // A picture from another site without CORS can't be read back.
          this.error = "This picture can't be edited in the browser.";
          this.saving = false;
          return;
        }
        canvas.toBlob(async function (blob) {
          try {
            var fd = new FormData();
            fd.append("file", new File([blob], "edited.jpg", { type: "image/jpeg" }));
            fd.append("csrfmiddlewaretoken", csrf());
            var data = await send(cfg.save, fd, function () {});
            self.saved = data;
            // Opened over the media grid: close and show the new picture in the grid.
            if (self.$root.closest("#overlay-host") && window.akilent.overlaySaved) {
              window.akilent.overlaySaved("Saved as a new picture.");
            } else {
              window.location.href = cfg.library;
            }
          } catch (e) {
            self.error = e.message;
          } finally {
            self.saving = false;
          }
        }, "image/jpeg", 0.92);
      },
    };
  };
})();
