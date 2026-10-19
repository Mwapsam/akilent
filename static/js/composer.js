/*
 * Content Studio composer (templates/publishing/post_new.html).
 *
 * One Alpine component, composer(), owns the editor, media, preview and action bar.
 * The form's named fields are the truth: autosave, the readiness check and the final
 * submit all send FormData(form), so what is checked is exactly what is saved.
 *
 * Server endpoints (from #post-editor-data .urls):
 *   readiness  POST -> {issues: [{code, level, message, field, platform, blocks, warns, fix_url}]}
 *   autosave   POST -> {saved, post_id, version, wa_link_urls} | 409 conflict / not_draft
 *   upload     POST one file -> {url, name, content_type}
 */
(function () {
  "use strict";

  var NAMES = { instagram: "Instagram", facebook: "Facebook" };
  var VERBS = { publish_now: "publish", schedule: "schedule", submit_review: "submit for review" };
  var AUTOSAVE_DELAY = 1500;
  var READINESS_DELAY = 800;
  var RECOVERY_MAX_AGE = 14 * 24 * 3600 * 1000;

  function editorData() {
    var el = document.getElementById("post-editor-data");
    return el ? JSON.parse(el.textContent) : {};
  }

  function kindOf(url, type) {
    if (type) return type.indexOf("video/") === 0 ? "video" : "image";
    if (/\.(mp4|mov|m4v|webm|avi)(\?|$)/i.test(url || "")) return "video";
    return "image";
  }

  // Looked up by id, not this.$refs: a method called from inside an x-for item gets
  // that item's scope, where $refs doesn't reach the form.
  function formEl() { return document.getElementById("composer-form"); }

  function csrf() {
    var el = document.querySelector("#composer-form [name=csrfmiddlewaretoken]");
    return el ? el.value : "";
  }

  // localStorage can be missing or throw (private mode, blocked storage): never rely on it.
  function storeGet(key) {
    try { var v = window.localStorage.getItem(key); return v ? JSON.parse(v) : null; } catch (e) { return null; }
  }
  function storeSet(key, value) {
    try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* not available */ }
  }
  function storeDel(key) {
    try { window.localStorage.removeItem(key); } catch (e) { /* not available */ }
  }

  var nextId = 1;
  function mediaItem(url, extra) {
    return Object.assign({
      id: nextId++, url: url || "", name: (url || "").split("/").pop(), thumb: "",
      kind: kindOf(url), status: "done", progress: 100, error: "", file: null,
    }, extra || {});
  }

  window.composer = function composer() {
    var d = editorData();
    var saved = d.cta || {};
    var links = d.wa_link_urls || {};
    var hasLinks = Object.keys(links).length > 0;
    var savedOpts = d.options || {};
    var ig = savedOpts.instagram || {};
    var fb = savedOpts.facebook || {};
    var pc = d.platform_captions || {};
    var scheduled = !!(d.scheduled_at || Object.keys(d.channel_times || {}).length);

    return {
      // ── Server state ──────────────────────────────────────────────────────
      d: d,
      urls: d.urls || {},
      limits: d.constraints || { caption_max: {}, hashtags_max: {} },
      postId: d.post_id || null,
      clientKey: d.client_key || "",
      version: d.version || "",
      statusLabel: d.status_label || "Draft",
      reviewGate: !!d.review_gate,
      waLinkUrls: links,
      defaults: d.cta_defaults || {},
      caps: d.option_caps || {},

      // ── Content ───────────────────────────────────────────────────────────
      selectedPlatforms: d.platforms && d.platforms.length ? d.platforms.slice() : ["instagram"],
      mediaType: d.media_type || "image",
      caption: d.caption || "",
      platformCaptions: { instagram: pc.instagram || "", facebook: pc.facebook || "" },
      customized: { instagram: !!pc.instagram, facebook: !!pc.facebook },
      hashtagsText: (d.hashtags || []).map(function (h) { return "#" + String(h).replace(/^#+/, ""); }).join(" "),
      media: (d.media_urls || []).map(function (u) { return mediaItem(u); }),
      cta: {
        kind: saved.kind || "none",
        text: saved.text || "",
        url: saved.url || "",
        prefill: saved.prefill || "",
        // A new WhatsApp CTA goes on every channel; a saved one keeps its choice.
        channels: {
          facebook: hasLinks ? !!links.facebook : true,
          instagram: hasLinks ? !!links.instagram : true,
        },
      },
      opts: {
        instagram: {
          alt_text: ig.alt_text || "",
          location: ig.location || null,
          collaborators: (ig.collaborators || []).map(function (n) { return "@" + n; }).join(", "),
          share_to_feed: ig.share_to_feed !== false,
          cover_url: ig.cover_url || "",
          thumb_offset: ig.thumb_offset ? String(ig.thumb_offset / 1000) : "",
          first_comment: ig.first_comment || "",
        },
        facebook: { first_comment: fb.first_comment || "" },
      },
      publishMode: d.review_gate || scheduled ? "schedule" : "now",
      scheduledAt: d.scheduled_at || "",
      perChannel: !!d.per_channel,
      channelTimes: Object.assign({ instagram: "", facebook: "" }, d.channel_times || {}),

      // ── UI ────────────────────────────────────────────────────────────────
      mobileView: "edit",
      captionTab: "all",
      previewPlatform: (d.platforms && d.platforms[0]) || "instagram",
      previewIndex: 0,
      igRatio: 1,
      expanded: { instagram: false, facebook: false },
      moreOpen: Object.values(savedOpts).some(function (o) { return o && Object.keys(o).length; }),
      loc: { query: "", results: [], loading: false, error: "", searched: false },
      coverUploading: false,
      coverError: "",
      dragIndex: null,
      dragOver: null,
      dropActive: false,
      mediaAnnouncement: "",
      undo: null,
      aiOpen: false,
      brief: { topic: "", goal: "engagement", cta: "" },
      generating: false,
      genError: "",
      captionOptions: [],

      // ── Saving and readiness ──────────────────────────────────────────────
      autosaveEnabled: d.autosave !== false,
      saveState: d.autosave === false ? "off" : "idle",
      saveMessage: "",
      savedAt: "",
      dirty: false,
      saving: false,
      rev: 0,
      recovery: null,
      issues: [],
      issuesLoaded: false,
      readinessFailed: false,
      issuesOpen: false,
      confirmOpen: false,
      confirmAction: "",
      submitting: false,

      init: function () {
        var self = this;
        this._cropQueue = [];
        this._readinessSeq = 0;
        this._retryDelay = 0;
        this.checkRecovery();
        if (this.selectedPlatforms.length) this.checkReadiness();

        window.addEventListener("online", function () {
          if (self.dirty && self.autosaveEnabled) self.autosave();
        });
        window.addEventListener("offline", function () {
          if (self.autosaveEnabled && self.dirty) self.saveState = "offline";
        });
        // Leaving with work the server doesn't have yet.
        window.addEventListener("beforeunload", function (e) {
          if (self.submitting) return;
          if (self.dirty || self.saving || self.uploadsPending()) {
            e.preventDefault();
            e.returnValue = "";
          }
        });
      },

      // Every edit lands here: by the form's input/change events, or called directly by
      // actions that change state without one (reorder, customize, AI pick).
      touched: function (e) {
        var self = this;
        // Fields that aren't part of the post (the AI brief, the place search).
        if (e && e.target && e.target.closest && e.target.closest("[data-not-post]")) return;
        this.rev++;
        this.dirty = true;
        this.syncTabs();
        if (this.autosaveEnabled && this.saveState !== "conflict") this.saveState = "dirty";
        clearTimeout(this._localTimer);
        this._localTimer = setTimeout(function () { self.writeLocal(); }, 400);
        clearTimeout(this._readyTimer);
        this._readyTimer = setTimeout(function () { self.checkReadiness(); }, READINESS_DELAY);
        if (this.autosaveEnabled && this.saveState !== "conflict") {
          clearTimeout(this._saveTimer);
          this._saveTimer = setTimeout(function () { self.autosave(); }, AUTOSAVE_DELAY);
        }
      },

      syncTabs: function () {
        var sel = this.selectedPlatforms;
        if (this.captionTab !== "all" && sel.indexOf(this.captionTab) === -1) this.captionTab = "all";
        if (sel.length && sel.indexOf(this.previewPlatform) === -1) this.previewPlatform = sel[0];
        var n = this.previewMedia().length;
        if (this.previewIndex >= n) this.previewIndex = Math.max(0, n - 1);
      },

      // ── Names and labels ──────────────────────────────────────────────────
      platformName: function (p) { return NAMES[p] || p; },
      actionVerb: function (a) { return VERBS[a] || "publish"; },
      fmt: function (n) { return Number(n || 0).toLocaleString(); },

      mediaTypeLabel: function () {
        var o = document.querySelector('#media-type option[value="' + this.mediaType + '"]');
        return o ? o.textContent.trim().toLowerCase() : this.mediaType;
      },

      igName: function () {
        var dest = (this.d.destinations || {}).instagram || {};
        return (dest.status === "connected" && dest.account_name ? dest.account_name : "your_account").replace(/^@/, "");
      },

      fbName: function () {
        var dest = (this.d.destinations || {}).facebook || {};
        return dest.status === "connected" && dest.account_name ? dest.account_name : "Your Page";
      },

      destinationAccount: function (p) {
        var dest = (this.d.destinations || {})[p] || {};
        if (dest.status === "connected") return dest.account_name || "Connected";
        return dest.status === "reconnect" ? "Reconnect needed" : "Not connected";
      },

      destinationSummary: function () {
        return this.selectedPlatforms.map(this.platformName).join(" · ") || "No destination yet";
      },

      timeLabel: function (p) {
        var value = this.perChannel && this.selectedPlatforms.length > 1 ? this.channelTimes[p] : this.scheduledAt;
        if (!value) return "No time set";
        // A wall-clock time in the workspace's timezone: format it without converting.
        var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value);
        if (!m) return value;
        var dt = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]));
        var text = dt.toLocaleString(undefined, {
          weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC",
        });
        return text + " (" + (this.d.tz_name || "") + ")";
      },

      // ── Caption: shared text and per-channel versions ────────────────────
      setCaptionTab: function (tab) {
        this.captionTab = tab;
        if (tab !== "all") this.previewPlatform = tab;
      },

      customizedList: function () {
        var self = this;
        return this.selectedPlatforms.filter(function (p) {
          return self.customized[p] && (self.platformCaptions[p] || "").trim();
        });
      },

      customize: function (p) {
        this.platformCaptions[p] = this.platformCaptions[p] || this.caption;
        this.customized[p] = true;
        this.touched();
        this.$nextTick(function () {
          var el = document.getElementById("caption-" + p);
          if (el) el.focus();
        });
      },

      resetOverride: function (p) {
        this.setUndo({
          type: "reset", platform: p, text: this.platformCaptions[p],
          message: this.platformName(p) + " uses the shared caption again.",
        });
        this.customized[p] = false;
        this.platformCaptions[p] = "";
        this.touched();
      },

      setUndo: function (undo) {
        var self = this;
        this.undo = undo;
        clearTimeout(this._undoTimer);
        this._undoTimer = setTimeout(function () { self.undo = null; }, 10000);
      },

      undoLast: function () {
        var u = this.undo;
        if (!u) return;
        if (u.type === "reset") {
          this.customized[u.platform] = true;
          this.platformCaptions[u.platform] = u.text;
        } else if (u.type === "ai") {
          this.caption = u.prev.caption;
          this.hashtagsText = u.prev.hashtagsText;
          if (u.prev.tab !== "all") this.platformCaptions[u.prev.tab] = u.prev.override;
        }
        this.undo = null;
        this.touched();
      },

      hashtags: function () {
        return this.hashtagsText.split(/\s+/).map(function (t) { return t.replace(/^#+/, "").trim(); }).filter(Boolean);
      },

      meterPlatforms: function () {
        return this.captionTab === "all" ? this.selectedPlatforms : [this.captionTab];
      },

      publishedLength: function (p) { return this._previewCaption(p).length; },

      countTone: function (p) {
        var max = this.limits.caption_max[p];
        var len = this.publishedLength(p);
        if (!max) return "";
        if (len > max) return "is-over";
        return len > max * (this.limits.near_limit || 0.9) ? "is-near" : "";
      },

      // The preview mirrors what is published: caption, then the CTA line, then hashtags.
      _previewBody: function (p) {
        var override = this.customized[p] ? (this.platformCaptions[p] || "").trim() : "";
        return override ? this.platformCaptions[p] : this.caption || "";
      },

      _previewTags: function () {
        return this.hashtags().map(function (h) { return "#" + h; }).join(" ");
      },

      // The CTA wording for a channel, or "" when the channel gets no CTA line.
      _ctaText: function (p) {
        var c = this.cta;
        if (c.kind === "none" || (c.kind === "whatsapp" && !c.channels[p])) return "";
        var text = (c.text || "").trim() || (c.kind === "link" ? this.defaults.link : this.defaults.whatsapp);
        return p === "instagram" ? text + (this.defaults.ig_suffix || "") : text;
      },

      // The tappable URL after the CTA wording (Facebook only).
      _ctaUrl: function (p) {
        if (p !== "facebook" || !this._ctaText(p)) return "";
        if (this.cta.kind === "link") return (this.cta.url || "").trim() || "https://…";
        return this.waLinkUrls.facebook || this.defaults.go_placeholder || "";
      },

      _ctaLine: function (p) {
        var text = this._ctaText(p);
        var url = this._ctaUrl(p);
        return text && url ? text + ": " + url : text;
      },

      _previewCaption: function (p) {
        return [this._previewBody(p), this._ctaLine(p), this._previewTags()].filter(Boolean).join("\n\n");
      },

      clip: function (p, text, n) {
        return this.expanded[p] || text.length <= n ? text : text.slice(0, n).trimEnd();
      },

      isClipped: function (p, text, n) { return !this.expanded[p] && text.length > n; },

      _hostname: function (url) {
        try { return new URL(url).hostname.replace(/^www\./, ""); } catch (e) { return ""; }
      },

      // ── Channel options (post_options.CAPABILITIES) ──────────────────────
      _can: function (p, option) {
        if (this.selectedPlatforms.indexOf(p) === -1) return false;
        return ((this.caps[p] || {})[option] || []).indexOf(this.mediaType) !== -1;
      },

      _anyOption: function (p) {
        var self = this;
        var platforms = p ? [p] : Object.keys(this.caps);
        return platforms.some(function (pl) {
          return Object.keys(self.caps[pl] || {}).some(function (o) { return self._can(pl, o); });
        });
      },

      _optionsSummary: function () {
        var ig = this.opts.instagram, fb = this.opts.facebook, set = [];
        if (this._can("instagram", "alt_text") && ig.alt_text.trim()) set.push("Alt text");
        if (this._can("instagram", "location") && ig.location) set.push(ig.location.name);
        var collabs = this._collaborators();
        if (this._can("instagram", "collaborators") && collabs.length) set.push(collabs.length + " collaborator" + (collabs.length > 1 ? "s" : ""));
        if (this._can("instagram", "cover_url") && (ig.cover_url || Number(ig.thumb_offset) > 0)) set.push("Cover");
        if (this._can("instagram", "share_to_feed") && !ig.share_to_feed) set.push("Reels tab only");
        if ((this._can("instagram", "first_comment") && ig.first_comment.trim()) ||
            (this._can("facebook", "first_comment") && fb.first_comment.trim())) set.push("First comment");
        return set.join(" · ");
      },

      _collaborators: function () {
        return (this.opts.instagram.collaborators || "").split(/[\s,]+/)
          .map(function (n) { return n.replace(/^@/, ""); }).filter(Boolean).slice(0, 3);
      },

      _firstComment: function (p) {
        return this._can(p, "first_comment") ? (this.opts[p].first_comment || "").trim() : "";
      },

      searchLocations: async function () {
        var q = this.loc.query.trim();
        this.loc.error = "";
        if (q.length < 2) { this.loc.results = []; this.loc.searched = false; return; }
        this.loc.loading = true;
        try {
          var r = await fetch(this.urls.locations + "?q=" + encodeURIComponent(q));
          var data = await r.json();
          if (q !== this.loc.query.trim()) return; // a newer search is under way
          this.loc.results = data.results || [];
          this.loc.error = data.error || "";
          this.loc.searched = true;
        } catch (e) {
          this.loc.error = "Couldn't search right now. Try again.";
        } finally {
          this.loc.loading = false;
        }
      },

      uploadCover: async function (event) {
        var file = event.target.files && event.target.files[0];
        event.target.value = "";
        if (!file) return;
        this.coverError = "";
        this.coverUploading = true;
        var fd = new FormData();
        fd.append("file", file);
        fd.append("csrfmiddlewaretoken", csrf());
        try {
          var r = await fetch(this.urls.upload, { method: "POST", body: fd });
          var data = await r.json();
          if (!r.ok || data.error) throw new Error(data.error || "Upload failed.");
          this.opts.instagram.cover_url = data.url;
          this.touched();
        } catch (e) {
          this.coverError = e.message || "Upload failed. Try again.";
        } finally {
          this.coverUploading = false;
        }
      },

      // ── Media ─────────────────────────────────────────────────────────────
      mediaUrls: function () {
        return this.media.filter(function (m) { return m.status === "done" && m.url; }).map(function (m) { return m.url; });
      },

      previewMedia: function () {
        return this.media.filter(function (m) { return m.status === "done" && m.url; });
      },

      uploadsPending: function () {
        return this.media.some(function (m) { return m.status === "uploading"; });
      },

      mediaHint: function () {
        return {
          image: "One image. Instagram takes anything from 4:5 portrait to 1.91:1 landscape.",
          carousel: "2 to 10 images, published in the order shown. Drag them, or use the arrows, to reorder.",
          video: "One video.",
          reel: "One vertical video. 9:16 fills the screen.",
          story: "One image or video. 9:16 fills the screen.",
          text: "Text posts have no media (Facebook only).",
        }[this.mediaType] || "";
      },

      announce: function (text) { this.mediaAnnouncement = text; },

      onDropFiles: function (e) {
        this.dropActive = false;
        if (this.dragIndex !== null) return; // a tile was dropped between tiles
        var files = e.dataTransfer && e.dataTransfer.files;
        if (files && files.length) this.onFiles(files);
      },

      onFiles: function (fileList) {
        var self = this;
        Array.prototype.forEach.call(fileList, function (file) {
          if (file.type.indexOf("image/") === 0 && file.type !== "image/gif") self._cropQueue.push(file);
          else self.uploadFile(file);
        });
        this.nextCrop();
      },

      // Images are cropped one at a time; each opens the cropper after the last closes.
      nextCrop: function () {
        var self = this;
        if (this._cropping || !this._cropQueue.length) return;
        var file = this._cropQueue.shift();
        this._cropping = true;
        var src = URL.createObjectURL(file);
        window._igCropOpen(src, { ratio: this.cropRatio() }, function (blob) {
          URL.revokeObjectURL(src);
          self.uploadFile(blob ? new File([blob], file.name.replace(/\.\w+$/, "") + ".jpg", { type: "image/jpeg" }) : file);
          self._cropping = false;
          self.nextCrop();
        });
      },

      cropRatio: function () {
        if (this.mediaType === "story") return 9 / 16;
        return this.selectedPlatforms.indexOf("instagram") !== -1 ? 1 : NaN;
      },

      recrop: function (i) {
        var self = this;
        var item = this.media[i];
        if (!item || item.kind !== "image") return;
        window._igCropOpen(item.url, { ratio: this.cropRatio(), crossOrigin: true }, function (blob) {
          if (!blob) return;
          var name = (item.name || "image").replace(/\.\w+$/, "") + ".jpg";
          self.uploadFile(new File([blob], name, { type: "image/jpeg" }), item.id);
        });
      },

      // Upload one file. With replaceId, the new file takes that tile's place in the order.
      uploadFile: function (file, replaceId) {
        var self = this;
        var fields = {
          name: file.name, thumb: file.type.indexOf("image/") === 0 ? URL.createObjectURL(file) : "",
          kind: kindOf("", file.type), status: "uploading", progress: 0, error: "", file: file,
        };
        var item;
        if (replaceId) {
          item = this.media.find(function (m) { return m.id === replaceId; });
          Object.assign(item, fields, { url: "" });
        } else {
          this.media.push(mediaItem("", fields));
          item = this.media[this.media.length - 1];
        }
        var id = item.id;
        var find = function () { return self.media.find(function (m) { return m.id === id; }); };

        var fd = new FormData();
        fd.append("file", file);
        fd.append("csrfmiddlewaretoken", csrf());
        var xhr = new XMLHttpRequest();
        xhr.upload.onprogress = function (e) {
          var m = find();
          if (m && e.lengthComputable) m.progress = Math.round((e.loaded / e.total) * 100);
        };
        xhr.onload = function () {
          var m = find();
          if (!m) return; // removed while uploading
          var data = {};
          try { data = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
          if (xhr.status === 200 && data.url) {
            Object.assign(m, { url: data.url, status: "done", progress: 100, file: null });
            self.announce(m.name + " uploaded.");
            self.touched();
          } else {
            Object.assign(m, { status: "error", error: data.error || "Upload failed (" + xhr.status + ")." });
          }
        };
        xhr.onerror = function () {
          var m = find();
          if (m) Object.assign(m, { status: "error", error: "Network error. Retry when you're back online." });
        };
        xhr.open("POST", this.urls.upload);
        xhr.send(fd);
      },

      retryUpload: function (item) {
        if (item.file) this.uploadFile(item.file, item.id);
      },

      removeMedia: function (i) {
        var item = this.media[i];
        this.media.splice(i, 1);
        this.announce((item ? item.name : "File") + " removed.");
        this.touched();
      },

      moveMedia: function (i, delta) {
        var j = i + delta;
        if (j < 0 || j >= this.media.length) return;
        var item = this.media.splice(i, 1)[0];
        this.media.splice(j, 0, item);
        this.announce("Moved to position " + (j + 1) + " of " + this.media.length + ".");
        this.touched();
        // Keep focus on the moved tile's arrow so keyboard users can keep moving it.
        this.$nextTick(function () {
          var tile = document.querySelectorAll(".cmp-tile")[j];
          if (!tile) return;
          var btns = tile.querySelectorAll(".cmp-tile-tools button");
          var btn = btns[delta < 0 ? 0 : 1];
          if (btn && btn.disabled) btn = btns[delta < 0 ? 1 : 0];
          if (btn) btn.focus();
        });
      },

      dropOn: function (i) {
        var from = this.dragIndex;
        this.dragIndex = null;
        this.dragOver = null;
        if (from === null || from === i) return;
        var item = this.media.splice(from, 1)[0];
        this.media.splice(i, 0, item);
        this.announce("Moved to position " + (i + 1) + " of " + this.media.length + ".");
        this.touched();
      },

      // ── Preview ───────────────────────────────────────────────────────────
      previewSlides: function () {
        return this.mediaType === "carousel" && this.previewPlatform === "instagram" ? this.previewMedia().length : 1;
      },

      currentPreview: function () {
        var items = this.previewMedia();
        if (!items.length) return null;
        return this.mediaType === "carousel" ? items[Math.min(this.previewIndex, items.length - 1)] : items[0];
      },

      // Instagram frames a feed post (and every carousel slide) by its first image's ratio.
      noteRatio: function (img) {
        var first = this.previewMedia()[0];
        if (!first || img.getAttribute("src") !== first.url || !img.naturalHeight) return;
        this.igRatio = Math.min(1.91, Math.max(0.8, img.naturalWidth / img.naturalHeight));
      },

      igAspect: function () {
        if (["story", "reel", "video"].indexOf(this.mediaType) !== -1) return "9 / 16";
        return this.previewMedia().length ? String(this.igRatio) : "1";
      },

      // ── AI captions ───────────────────────────────────────────────────────
      briefPlatform: function () {
        return this.captionTab !== "all" ? this.captionTab : this.selectedPlatforms[0] || "instagram";
      },

      generateCaptions: async function () {
        var self = this;
        if (!this.brief.topic.trim() || this.generating) return;
        this.generating = true;
        this.genError = "";
        this.captionOptions = [];
        var fd = new FormData();
        fd.append("prompt", this.brief.topic);
        fd.append("media_type", this.mediaType);
        fd.append("platform", this.briefPlatform());
        fd.append("goal", this.brief.goal);
        fd.append("cta", this.brief.cta || "");
        fd.append("csrfmiddlewaretoken", csrf());
        var data;
        try {
          var res = await fetch(this.urls.caption_generate, { method: "POST", body: fd });
          data = await res.json();
        } catch (e) {
          this.genError = "Network error. Please try again.";
          this.generating = false;
          return;
        }
        if (data.error) {
          this.genError = data.error;
          this.generating = false;
          return;
        }
        var attempts = 0;
        var poll = setInterval(async function () {
          attempts++;
          if (attempts > 40) {
            clearInterval(poll);
            self.genError = "That took too long. Please try again.";
            self.generating = false;
            return;
          }
          var result;
          try {
            var r = await fetch(self.urls.caption_result.replace("PLACEHOLDER", data.draft_id));
            result = await r.json();
          } catch (e) {
            return; // a network blip: keep polling
          }
          if (result.status === "ready") {
            clearInterval(poll);
            self.captionOptions = result.options && result.options.length
              ? result.options : [{ caption: result.caption, hashtags: result.hashtags || [] }];
            self.generating = false;
          } else if (result.status === "failed") {
            clearInterval(poll);
            self.genError = result.error || "That didn't work. Please try again.";
            self.generating = false;
          }
        }, 1500);
      },

      // Applied only on an explicit pick, and undoable.
      selectOption: function (opt) {
        var tab = this.captionTab;
        var intoOverride = tab !== "all" && this.customized[tab];
        this.setUndo({
          type: "ai", message: "Caption replaced with the suggestion.",
          prev: { caption: this.caption, hashtagsText: this.hashtagsText, tab: intoOverride ? tab : "all", override: intoOverride ? this.platformCaptions[tab] : "" },
        });
        if (intoOverride) this.platformCaptions[tab] = opt.caption;
        else this.caption = opt.caption;
        if (opt.hashtags && opt.hashtags.length) {
          this.hashtagsText = opt.hashtags.map(function (h) { return "#" + String(h).replace(/^#+/, ""); }).join(" ");
        }
        this.touched();
      },

      // ── Readiness ─────────────────────────────────────────────────────────
      primaryAction: function () {
        if (this.reviewGate) return "submit_review";
        return this.publishMode === "schedule" ? "schedule" : "publish_now";
      },

      blocking: function (action) {
        return this.issues.filter(function (i) { return i.blocks.indexOf(action) !== -1; });
      },

      blockedFor: function (action) {
        return !this.selectedPlatforms.length || this.blocking(action).length > 0;
      },

      visibleIssues: function () {
        var a = this.primaryAction();
        var list = this.issues.filter(function (i) { return i.blocks.indexOf(a) !== -1 || i.warns.indexOf(a) !== -1; });
        return list.sort(function (x, y) { return (y.blocks.indexOf(a) !== -1) - (x.blocks.indexOf(a) !== -1); });
      },

      readyTone: function () {
        var a = this.primaryAction();
        if (!this.selectedPlatforms.length || this.blocking(a).length) return "is-bad";
        return this.visibleIssues().length || this.readinessFailed ? "is-warn" : "is-ok";
      },

      readyIcon: function () { return this.readyTone() === "is-ok" ? "✓" : "!"; },

      readyText: function () {
        var a = this.primaryAction();
        if (!this.selectedPlatforms.length) return "Choose where to publish";
        if (this.readinessFailed) return "Couldn't check. We check again when you " + this.actionVerb(a) + ".";
        if (!this.issuesLoaded) return "Checking…";
        var n = this.blocking(a).length;
        if (n) return n + (n === 1 ? " thing" : " things") + " to fix";
        var w = this.visibleIssues().length;
        return "Ready to " + this.actionVerb(a) + (w ? " · " + w + (w === 1 ? " note" : " notes") : "");
      },

      checkReadiness: async function () {
        var seq = ++this._readinessSeq;
        await this.$nextTick();
        var fd = new FormData(formEl());
        fd.delete("action");
        try {
          var r = await fetch(this.urls.readiness, { method: "POST", body: fd });
          if (!r.ok) throw new Error(String(r.status));
          var data = await r.json();
          if (seq !== this._readinessSeq) return; // a newer check is on its way
          this.issues = data.issues || [];
          this.issuesLoaded = true;
          this.readinessFailed = false;
        } catch (e) {
          if (seq === this._readinessSeq) this.readinessFailed = true;
        }
      },

      goToIssue: function (issue) {
        var f = issue.field || "";
        var id = {
          destinations: "dest-first", caption: "caption-input", hashtags: "hashtags-input",
          media: "media-add", media_type: "media-type", cta: "composer-cta",
          options: "more-options-toggle", schedule: "scheduled-at",
        }[f];
        this.issuesOpen = false;
        this.mobileView = "edit";
        if (f === "caption") this.setCaptionTab("all");
        if (f.indexOf("caption_") === 0) {
          var p = f.slice(8);
          this.setCaptionTab(p);
          id = "caption-" + p;
        }
        if (f === "options") this.moreOpen = true;
        if (f === "schedule" || f.indexOf("scheduled_at_") === 0) this.publishMode = "schedule";
        if (f.indexOf("scheduled_at_") === 0) {
          this.perChannel = true;
          id = "scheduled-at-" + f.slice(13);
        }
        this.$nextTick(function () {
          var el = document.getElementById(id);
          if (!el) return;
          el.scrollIntoView({ block: "center", behavior: "smooth" });
          el.focus({ preventScroll: true });
        });
      },

      // ── Autosave and recovery ─────────────────────────────────────────────
      saveText: function () {
        switch (this.saveState) {
          case "dirty": return "Unsaved changes";
          case "saving": return "Saving…";
          case "saved": return "Saved " + this.savedAt;
          case "error": return "Couldn't save" + (this.saveMessage ? ": " + this.saveMessage : "");
          case "offline": return "Offline. Your changes are kept in this browser.";
          case "conflict": return "Not saved: changed elsewhere";
          case "off": return this.dirty ? "Unsaved changes. Press a button to save." : "Changes save when you press a button";
          default: return this.saveMessage || "Drafts save automatically";
        }
      },

      // A new post's edits are kept under "new" until its first save gives it an id.
      localKey: function () {
        return "akilent:composer:" + (this.postId ? "post:" + this.postId : "new");
      },

      snapshot: function () {
        return {
          caption: this.caption, platformCaptions: this.platformCaptions, customized: this.customized,
          hashtagsText: this.hashtagsText, selectedPlatforms: this.selectedPlatforms, mediaType: this.mediaType,
          media: this.mediaUrls(), cta: this.cta, opts: this.opts, publishMode: this.publishMode,
          scheduledAt: this.scheduledAt, perChannel: this.perChannel, channelTimes: this.channelTimes,
        };
      },

      writeLocal: function () {
        if (!this.dirty) return;
        storeSet(this.localKey(), {
          t: Date.now(), version: this.version, postId: this.postId, clientKey: this.clientKey,
          state: JSON.parse(JSON.stringify(this.snapshot())),
        });
      },

      clearLocal: function () {
        clearTimeout(this._localTimer);
        storeDel(this.localKey());
        if (this.postId) storeDel("akilent:composer:post:" + this.postId);
      },

      checkRecovery: function () {
        var s = storeGet(this.localKey());
        if (!s || !s.state || Date.now() - s.t > RECOVERY_MAX_AGE) return;
        var when = new Date(s.t).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
        this.recovery = { when: when, data: s };
      },

      restoreRecovery: function () {
        var s = this.recovery && this.recovery.data;
        this.recovery = null;
        if (!s) return;
        var st = s.state;
        this.caption = st.caption || "";
        Object.assign(this.platformCaptions, st.platformCaptions || {});
        Object.assign(this.customized, st.customized || {});
        this.hashtagsText = st.hashtagsText || "";
        this.selectedPlatforms = st.selectedPlatforms || this.selectedPlatforms;
        this.mediaType = st.mediaType || this.mediaType;
        this.media = (st.media || []).map(function (u) { return mediaItem(u); });
        Object.assign(this.cta, st.cta || {});
        if (st.opts) {
          Object.assign(this.opts.instagram, st.opts.instagram || {});
          Object.assign(this.opts.facebook, st.opts.facebook || {});
        }
        if (!this.reviewGate) this.publishMode = st.publishMode || this.publishMode;
        this.scheduledAt = st.scheduledAt || "";
        this.perChannel = !!st.perChannel;
        Object.assign(this.channelTimes, st.channelTimes || {});
        // A new post's earlier autosaves went to its own row: keep saving there.
        if (!this.d.post_id) {
          if (s.clientKey) this.clientKey = s.clientKey;
          if (s.postId) this.postId = s.postId;
        }
        this.touched();
      },

      discardRecovery: function () {
        this.recovery = null;
        storeDel(this.localKey());
      },

      reloadServerCopy: function () {
        this.clearLocal();
        this.dirty = false;
        window.location.href = this.urls.edit + "?post_id=" + this.postId;
      },

      autosave: async function (force) {
        var self = this;
        if (!this.autosaveEnabled) return;
        clearTimeout(this._saveTimer);
        if (this.uploadsPending()) {
          this._saveTimer = setTimeout(function () { self.autosave(force); }, AUTOSAVE_DELAY);
          return;
        }
        if (this.saving) { this._again = true; return; }
        if (!navigator.onLine) { this.saveState = "offline"; return; }
        await this.$nextTick();

        var fd = new FormData(formEl());
        fd.delete("action");
        fd.set("version", this.version || "");
        if (force) fd.set("force", "1");
        var startedRev = this.rev;
        this.saving = true;
        this.saveState = "saving";
        var r, data = {};
        try {
          r = await fetch(this.urls.autosave, { method: "POST", body: fd, headers: { "X-Requested-With": "XMLHttpRequest" } });
          try { data = await r.json(); } catch (e) { data = {}; }
        } catch (e) {
          this.saving = false;
          this.saveState = navigator.onLine ? "error" : "offline";
          this.saveMessage = navigator.onLine ? "the server didn't answer" : "";
          this.retrySoon();
          return;
        }
        this.saving = false;

        if (r.ok && data.saved) {
          this.postId = data.post_id;
          this.version = data.version;
          // The viewer's own clock: the workspace timezone may differ from where they sit.
          this.savedAt = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
          this.saveMessage = "";
          this._retryDelay = 0;
          if (data.wa_link_urls) this.waLinkUrls = data.wa_link_urls;
          if (this.statusLabel === "New") this.statusLabel = "Draft";
          // A refresh now reopens this draft rather than an empty composer.
          if (!/[?&]post_id=/.test(window.location.search)) {
            try { window.history.replaceState(null, "", this.urls.edit + "?post_id=" + data.post_id); } catch (e) { /* ignore */ }
          }
          if (this.rev === startedRev) {
            this.dirty = false;
            this.saveState = "saved";
            storeDel("akilent:composer:new");
            this.clearLocal();
          } else {
            this.saveState = "dirty"; // edited while saving: save again
            this._saveTimer = setTimeout(function () { self.autosave(); }, AUTOSAVE_DELAY);
          }
        } else if (r.status === 409 && data.reason === "not_draft") {
          this.autosaveEnabled = false;
          this.saveState = "off";
          this.saveMessage = data.message || "";
        } else if (r.status === 409) {
          this.saveState = "conflict";
        } else if (r.ok) {
          this.saveState = "idle";
          this.saveMessage = data.reason === "no_destination"
            ? "Choose where to publish and the draft saves itself"
            : "Start writing and the draft saves itself";
        } else if (r.status === 422) {
          this.saveState = "error";
          this.saveMessage = data.message || "a field needs fixing";
        } else if (r.status === 403) {
          this.saveState = "error";
          this.saveMessage = "your session expired. Sign in again in another tab, then retry";
        } else {
          this.saveState = "error";
          this.saveMessage = "the server had a problem";
          this.retrySoon();
        }
        if (this._again) { this._again = false; this.autosave(); }
      },

      // Back off 5s, 15s, 30s, then every 60s while saving keeps failing.
      retrySoon: function () {
        var self = this;
        this._retryDelay = this._retryDelay ? Math.min(this._retryDelay * 2, 60000) : 5000;
        clearTimeout(this._saveTimer);
        this._saveTimer = setTimeout(function () { self.autosave(); }, this._retryDelay);
      },

      // ── Submitting ────────────────────────────────────────────────────────
      onSubmit: function (e) {
        var btn = e.submitter;
        if (!btn || !btn.value || this.submitting) return;
        var action = btn.value;
        if (this.uploadsPending()) {
          if (window.toast) window.toast("warning", "Wait for the uploads to finish first.");
          return;
        }
        if (action === "schedule" || action === "publish_now") {
          this.confirmAction = action;
          this.confirmOpen = true;
          return;
        }
        this.submitWith(action);
      },

      submitWith: function (action) {
        var self = this;
        if (this.submitting) return;
        this.submitting = true;
        this.confirmOpen = false;
        clearTimeout(this._saveTimer);
        formEl().querySelector("[name=action]").value = action;
        this.clearLocal();
        this.$nextTick(function () { formEl().submit(); });
      },
    };
  };

  // ── Cropper modal (outside Alpine so Cropper.js owns its <img>) ─────────────
  var cropper = null;
  var cropCb = null;
  var returnFocus = null;

  function modal() { return document.getElementById("ig-crop-modal"); }

  function closeCrop(result) {
    var m = modal();
    if (!m) return;
    m.classList.add("hidden");
    if (cropper) { cropper.destroy(); cropper = null; }
    var cb = cropCb;
    cropCb = null;
    if (cb) cb(result);
    var back = returnFocus && document.contains(returnFocus) ? returnFocus : document.getElementById("media-add");
    if (back && back.focus) back.focus();
    returnFocus = null;
  }

  // opts.ratio: starting aspect ratio (NaN = free). opts.crossOrigin: the image is
  // already uploaded, so it must be loaded with CORS for the crop to be readable.
  window._igCropOpen = function (src, opts, callback) {
    opts = opts || {};
    cropCb = callback;
    returnFocus = document.activeElement;
    var m = modal();
    var img = document.getElementById("ig-crop-img");
    var err = document.getElementById("ig-crop-error");
    err.hidden = true;
    var igSelected = !!document.querySelector("[name=platforms][value=instagram]:checked");
    document.getElementById("ig-crop-title").textContent = igSelected ? "Crop for Instagram" : "Crop image";
    img.removeAttribute("crossorigin");
    if (opts.crossOrigin) img.crossOrigin = "anonymous";
    img.onload = function () {
      if (cropper) { cropper.destroy(); cropper = null; }
      cropper = new window.Cropper(img, {
        aspectRatio: opts.ratio === undefined ? 1 : opts.ratio, viewMode: 1, autoCropArea: 0.9, background: false,
      });
    };
    img.onerror = function () {
      err.textContent = "This image couldn't be opened for cropping.";
      err.hidden = false;
    };
    img.src = src;
    m.classList.remove("hidden");
    document.getElementById("ig-crop-panel").focus();
  };

  document.addEventListener("DOMContentLoaded", function () {
    var m = modal();
    if (!m) return;

    m.addEventListener("click", function (e) {
      if (e.target === m) { closeCrop(null); return; }
      var btn = e.target.closest("[data-ratio]");
      if (btn && cropper) {
        var r = btn.getAttribute("data-ratio");
        if (r === "free") { cropper.setAspectRatio(NaN); return; }
        var parts = r.split("/");
        cropper.setAspectRatio(Number(parts[0]) / Number(parts[1]));
      }
    });

    // Esc skips the crop (uploads the original), the same as the Skip button.
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !m.classList.contains("hidden")) {
        e.preventDefault();
        closeCrop(null);
      }
    });

    document.getElementById("ig-crop-skip").addEventListener("click", function () { closeCrop(null); });

    document.getElementById("ig-crop-apply").addEventListener("click", function () {
      if (!cropper) return;
      try {
        cropper.getCroppedCanvas({ maxWidth: 1440, maxHeight: 1800 }).toBlob(function (blob) { closeCrop(blob); }, "image/jpeg", 0.92);
      } catch (e) {
        // A stored image served without CORS can't be read back from the canvas.
        var err = document.getElementById("ig-crop-error");
        err.textContent = "This image can't be cropped here. Upload the file again to crop it.";
        err.hidden = false;
      }
    });
  });
})();
