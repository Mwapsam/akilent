/* Live conversation thread: renders messages from a JSON feed, polls for new
 * ones, and sends replies without a page reload. Config comes from a
 * json_script block: { messages, lastId, feedUrl, open }. */
document.addEventListener('alpine:init', () => {
  const STATUS_LABELS = { queued: 'Sending…', sent: 'Sent', delivered: 'Delivered', read: 'Read', failed: 'Failed' };
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
    aiUsedId: null,

    init() {
      this.$nextTick(() => this.scrollBottom(true));
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) { this.interval = BASE_INTERVAL; this.schedule(0); }
      });
      this.schedule();
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
        out.push({ key: 'm-' + m.id, type: 'msg', m, showMeta: true, showFailure: m.status === 'failed' });
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
        });
        this.lastId = Math.max(this.lastId, raw.id);
      }
      for (const m of this.messages) {
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
});
