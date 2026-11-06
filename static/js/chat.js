/* Live conversation thread: renders messages from a JSON feed, polls for new
 * ones, and sends replies without a page reload. Config comes from a
 * json_script block: { messages, lastId, feedUrl, open }.
 *
 * Loaded with the app shell (base.html), not by the page: a page swapped in place
 * would otherwise render x-data="chat(...)" before its own script had loaded. The
 * component stops polling in destroy(), which Alpine calls when the page is swapped away. */
(function () {
function registerChat() {
  // How far apart two messages from the same side can be and still read as one group.
  const GROUP_MINUTES = 5;
  const STATUS_LABELS = { queued: 'Sending…', sent: 'Sent', delivered: 'Delivered', read: 'Read', failed: 'Failed', held: 'Held (plan limit)', unconfirmed: 'Not confirmed', unknown: 'Not confirmed' };
  const BASE_INTERVAL = 3000;
  const MAX_INTERVAL = 30000;
  // Failed polls in a row before the thread says it isn't updating (one miss is noise).
  const LOST_AFTER = 2;
  const SEND_TIMEOUT = 30000;
  const UPLOAD_TIMEOUT = 120000;
  // Sending again reuses the attempt's key, which the server dedupes on (apps/conversations/
  // inbox_keys.py): if the first one went, the customer doesn't get it twice.
  const UNKNOWN_TEXT = "Not confirmed. This may or may not have been sent. Sending it again is safe: if it already went, the customer won't get it twice.";
  // After a reload the file itself is gone, so it can't be sent again from here.
  const UNKNOWN_MEDIA_TEXT = "Not confirmed. This may or may not have been sent. If it went, it will show here.";
  // A random key per send attempt (see UNKNOWN_TEXT). The server accepts 8-64 letters,
  // digits and hyphens.
  const newKey = () => (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : 'k' + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  // A page time further than this from the browser's clock is an old page (restored from
  // history), not a wrong clock: it isn't used, and the first poll measures the clock instead.
  const STALE_PAGE_MS = 5 * 60 * 1000;

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
    let r, data;
    try {
      r = await fetch(url, {
        method: 'POST', body: fd, credentials: 'same-origin', signal: ctrl.signal,
        headers: { 'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json' },
      });
      // The timeout covers the body too: headers can arrive and the rest stall.
      data = await r.json().catch((e) => { if (ctrl.signal.aborted) throw e; return null; });
    } catch (e) {
      // Even if the browser now says it is offline, the connection may have gone
      // after the request reached the server.
      return { kind: 'unknown' };
    } finally {
      clearTimeout(timer);
    }
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

  // The docked details column: its width (dragged, remembered per browser) and whether it's hidden.
  const SIDE_MIN = 280;
  const SIDE_MAX = 440;
  const SIDE_DEFAULT = 320;
  const SIDE_WIDTH_KEY = 'inbox-side-w';
  const SIDE_COLLAPSED_KEY = 'inbox-side-collapsed';
  const clampSide = (w) => Math.min(SIDE_MAX, Math.max(SIDE_MIN, Math.round(w)));

  Alpine.data('chat', (cfg) => ({
    messages: cfg.messages || [],
    lastId: cfg.lastId || 0,
    // The thread opens on its latest messages; older ones load on request (loadEarlier).
    hasEarlier: !!cfg.hasEarlier,
    loadingEarlier: false,
    open: cfg.open,
    windowOpen: cfg.windowOpen !== false,
    statusHtml: cfg.statusHtml || '',
    // "New messages" (Phase 5E): where the customer's unread run starts when it opened.
    firstUnreadId: cfg.firstUnreadId || null,
    unreadCount: cfg.unreadCount || 0,
    // "Show the details": opens the drawer where the details don't fit beside the chat, and
    // brings the column back where they do (docked, collapsed).
    infoOpen: false,
    followupLabel: '',  // set when an AI-suggested follow-up is applied (aiApply)
    // True while the details are open as a drawer (narrow pane), not as a column.
    infoDrawer: false,
    // Docked: the details are a column beside the chat (wide enough for both; see .cv-side in
    // assets/app.css). The column can be hidden and resized, and both are remembered.
    docked: false,
    sideCollapsed: false,
    sideW: SIDE_DEFAULT,
    // The window closed after a send failed for a reason that's about the WhatsApp account:
    // the composer shows that first, and this is "Try a template anyway".
    tplAnyway: false,
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
    // Files and voice notes that never left (offline, or another send in progress), kept
    // to send when the person chooses. Each has its own notice by the reply box.
    held: [],
    // 'ok' | 'lost' | 'signedOut': lost once LOST_AFTER polls in a row have failed;
    // signedOut when the server answered with the sign-in page instead.
    connection: 'ok',
    online: navigator.onLine,
    _pollFailures: 0,
    // How far the server's clock is ahead of this browser's (first from the page, then from
    // each poll). Sends are stamped in server time, so matching them to the real message
    // (and sorting) holds on a wrong PC clock.
    // Every field a method sets is declared here. Alpine stores a field that isn't on the
    // component on the outermost x-data instead (base.html's <body x-data>), which every
    // component on every page shares and which outlives a swapped page: one conversation's
    // _destroyed then stopped the next one from ever polling.
    _destroyed: false,
    // Files whose send is unconfirmed, by key, so "Send again" can resend the same file
    // with the same key. Kept only while the page is open.
    _blobs: {},
    _draftKey: '',
    _unknownKey: '',
    _fit: null,
    _sideObserver: null,
    _onVisible: null,
    _onSseMessage: null,
    _onOnline: null,
    _onOffline: null,
    _skew: (() => {
      const skew = cfg.serverNow ? new Date(cfg.serverNow).getTime() - Date.now() : 0;
      return Math.abs(skew) < STALE_PAGE_MS ? skew : 0;
    })(),
    serverTime() { return new Date(Date.now() + this._skew).toISOString(); },

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
          // Matched by the attempt's key, so only this send counts, never a teammate's.
          // (A note kept before keys existed has none: it stays until dismissed.)
          const arrived = !!u.clientKey && this.messages.some((m) => m.clientKey === u.clientKey);
          if (!arrived) {
            this.messages.push({ id: u.id, direction: 'outbound', body: u.body, ts: u.ts, status: 'unknown', unknown: true, mediaUnknown: !!u.media, caption: u.caption || '', clientKey: u.clientKey || '' });
          }
        }
        this.sortMessages();
        this.saveUnknown();
      } catch (e) { /* storage blocked or bad JSON: nothing kept */ }
      this.$nextTick(() => {
        this.scrollBottom(true);
        // More unread than fits: open on "New messages", not past it.
        const d = this.$root.querySelector('.cv-unread');
        const t = this.thread;
        if (d && t && d.offsetTop < t.scrollTop) t.scrollTop = Math.max(0, d.offsetTop - 8);
      });
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
      // The details panel: a column beside the chat where both fit, a drawer where they don't.
      // The CSS decides (a container query on the pane's width); this follows it.
      try {
        this.sideCollapsed = localStorage.getItem(SIDE_COLLAPSED_KEY) === '1';
        const w = parseInt(localStorage.getItem(SIDE_WIDTH_KEY), 10);
        if (w) this.sideW = clampSide(w);
      } catch (e) { /* storage blocked: the defaults */ }
      this.$el.style.setProperty('--cv-side-w', this.sideW + 'px');
      this.$watch('sideW', (w) => this.$el.style.setProperty('--cv-side-w', w + 'px'));
      this.$watch('infoOpen', (open) => {
        if (open && this.docked) {
          this.infoOpen = false; // the details are a column here: show it if it was hidden
          this.setSideCollapsed(false);
        } else if (open) {
          this.infoDrawer = true;
          if (window.akDialog) window.akDialog.open(this.$refs.side, { close: () => { this.infoOpen = false; } });
        } else if (this.infoDrawer) {
          this.infoDrawer = false;
          if (window.akDialog) window.akDialog.close(this.$refs.side);
        }
      });
      // Widened until the details fit beside the chat while the drawer was open: they're a
      // column now, so the dialog has to end, or the page would stay inert behind nothing.
      const measureSide = () => {
        const docked = getComputedStyle(this.$refs.side).position !== 'fixed';
        if (docked === this.docked) return;
        this.docked = docked;
        if (docked && this.infoDrawer) this.infoOpen = false;
      };
      measureSide();
      if (window.ResizeObserver) {
        this._sideObserver = new ResizeObserver(measureSide);
        this._sideObserver.observe(this.$el);
      }
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
      // An old page (restored from history) has no usable clock: measure it now.
      const fresh = !cfg.serverNow || Math.abs(new Date(cfg.serverNow).getTime() - Date.now()) < STALE_PAGE_MS;
      this.schedule(fresh ? undefined : 0);
    },

    destroy() {
      this._destroyed = true;
      // Leaving mid-recording must release the microphone, not send a half note.
      this.stopRecording(false);
      clearTimeout(this.timer);
      window.removeEventListener('resize', this._fit);
      if (this._sideObserver) this._sideObserver.disconnect();
      if (this.infoDrawer && window.akDialog) window.akDialog.close(this.$refs.side);
      document.removeEventListener('visibilitychange', this._onVisible);
      document.removeEventListener('sse:message.created', this._onSseMessage);
      window.removeEventListener('online', this._onOnline);
      window.removeEventListener('offline', this._onOffline);
    },
    saveUnknown() {
      try {
        const list = this.messages.filter((m) => m.unknown && m.body)
          .map((m) => ({ id: m.id, body: m.body, ts: m.ts, media: !!m.mediaUnknown, caption: m.caption || '', clientKey: m.clientKey || '' }));
        if (list.length) sessionStorage.setItem(this._unknownKey, JSON.stringify(list));
        else sessionStorage.removeItem(this._unknownKey);
      } catch (e) { /* storage blocked */ }
    },
    // "Put back in reply box" on a send that was never confirmed: the person has read
    // that it may already have gone, and chooses to send it again.
    restoreUnknown(m) {
      // A file can't be put back, only its caption; with none, there is nothing to do.
      const text = m.mediaUnknown ? m.caption : m.body;
      if (!text) return;
      this.messages = this.messages.filter((x) => x.id !== m.id);
      if (m.clientKey) delete this._blobs[m.clientKey];
      this.saveUnknown();
      this.draft = this.draft.trim() ? this.draft + '\n' + text : text;
      this.$nextTick(() => { this.grow(this.$refs.input); this.$refs.input.focus(); });
    },
    dismissUnknown(m) {
      this.messages = this.messages.filter((x) => x.id !== m.id);
      if (m.clientKey) delete this._blobs[m.clientKey];
      this.saveUnknown();
    },
    // In time order (day dividers and meta runs depend on it); in-flight sends last.
    sortMessages() {
      this.messages.sort((a, b) => (a.pending ? 1 : 0) - (b.pending ? 1 : 0) || new Date(a.ts) - new Date(b.ts));
    },
    checkNow() { this.interval = BASE_INTERVAL; this.schedule(0); },
    connectionText() {
      if (this.connection === 'signedOut') return "You've been signed out, so new messages won't show and replies won't send. Sign in again in another tab and this will catch up.";
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
        if (m.id === this.firstUnreadId) {
          out.push({ key: 'unread', type: 'unread', label: this.unreadCount > 1 ? this.unreadCount + ' new messages' : 'New message' });
        }
        out.push({ key: 'm-' + m.id, type: 'msg', m, showMeta: true, showFailure: m.status === 'failed' || m.status === 'held' || m.status === 'unconfirmed' || !!m.unknown });
      }
      // Messages sent in a row (same side, same sender, GROUP_MINUTES apart at most) are one
      // group: only its last shows the tail, time and ticks; the others show their time beside
      // the bubble on hover (.cv-hover-time), so nothing moves.
      for (let i = 0; i < out.length - 1; i++) {
        const a = out[i], b = out[i + 1];
        if (a.type === 'msg' && b.type === 'msg' && a.m.direction === b.m.direction && !!a.m.byAi === !!b.m.byAi
            && !a.showFailure && this.minute(b.m.ts) - this.minute(a.m.ts) <= GROUP_MINUTES) a.showMeta = false;
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
      if (m.unknown) return m.mediaUnknown ? UNKNOWN_MEDIA_TEXT : UNKNOWN_TEXT;
      return m.failureReason || 'This message could not be delivered.';
    },

    // The header's badges from a poll. Their hover text carries a time ("… (5 minutes)") that
    // moves on while what they show doesn't; re-rendering for that redrew the header just after
    // a conversation opened. When only the hover text changed, it's updated in place.
    applyStatus(html) {
      if (html === this.statusHtml) return;
      const strip = (h) => h.replace(/\s+title="[^"]*"/g, '');
      const box = this.$root.querySelector('.cv-h-status');
      if (box && strip(html) === strip(this.statusHtml)) {
        const next = document.createElement('div');
        next.innerHTML = html;
        const now = box.querySelectorAll('[title]');
        const fresh = next.querySelectorAll('[title]');
        if (now.length === fresh.length) {
          fresh.forEach((el, i) => now[i].setAttribute('title', el.getAttribute('title')));
          return;
        }
      }
      this.statusHtml = html;
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
    // "Show earlier messages": the page before the oldest one shown, put above it without
    // moving what the reader is looking at (the thread grows upwards by exactly its height).
    async loadEarlier() {
      const first = this.messages.find((m) => typeof m.id === 'number');
      if (!first || this.loadingEarlier) return;
      this.loadingEarlier = true;
      try {
        const r = await fetch(cfg.feedUrl + '?before=' + first.id, {
          credentials: 'same-origin',
          headers: { 'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json' },
        });
        if (!r.ok || !(r.headers.get('Content-Type') || '').includes('json')) throw new Error('HTTP ' + r.status);
        const data = await r.json();
        const t = this.thread;
        const before = t.scrollHeight;
        const known = new Set(this.messages.map((m) => m.id));
        this.messages = data.messages.filter((m) => !known.has(m.id)).concat(this.messages);
        this.sortMessages();
        this.hasEarlier = !!data.hasEarlier;
        this.$nextTick(() => { t.scrollTop += t.scrollHeight - before; });
      } catch (e) {
        if (window.toast) window.toast('error', "Couldn't load earlier messages. Try again.");
      } finally {
        this.loadingEarlier = false;
      }
    },
    // The header's details button: hides or shows the column where it's docked, opens or
    // closes the drawer where it isn't.
    toggleInfo() {
      if (this.docked) this.setSideCollapsed(!this.sideCollapsed);
      else this.infoOpen = !this.infoOpen;
    },
    setSideCollapsed(collapsed) {
      this.sideCollapsed = collapsed;
      try { localStorage.setItem(SIDE_COLLAPSED_KEY, collapsed ? '1' : '0'); } catch (e) { /* not kept */ }
    },
    // Dragging the docked column's edge (or arrow keys on it) sets its width, remembered for
    // every conversation. The CSS keeps the chat at least 420px wide whatever is chosen.
    startResize(e) {
      if (!this.docked || e.button !== 0) return;
      e.preventDefault();
      const startX = e.clientX;
      const startW = this.sideW;
      const move = (ev) => { this.sideW = clampSide(startW + startX - ev.clientX); };
      const up = () => {
        window.removeEventListener('pointermove', move);
        document.documentElement.classList.remove('is-resizing');
        this.saveSideW();
      };
      document.documentElement.classList.add('is-resizing');
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up, { once: true });
    },
    resizeSide(by) {
      this.sideW = clampSide(this.sideW + by);
      this.saveSideW();
    },
    saveSideW() {
      try { localStorage.setItem(SIDE_WIDTH_KEY, String(this.sideW)); } catch (e) { /* not kept */ }
    },
    // The last thing sent, when it failed because of the WhatsApp account rather than this
    // message (views._failure gives those, and only those, a failureFix): with the window
    // closed, a template would fail the same way, so the composer says this first.
    get accountProblem() {
      for (let i = this.messages.length - 1; i >= 0; i--) {
        const m = this.messages[i];
        if (m.direction !== 'outbound' || m.pending || m.unknown) continue;
        return m.status === 'failed' && m.failureFix ? m : null;
      }
      return null;
    },
    // The details panel loads once it shows (views.conversation_panel), so a section asked
    // for right as the drawer opens may not be there yet: wait for it, briefly.
    focusPanelSection(id) {
      let tries = 0;
      const go = () => {
        const s = document.getElementById(id);
        if (s) { s.scrollIntoView({ block: 'nearest' }); s.focus({ preventScroll: true }); return; }
        if (++tries < 40) setTimeout(go, 100);
      };
      setTimeout(go, 120);
    },

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
        const sent = Date.now();
        const r = await fetch(cfg.feedUrl + '?after=' + this.lastId, {
          credentials: 'same-origin',
          headers: { 'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json' },
        });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        // The session ended: the feed was redirected to the sign-in page itself. Retrying
        // can't fix that, so it isn't a lost connection; keep checking slowly for a new
        // sign-in. Any other page that isn't the feed (a proxy's or maintenance page) is a
        // failed poll like any other.
        if (r.redirected && cfg.loginUrl && new URL(r.url).pathname === new URL(cfg.loginUrl, location.href).pathname) {
          this.connection = 'signedOut';
          this._pollFailures = 0;
          this.interval = MAX_INTERVAL;
          this.schedule();
          return;
        }
        if (!(r.headers.get('Content-Type') || '').includes('json')) throw new Error('not the feed');
        data = await r.json();
        // The server read its clock somewhere between sending and arriving: the midpoint
        // is within half the round trip, unlike the page's own estimate, which misses the
        // time spent rendering and loading the page.
        if (data && data.serverNow) {
          const got = Date.now();
          this._skew = new Date(data.serverNow).getTime() - (sent + got) / 2;
        }
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
          // The real message replaces its "Sending…" or "Not confirmed" bubble (it did go).
          // Matched by the key this browser sent with the attempt, which the server keeps on
          // the message (apps/conversations/inbox_keys.py): exact, for text and files alike,
          // so a teammate's identical words or photo, or an AI reply, is never taken for it.
          // A message with no key at all (recorded from Instagram's echo before our own record,
          // a projection that failed, older data) can still be this send: matched by its
          // words as before, text only, never an AI reply. A teammate's inbox reply always
          // carries its own key, so it never comes this way.
          const after = (m) => new Date(raw.timestamp) >= new Date(m.ts) - 60000;
          const i = raw.clientKey
            ? this.messages.findIndex((m) => (m.pending || m.unknown) && m.clientKey === raw.clientKey)
            : raw.byAi ? -1
              : this.messages.findIndex((m) => (m.pending || m.unknown) && !m.mediaUnknown && !m.mediaPending && m.body === raw.body && after(m));
          if (i !== -1) {
            const wasUnknown = this.messages[i].unknown;
            delete this._blobs[this.messages[i].clientKey];
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
          status: raw.status, failureReason: raw.failureReason || null, failureFix: raw.failureFix || null,
          byAi: !!raw.byAi, media: raw.media || null,
          fresh: true, // arrived while open: it slides in (.cv-msg--fresh)
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
        const fix = data.failureFixes && data.failureFixes[String(m.id)];
        if (fix) m.failureFix = fix;
      }
      this.sortMessages();
      if (data.status_html !== undefined) this.applyStatus(data.status_html);
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
        // A suggested follow-up now exists: the composer says so in the page's words, and
        // the badges re-read the shared count.
        if (extra.kind === 'follow_up' && data.followup_label) {
          this.followupLabel = data.followup_label;
          if (window.htmx) htmx.trigger(document.body, 'followup-changed');
        }
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
      // The pickers (the composer's own once the window has closed, the dialog while it's
      // open) take the proposal from the event.
      const apply = () => window.dispatchEvent(new CustomEvent('ai-template-apply', { detail: { templateId: p.templateId, values: p.values, proposal: p.id } }));
      if (!this.windowOpen) {
        this.tplAnyway = true;
        apply();
        setTimeout(() => { const r = this.$root.querySelector('#template-inline-list input:checked') || this.$root.querySelector('#template-inline-list input'); if (r) r.focus(); }, 50);
        return;
      }
      window.dispatchEvent(new CustomEvent('modal-open', { detail: 'chat-template' }));
      apply();
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
    // A file or voice note that never left (offline, or another send in progress) is kept,
    // so a recording isn't lost. A refused one isn't: sending it again would be refused
    // the same way (too large, wrong type, window closed), so the reason is said instead.
    hold(blob, filename, voice, note) {
      this.held = this.held.concat([{ id: 'h' + Date.now() + Math.random().toString(36).slice(2, 6), blob, filename, voice, note }]);
    },
    sendHeld(h) {
      if (this.sending) return;
      this.held = this.held.filter((x) => x.id !== h.id);
      this.uploadMedia(h.blob, h.filename, h.voice);
    },
    discardHeld(h) { this.held = this.held.filter((x) => x.id !== h.id); },
    // ``again`` ({ key, caption }): "Send again" on an unconfirmed file, with the same key.
    async uploadMedia(blob, filename, voice, again) {
      if (!this.sendMediaUrl) return;
      const what = voice ? 'This voice note' : (filename ? '"' + filename + '"' : 'This file');
      if (this.sending) {
        this.hold(blob, filename, voice, what + " wasn't sent yet: the last message was still sending. It's kept here.");
        return;
      }
      this.sendError = '';
      const form = this.$refs.composer;
      const fd = new FormData();
      fd.set('csrfmiddlewaretoken', new FormData(form).get('csrfmiddlewaretoken'));
      fd.set('file', blob, filename);
      if (voice) fd.set('voice', '1');
      const caption = again ? again.caption : (voice ? '' : this.draft.trim());
      if (caption) fd.set('caption', caption);
      const key = again ? again.key : newKey();
      fd.set('client_key', key);
      const label = voice ? '[Voice message]' : '[' + (filename || 'File') + ']';
      if (!navigator.onLine) {
        this.hold(blob, filename, voice, what + " wasn't sent: you're offline. It's kept here; send it once you're back online.");
        return;
      }
      const pending = { id: 'p' + Date.now(), direction: 'outbound', body: 'Sending ' + label + '…', ts: this.serverTime(), status: '', pending: true, mediaPending: true, clientKey: key };
      this.messages.push(pending);
      this._blobs[key] = { blob, filename, voice };
      if (caption && !again) this.draft = '';
      this.$nextTick(() => this.scrollBottom());
      this.sending = true;
      try {
        const out = await postSend(this.sendMediaUrl, fd, UPLOAD_TIMEOUT);
        if (out.kind === 'ok' && !(out.data && out.data.unconfirmed)) {
          this.messages = this.messages.filter((m) => m.id !== pending.id);
          delete this._blobs[key];
          this.interval = BASE_INTERVAL;
          this.schedule(300);
        } else if (out.kind === 'ok' || out.kind === 'unknown') {
          // The file can't be put back, so the bubble stays to say what is known, and is
          // kept across a reload until dismissed: it may not have gone, and it can't be
          // matched to the thread (see apply). Only a caption can be put back.
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
          delete this._blobs[key];
          if (caption) this.draft = this.draft.trim() ? this.draft + '\n' + caption : caption;
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
      this.sendError = '';
      if (!navigator.onLine) {
        // Nothing left the browser, so the reply simply stays in the box (and the warning
        // about the other reply stays acknowledged, not dismissed: nothing was sent).
        this.sendError = "You're offline, so this wasn't sent. Your reply is still in the box.";
        return;
      }
      this.collision = '';
      this.collisionAck = false;
      this.draft = '';
      await this.deliver(body, newKey());
    },
    // "Send again" on a send that was never confirmed: the same words or file with the same
    // key, so if the first one did go the server returns it instead of sending it twice.
    hasFile(m) { return !!(m.clientKey && this._blobs[m.clientKey]); },
    canSendAgain(m) { return !!m.clientKey && (!m.mediaUnknown || (this.hasFile(m) && !!this.sendMediaUrl)); },
    sendAgain(m) {
      // Everything that could stop the resend is checked before the note is taken away: it
      // is the only record that this may not have gone.
      if (this.sending || !this.canSendAgain(m)) return;
      if (!navigator.onLine) { this.sendError = "You're offline, so it wasn't sent again."; return; }
      this.messages = this.messages.filter((x) => x.id !== m.id);
      this.saveUnknown();
      if (m.mediaUnknown) {
        const f = this._blobs[m.clientKey];
        this.uploadMedia(f.blob, f.filename, f.voice, { key: m.clientKey, caption: m.caption || '' });
      } else {
        this.deliver(m.body, m.clientKey, { again: true });
      }
    },
    // ``again``: "Send again" on an earlier reply, not the one in the box, so the AI
    // suggestion currently in the box isn't recorded as used by it.
    async deliver(body, key, opts) {
      const again = !!(opts && opts.again);
      const form = this.$refs.composer;
      const fd = new FormData(form);
      fd.set('body', body);
      fd.set('client_key', key);
      if (again) fd.delete('ai_proposal');

      const pending = { id: 'p' + Date.now(), direction: 'outbound', body, ts: this.serverTime(), status: '', pending: true, clientKey: key };
      this.messages.push(pending);
      this.$nextTick(() => { this.grow(this.$refs.input); this.scrollBottom(); });
      this.sending = true;

      try {
        const out = await postSend(form.getAttribute('action') || window.location.pathname, fd, SEND_TIMEOUT);
        if (out.kind === 'ok' && !(out.data && out.data.unconfirmed)) {
          this.interval = BASE_INTERVAL;
          this.schedule(300);
          if (!again) {
            if (this.aiUsedId && this.ai.proposal && this.ai.proposal.id === this.aiUsedId) this.ai.proposal.status = 'used';
            this.aiUsedId = null;
          }
        } else if (out.kind === 'ok' || out.kind === 'unknown') {
          // It may have gone (or the server said its first attempt is unconfirmed). The
          // bubble says so and stays until the thread shows the real message (apply() swaps
          // it in) or the person chooses what to do; the text is not put back in the box,
          // where one more Enter would send it again under a new key.
          const i = this.messages.indexOf(pending);
          if (i !== -1) this.messages.splice(i, 1, Object.assign({}, pending, { pending: false, unknown: true, status: 'unknown' }));
          this.saveUnknown();
          this.schedule(0);
        } else {
          this.messages = this.messages.filter((m) => m.id !== pending.id);
          this.draft = this.draft.trim() ? this.draft + '\n' + body : body;
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
    _outside: null, // declared: see chat's note on undeclared fields
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
    _onOther: null, // declared: see chat's note on undeclared fields
    _raf: null,
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

  /* The template picker (_template_form.html): in the composer while the window is closed it
   * opens over the thread (``inline``), so the thread keeps its height; in the dialog it's the
   * dialog's body. Search over names and text, a live preview with the values typed so far,
   * and Send only once a template is chosen and every value it needs is filled. Each option
   * carries its text and parameter names (data-content, data-fields), so nothing is sent
   * twice. An AI suggestion arrives on ai-template-apply (chat.js aiReviewTemplate). */
  Alpine.data('tplPicker', (inline) => ({
    inline: !!inline,
    pick: !inline,
    tpl: '',
    proposal: '',
    q: '',
    vals: {},
    option(id) { return this.$root.querySelector('[data-tpl-option="' + (id || this.tpl) + '"]'); },
    fields(id) {
      const o = this.option(id);
      return o && o.dataset.fields ? o.dataset.fields.split('|') : [];
    },
    shows(text) { const q = this.q.trim().toLowerCase(); return !q || text.includes(q); },
    noMatch() {
      return !!this.q.trim() && ![...this.$root.querySelectorAll('[data-tpl-option]')].some((o) => this.shows(o.dataset.search));
    },
    // As the send fills it (apps.whatsapp.campaign_ui.render_body): the value at position i
    // fills {{i}}, or {{its name}}.
    preview() {
      const o = this.option();
      if (!o) return '';
      const fields = this.fields();
      return o.dataset.content.replace(/\{\{\s*([^}\s]+)\s*\}\}/g, (all, name) => {
        const field = /^\d+$/.test(name) ? fields[Number(name) - 1] : (fields.includes(name) ? name : null);
        if (field === undefined || field === null) return all;
        const v = (this.vals['var__' + this.tpl + '__' + field] || '').trim();
        return v || '[' + (/^\d+$/.test(field) ? 'Value ' + field : field.replace(/_/g, ' ')) + ']';
      });
    },
    ready() {
      return !!this.tpl && this.fields().every((f) => (this.vals['var__' + this.tpl + '__' + f] || '').trim());
    },
    open() {
      this.pick = true;
      // Into the picker: its search, else the chosen (or first) template.
      this.$nextTick(() => {
        const s = this.$refs.tplSearch;
        const r = this.$root.querySelector('[data-tpl-option] input:checked') || this.$root.querySelector('[data-tpl-option] input');
        if (s && s.getClientRects().length) s.focus(); else if (r) r.focus();
      });
    },
    close() {
      if (!this.inline) { this.$dispatch('modal-close', 'chat-template'); return; }
      this.pick = false;
      this.$nextTick(() => { const b = this.$refs.tplOpen; if (b) b.focus(); });
    },
    apply(detail) {
      this.tpl = String(detail.templateId);
      this.proposal = String(detail.proposal || '');
      for (const [k, v] of Object.entries(detail.values || {})) this.vals['var__' + detail.templateId + '__' + k] = v;
      if (this.inline) this.pick = true;
    },
  }));
}

if (window.Alpine && window.Alpine.version) registerChat();
else document.addEventListener('alpine:init', registerChat);
})();
