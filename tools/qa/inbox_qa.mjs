// Phase 1 (inbox) browser QA: drives locally installed Chrome (headless) over the DevTools
// protocol and checks send outcomes, connection notices, the details drawer, pane loading
// and phone layout. Every POST is intercepted, so nothing is sent to a customer.
// Usage: node tools/qa/inbox_qa.mjs <repoRoot> <outDir>   (QA_SCAN=1 contrast scan, QA_HDR=1 header sizes)
// Needs the dev server on :8000 and QA_UI_EMAIL / QA_UI_PASSWORD in .env.
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir] = process.argv.slice(2);
const BASE = "http://127.0.0.1:8000";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9334;
const profile = join(outDir, "chrome-profile-p1");
const shots = join(repo, "docs/design/qa/phase1");
mkdirSync(shots, { recursive: true });

const env = Object.fromEntries(readFileSync(join(repo, ".env"), "utf8").split(/\r?\n/)
  .filter((l) => /^QA_UI_/.test(l)).map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]));
const scanner = readFileSync(join(repo, "tools/qa/ui_scan.js"), "utf8");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });

async function target() {
  for (let i = 0; i < 50; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find((t) => t.type === "page");
      if (page) return page.webSocketDebuggerUrl;
    } catch { /* not up yet */ }
    await sleep(200);
  }
  throw new Error("Chrome did not start");
}

process.on("exit", () => { try { chrome.kill(); } catch {} });
process.on("uncaughtException", (e) => { console.error("FAIL", e.message); process.exit(1); });
process.on("unhandledRejection", (e) => { console.error("FAIL", e && e.message); process.exit(1); });
const ws = new WebSocket(await target());
await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let seq = 0;
const pending = new Map();
const waiters = [];
ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) { const p = pending.get(msg.id); pending.delete(msg.id); msg.error ? p.rej(new Error(JSON.stringify(msg.error))) : p.res(msg.result); }
  else if (msg.method) waiters.filter((w) => w.method === msg.method).forEach((w) => { waiters.splice(waiters.indexOf(w), 1); w.res(msg.params); });
});
const send = (method, params = {}) => new Promise((res, rej) => { const id = ++seq; pending.set(id, { res, rej }); ws.send(JSON.stringify({ id, method, params })); });
const once = (method, timeout = 20000) => new Promise((res, rej) => { const w = { method, res }; waiters.push(w); setTimeout(() => rej(new Error("timeout " + method)), timeout); });
const evaluate = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
};
async function go(path, settle = 1300) {
  process.stderr.write(`go ${path}\n`);
  const loaded = once("Page.loadEventFired");
  await send("Page.navigate", { url: BASE + path });
  await loaded; await sleep(settle);
}
async function viewport(w, h, mobile) {
  await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: mobile ? 3 : 1, mobile });
  await send("Emulation.setTouchEmulationEnabled", mobile ? { enabled: true, maxTouchPoints: 5 } : { enabled: false });
}
async function clickSel(sel) {
  const p = await evaluate(`(() => { const el=document.querySelector(${JSON.stringify(sel)}); if(!el) return null; el.scrollIntoView({block:'center'}); const r=el.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2}; })()`);
  if (!p) throw new Error("no element " + sel);
  for (const type of ["mousePressed", "mouseReleased"]) await send("Input.dispatchMouseEvent", { type, x: p.x, y: p.y, button: "left", clickCount: 1 });
}
async function key(k, code, keyCode) {
  for (const type of ["keyDown", "keyUp"]) await send("Input.dispatchKeyEvent", { type, key: k, code, windowsVirtualKeyCode: keyCode });
}
const chat = (expr) => evaluate(`(() => { const c = Alpine.$data(document.querySelector('.cv')); return (${expr}); })()`);

// ---- Network interception: no POST leaves the browser (nothing is sent to a customer). ----
let rules = [];
const T0 = Date.now();
const log = [];
ws.addEventListener("message", async (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.method !== "Fetch.requestPaused") return;
  const { requestId, request } = msg.params;
  const rule = rules.find((r) => r.match(request));
  const act = rule ? rule.act : (request.method === "POST" ? { status: 200, body: { ok: true } } : null);
  log.push(`${((Date.now() - T0) / 1000).toFixed(1)}s ${request.method} ${new URL(request.url).pathname} -> ${act ? (act.fail || act.status || (act.slowHtml && "slow") || (act.delayFail && "delayFail")) : "pass"}`);
  try {
    if (act && act.delayFail) { await sleep(act.delayFail); await send("Fetch.failRequest", { requestId, errorReason: "ConnectionRefused" }); }
    else if (act && act.slowHtml) { await sleep(act.slowHtml); await send("Fetch.fulfillRequest", { requestId, responseCode: act.status || 200,
      responseHeaders: [{ name: "Content-Type", value: "text/html" }], body: Buffer.from(act.html).toString("base64") }); }
    else if (act && act.delay) { await sleep(act.delay); await send("Fetch.continueRequest", { requestId }); }
    else if (!act) await send("Fetch.continueRequest", { requestId });
    else if (act.fail) await send("Fetch.failRequest", { requestId, errorReason: act.fail });
    else await send("Fetch.fulfillRequest", { requestId, responseCode: act.status,
      responseHeaders: [{ name: "Content-Type", value: act.html !== undefined ? "text/html" : "application/json" }, ...(act.headers || [])],
      body: Buffer.from(act.html !== undefined ? act.html : JSON.stringify(act.body)).toString("base64") });
  } catch (e) { process.stderr.write("fetch handler: " + e.message + "\n"); }
});

await send("Page.enable"); await send("Runtime.enable");
// Always the current scripts and styles: a cached copy would test old code.
await send("Network.enable"); await send("Network.setCacheDisabled", { cacheDisabled: true });
await go("/auth/login/");
const login = once("Page.loadEventFired");
await evaluate(`(() => { const f=document.querySelector('input[type=password]').form; f.querySelector('input[name=username]').value=${JSON.stringify(env.QA_UI_EMAIL)};
  f.querySelector('input[type=password]').value=${JSON.stringify(env.QA_UI_PASSWORD)}; })()`);
