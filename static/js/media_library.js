/*
 * Media library (templates/publishing/media_library.html, media_edit.html).
 *
 * mediaLibrary(): upload several files at once, through the same endpoints as the
 * composer (straight to S3 when direct upload is on, otherwise through the server),
 * and search and add Pexels stock photos. The library records each upload itself.
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

  window.mediaLibrary = function mediaLibrary() {
    var cfg = config("media-library-config");
    return {
      uploads: [],
      busy: false,
      stockQuery: "",
      stockPhotos: [],
      stockError: "",
      stockLoading: false,
      adding: null,

      onFiles: async function (files) {
        var self = this;
        this.busy = true;
        Array.prototype.forEach.call(files, function (f) { self.uploads.push({ name: f.name, progress: 0, error: "", done: false, file: f }); });
        for (var i = 0; i < this.uploads.length; i++) {
          var u = this.uploads[i];
          if (u.done || u.error) continue;
          try {
            await this.uploadOne(u);
            u.done = true;
            u.progress = 100;
          } catch (e) {
            u.error = e.message;
          }
        }
        this.busy = false;
        if (this.uploads.every(function (u) { return u.done; })) window.location.reload();
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
        var started = await postForm(cfg.upload_start, { name: f.name, content_type: f.type, size: f.size, sha256: "" });
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
          window.location.reload();
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
          self.error = "This picture can't be edited yet: the media server doesn't allow it " +
            "to be read back, so it's shown as a preview only. Your files and posts aren't affected.";
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
