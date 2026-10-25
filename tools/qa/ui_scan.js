/*
 * UI quality scan — paste into DevTools or run through a browser automation tool.
 *
 *   await window.__uiScan()            // scan the current page
 *   await window.__uiScan({ max: 20 }) // cap the examples listed per check
 *   await window.__uiScan({ win: iframe.contentWindow }) // scan a same-origin frame,
 *                                       // e.g. one sized to a phone viewport
 *
 * Checks (supporting evidence only — this never certifies WCAG conformance):
 *   overflow  page wider than the viewport, and the elements that cause it
 *   contrast  rendered text below 4.5:1 (3:1 for large text), measured against the
 *             EFFECTIVE background: ancestors' colours composited with alpha and
 *             opacity. Text over images/gradients is listed as "manual" and never
 *             counted as a pass.
 *   targets   visible interactive elements smaller than 44x44 CSS px (24x24 is the
 *             WCAG 2.2 AA floor; 44 is this product's own touch rule)
 *   inputs    form controls under 16px font (iOS Safari zooms on focus)
 *
 * Results are deterministic for a given page state, so they can be pasted into
 * docs/design/ui-quality-log.md as evidence.
 */
(function () {
  let W = window;
  function parse(c) {
    const m = c && c.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  }
  function over(top, bottom) {
    const a = top.a + bottom.a * (1 - top.a);
    if (!a) return { r: 0, g: 0, b: 0, a: 0 };
    const mix = (k) => (top[k] * top.a + bottom[k] * bottom.a * (1 - top.a)) / a;
    return { r: mix("r"), g: mix("g"), b: mix("b"), a };
  }
  function lum(c) {
    const f = (v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  }
  function ratio(a, b) {
    const x = lum(a), y = lum(b);
    return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
  }
  // Effective background: composite every ancestor layer, bottom (canvas) first.
  // Returns null when an image or gradient sits behind the text (manual check).
  function background(el) {
    const layers = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const s = W.getComputedStyle(n);
      if (s.backgroundImage && s.backgroundImage !== "none") return null;
      const c = parse(s.backgroundColor);
      const o = parseFloat(s.opacity);
      if (c && c.a > 0) layers.push({ ...c, a: c.a * (isNaN(o) ? 1 : o) });
    }
    let acc = { r: 255, g: 255, b: 255, a: 1 };
    for (let i = layers.length - 1; i >= 0; i--) acc = over(layers[i], acc);
    return acc;
  }
  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const s = W.getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && parseFloat(s.opacity) > 0.01;
  }
  function label(el) {
    let s = el.tagName.toLowerCase();
    if (el.id) s += "#" + el.id;
    const cls = (el.getAttribute("class") || "").trim().split(/\s+/).slice(0, 3).join(".");
    if (cls) s += "." + cls;
    const t = (el.innerText || el.getAttribute("aria-label") || el.value || "").trim().slice(0, 40);
    return t ? s + ' "' + t.replace(/\s+/g, " ") + '"' : s;
  }

  window.__uiScan = async function (opts) {
    W = (opts && opts.win) || window;
    const document = W.document;
    const max = (opts && opts.max) || 12;
    const vw = document.documentElement.clientWidth;
    const out = {
      url: W.location.pathname + W.location.search,
      viewport: W.innerWidth + "x" + W.innerHeight,
      theme: document.documentElement.dataset.theme ||
        (W.matchMedia("(prefers-color-scheme: dark)").matches ? "dark(os)" : "light(os)"),
    };

    // Overflow
    const sw = document.documentElement.scrollWidth;
    const wide = [];
    if (sw > vw) {
      document.querySelectorAll("body *").forEach((el) => {
        if (!visible(el)) return;
        const r = el.getBoundingClientRect();
        if (r.right <= vw + 1 || W.getComputedStyle(el).position === "fixed") return;
        // Content inside a scroll/clip container that itself fits does not widen the
        // page (a wrapped table scrolling sideways in .table-wrap). Counting it was a
        // false positive in the 2026-10-25 baseline.
        for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
          const ox = W.getComputedStyle(a).overflowX;
          if ((ox === "auto" || ox === "scroll" || ox === "hidden" || ox === "clip") && a.getBoundingClientRect().right <= vw + 1) return;
        }
        wide.push(label(el) + " right=" + Math.round(r.right));
      });
    }
    out.overflow = { scrollWidth: sw, clientWidth: vw, offenders: wide.slice(0, max), count: wide.length };

    // Contrast (text-bearing elements)
    const fails = [], manual = [];
    let checked = 0;
    const seen = new Set();
    document.querySelectorAll("body *").forEach((el) => {
      if (!visible(el)) return;
      const own = Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim());
      if (!own) return;
      if (el.closest("[aria-hidden=true], svg")) return;
      const box = el.getBoundingClientRect();
      if (box.width <= 1 || box.height <= 1) return; // sr-only: read by screen readers, never seen
      const s = W.getComputedStyle(el);
      const fg = parse(s.color);
      if (!fg) return;
      const bg = background(el);
      if (!bg) { manual.push(label(el)); return; }
      const ofg = over({ ...fg, a: fg.a * parseFloat(s.opacity) }, bg);
      const r = ratio(ofg, bg);
      const size = parseFloat(s.fontSize), bold = parseInt(s.fontWeight, 10) >= 700;
      const large = size >= 24 || (bold && size >= 18.66);
      const need = large ? 3 : 4.5;
      const disabled = el.closest(":disabled, [aria-disabled=true]");
      checked++;
      if (r < need && !disabled) {
        const key = s.color + "|" + Math.round(r * 100);
        const entry = label(el) + " " + r.toFixed(2) + ":1 (need " + need + ")";
        if (!seen.has(key)) { seen.add(key); fails.push(entry); }
        else fails.push(null);
      }
    });
    out.contrast = { checked, failing: fails.length, examples: fails.filter(Boolean).slice(0, max), manualReview: manual.length, manualExamples: manual.slice(0, 5) };

    // Touch targets
    const small = [];
    document.querySelectorAll("a[href], button, input:not([type=hidden]), select, textarea, [role=button], [tabindex]:not([tabindex='-1'])").forEach((el) => {
      if (!visible(el)) return;
      if (el.closest("p, li") && el.tagName === "A" && !el.className) return; // inline text links are exempt
      const r = el.getBoundingClientRect();
      if (r.width <= 1 || r.height <= 1) return; // visually hidden until focused (skip links)
      if (r.width < 44 || r.height < 44) small.push({ el: label(el), w: Math.round(r.width), h: Math.round(r.height), belowAA: r.width < 24 || r.height < 24 });
    });
    out.targets = { under44: small.length, under24: small.filter((s) => s.belowAA).length, examples: small.filter((s) => s.belowAA).slice(0, max).concat(small.filter((s) => !s.belowAA).slice(0, 4)) };

    // iOS zoom risk
    const tiny = [];
    document.querySelectorAll("input:not([type=hidden]):not([type=checkbox]):not([type=radio]), select, textarea").forEach((el) => {
      if (!visible(el)) return;
      const fs = parseFloat(W.getComputedStyle(el).fontSize);
      if (fs < 16) tiny.push(label(el) + " " + fs + "px");
    });
    out.inputs = { under16px: tiny.length, examples: tiny.slice(0, max) };
    return out;
  };
  return "ui_scan loaded";
})();