await clickSel("input[type=password]");
const btnSel = "form:has(input[type=password]) [type=submit]";
await clickSel(btnSel);
await Promise.race([login, sleep(4000)]); await sleep(800);
await go("/dashboard/");
if ((await evaluate("location.pathname")).startsWith("/auth/login")) throw new Error("login failed");
await evaluate("localStorage.setItem('welcomeTourSeen','1')");
await send("Fetch.enable", { patterns: [{ urlPattern: "*", requestStage: "Request" }] });
if (process.env.QA_HDR) {
  const m = `(() => { const q=(s)=>{const e=document.querySelector(s); if(!e) return null; const r=e.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)];};
    const ctrls=[...document.querySelectorAll('.cv-header a, .cv-header button')].filter(e=>e.offsetParent).map(e=>{const r=e.getBoundingClientRect(); return [e.getAttribute('aria-label')||e.textContent.trim().slice(0,12), Math.round(r.width), Math.round(r.height)];});
    return { coarse: matchMedia('(pointer: coarse)').matches, cssHasRule: [...document.styleSheets].some(ss => { try { return [...ss.cssRules].some(r => r.cssText.includes('2.75rem') && r.cssText.includes('cv-iconbtn')); } catch (e) { return false; } }), cssHref: [...document.querySelectorAll('link[rel=stylesheet]')].map(l => l.href).join(' '), name: q('.cv-h-name'), avatar: q('.cv-h-avatar'), ctrls, hscroll: document.documentElement.scrollWidth - document.documentElement.clientWidth, label: q('.inbox-filter-toggle') }; })()`;
  for (const [w, h] of [[360, 740], [390, 844], [768, 1024]]) {
    await viewport(w, h, true);
    await go("/inbox/?view=all"); const lab = await evaluate(m);
    const href = await evaluate("document.querySelector('.inbox-row').getAttribute('href')");
    await go(href); const r = await evaluate(m);
    console.log(w, JSON.stringify({ ...r, label: lab.label }));
    await send("Page.captureScreenshot", { format: "jpeg", quality: 60 }).then((x) => writeFileSync(join(shots, `header-${w}.jpg`), Buffer.from(x.data, "base64")));
  }
  await viewport(1280, 800, false); await go("/inbox/?view=all"); console.log(1280, JSON.stringify((await evaluate(m)).label));
  process.exit(0);
}
if (process.env.QA_SCAN) {
  const NO_MOTION = `(() => { const s=document.createElement('style'); s.textContent='*,*::before,*::after{transition:none!important;animation:none!important}'; document.head.appendChild(s); })()`;
  const out = [];
  const states = `(() => { const c = Alpine.$data(document.querySelector('.cv')); c.sendError = "The 24-hour window is closed. Your reply is still in the box.";
    c.connection = 'lost'; c.messages.push({ id: 'pq1', direction: 'outbound', body: 'QA unconfirmed', ts: new Date().toISOString(), status: 'unknown', unknown: true });
    c.messages.push({ id: 'pq2', direction: 'outbound', body: 'QA failed', ts: new Date().toISOString(), status: 'failed', failureReason: 'The customer has not written in 24 hours.' }); })()`;
  const scan = async (label) => {
    for (const theme of ["light", "dark"]) {
      const r = await evaluate(`(async () => { document.documentElement.dataset.theme=${JSON.stringify(theme)}; await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))); return await window.__uiScan({max:8}); })()`);
      out.push({ label, theme, overflow: r.overflow.count, contrastFail: r.contrast.failing, checked: r.contrast.checked, ex: r.contrast.examples, tex: r.targets.examples, under44: r.targets.under44, inputsUnder16: r.inputs.under16px });
      writeFileSync(join(shots, `scan-${label}-${theme}.jpg`), Buffer.from((await send("Page.captureScreenshot", { format: "jpeg", quality: 60 })).data, "base64"));
    }
  };
  mkdirSync(shots, { recursive: true });
  await viewport(390, 844, true);
  await go("/inbox/?view=all"); await evaluate(scanner); await evaluate(NO_MOTION); await scan("inbox-390");
  const href = await evaluate("document.querySelector('.inbox-row').getAttribute('href')");
  await go(href); await evaluate(scanner); await evaluate(NO_MOTION); await evaluate(states); await sleep(300);
  await evaluate("document.querySelector('.cv-thread').scrollTop = 1e6"); await sleep(200); await scan("thread-states-390");
  await viewport(1280, 800, false);
  await go("/inbox/?view=all", 1500); await evaluate(scanner); await evaluate(NO_MOTION);
  await clickSel(".inbox-row"); await sleep(2000); await evaluate(states); await sleep(300); await scan("pane-states-1280");
  writeFileSync(join(outDir, "inbox_scan.json"), JSON.stringify(out, null, 1));
  console.log(JSON.stringify(out.map(o => ({ l: o.label, t: o.theme, of: o.overflow, cf: o.contrastFail, ck: o.checked, u44: o.under44, i16: o.inputsUnder16 }))));
  process.exit(0);
}

if (process.env.QA_FLICKER) {
  const watch = `(async () => {
    const muts = []; const shifts = []; const ev = []; let vt = 0;
    new MutationObserver(ms => { for (const m of ms) { const t = m.target; muts.push(m.type + ':' + (t.id || (t.className && t.className.toString()) || t.nodeName || '').slice(0, 50) + ':' + (m.attributeName || '') + ':' + [...m.addedNodes].map(n => n.nodeName).join(',')); } })
      .observe(document.documentElement, { subtree: true, childList: true, attributes: true, characterData: true });
    try { new PerformanceObserver(l => { for (const e of l.getEntries()) shifts.push(+e.value.toFixed(4)); }).observe({ type: 'layout-shift', buffered: false }); } catch (e) {}
    const origVT = document.startViewTransition && document.startViewTransition.bind(document);
    if (origVT) document.startViewTransition = function () { vt++; return origVT.apply(document, arguments); };
    ['htmx:beforeRequest', 'htmx:afterSwap'].forEach(n => document.addEventListener(n, e => ev.push(Math.round(performance.now() / 100) / 10 + 's ' + n + ' ' + ((e.detail.requestConfig || {}).path || '').replace(location.origin, '') + ' ' + (e.detail.xhr ? e.detail.xhr.status : ''))));
    await new Promise(r => setTimeout(r, 20000));
    const c = {}; for (const m of muts) c[m] = (c[m] || 0) + 1;
    return { mutations: muts.length, top: Object.entries(c).sort((a, b) => b[1] - a[1]).slice(0, 12), shiftCount: shifts.length, shiftSum: +shifts.reduce((a, b) => a + b, 0).toFixed(4), viewTransitions: vt, events: ev.slice(0, 14) };
  })()`;
  for (const [w, h, mob] of [[1280, 800, false], [390, 844, true]]) {
    await viewport(w, h, mob);
    await go("/inbox/?view=all", 1500);
    console.log("LIST", w, JSON.stringify(await evaluate(watch)));
    const href = await evaluate("document.querySelector('.inbox-row').getAttribute('href')");
    if (w === 1280) { await clickSel(".inbox-row"); await sleep(2500); } else { await go(href, 1500); }
    console.log("THREAD", w, JSON.stringify(await evaluate(watch)));
  }
  process.exit(0);
}
const results = [];
const check = (id, pass, detail) => { results.push({ id, pass: !!pass, detail }); process.stderr.write(`${pass ? "PASS" : "FAIL"} ${id} ${detail ? JSON.stringify(detail) : ""}\n`); };
mkdirSync(shots, { recursive: true });
const shot = async (name) => writeFileSync(join(shots, name + ".jpg"), Buffer.from((await send("Page.captureScreenshot", { format: "jpeg", quality: 60 })).data, "base64"));
const toasts = () => evaluate("Alpine.store('toasts').items.map(t => t.type + ': ' + t.message)");
const visible = (sel) => evaluate(`[...document.querySelectorAll(${JSON.stringify(sel)})].filter(e => e.offsetParent).map(e => e.innerText.replace(/\\s+/g, ' ').trim())`);

