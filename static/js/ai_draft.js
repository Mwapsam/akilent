// AI setup drafts (apps.ai.drafting): queue one, wait for it, then hand it to the page.
// Usage: x-data="aiDraft({kind: 'automation', createUrl, statusUrl, onReady(draft){...}})".
// The model runs in the background; this only polls. Nothing is saved until the person acts.
(function () {
  function csrf() {
    const el = document.querySelector('[name=csrfmiddlewaretoken]');
    return el ? el.value : '';
  }

  window.aiDraft = function (opts) {
    return {
      busy: false,
      error: '',
      draft: null,
      async start(fields) {
        this.busy = true;
        this.error = '';
        this.draft = null;
        const fd = new FormData();
        fd.set('csrfmiddlewaretoken', csrf());
        fd.set('kind', opts.kind);
        for (const [k, v] of Object.entries(fields || {})) fd.set(k, v == null ? '' : v);
        try {
          const r = await fetch(opts.createUrl, {
            method: 'POST', body: fd, credentials: 'same-origin',
            headers: { 'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json' },
          });
          const data = await r.json().catch(() => ({}));
          if (!r.ok || !data.ok) throw new Error(data.error || 'Something went wrong.');
          await this.wait(data.draft.id);
        } catch (e) {
          this.error = e.message;
          this.busy = false;
        }
      },
      async wait(id) {
        const until = Date.now() + 90000;
        while (Date.now() < until) {
          await new Promise((res) => setTimeout(res, 1500));
          const r = await fetch(opts.statusUrl.replace('0', String(id)), {
            credentials: 'same-origin', headers: { Accept: 'application/json' },
          });
          const data = await r.json().catch(() => ({}));
          const d = data.draft || {};
          if (d.status === 'ready') {
            this.draft = d;
            this.busy = false;
            if (opts.onReady) opts.onReady.call(this, d);
            return;
          }
          if (d.status === 'error') throw new Error(d.error || 'The draft failed. Try again.');
        }
        throw new Error('This is taking too long. Try again in a moment.');
      },
    };
  };
})();
