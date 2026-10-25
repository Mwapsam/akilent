/* Live conversation thread: renders messages from a JSON feed, polls for new
 * ones, and sends replies without a page reload. Config comes from a
 * json_script block: { messages, lastId, feedUrl, open }.
 *
 * Loaded with the app shell (base.html), not by the page: a page swapped in place
 * would otherwise render x-data="chat(...)" before its own script had loaded. The
 * component stops polling in destroy(), which Alpine calls when the page is swapped away. */
(function () {
function registerChat() {
  const STATUS_LABELS = { queued: 'Sending…', sent: 'Sent', delivered: 'Delivered', read: 'Read', failed: 'Failed', held: 'Held (plan limit)', unconfirmed: 'Not confirmed', unknown: 'Not confirmed' };
  const BASE_INTERVAL = 3000;
  const MAX_INTERVAL = 30000;
  // Failed polls in a row before the thread says it isn't updating (one miss is noise).
  const LOST_AFTER = 2;
  const SEND_TIMEOUT = 30000;
  const UPLOAD_TIMEOUT = 120000;
  const UNKNOWN_TEXT = "Not confirmed. This may or may not have been sent. If it doesn't appear here shortly, put it back in the reply box and send it again.";

  // Posts a send and says what is actually known about it afterwards:
  //   ok       - the server took it;
  //   refused  - the server answered and did not take it (a 4xx, or a sign-in page
  //              after the session ended): safe to say it wasn't sent;
  //   unknown  - it may have been sent: the connection dropped, it timed out, or the
  //              server failed part-way. Never offered as a plain retry.
  // Which failures are "may have gone" comes from app.js (window.akFailure), the same
  // rules every other save uses. Callers check navigator.onLine first: offline, nothing
  // is posted at all.
  async function postSend(url, fd, timeout) {
    const F = window.akFailure;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    let r;
    try {
      r = await fetch(url, {
        method: 'POST', body: fd, credentials: 'same-origin', signal: ctrl.signal,
        headers: { 'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json' },
      });
    } catch (e) {
      // Even if the browser now says it is offline, the connection may have gone
      // after the request reached the server.
      return { kind: 'unknown' };
    } finally {
      clearTimeout(timer);
    }
    const data = await r.json().catch(() => null);
    if (r.ok && data && data.ok) return { kind: 'ok', data };
    if (!r.ok) {
      const failure = F.describe({ status: r.status }, 'response');
      if (F.maybeSaved[failure.kind]) return { kind: 'unknown' };
      return { kind: 'refused', error: (data && data.error) || F.SAVE_COPY[failure.kind] || "This wasn't sent." };
    }
    // A 2xx that isn't the view's own answer: the session ended and this is the sign-in page.
    return { kind: 'refused', error: r.redirected ? "You've been signed out, so this wasn't sent. Sign in again in another tab, then send it." : (data && data.error) || "This wasn't sent." };
  }

  const dayKey = (d) => d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate();

  Alpine.data('chat', (cfg) => ({
    messages: cfg.messages || [],
    lastId: cfg.lastId || 0,
    open: cfg.open,
    windowOpen: cfg.windowOpen !== false,
    statusHtml: cfg.statusHtml || '',
    infoOpen: false,
    // True while the details are open as a drawer (narrow pane), not as a column.
    infoDrawer: false,
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
    // Teammates in this conversation (from messages_feed), and the two-replies guard.
    presence: { viewing: [], typing: [] },
    collision: '',
    collisionAck: false,
    _lastTypingPing: 0,
    // Why the last send didn't go, said next to the reply box (it stays until the next
    // send or a dismiss, so it can't be missed the way a toast can).
    sendError: '',
    // 'ok' | 'lost': lost once LOST_AFTER polls in a row have failed.
    connection: 'ok',
    online: navigator.onLine,
    _pollFailures: 0,

    // "[Photo]" / "[Voice message]" placeholders say nothing once the media itself shows.
    showText(m) {
      if (!m.body) return !m.media;
      if (!m.media || m.media.state !== 'ready') return true;
      return !/^(\[[^\]]+\]\s*)+$/.test(m.body.trim());
    },

    init() {
      // An unsent reply survives switching conversations in the two-pane inbox, and a reload.
      this._draftKey = 'inbox-draft:' + cfg.id;
      try { this.draft = sessionStorage.getItem(this._draftKey) || ''; } catch (e) { /* storage blocked */ }
      if (this.draft) this.$nextTick(() => { if (this.$refs.input) this.grow(this.$refs.input); });
      this.$watch('draft', (value) => {
        try {
          if (value) sessionStorage.setItem(this._draftKey, value);
          else sessionStorage.removeItem(this._draftKey);
        } catch (e) { /* storage blocked: the draft just isn't kept */ }
        if (value.trim()) this.pingTyping();
      });
      // Sends whose outcome was never confirmed are kept until the thread shows them (or
      // the user dismisses them), so a reload or switching away can't lose the text of a
      // reply that may never have gone.
      this._unknownKey = 'inbox-unconfirmed:' + cfg.id;
      try {
        const kept = JSON.parse(sessionStorage.getItem(this._unknownKey) || '[]');
        for (const u of kept) {
          if (!u || !u.body) continue;
          const arrived = u.media
            ? this.messages.some((m) => m.direction === 'outbound' && m.media && !m.byAi && new Date(m.ts) >= new Date(u.ts) - 60000)
            : this.messages.some((m) => m.direction === 'outbound' && !m.byAi && m.body === u.body && new Date(m.ts) >= new Date(u.ts) - 60000);
          if (!arrived) {
            this.messages.push({ id: u.id, direction: 'outbound', body: u.body, ts: u.ts, status: 'unknown', unknown: true, mediaUnknown: !!u.media, caption: u.caption || '' });
          }
        }
        this.sortMessages();
        this.saveUnknown();
      } catch (e) { /* storage blocked or bad JSON: nothing kept */ }
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
      // The details panel: a drawer only while the info button shows (narrow pane).
      const isDrawer = () => !!this.$refs.infoBtn && getComputedStyle(this.$refs.infoBtn).display !== 'none';
      this.$watch('infoOpen', (open) => {
        if (open && isDrawer()) {
          this.infoDrawer = true;
          window.akDialog.open(this.$refs.side, { close: () => { this.infoOpen = false; } });
        } else if (!open && this.infoDrawer) {
          this.infoDrawer = false;
          window.akDialog.close(this.$refs.side);
        } else if (open) {
          this.infoOpen = false; // wide: the details are already on screen as a column
        }
      });
      // Widened past the drawer breakpoint while it was open: it is a column now, so the
      // dialog has to end, or the page would stay inert behind nothing.
      this._onResizeInfo = () => { if (this.infoDrawer && !isDrawer()) this.infoOpen = false; };
      window.addEventListener('resize', this._onResizeInfo, { passive: true });
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
      this._onOnline = () => { this.online = true; this.interval = BASE_INTERVAL; this.schedule(0); };
      this._onOffline = () => { this.online = false; };
      window.addEventListener('online', this._onOnline);
      window.addEventListener('offline', this._onOffline);
      this.schedule();
    },

    destroy() {
      this._destroyed = true;
      // Leaving mid-recording must release the microphone, not send a half note.
      this.stopRecording(false);
      clearTimeout(this.timer);
      window.removeEventListener('resize', this._fit);
      window.removeEventListener('resize', this._onResizeInfo);
      if (this.infoDrawer && window.akDialog) window.akDialog.close(this.$refs.side);
      document.removeEventListener('visibilitychange', this._onVisible);
      document.removeEventListener('sse:message.created', this._onSseMessage);
      window.removeEventListener('online', this._onOnline);
      window.removeEventListener('offline', this._onOffline);
    },
    saveUnknown() {
      try {
        const list = this.messages.filter((m) => m.unknown && m.body)
          .map((m) => ({ id: m.id, body: m.body, ts: m.ts, media: !!m.mediaUnknown, caption: m.caption || '' }));
        if (list.length) sessionStorage.setItem(this._unknownKey, JSON.stringify(list));
        else sessionStorage.removeItem(this._unknownKey);
      } catch (e) { /* storage blocked */ }
    },
    // "Put back in reply box" on a send that was never confirmed: the person has read
    // that it may already have gone, and chooses to send it again.
    restoreUnknown(m) {
      this.messages = this.messages.filter((x) => x.id !== m.id);
      this.saveUnknown();
      // A file can't be put back, only its caption.
      const text = m.mediaUnknown ? m.caption : m.body;
      if (text) this.draft = this.draft.trim() ? this.draft + '\n' + text : text;
      this.$nextTick(() => { this.grow(this.$refs.input); this.$refs.input.focus(); });
    },
    dismissUnknown(m) {
      this.messages = this.messages.filter((x) => x.id !== m.id);
      this.saveUnknown();
    },
    // In time order (day dividers and meta runs depend on it); in-flight sends last.
    sortMessages() {
      this.messages.sort((a, b) => (a.pending ? 1 : 0) - (b.pending ? 1 : 0) || new Date(a.ts) - new Date(b.ts));
    },
    checkNow() { this.interval = BASE_INTERVAL; this.schedule(0); },
    connectionText() {
      if (this.connection !== 'lost') return '';
      return this.online
        ? "Not connected. New messages and delivery ticks may be out of date. Reconnecting…"
        : "You're offline. New messages will show when you reconnect.";
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
        out.push({ key: 'm-' + m.id, type: 'msg', m, showMeta: true, showFailure: m.status === 'failed' || m.status === 'held' || m.status === 'unconfirmed' || !!m.unknown });
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
      if (m.unknown) return UNKNOWN_TEXT;
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
      // Only reaching the server counts toward "Not connected". A payload this code can't
      // handle is a bug, logged as one, not reported to the user as a lost connection.
      let data = null;
      try {
        const r = await fetch(cfg.feedUrl + '?after=' + this.lastId, {
          credentials: 'same-origin',
          headers: { 'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json' },
        });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        data = await r.json();
        this.interval = BASE_INTERVAL;
        this._pollFailures = 0;
        this.connection = 'ok';
      } catch (e) {
        this.interval = Math.min(this.interval * 2, MAX_INTERVAL);
        this._pollFailures += 1;
        if (this._pollFailures >= LOST_AFTER) this.connection = 'lost';
      }
      if (data) {
        try { this.apply(data); } catch (e) { console.error('chat: could not apply the message feed', e); }
      }
      this.schedule();
    },
    apply(data) {
      const stick = this.nearBottom();
      const known = new Set(this.messages.map((m) => m.id));
      let inbound = false;
      for (const raw of data.messages) {
        if (known.has(raw.id)) continue;
        if (raw.direction === 'outbound') {
          // The real message replaces its optimistic placeholder.
          // So does a send whose outcome was unknown: it did go. A file or voice note has no
          // text to match, so it pairs with the first outbound media message sent after it
          // was attempted (allowing a minute of clock difference).
          // Never an AI reply, and only a message created after the attempt (a minute of
          // clock difference allowed). A teammate sending the same words at the same moment
          // can't be told apart: messages don't record which person sent them (gap G2).
          const after = (m) => new Date(raw.timestamp) >= new Date(m.ts) - 60000;
          let i = raw.byAi ? -1 : this.messages.findIndex((m) => (m.pending || m.unknown) && !m.mediaUnknown && m.body === raw.body && after(m));
          if (i === -1 && raw.media && !raw.byAi) {
            i = this.messages.findIndex((m) => m.mediaUnknown && after(m));
          }
          if (i !== -1) {
            const wasUnknown = this.messages[i].unknown;
            this.messages.splice(i, 1);
            if (wasUnknown) this.saveUnknown();
          }
          else if (this.draft.trim()) {
            // Someone else answered while this reply was being written.
            this.collision = raw.byAi ? 'AI just sent a reply.' : 'A reply was just sent in this conversation.';
            this.collisionAck = false;
          }
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
      this.sortMessages();
      if (data.status_html !== undefined) this.statusHtml = data.status_html;
      this.open = data.open;
      if (data.windowOpen !== undefined) this.windowOpen = data.windowOpen;
      if (this.ai.enabled && data.aiProposal !== undefined) this.ai.proposal = data.aiProposal;
      if (data.presence) this.presence = { viewing: data.presence.viewing || [], typing: data.presence.typing || [] };
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
    // Keep the team's reply as a knowledge-base answer, so AI can answer it next time.
    async aiLearn(question, answer) {
      const p = this.ai.proposal;
      if (!p) return false;
      try {
        const data = await this.aiPost(this.ai.learnUrl, { proposal: p.id, question, answer });
        if (data.proposal) this.ai.proposal = data.proposal;
        if (window.toast) window.toast('success', data.message);
        return true;
      } catch (e) {
        if (window.toast) window.toast('danger', e.message);
        return false;
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
      this.sendError = '';
      const form = this.$refs.composer;
      const fd = new FormData();
      fd.set('csrfmiddlewaretoken', new FormData(form).get('csrfmiddlewaretoken'));
      fd.set('file', blob, filename);
      if (voice) fd.set('voice', '1');
      const caption = voice ? '' : this.draft.trim();
      if (caption) fd.set('caption', caption);
      const label = voice ? '[Voice message]' : '[' + (filename || 'File') + ']';
      if (!navigator.onLine) {
        this.sendError = "You're offline, so " + (voice ? 'the voice note' : 'the file') + " wasn't sent.";
        return;
      }
      const pending = { id: 'p' + Date.now(), direction: 'outbound', body: 'Sending ' + label + '…', ts: new Date().toISOString(), status: '', pending: true };
      this.messages.push(pending);
      if (caption) this.draft = '';
      this.$nextTick(() => this.scrollBottom());
      this.sending = true;
      try {
        const out = await postSend(this.sendMediaUrl, fd, UPLOAD_TIMEOUT);
        if (out.kind === 'ok') {
          this.messages = this.messages.filter((m) => m.id !== pending.id);
          this.interval = BASE_INTERVAL;
          this.schedule(300);
        } else if (out.kind === 'unknown') {
          // The file can't be put back, so the bubble stays to say what is known.
          // Not kept across a reload: it has no text to restore.
          // Replaced through the reactive array (not by mutating the object pushed earlier,
          // which Alpine would not notice), so the bubble re-renders as Not confirmed.
          const i = this.messages.findIndex((m) => m.id === pending.id);
          // The caption was taken out of the reply box; it stays with the bubble, can be put
          // back, and is kept across a reload like a text reply.
          if (i !== -1) this.messages.splice(i, 1, Object.assign({}, pending, { pending: false, unknown: true, mediaUnknown: true, status: 'unknown', body: caption ? label + ' ' + caption : label, caption }));
          this.saveUnknown();
          this.schedule(0);
        } else {
          this.messages = this.messages.filter((m) => m.id !== pending.id);
          if (caption) this.draft = caption;
          this.sendError = out.error;
        }
      } finally {
        this.sending = false;
      }
    },

    // ── presence ─────────────────────────────────────────────────
    presenceText() {
      const names = (list) => list.length === 1 ? list[0]
        : list.length === 2 ? list[0] + ' and ' + list[1]
        : list[0] + ' and ' + (list.length - 1) + ' others';
      const p = this.presence;
      if (p.typing.length) return names(p.typing) + (p.typing.length === 1 ? ' is typing…' : ' are typing…');
      if (p.viewing.length) return names(p.viewing) + (p.viewing.length === 1 ? ' is also viewing' : ' are also viewing');
      return '';
    },
    // Tells teammates (and, where the channel allows, the customer) that a reply is being
    // written. At most one ping per 4 seconds of typing; the server rate-limits the
    // customer-facing indicator further. Best effort: a failed ping changes nothing.
    pingTyping() {
      const now = Date.now();
      if (!cfg.typingUrl || !this.open || now - this._lastTypingPing < 4000) return;
      this._lastTypingPing = now;
      const token = this.$refs.composer && this.$refs.composer.querySelector('[name=csrfmiddlewaretoken]');
      if (!token) return;
      const fd = new FormData();
      fd.set('csrfmiddlewaretoken', token.value);
      fetch(cfg.typingUrl, {
        method: 'POST', body: fd, credentials: 'same-origin',
        headers: { 'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json' },
      }).catch(() => {});
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
      if (this.collision && !this.collisionAck) {
        // A reply just went out from someone else: the first Send only asks to check it.
        this.collisionAck = true;
        return;
      }
      this.collision = '';
      this.collisionAck = false;
      this.sendError = '';
      if (!navigator.onLine) {
        // Nothing left the browser, so the reply simply stays in the box.
        this.sendError = "You're offline, so this wasn't sent. Your reply is still in the box.";
        return;
      }
      const form = this.$refs.composer;
      const fd = new FormData(form);
      fd.set('body', body);

      const pending = { id: 'p' + Date.now(), direction: 'outbound', body, ts: new Date().toISOString(), status: '', pending: true };
      this.messages.push(pending);
      this.draft = '';
      this.$nextTick(() => { this.grow(this.$refs.input); this.scrollBottom(); });
      this.sending = true;

      try {
        const out = await postSend(form.getAttribute('action') || window.location.pathname, fd, SEND_TIMEOUT);
        if (out.kind === 'ok') {
          this.interval = BASE_INTERVAL;
          this.schedule(300);
          if (this.aiUsedId && this.ai.proposal && this.ai.proposal.id === this.aiUsedId) this.ai.proposal.status = 'used';
          this.aiUsedId = null;
        } else if (out.kind === 'unknown') {
          // It may have gone. The bubble says so and stays until the thread shows the real
          // message (apply() swaps it in) or the person chooses what to do; the text is not
          // put back in the box, where one more Enter would send it a second time.
          const i = this.messages.indexOf(pending);
          if (i !== -1) this.messages.splice(i, 1, Object.assign({}, pending, { pending: false, unknown: true, status: 'unknown' }));
          this.saveUnknown();
          this.schedule(0);
        } else {
          this.messages = this.messages.filter((m) => m.id !== pending.id);
          this.draft = body;
          this.sendError = out.error + ' Your reply is still in the box.';
        }
      } finally {
        this.sending = false;
        this.$nextTick(() => { this.grow(this.$refs.input); this.$refs.input.focus({ preventScroll: true }); });
      }
    },
  }));

  /* Menu button for the conversation header (Assign, Close, More): opens a list of choices,
   * arrow keys / Home / End move between them, Esc closes and returns focus to the button,
   * Tab or a click outside closes. Each choice is a submit button or link, so nothing
   * happens until one is picked. State is `menuOpen`, not `open`, so a menu nested in the
   * chat component can still read the conversation's own `open` flag. */
  Alpine.data('cvMenu', () => ({
    menuOpen: false,
    init() {
      this._outside = (e) => { if (this.menuOpen && !this.$el.contains(e.target)) this.menuOpen = false; };
      document.addEventListener('click', this._outside, true);
      this.$el.addEventListener('keydown', (e) => this.onKey(e));
    },
    destroy() { document.removeEventListener('click', this._outside, true); },
    items() {
      return [...this.$refs.menu.querySelectorAll('[role^="menuitem"]')].filter((el) => el.offsetParent !== null && !el.disabled);
    },
    toggle() {
      this.menuOpen = !this.menuOpen;
      if (this.menuOpen) this.$nextTick(() => { const first = this.items()[0]; if (first) first.focus(); });
    },
    close(refocus) {
      this.menuOpen = false;
      if (refocus) this.$refs.trigger.focus();
    },
    onKey(e) {
      if (e.key === 'Escape' && this.menuOpen) { e.preventDefault(); e.stopPropagation(); this.close(true); return; }
      if (!this.menuOpen) {
        if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && e.target === this.$refs.trigger) { e.preventDefault(); this.toggle(); }
        return;
      }
      if (e.key === 'Tab') { this.close(false); return; }
      const list = this.items();
      if (!list.length) return;
      const i = list.indexOf(document.activeElement);
      let next = null;
      if (e.key === 'ArrowDown') next = list[(i + 1) % list.length];
      else if (e.key === 'ArrowUp') next = list[(i - 1 + list.length) % list.length];
      else if (e.key === 'Home') next = list[0];
      else if (e.key === 'End') next = list[list.length - 1];
      if (next) { e.preventDefault(); next.focus(); }
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