// Find an open conversation whose 24h window is open.
await viewport(390, 844, true);
await go("/inbox/?view=all");
const rows = await evaluate("[...document.querySelectorAll('.inbox-row')].map(a => a.getAttribute('href'))");
let conv = null;
for (const href of rows.slice(0, 12)) {
  await go(href, 900);
  const cfg = await evaluate("JSON.parse(document.getElementById('chat-config').textContent)");
  if (cfg.open && cfg.windowOpen !== false) { conv = href; break; }
}
if (!conv) conv = rows[0];
check("setup.conversation", !!conv, { conv, rows: rows.length });
const detailPath = conv;
const isSend = (r) => r.method === "POST" && new URL(r.url).pathname === detailPath;
await evaluate("sessionStorage.clear()");
await go(detailPath);

async function typeAndSend(text) {
  await chat(`c.draft = ${JSON.stringify(text)}`);
  await sleep(150);
  await chat("c.send()");
}

// 1. Refused (4xx with a reason): nothing sent, text back in the box, reason beside it.
rules = [{ match: isSend, act: { status: 400, body: { ok: false, error: "The 24-hour window is closed." } } }];
await typeAndSend("QA refused reply");
await sleep(700);
let st = await chat("({ draft: c.draft, err: c.sendError, bubbles: c.messages.filter(m => m.body === 'QA refused reply').length })");
let vis = await visible(".cv-send-error");
check("send.refused", st.draft === "QA refused reply" && st.err.includes("24-hour window") && st.bubbles === 0 && vis.length === 1, { st, vis });
await shot("send-refused-390");
await chat("(c.sendError = '', c.draft = '')");

// 2. Server error (5xx): may have gone -> unconfirmed bubble, text NOT back in the box.
rules = [{ match: isSend, act: { status: 500, html: "<h1>Server Error</h1>" } }];
await typeAndSend("QA unknown reply");
await sleep(700);
st = await chat("({ draft: c.draft, err: c.sendError, m: c.messages.filter(m => m.body === 'QA unknown reply').map(m => ({ unknown: m.unknown, status: m.status, pending: m.pending })) })");
let failText = await visible(".cv-fail.is-unknown");
check("send.server-error-unknown", st.draft === "" && st.m.length === 1 && st.m[0].unknown && !st.m[0].pending && failText.some((t) => t.includes("Not confirmed") && t.includes("Check now") && t.includes("Put back in reply box") && !t.includes("Edit & resend")), { st, failText });
await shot("send-unknown-390");

// 3. It survives a reload until the thread shows it.
rules = [];
await go(detailPath);
st = await chat("c.messages.filter(m => m.body === 'QA unknown reply').map(m => m.unknown)");
check("send.unknown-survives-reload", st.length === 1 && st[0] === true, st);

// 4. The real message arriving reconciles it (as if it had gone after all).
await chat(`c.apply({ messages: [{ id: 99999999, direction: 'outbound', body: 'QA unknown reply', timestamp: new Date().toISOString(), status: 'sent' }], statuses: {}, open: true })`);
await sleep(200);
st = await chat("({ unknown: c.messages.filter(m => m.unknown).length, real: c.messages.filter(m => m.id === 99999999).length, stored: sessionStorage.getItem('inbox-unconfirmed:' + JSON.parse(document.getElementById('chat-config').textContent).id) })");
check("send.unknown-reconciles", st.unknown === 0 && st.real === 1 && !st.stored, st);
await go(detailPath); // drop the fake message

// 5. Dropped connection mid-send: unknown too.
rules = [{ match: isSend, act: { fail: "ConnectionReset" } }];
await typeAndSend("QA dropped reply");
await sleep(700);
st = await chat("({ m: c.messages.filter(m => m.body === 'QA dropped reply').map(m => m.unknown), draft: c.draft })");
check("send.network-drop-unknown", st.m.length === 1 && st.m[0] === true && st.draft === "", st);
// Put back knowingly -> text in the box, bubble gone.
await clickSel(".cv-fail.is-unknown .cv-fail-actions button:nth-child(2)");
await sleep(300);
st = await chat("({ draft: c.draft, left: c.messages.filter(m => m.unknown).length })");
check("send.put-back", st.draft === "QA dropped reply" && st.left === 0, st);
await chat("(c.draft = '')");

