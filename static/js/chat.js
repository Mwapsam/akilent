/* Live conversation thread: renders messages from a JSON feed, polls for new
 * ones, and sends replies without a page reload. Config comes from a
 * json_script block: { messages, lastId, feedUrl, open }.
 *
 * Loaded with the app shell (base.html), not by the page: a page swapped in place
 * would otherwise render x-data="chat(...)" before its own script had loaded. The
 * component stops polling in destroy(), which Alpine calls when the page is swapped away. */
(function () {
function registerChat() {
  const STATUS_LABELS = { queued: 'Sending…', sent: 'Sent', delivered: 'Delivered', read: 'Read', failed: 'Failed', held: 'Held (plan limit)', unconfirmed: 'Not confirmed' };
  const BASE_INTERVAL = 3000;
  const MAX_INTERVAL = 30000;

  const dayKey = (d) => d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate();

  Alpine.data('chat', (cfg) => ({
    messages: cfg.messages || [],
    lastId: cfg.lastId || 0,
    open: cfg.open,
    windowOpen: cfg.windowOpen !== false,
    statusHtml: cfg.statusHtml || '',
    infoOpen: false,
    draft: '',
    sending: false,
    newBelow: false,
    interval: BASE_INTERVAL,
    timer: null,
    coarse: window.matchMedia('(pointer: coarse)').matches,
    ai: Object.assign({ enabled: false, proposal: null }, cfg.ai || {}),
    sendMediaUrl: cfg.sendMediaUrl || '',
    // Voice note recording (MediaRecorder): idle → recording → sent or cancelled.
    recording: false,
    recSeconds: 0,
    _recorder: null,
    _recTimer: null,
    _recChunks: [],
    _recCancelled: false,
    canRecord: !!(navigator.mediaDevices && window.MediaRecorder),
    aiUsedId: null,

    // "[Photo]" / "[Voice message]" placeholders say nothing once the media itself shows.
    showText(m) {
      if (!m.body) return !m.media;
      if (!m.media || m.media.state !== 'ready') return true;
      return !/^(\[[^\]]+\]\s*)+$/.test(m.body.trim());
    },

    init() {
      this.$nextTick(() => this.scrollBottom(true));
      // --cv-top defaults to a fixed 7rem, which only holds when the app shell's topbar is the
      // only thing above .cv. Embedded in the two-pane inbox, the inbox header/filters push it
      // down further, so measure the real offset instead of assuming one — this is what keeps
      // the page itself from scrolling (the thread scrolls internally regardless of placement).
      this._fit = () => {
        const main = document.getElementById('main');
        const pad = main ? parseFloat(getComputedStyle(main).paddingBottom) || 0 : 0;
        const top = this.$el.getBoundingClientRect().top + window.scrollY;
        this.$el.style.setProperty('--cv-top', `${Math.round(top + pad)}px`);
      };
      requestAnimationFrame(this._fit);
      window.addEventListener('resize', this._fit, { passive: true });
      this._onVisible = () => {
        if (!document.hidden) { this.interval = BASE_INTERVAL; this.schedule(0); }
      };
      document.addEventListener('visibilitychange', this._onVisible);
      // static/js/vendor/htmx-ext-sse.js turns a "message.created" server event into a
      // same-named DOM CustomEvent wherever an hx-trigger="sse:message.created" element exists
      // (see the marker in templates/conversations/_conversation_pane.html); its `detail` is the
      // raw SSE MessageEvent, so `detail.data` is the JSON string apps/core/realtime.py sent.
      // Ignored entirely when REALTIME_SSE_ENABLED is off — nothing ever fires this event then,
      // and the existing poll (above) is what keeps the thread current either way.
      this._onSseMessage = (e) => {
        try {
          const ids = JSON.parse(e.detail.data);
          if (ids.conversation_id === cfg.id) { this.interval = BASE_INTERVAL; this.schedule(0); }
        } catch (err) { /* not our conversation's event, or not JSON — ignore */ }
      };
      document.addEventListener('sse:message.created', this._onSseMessage);
      this.schedule();
    },

    destroy() {
      this._destroyed = true;
      // Leaving mid-recording must release the microphone, not send a half note.
      this.stopRecording(false);
      clearTimeout(this.timer);
      window.removeEventListener('resize', this._fit);
      document.removeEventListener('visibilitychange', this._onVisible);
      document.removeEventListener('sse:message.created', this._onSseMessage);
    },

    // ── rendering ────────────────────────────────────────────────
    get items() {
      const out = [];
      let lastDay = null;
      for (const m of this.messages) {
        const d = new Date(m.ts);
        const key = dayKey(d);
        if (key !== lastDay) {
          out.push({ key: 'day-' + key, type: 'day', label: this.dayLabel(d) });
          lastDay = key;
        }
        out.push({ key: 'm-' + m.id, type: 'msg', m, showMeta: true, showFailure: m.status === 'failed' || m.status === 'held' || m.status === 'unconfirmed' });
      }
      // Only the last message of a same-direction, same-minute run shows its meta line.
      for (let i = 0; i < out.length - 1; i++) {
        const a = out[i], b = out[i + 1];
        if (a.type === 'msg' && b.type === 'msg' && a.m.direction === b.m.direction
            && this.minute(a.m.ts) === this.minute(b.m.ts)) a.showMeta = false;
      }
      return out;
    },
    minute(ts) { return Math.floor(new Date(ts).getTime() / 60000); },
    dayLabel(d) {
      const today = new Date();
      const yest = new Date(); yest.setDate(today.getDate() - 1);
      if (dayKey(d) === dayKey(today)) return 'Today';
      if (dayKey(d) === dayKey(yest)) return 'Yesterday';
      return d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric', year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
    },
    time(ts) { return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); },
    meta(m) {
      if (m.direction !== 'outbound') return this.time(m.ts);
      const label = m.pending ? 'Sending…' : (STATUS_LABELS[m.status] || '');
      return this.time(m.ts) + (label ? ' · ' + label : '');
    },
    failureText(m) {
      // A plain-language reason from apps.whatsapp.friendly_errors, translated
      // server-side at projection time (see docs/plans R0). Falls back if a
      // message somehow has no reason recorded.
      return m.failureReason || 'This message could not be delivered.';
    },

    // ── scrolling ────────────────────────────────────────────────
    get thread() { return this.$refs.thread; },
    nearBottom() {
      const t = this.thread;
      return t.scrollHeight - t.scrollTop - t.clientHeight < 120;
    },
    scrollBottom(instant) {
      const t = this.thread;
      t.scrollTo({ top: t.scrollHeight, behavior: instant ? 'auto' : 'smooth' });
      this.newBelow = false;
    },
    onScroll() { if (this.nearBottom()) this.newBelow = false; },

    // ── polling ──────────────────────────────────────────────────
    schedule(delay) {
      clearTimeout(this.timer);
      if (this._destroyed) return;
      this.timer = setTimeout(() => this.poll(), delay === undefined ? this.interval : delay);
    },
    async poll() {
      if (document.hidden) return; // resumed on visibilitychange
      try {
        const r = await fetch(cfg.feedUrl + '?after=' + this.lastId, {
          credentials: 'same-origin',
          headers: { 'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json' },
        });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        this.apply(await r.json());
        this.interval = BASE_INTERVAL;
      } catch (e) {
        this.interval = Math.min(this.interval * 2, MAX_INTERVAL);
      } finally {
        this.schedule();
      }
    },
    apply(data) {
      const stick = this.nearBottom();
      const known = new Set(this.messages.map((m) => m.id));
      let inbound = false;
      for (const raw of data.messages) {
        if (known.has(raw.id)) continue;
        if (raw.direction === 'outbound') {
          // The real message replaces its optimistic placeholder.
          const i = this.messages.findIndex((m) => m.pending && m.body === raw.body);
          if (i !== -1) this.messages.splice(i, 1);
        } else {
          inbound = true;
        }
        this.messages.push({
          id: raw.id, direction: raw.direction, body: raw.body, ts: raw.timestamp,
          status: raw.status, failureReason: raw.failureReason || null,
          byAi: !!raw.byAi, media: raw.media || null,
        });
        this.lastId = Math.max(this.lastId, raw.id);
      }
      for (const m of this.messages) {
        // A photo or voice note that was still downloading: swap in its current state.
        const media = data.media && data.media[String(m.id)];
        if (media && (!m.media || m.media.state !== media.state)) m.media = media;
        const s = data.statuses[String(m.id)];
        if (s !== undefined && s !== m.status) m.status = s;
        const reason = data.failureReasons && data.failureReasons[String(m.id)];
        if (reason) m.failureReason = reason;
      }
      this.messages.sort((a, b) => (a.pending ? 1 : 0) - (b.pending ? 1 : 0) || new Date(a.ts) - new Date(b.ts));
      if (data.status_html !== undefined) this.statusHtml = data.status_html;
      this.open = data.open;
      if (data.windowOpen !== undefined) this.windowOpen = data.windowOpen;
      if (this.ai.enabled && data.aiProposal !== undefined) this.ai.proposal = data.aiProposal;
      if (data.messages.length) {
        this.$nextTick(() => {
          if (stick) this.scrollBottom();
          else if (inbound) this.newBelow = true;
        });
      }
    },

    // ── AI proposals (a person always reviews and sends) ─────────
    async aiPost(url, extra) {
      const fd = new FormData();
      fd.set('csrfmiddlewaretoken', this.$refs.composer.querySelector('[name=csrfmiddlewaretoken]').value);
      for (const [k, v] of Object.entries(extra || {})) fd.set(k, v);
      const r = await fetch(url, {
        method: 'POST', body: fd, credentials: 'same-origin',
        headers: { 'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json' },
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.ok) throw new Error(data.error || 'Something went wrong.');
      return data;
    },
    async aiSuggest() {
      try {
        const data = await this.aiPost(this.ai.suggestUrl);
        this.ai.proposal = data.proposal;
        this.interval = BASE_INTERVAL;
        this.schedule(3500);
      } catch (e) { if (window.toast) window.toast('danger', e.message); }
    },
    async aiDismiss() {
      const p = this.ai.proposal;
      if (!p) return;
      this.ai.proposal = Object.assign({}, p, { status: 'dismissed' });
      try { await this.aiPost(this.ai.dismissUrl, { proposal: p.id }); } catch (e) { /* the card is gone either way */ }
    },
    async aiApply(extra) {
      const p = this.ai.proposal;
      if (!p || !extra || extra.applied) return;
      extra.applied = true;  // optimistic: the chip shows ✓ at once
      try {
        const data = await this.aiPost(this.ai.applyUrl, { proposal: p.id, index: extra.index });
        if (data.proposal) this.ai.proposal = data.proposal;
        if (window.toast) window.toast('success', data.message);
      } catch (e) {
        extra.applied = false;
        if (window.toast) window.toast('danger', e.message);
      }
    },
    aiUseReply() {
      const p = this.ai.proposal;
      if (!p) return;
      this.draft = p.text || '';
      this.aiUsedId = p.id;
      this.$nextTick(() => { this.grow(this.$refs.input); this.$refs.input.focus(); });
      if (window.toast) window.toast('info', 'AI draft added. Check it before sending.');
    },
    aiReviewTemplate() {
      const p = this.ai.proposal;
      if (!p || !p.templateId) return;
      this.aiUsedId = p.id;
      if (this.$refs.tplPicker) this.$refs.tplPicker.open = true;
      window.dispatchEvent(new CustomEvent('ai-template-apply', { detail: { templateId: p.templateId, values: p.values } }));
    },

    // ── media: files and voice notes ─────────────────────────────
    pickFile() { this.$refs.mediaInput.click(); },
    onFile(e) {
      const file = e.target.files && e.target.files[0];
      e.target.value = '';
      if (file) this.uploadMedia(file, file.name, false);
    },
    async startRecording() {
      if (this.recording || this.sending) return;
      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch (err) {
        if (window.toast) window.toast('danger', 'Allow microphone access to record a voice note.');
        return;
      }
      const type = ['audio/ogg;codecs=opus', 'audio/webm;codecs=opus', 'audio/mp4']
        .find((t) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t));
      const rec = type ? new MediaRecorder(stream, { mimeType: type }) : new MediaRecorder(stream);
      this._recChunks = [];
      this._recCancelled = false;
      rec.ondataavailable = (ev) => { if (ev.data && ev.data.size) this._recChunks.push(ev.data); };
      rec.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        clearInterval(this._recTimer);
        this.recording = false;
        if (this._recCancelled || !this._recChunks.length) return;
        const mime = (rec.mimeType || 'audio/webm').split(';')[0];
        const ext = mime.includes('ogg') ? 'ogg' : mime.includes('mp4') ? 'm4a' : 'webm';
        const blob = new Blob(this._recChunks, { type: mime });
        this.uploadMedia(blob, 'voice-note.' + ext, true);
      };
      this._recorder = rec;
      this.recSeconds = 0;
      this.recording = true;
      this._recTimer = setInterval(() => {
        this.recSeconds += 1;
        if (this.recSeconds >= 300) this.stopRecording(true); // 5-minute cap
      }, 1000);
      rec.start();
    },
    stopRecording(send) {
      if (!this._recorder || this._recorder.state === 'inactive') return;
      this._recCancelled = !send;
      this._recorder.stop();
    },
    recLabel() {
      const m = Math.floor(this.recSeconds / 60);
      const s = String(this.recSeconds % 60).padStart(2, '0');
      return m + ':' + s;
    },
    async uploadMedia(blob, filename, voice) {
      if (this.sending || !this.sendMediaUrl) return;
      const form = this.$refs.composer;
      const fd = new FormData();
      fd.set('csrfmiddlewaretoken', new FormData(form).get('csrfmiddlewaretoken'));
      fd.set('file', blob, filename);
      if (voice) fd.set('voice', '1');
      const caption = voice ? '' : this.draft.trim();
      if (caption) fd.set('caption', caption);
      const label = voice ? '[Voice message]' : '[' + (filename || 'File') + ']';
      const pending = { id: 'p' + Date.now(), direction: 'outbound', body: 'Sending ' + label + '…', ts: new Date().toISOString(), status: '', pending: true };
      this.messages.push(pending);
      if (caption) this.draft = '';
      this.$nextTick(() => this.scrollBottom());
      this.sending = true;
      try {
        const r = await fetch(this.sendMediaUrl, {
          method: 'POST', body: fd, credentials: 'same-origin',
          headers: { 'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json' },
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || !data.ok) throw new Error(data.error || 'Could not send that file.');
        this.interval = BASE_INTERVAL;
        this.schedule(300);
      } catch (e) {
        if (caption) this.draft = caption;
        if (window.toast) window.toast('danger', e.message);
      } finally {
        this.messages = this.messages.filter((m) => m.id !== pending.id);
        this.sending = false;
      }
    },

    // ── composing ────────────────────────────────────────────────
    grow(el) {
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, 144) + 'px';
    },
    onEnter(e) {
      if (this.coarse || e.shiftKey || e.isComposing) return;
      e.preventDefault();
      this.send();
    },
    async send() {
      const body = this.draft.trim();
      if (!body || this.sending) return;
      const form = this.$refs.composer;
      const fd = new FormData(form);
      fd.set('body', body);

      const pending = { id: 'p' + Date.now(), direction: 'outbound', body, ts: new Date().toISOString(), status: '', pending: true };
      this.messages.push(pending);
      this.draft = '';
      this.$nextTick(() => { this.grow(this.$refs.input); this.scrollBottom(); });
      this.sending = true;

      try {
        const r = await fetch(form.getAttribute('action') || window.location.pathname, {
          method: 'POST', body: fd, credentials: 'same-origin',
          headers: { 'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json' },
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || !data.ok) throw new Error(data.error || 'Could not send the message.');
        this.interval = BASE_INTERVAL;
        this.schedule(300);
        if (this.aiUsedId && this.ai.proposal && this.ai.proposal.id === this.aiUsedId) this.ai.proposal.status = 'used';
        this.aiUsedId = null;
      } catch (e) {
        this.messages = this.messages.filter((m) => m.id !== pending.id);
        this.draft = body;
        if (window.toast) window.toast('danger', e.message);
      } finally {
        this.sending = false;
        this.$nextTick(() => { this.grow(this.$refs.input); this.$refs.input.focus({ preventScroll: true }); });
      }
    },
  }));

  /* Voice note player, styled like Instagram / WhatsApp instead of the browser's
   * audio bar: play/pause, a waveform that fills as it plays (click or arrow keys
   * to seek), elapsed / total time and a 1× / 1.5× / 2× speed toggle. The waveform
   * shape is drawn from the message id, not the audio — decoding every note to
   * draw it would download them all up front. One note plays at a time. */
  const BAR_COUNT = 32;
  const RATES = [1, 1.5, 2];
  const clock = (s) => {
    if (!isFinite(s) || s < 0) return '0:00';
    s = Math.round(s);
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  };

  Alpine.data('voiceNote', (url, seed) => ({
    url,
    playing: false,
    current: 0,
    duration: 0,
    rate: 1,
    failed: false,
    bars: (() => {
      // Small deterministic PRNG so a note keeps its shape across re-renders.
      let x = (Number(seed) || 1) * 2654435761 % 4294967296;
      return Array.from({ length: BAR_COUNT }, (_, i) => {
        x = (x * 1664525 + 1013904223) % 4294967296;
        const edge = Math.min(i, BAR_COUNT - 1 - i) < 3 ? 0.6 : 1;  // softer at both ends
        return Math.round((28 + (x / 4294967296) * 72) * edge);
      });
    })(),

    init() {
      this._onOther = (e) => { if (e.detail !== this && this.playing) this.$refs.audio.pause(); };
      window.addEventListener('voice-note:play', this._onOther);
    },
    destroy() {
      window.removeEventListener('voice-note:play', this._onOther);
      cancelAnimationFrame(this._raf);
      // A removed <audio> keeps playing in some browsers.
      if (this.$refs.audio) this.$refs.audio.pause();
    },

    get progress() { return this.duration ? Math.min(this.current / this.duration, 1) : 0; },
    get label() { return clock(this.playing || this.current ? this.current : this.duration); },

    toggle() {
      const a = this.$refs.audio;
      if (this.failed) return;
      if (a.paused) {
        window.dispatchEvent(new CustomEvent('voice-note:play', { detail: this }));
        a.playbackRate = this.rate;
        a.play().catch(() => { this.failed = true; });
      } else {
        a.pause();
      }
    },
    cycleRate() {
      this.rate = RATES[(RATES.indexOf(this.rate) + 1) % RATES.length];
      this.$refs.audio.playbackRate = this.rate;
    },
    seekTo(fraction) {
      const a = this.$refs.audio;
      if (!this.duration) return;
      a.currentTime = Math.max(0, Math.min(fraction, 1)) * this.duration;
      this.current = a.currentTime;
    },
    seekClick(e) {
      const r = e.currentTarget.getBoundingClientRect();
      this.seekTo((e.clientX - r.left) / r.width);
    },
    seekBy(seconds) {
      if (this.duration) this.seekTo((this.current + seconds) / this.duration);
    },

    // <audio> events
    onMeta() {
      const d = this.$refs.audio.duration;
      if (isFinite(d)) this.duration = d;
    },
    onPlay() {
      this.playing = true;
      const tick = () => {
        this.current = this.$refs.audio.currentTime;
        if (this.playing) this._raf = requestAnimationFrame(tick);
      };
      tick();
    },
    onPause() {
      this.playing = false;
      cancelAnimationFrame(this._raf);
    },
    onEnded() {
      this.onPause();
      this.current = 0;
    },
  }));
}

if (window.Alpine && window.Alpine.version) registerChat();
else document.addEventListener('alpine:init', registerChat);
})();