// 6. Offline: nothing leaves, so the reply just stays in the box.
rules = [];
await send("Network.enable");
await send("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
await sleep(300);
await typeAndSend("QA offline reply");
await sleep(400);
st = await chat("({ draft: c.draft, err: c.sendError, bubbles: c.messages.filter(m => m.body === 'QA offline reply').length })");
check("send.offline-kept", st.draft === "QA offline reply" && st.err.startsWith("You're offline") && st.bubbles === 0, st);
// The thread's own poll fails while offline: it says so, once.
await sleep(9000);
st = await visible(".cv-conn");
check("thread.offline-notice", st.length === 1 && st[0].startsWith("You're offline"), st);
await shot("thread-offline-390");
await send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
await sleep(3000);
st = await visible(".cv-conn");
check("thread.reconnects", st.length === 0, st);
await chat("(c.draft = '', c.sendError = '')");

// 7. Server unreachable while online: "Not connected" after two misses, gone on recovery.
const feedPath = await evaluate("new URL(JSON.parse(document.getElementById('chat-config').textContent).feedUrl, location.href).pathname");
rules = [{ match: (r) => r.method === "GET" && new URL(r.url).pathname === feedPath, act: { status: 502, html: "bad gateway" } }];
await sleep(11000);
st = await visible(".cv-conn");
check("thread.server-unreachable", st.length === 1 && st[0].startsWith("Not connected"), st);
rules = [];
await chat("c.checkNow()");
await sleep(1500);
st = await visible(".cv-conn");
check("thread.recovers", st.length === 0, st);

// 8. Details drawer at phone width: dialog contract.
await evaluate("document.querySelector('.cv-info-btn').focus()");
await clickSel(".cv-info-btn");
await sleep(500);
st = await evaluate(`(() => { const a=document.getElementById('chat-info'); return { role: a.getAttribute('role'), modal: a.getAttribute('aria-modal'), focusIn: a.contains(document.activeElement), paneInert: document.querySelector('.cv-pane').inert, depth: akDialog.depth }; })()`);
check("drawer.opens-as-dialog", st.role === "dialog" && st.modal === "true" && st.focusIn && st.paneInert && st.depth === 1, st);
await shot("drawer-open-390");
await key("Escape", "Escape", 27);
await sleep(400);
st = await evaluate(`(() => ({ open: Alpine.$data(document.querySelector('.cv')).infoOpen, focusBack: document.activeElement === document.querySelector('.cv-info-btn'), inert: document.querySelectorAll('[data-dialog-inert]').length, depth: akDialog.depth }))()`);
check("drawer.esc-closes-restores", !st.open && st.focusBack && st.inert === 0 && st.depth === 0, st);
await clickSel(".cv-info-btn"); await sleep(500);
const bp = await evaluate("(() => { const b=document.querySelector('.cv-backdrop'); const r=b.getBoundingClientRect(); return {x:r.left+10,y:r.top+r.height/2}; })()");
for (const type of ["mousePressed", "mouseReleased"]) await send("Input.dispatchMouseEvent", { type, x: bp.x, y: bp.y, button: "left", clickCount: 1 });
await sleep(400);
st = await evaluate("({ open: Alpine.$data(document.querySelector('.cv')).infoOpen, depth: akDialog.depth, inert: document.querySelectorAll('[data-dialog-inert]').length })");
check("drawer.backdrop-closes", !st.open && st.depth === 0 && st.inert === 0, st);

// 9. Phone layout: reply box on screen, no sideways scroll, back link, 16px input.
const layout = `(() => { const r=document.querySelector('.cv-composer').getBoundingClientRect(); const back=document.querySelector('.cv-h-back'); return { composerBottom: Math.round(r.bottom), vh: innerHeight, hscroll: document.documentElement.scrollWidth - document.documentElement.clientWidth, back: !!back && getComputedStyle(back).display !== 'none', fontSize: getComputedStyle(document.querySelector('.cv-input')).fontSize }; })()`;
st = await evaluate(layout);
check("phone.layout-390", st.composerBottom <= st.vh && st.hscroll <= 0 && st.back && parseFloat(st.fontSize) >= 16, st);
await viewport(360, 740, true); await go(detailPath);
st = await evaluate(layout);
check("phone.layout-360", st.composerBottom <= st.vh && st.hscroll <= 0 && st.back && parseFloat(st.fontSize) >= 16, st);
await shot("thread-360");

// 9b. Phone list <-> thread: an unsent draft survives going back to the list and returning,
// and the browser's Back (Android Back) returns to the list.
await go("/inbox/?view=all");
await clickSel(`.inbox-row[href="${detailPath}"]`);
await sleep(1800);
await chat("(c.draft = 'QA draft kept across panes')");
await sleep(200);
await send("Runtime.evaluate", { expression: "history.back()" });
for (let waited = 0; waited < 6000; waited += 300) {
  await sleep(300);
  if (await evaluate("location.pathname === '/inbox/' && !!document.querySelector('.inbox-list')")) break;
}
st = { path: await evaluate("location.pathname"), list: await evaluate("!!document.querySelector('.inbox-list')") };
check("phone.back-returns-to-list", st.path === "/inbox/" && st.list, st);
await clickSel(`.inbox-row[href="${detailPath}"]`);
await sleep(1800);
st = await chat("c.draft");
check("phone.draft-preserved", st === "QA draft kept across panes", st);
await chat("(c.draft = '')");

// 10. Desktop two-pane: list refresh failure is said once, with no toast pile-up.
await viewport(1280, 800, false);
await go("/inbox/?view=all", 1500);
await evaluate("Alpine.store('toasts').items.splice(0)");
const feedBefore = log.filter((l) => l.includes("/inbox/feed")).length;
rules = [{ match: (r) => r.method === "GET" && new URL(r.url).pathname.startsWith("/inbox/feed"), act: { status: 503, html: "down" } }];
await sleep(18500); // two 8-second refreshes
st = { toasts: await toasts(), notice: (await visible(".inbox-list-col > .alert-warning")).length, stale: await evaluate("document.getElementById('inbox-body').hasAttribute('data-stale')"), feedCalls: log.filter((l) => l.includes("/inbox/feed")).length - feedBefore };
check("list.refresh-failure-quiet", st.toasts.length === 0 && st.notice === 1 && st.stale && st.feedCalls >= 2, st);
await shot("list-stale-1280");
rules = [];
await sleep(9000);
st = { notice: (await visible(".inbox-list-col > .alert-warning")).length, stale: await evaluate("document.getElementById('inbox-body').hasAttribute('data-stale')") };
check("list.refresh-recovers", st.notice === 0 && !st.stale, st);

// 11. A conversation that can't load opens an error in the pane, not a dead page.
const hrefs = await evaluate("[...document.querySelectorAll('.inbox-row')].map(a => a.getAttribute('href'))");
const [a, b] = hrefs;
await clickSel(`.inbox-row[href="${b}"]`);
await sleep(1800);
st = await evaluate(`(() => ({ path: location.pathname, loaded: !!document.querySelector('#conversation-pane-inner .cv'), split: !!document.getElementById('inbox-split') }))()`);
check("pane.plain-click-loads", st.loaded && st.split && st.path === b, { ...st, log: log.slice(-5) });
if (!st.split) await go("/inbox/?view=all", 1500);
rules = [{ match: (r) => r.method === "GET" && new URL(r.url).pathname === a, act: { fail: "ConnectionRefused" } }];
await clickSel(`.inbox-row[href="${a}"]`);
await sleep(1500);
st = await evaluate(`(() => ({ path: location.pathname, err: !!document.querySelector('#conversation-pane-inner [data-error-state]'), title: document.querySelector('#conversation-pane-inner [data-error-title]')?.textContent, loading: document.getElementById('inbox-split').classList.contains('is-loading'), toasts: Alpine.store('toasts').items.length }))()`);
check("pane.load-failure-in-place", st.path.startsWith("/inbox/") && !!(await evaluate("!!document.getElementById('inbox-split')")) && st.err && !st.loading && st.toasts === 0, st);
await shot("pane-error-1280");
rules = [];
await clickSel("#conversation-pane-inner [data-error-retry]");
await sleep(1800);
st = await evaluate(`(() => ({ path: location.pathname, loaded: !!document.querySelector('#conversation-pane-inner .cv'), split: !!document.getElementById('inbox-split'), pane: (document.getElementById('conversation-pane-inner')||{}).innerHTML?.slice(0, 160) }))()`);
check("pane.retry-loads", st.loaded && st.path === a, { ...st, log: log.slice(-6) });

// 12. Race: click B then C quickly; the last one wins, title and URL included.
const c3 = hrefs[2] || hrefs[0];
// B answers slowly, so it arrives after C: C must still be what is shown.
rules = [{ match: (r) => r.method === "GET" && new URL(r.url).pathname === b, act: { delay: 1500 } }];
await clickSel(`.inbox-row[href="${b}"]`);
await clickSel(`.inbox-row[href="${c3}"]`);
await sleep(3500);
rules = [];
st = await evaluate(`(() => ({ path: location.pathname, current: document.querySelector('.inbox-row[aria-current="true"]')?.getAttribute('href'), title: document.title, paneTitle: document.querySelector('#conversation-pane-inner [data-title]')?.getAttribute('data-title') }))()`);
check("pane.last-click-wins", st.path === c3 && st.current === c3 && !!st.paneTitle && st.title.startsWith(st.paneTitle), { ...st, b, c3, log: log.slice(-5) });

// 13. Wide pane: details are a column, never a dialog.
st = await evaluate(`(() => ({ role: document.getElementById('chat-info').getAttribute('role'), inert: document.querySelectorAll('[data-dialog-inert]').length }))()`);
check("pane.details-column-not-dialog", st.role === null && st.inert === 0, st);

// 14. Forms inside the pane that post elsewhere (tags) still re-render it. A real POST to
// the local contact: it only adds a tag to a QA contact.
const tagName = "qa-tag-" + Date.now().toString(36);
rules = [{ match: (r) => r.method === "POST" && /\/contacts\/[^/]+\/tags/.test(new URL(r.url).pathname), act: null }];
await evaluate(`document.getElementById('conversation-add-tag').value = ${JSON.stringify(tagName)}`);
await evaluate("document.getElementById('conversation-add-tag').form.requestSubmit()");
await sleep(2000);
rules = [];
st = await evaluate(`(() => ({ shown: document.querySelector('#conversation-pane-inner')?.textContent.includes(${JSON.stringify(tagName)}), path: location.pathname, split: !!document.getElementById('inbox-split') }))()`);
check("pane.in-pane-form-swaps", st.shown && st.split, st);

// 15. A file whose outcome is unknown re-renders as Not confirmed, and reconciles with the
// media message once it shows up.
const mediaPath = await evaluate("new URL(JSON.parse(document.getElementById('chat-config').textContent).sendMediaUrl || '/none/', location.href).pathname");
rules = [{ match: (r) => r.method === "POST" && new URL(r.url).pathname === mediaPath, act: { status: 502, html: "bad gateway" } }];
await chat("(c.uploadMedia(new Blob(['x'], { type: 'image/png' }), 'qa.png', false), 0)");
await sleep(1200);
rules = [];
st = { fail: await visible("#conversation-pane-inner .cv-fail.is-unknown"), m: await chat("c.messages.filter(m => m.mediaUnknown).map(m => ({ body: m.body, pending: m.pending }))") };
check("media.unknown-renders", st.m.length === 1 && !st.m[0].pending && st.fail.some((t) => t.includes("Not confirmed") && t.includes("Dismiss") && !t.includes("Put back")), st);
await chat(`c.apply({ messages: [{ id: 99999998, direction: 'outbound', body: '', timestamp: new Date().toISOString(), status: 'sent', media: { state: 'ready', kind: 'image', url: '/static/favicon.ico' } }], statuses: {}, open: true })`);
await sleep(200);
st = { left: await chat("c.messages.filter(m => m.mediaUnknown).length"), text: await visible("#conversation-pane-inner .cv-fail.is-unknown") };
check("media.unknown-not-matched-to-any-file", st.left === 1 && st.text.some((t) => t.includes("Check the thread above")), st);
await chat("(c.messages.filter(m => m.mediaUnknown).forEach(m => c.dismissUnknown(m)), c.messages = c.messages.filter(m => m.id !== 99999998), 0)");

// 16. Closing the pane (Back to the inbox) while a load is in flight: the late answer never
// lands and pushes no history entry.
await go("/inbox/?view=all", 1500);
const hs = await evaluate("[...document.querySelectorAll('.inbox-row')].map(a => a.getAttribute('href'))");
await clickSel(`.inbox-row[href="${hs[0]}"]`); await sleep(1800);
rules = [{ match: (r) => r.method === "GET" && new URL(r.url).pathname === hs[1], act: { delay: 1500 } }];
await clickSel(`.inbox-row[href="${hs[1]}"]`); await sleep(100);
await send("Runtime.evaluate", { expression: "history.back()" });
await sleep(2500);
rules = [];
st = await evaluate(`(() => ({ path: location.pathname, open: document.getElementById('inbox-split').classList.contains('has-conversation'), pane: document.getElementById('conversation-pane-inner').innerHTML.trim().length, len: history.length }))()`);
check("pane.close-drops-late-load", st.path === "/inbox/" && !st.open && st.pane === 0, st);

// 17. Unconfirmed sends restored from storage keep time order.
await viewport(390, 844, true);
await go(detailPath);
const convId = await evaluate("JSON.parse(document.getElementById('chat-config').textContent).id");
await evaluate(`sessionStorage.setItem('inbox-unconfirmed:${convId}', JSON.stringify([{ id: 'pold', body: 'QA old unconfirmed', ts: new Date(Date.now() - 864e5 * 3).toISOString() }]))`);
await go(detailPath);
st = await chat("(() => { const ts = c.messages.map(m => new Date(m.ts).getTime()); return { sorted: ts.every((t, i) => i === 0 || ts[i - 1] <= t), idx: c.messages.findIndex(m => m.id === 'pold'), n: c.messages.length }; })()");
check("send.restored-in-time-order", st.sorted && st.idx === 0, st);
await chat("c.dismissUnknown(c.messages.find(m => m.id === 'pold'))");

// 18. A one-off load that fails is still said (it is not a background refresh).
await viewport(1280, 800, false);
rules = [{ match: (r) => r.method === "GET" && /panels/.test(new URL(r.url).pathname), act: { status: 500, html: "<h1>Server Error</h1>" } }];
await go("/dashboard/", 2500);
rules = [];
st = { toasts: await toasts(), regionError: await evaluate("!!document.querySelector('#dashboard-panels [data-error-state]')") };
check("load.one-off-failure-said", st.toasts.length > 0 || st.regionError, st);

// ---- Review round 2 ----
// R1. A server-pushed refresh still shows what the server chose to say (follow-ups due),
// and its failure stays quiet.
await viewport(1280, 800, false);
await go("/inbox/?view=all", 1500);
await evaluate("Alpine.store('toasts').items.splice(0)");
rules = [{ match: (r) => new URL(r.url).pathname === "/qa-push/", act: { status: 200, html: "", headers: [{ name: "X-Toast", value: "info|You have 2 follow-ups due" }] } }];
await evaluate(`(() => { const d = document.createElement('div'); d.id = 'qa-push'; d.hidden = true; d.setAttribute('hx-get', '/qa-push/'); d.setAttribute('hx-trigger', 'sse:qa.push'); d.setAttribute('hx-swap', 'none'); d.setAttribute('data-auto-refresh', ''); document.body.appendChild(d); htmx.process(d); htmx.trigger(d, 'sse:qa.push'); })()`);
await sleep(1200);
st = await toasts();
check("auto.server-toast-still-shown", st.some((t) => t.startsWith("info: You have 2 follow-ups due")), st);
await evaluate("Alpine.store('toasts').items.splice(0)");
rules = [{ match: (r) => new URL(r.url).pathname === "/qa-push/", act: { status: 500, html: "x" } }];
await evaluate("htmx.trigger(document.getElementById('qa-push'), 'sse:qa.push')");
await sleep(1200);
st = { toasts: await toasts(), stale: await evaluate("document.getElementById('qa-push').hasAttribute('data-stale')") };
check("auto.push-failure-quiet", st.toasts.length === 0 && st.stale, st);

// R5. A request from code with a non-refresh source is the user's: its failure is said.
// (htmx.ajax sends no event, so an opted-in element as source would count as a refresh.)
rules = [{ match: (r) => new URL(r.url).pathname === "/qa-ajax/", act: { status: 500, html: "x" } }];
await evaluate("Alpine.store('toasts').items.splice(0)");
await evaluate("htmx.ajax('GET', '/qa-ajax/', { source: document.getElementById('conversation-pane-inner'), target: '#qa-push', swap: 'none' })");
await sleep(1200);
st = await toasts();
check("auto.code-request-not-silenced", st.length > 0, st);
rules = [];

// R6. Coming back online refreshes the list as a background refresh: the stale notice clears.
rules = [{ match: (r) => r.method === "GET" && new URL(r.url).pathname.startsWith("/inbox/feed"), act: { status: 503, html: "down" } }];
await sleep(17500);
const staleBefore = (await visible(".inbox-list-col > .alert-warning")).length;
rules = [];
await evaluate("Alpine.store('toasts').items.splice(0)");
await evaluate("window.dispatchEvent(new Event('online'))");
await sleep(2000);
st = { staleBefore, notice: (await visible(".inbox-list-col > .alert-warning")).length, toasts: (await toasts()).filter((t) => t.startsWith("danger")) };
check("list.online-refresh-clears-notice", st.staleBefore === 1 && st.notice === 0 && st.toasts.length === 0, st);

// R2. A row whose load failed opens when clicked again.
const rh = await evaluate("[...document.querySelectorAll('.inbox-row')].map(a => a.getAttribute('href'))");
rules = [{ match: (r) => r.method === "GET" && new URL(r.url).pathname === rh[0], act: { fail: "ConnectionRefused" } }];
await clickSel(`.inbox-row[href="${rh[0]}"]`); await sleep(1500);
rules = [];
await clickSel(`.inbox-row[href="${rh[0]}"]`); await sleep(2000);
st = await evaluate(`(() => ({ path: location.pathname, loaded: !!document.querySelector('#conversation-pane-inner .cv') }))()`);
check("pane.failed-row-reclickable", st.loaded && st.path === rh[0], st);

// R7. A form inside the pane failing while another conversation loads doesn't break the load.
rules = [
  { match: (r) => r.method === "GET" && new URL(r.url).pathname === rh[1], act: { delay: 1500 } },
  { match: (r) => r.method === "POST" && /\/tags\//.test(new URL(r.url).pathname), act: { status: 500, html: "x" } },
];
await evaluate("document.getElementById('conversation-add-tag').value = 'qa-fail'");
await clickSel(`.inbox-row[href="${rh[1]}"]`); await sleep(100);
await evaluate("document.getElementById('conversation-add-tag')?.form.requestSubmit()");
await sleep(2800);
rules = [];
st = await evaluate(`(() => ({ path: location.pathname, loaded: !!document.querySelector('#conversation-pane-inner .cv'), loading: document.getElementById('inbox-split').classList.contains('is-loading') }))()`);
check("pane.form-error-doesnt-fail-load", st.loaded && !st.loading && st.path === rh[1], st);

// R3. An AI reply with the same words doesn't stand in for an unconfirmed send.
await chat(`(c.messages.push({ id: 'pai', direction: 'outbound', body: 'Thanks!', ts: new Date().toISOString(), status: 'unknown', unknown: true }), 0)`);
await chat(`c.apply({ messages: [{ id: 99999997, direction: 'outbound', body: 'Thanks!', timestamp: new Date().toISOString(), status: 'sent', byAi: true }], statuses: {}, open: true })`);
st = await chat("c.messages.filter(m => m.id === 'pai').length");
check("send.ai-reply-not-matched", st === 1, st);
await chat("c.dismissUnknown(c.messages.find(m => m.id === 'pai'))");

// R4. A file's caption survives an unknown outcome: shown, put back, kept across reload.
const mp = await evaluate("new URL(JSON.parse(document.getElementById('chat-config').textContent).sendMediaUrl || '/none/', location.href).pathname");
rules = [{ match: (r) => r.method === "POST" && new URL(r.url).pathname === mp, act: { status: 504, html: "timeout" } }];
await chat("(c.draft = 'Here is your invoice, due Friday', c.uploadMedia(new Blob(['x'], { type: 'application/pdf' }), 'invoice.pdf', false), 0)");
await sleep(1200);
rules = [];
st = { draft: await chat("c.draft"), fail: await visible("#conversation-pane-inner .cv-fail.is-unknown") };
check("media.caption-kept-on-unknown", st.draft === "" && st.fail.some((t) => t.includes("Put caption back in reply box")), st);
const here = await evaluate("location.pathname");
await go(here, 2000);
st = await chat("c.messages.filter(m => m.mediaUnknown).map(m => m.caption)");
check("media.caption-survives-reload", st.length === 1 && st[0] === "Here is your invoice, due Friday", st);
await chat("c.restoreUnknown(c.messages.find(m => m.mediaUnknown))");
st = await chat("c.draft");
check("media.caption-put-back", st === "Here is your invoice, due Friday", st);
await chat("(c.draft = '')");

// R8. A feed payload the client can't handle is not reported as a lost connection.
const fp = await evaluate("new URL(JSON.parse(document.getElementById('chat-config').textContent).feedUrl, location.href).pathname");
rules = [{ match: (r) => r.method === "GET" && new URL(r.url).pathname === fp, act: { status: 200, body: { unexpected: true } } }];
await sleep(10000);
rules = [];
st = await visible(".cv-conn");
check("thread.client-bug-not-connection", st.length === 0, st);

// ---- Review round 3 ----
// S1. The page carries the server's clock, and a send from a browser whose clock is 2 minutes
// fast is still matched to the real message (no duplicate stuck on Sending…).
st = await chat("({ has: !!JSON.parse(document.getElementById('chat-config').textContent).serverNow, skew: Math.abs(c._skew) })");
check("send.server-clock-known", st.has && st.skew < 10000, st);
await chat("(c._skew = -120000, 0)");
await typeAndSend("QA skewed clock reply");
await sleep(600);
await chat(`c.apply({ messages: [{ id: 99999996, direction: 'outbound', body: 'QA skewed clock reply', timestamp: new Date(Date.now() - 120000 + 1000).toISOString(), status: 'sent' }], statuses: {}, open: true })`);
st = await chat("c.messages.filter(m => m.body === 'QA skewed clock reply').map(m => ({ pending: !!m.pending, unknown: !!m.unknown }))");
check("send.fast-clock-reconciles", st.length === 1 && !st[0].pending && !st[0].unknown, st);
await chat("(c._skew = 0, c.messages = c.messages.filter(m => m.id !== 99999996), 0)");

// S2. A poll answered by the sign-in page says the user is signed out, not "Not connected",
// and clears once the feed answers again.
const loginPath = await chat("new URL(JSON.parse(document.querySelector('#conversation-pane-inner #chat-config, #chat-config').textContent).loginUrl, location.href).pathname");
rules = [
  { match: (r) => r.method === "GET" && new URL(r.url).pathname === fp, act: { status: 302, html: "", headers: [{ name: "Location", value: loginPath + "?next=/inbox/" }] } },
  { match: (r) => r.method === "GET" && new URL(r.url).pathname === loginPath, act: { status: 200, html: "<!doctype html><title>Sign in</title><form></form>" } },
];
await chat("c.checkNow()");
await sleep(1500);
st = await visible(".cv-conn");
check("thread.signed-out-said", st.length === 1 && st[0].startsWith("You've been signed out"), { st, loginPath });
rules = [];
await chat("c.checkNow()");
await sleep(1500);
st = await visible(".cv-conn");
check("thread.signed-in-again-clears", st.length === 0, st);

// S3. A file with no caption whose outcome is unknown: nothing to put back, so the bubble stays.
await chat(`(c.messages.push({ id: 'pvoice', direction: 'outbound', body: '[Voice message]', ts: c.serverTime(), status: 'unknown', unknown: true, mediaUnknown: true, caption: '' }), 0)`);
await chat("c.restoreUnknown(c.messages.find(m => m.id === 'pvoice'))");
st = { left: await chat("c.messages.filter(m => m.id === 'pvoice').length"), draft: await chat("c.draft") };
check("media.no-caption-put-back-noop", st.left === 1 && st.draft === "", st);
await chat("c.dismissUnknown(c.messages.find(m => m.id === 'pvoice'))");

// S4. A form inside the pane answering after the person moved to another conversation never
// replaces it: sent before the click, and sent while the next one was still loading.
await viewport(1280, 800, false);
await go("/inbox/?view=all", 1500);
const sh = await evaluate("[...document.querySelectorAll('.inbox-row')].map(a => a.getAttribute('href'))");
const paneId = () => evaluate("(() => { const e = document.querySelector('#conversation-pane-inner #chat-config'); return e ? JSON.parse(e.textContent).id : null; })()");
await clickSel(`.inbox-row[href="${sh[0]}"]`); await sleep(1800);
const idA = await paneId();
rules = [{ match: (r) => r.method === "POST" && /\/tags/.test(new URL(r.url).pathname), act: { slowHtml: 1800, html: "<div data-title=\"QA stale A\" id=\"qa-stale\">stale A</div>" } }];
await evaluate("document.getElementById('conversation-add-tag').value = 'qa-stale'");
await evaluate("document.getElementById('conversation-add-tag').form.requestSubmit()");
await sleep(100);
await clickSel(`.inbox-row[href="${sh[1]}"]`);
await sleep(3000);
st = { path: await evaluate("location.pathname"), stale: await evaluate("!!document.getElementById('qa-stale')"), id: await paneId(), idA };
check("pane.stale-form-before-click-dropped", st.path === sh[1] && !st.stale && st.id && st.id !== idA, st);
// Sent while the next conversation is still loading (it was still showing on screen).
rules.push({ match: (r) => r.method === "GET" && new URL(r.url).pathname === sh[2], act: { delay: 1200 } });
await clickSel(`.inbox-row[href="${sh[2]}"]`); await sleep(100);
await evaluate("document.getElementById('conversation-add-tag')?.form.requestSubmit()");
await sleep(3500);
rules = [];
st = { path: await evaluate("location.pathname"), stale: await evaluate("!!document.getElementById('qa-stale')"), loaded: !!(await paneId()) };
check("pane.stale-form-during-load-dropped", st.path === sh[2] && !st.stale && st.loaded, st);

// ---- Review round 4 (T1 revised in round 5) ----
// T1. Someone's file message showing up while this upload is still out is not taken for it
// (it may be a teammate's): the upload's own answer decides, here Not confirmed.
const mp2 = await evaluate("new URL(JSON.parse(document.querySelector('#conversation-pane-inner #chat-config').textContent).sendMediaUrl, location.href).pathname");
rules = [{ match: (r) => r.method === "POST" && new URL(r.url).pathname === mp2, act: { slowHtml: 1500, status: 504, html: "timeout" } }];
await chat("(c.uploadMedia(new Blob(['x'], { type: 'image/png' }), 'qa-late.png', false), 0)");
await sleep(300);
await chat(`c.apply({ messages: [{ id: 99999995, direction: 'outbound', body: '', timestamp: c.serverTime(), status: 'sent', media: { state: 'ready', kind: 'image', url: '/static/favicon.ico' } }], statuses: {}, open: true })`);
await sleep(2000);
rules = [];
st = await chat("({ unknown: c.messages.filter(m => m.mediaUnknown).length, pending: c.messages.filter(m => m.mediaPending && m.pending).length, sending: c.sending })");
check("media.upload-not-paired-with-another-file", st.unknown === 1 && st.pending === 0 && !st.sending, st);
await chat("(c.messages.filter(m => m.mediaUnknown).forEach(m => c.dismissUnknown(m)), c.messages = c.messages.filter(m => m.id !== 99999995), 0)");

// T2. A reply whose answer stalls after the headers still ends: Not confirmed, and the box
// works again (the send timeout covers the body).
await chat(`(() => { window.__realFetch = window.fetch; window.fetch = (u, o) => (o && o.method === 'POST' && o.body instanceof FormData && o.body.get('body') === 'QA stalled body')
  ? Promise.resolve({ ok: true, status: 200, redirected: false, json: () => new Promise((_, rej) => o.signal.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError')))) })
  : window.__realFetch(u, o); return 0; })()`);
await typeAndSend("QA stalled body");
await sleep(31500);
await evaluate("window.fetch = window.__realFetch");
st = await chat("({ sending: c.sending, m: c.messages.filter(m => m.body === 'QA stalled body').map(m => ({ unknown: !!m.unknown, pending: !!m.pending })) })");
check("send.stalled-body-times-out", !st.sending && st.m.length === 1 && st.m[0].unknown && !st.m[0].pending, st);
await chat("c.dismissUnknown(c.messages.find(m => m.body === 'QA stalled body'))");

// T3. Offline: a voice note isn't thrown away; it is kept and can be sent once back online.
await send("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
await sleep(300);
await chat("(c.uploadMedia(new Blob(['x'], { type: 'audio/webm' }), 'voice-note.webm', true), 0)");
await sleep(300);
st = { held: await chat("c.held.length === 1 && c.held[0].voice"), shown: await visible("#conversation-pane-inner .cv-send-error") };
check("voice.offline-kept", st.held && st.shown.some((t) => t.includes("Send voice note") && t.includes("Discard")), st);
await send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
await sleep(500);
await chat("c.sendHeld(c.held[0])");
await sleep(1200);
st = await chat("({ held: c.held.length, err: c.sendError, sending: c.sending })");
check("voice.held-sends-when-online", st.held === 0 && !st.err && !st.sending, st);

// T4. Pressing Send offline keeps the "someone else just replied" warning: nothing was sent.
await chat("(c.collision = 'A reply was just sent in this conversation.', c.collisionAck = false, c.draft = 'QA collide', 0)");
await send("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
await sleep(300);
await chat("c.send()"); await chat("c.send()");
st = await chat("({ collision: c.collision, ack: c.collisionAck, draft: c.draft })");
check("send.offline-keeps-collision-warning", !!st.collision && st.ack && st.draft === "QA collide", st);
await send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
await chat("(c.collision = '', c.collisionAck = false, c.draft = '', c.sendError = '')");

// T5. A form posted inside the pane while the next conversation loads doesn't hide that
// load's failure: the pane says so in place, and the list stays.
await sleep(500);
rules = [{ match: (r) => r.method === "GET" && new URL(r.url).pathname === sh[0], act: { delayFail: 1200 } }];
await clickSel(`.inbox-row[href="${sh[0]}"]`); await sleep(100);
await evaluate("document.getElementById('conversation-add-tag') && (document.getElementById('conversation-add-tag').value = 'qa-during-fail', document.getElementById('conversation-add-tag').form.requestSubmit())");
await sleep(2500);
rules = [];
st = await evaluate(`(() => ({ path: location.pathname, split: !!document.getElementById('inbox-split'), err: !!document.querySelector('#conversation-pane-inner [data-error-state]') }))()`);
check("pane.load-failure-said-despite-form", st.split && st.err, st);

// ---- Review round 5 ----
// T5 left the pane on its error state: open a conversation to work in, and use its own
// feed and upload URLs (the ones above belong to other conversations).
await clickSel(`.inbox-row[href="${sh[1]}"]`);
await sleep(2000);
const cfg5 = await evaluate("JSON.parse(document.querySelector('#conversation-pane-inner #chat-config').textContent)");
const fp5 = new URL(cfg5.feedUrl, "http://x").pathname;
const mp5 = new URL(cfg5.sendMediaUrl, "http://x").pathname;
// A conversation opened after another one was closed still polls (undeclared fields used to
// land on <body x-data>, so the closed one's _destroyed stopped every later one).
const pollsBefore = log.filter((l) => l.includes(fp5)).length;
await sleep(7000);
st = { destroyed: await chat("c._destroyed"), timer: await chat("!!c.timer"), polls: log.filter((l) => l.includes(fp5)).length - pollsBefore, bodyHas: await evaluate("'_destroyed' in Alpine.$data(document.body)") };
check("pane.next-conversation-polls", !st.destroyed && st.timer && st.polls >= 1 && !st.bodyHas, st);
// U1. A kept voice note stays reachable after a text reply is sent, and a second kept file
// doesn't replace it.
await send("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
await sleep(300);
await chat("(c.uploadMedia(new Blob(['x'], { type: 'audio/webm' }), 'voice-note.webm', true), c.uploadMedia(new Blob(['y'], { type: 'image/png' }), 'qa-b.png', false), 0)");
await send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
await sleep(500);
await typeAndSend("QA text after a kept voice note");
await sleep(1200);
st = { held: await chat("c.held.map(h => h.voice ? 'voice' : h.filename)"), shown: await visible("#conversation-pane-inner .cv-send-error") };
check("held.survives-text-send-and-second-file", st.held.length === 2 && st.held[0] === "voice" && st.shown.filter((t) => t.includes("Discard")).length === 2, st);
await chat("(c.held = [], 0)");

// U2. A refused file is not kept for a retry that would be refused again: its reason is said.
rules = [{ match: (r) => r.method === "POST" && new URL(r.url).pathname === mp5, act: { status: 400, body: { ok: false, error: "This file type can't be sent." } } }];
await chat("(c.uploadMedia(new Blob(['x'], { type: 'application/x-msdownload' }), 'qa.exe', false), 0)");
await sleep(1200);
rules = [];
st = await chat("({ held: c.held.length, err: c.sendError })");
check("upload.refused-not-held", st.held === 0 && st.err.includes("can't be sent"), st);
await chat("(c.sendError = '', 0)");

// U3. A page that isn't the feed (a proxy's or maintenance page) is a failed poll, not
// "signed out".
rules = [{ match: (r) => r.method === "GET" && new URL(r.url).pathname === fp5, act: { status: 200, html: "<!doctype html><title>Maintenance</title>" } }];
await chat("c.checkNow()");
await sleep(9000);
st = { now: ((Date.now() - T0) / 1000).toFixed(1), conn: await visible(".cv-conn"), state: await chat("({ connection: c.connection, failures: c._pollFailures, interval: c.interval, sending: c.sending, destroyed: !!c._destroyed, cvs: document.querySelectorAll('.cv').length })"), feed: log.filter((l) => l.includes("/messages/")).slice(-8) };
rules = [];
check("thread.other-page-not-signed-out", st.conn.length === 1 && st.conn[0].startsWith("Not connected"), st);
await chat("c.checkNow()");
await sleep(1500);

// U4. A mutation that re-rendered a region beats an older read failing late: the fresh
// content stays, no error state replaces it.
rules = [
  { match: (r) => new URL(r.url).pathname === "/qa-slow-read/", act: { delayFail: 1500 } },
  { match: (r) => new URL(r.url).pathname === "/qa-write/", act: { status: 200, html: "<p id=\"qa-fresh\">fresh</p>" } },
];
await evaluate(`(() => { const r = document.createElement('div'); r.id = 'qa-region'; r.setAttribute('data-error-region', ''); r.innerHTML = '<p>old</p>'; document.body.appendChild(r);
  const a = document.createElement('div'); a.id = 'qa-src-a'; const b = document.createElement('div'); b.id = 'qa-src-b'; document.body.append(a, b);
  htmx.ajax('GET', '/qa-slow-read/', { source: a, target: '#qa-region', swap: 'innerHTML' }).catch(() => {});
  setTimeout(() => htmx.ajax('POST', '/qa-write/', { source: b, target: '#qa-region', swap: 'innerHTML' }), 200); return 0; })()`);
await sleep(2600);
rules = [];
st = await evaluate("({ fresh: !!document.getElementById('qa-fresh'), err: !!document.querySelector('#qa-region [data-error-state]') })");
check("region.write-beats-late-read-failure", st.fresh && !st.err, st);

writeFileSync(join(outDir, "inbox_qa_results.json"), JSON.stringify({ results, log: log.slice(-80) }, null, 1));
console.log(JSON.stringify({ pass: results.filter((r) => r.pass).length, fail: results.filter((r) => !r.pass).map((r) => r.id) }));
process.exit(0);
