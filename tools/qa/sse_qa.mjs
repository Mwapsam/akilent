// Live updates (SSE) in the browser, against an ASGI server with REALTIME_SSE_ENABLED=True:
// the stream connects, a message event refreshes the open thread, another conversation's
// event doesn't, and a conversation event refreshes the list without a toast.
// Usage: QA_BASE=http://127.0.0.1:8002 node tools/qa/sse_qa.mjs <repoRoot> <outDir>
// (run uvicorn automator.asgi:application on :8002 with REALTIME_SSE_ENABLED=True; on SQLite
// stop the other dev server first: two servers lock the database).
// Phase 1 (inbox) browser QA: drives locally installed Chrome (headless) over the DevTools
// protocol and checks send outcomes, connection notices, the details drawer, pane loading
// and phone layout. Every POST is intercepted, so nothing is sent to a customer.
// Usage: node tools/qa/inbox_qa.mjs <repoRoot> <outDir>   (QA_SCAN=1 contrast scan, QA_HDR=1 header sizes)
// Needs the dev server on :8000 and QA_UI_EMAIL / QA_UI_PASSWORD in .env.
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir] = process.argv.slice(2);
const BASE = process.env.QA_BASE || "http://127.0.0.1:8000";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9341;
const profile = join(outDir, "chrome-profile-sse");
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
mkdirSync(shots, { recursive: true });
const shot = async (name) => writeFileSync(join(shots, name + ".jpg"), Buffer.from((await send("Page.captureScreenshot", { format: "jpeg", quality: 60 })).data, "base64"));
const toasts = () => evaluate("Alpine.store('toasts').items.map(t => t.type + ': ' + t.message)");
// ---- Live updates (SSE) against the ASGI server: QA_BASE=http://127.0.0.1:8002 ----
import { execFileSync } from "node:child_process";
const py = (code) => execFileSync(join(repo, ".venv/Scripts/python.exe"), ["manage.py", "shell", "-c", code],
  { cwd: repo, env: { ...process.env, REDIS_URL: "redis://127.0.0.1:6379/2", REDIS_CACHE_URL: "redis://127.0.0.1:6379/1" }, encoding: "utf8" });
const accountId = py(`from apps.accounts.models import Membership; print(Membership.objects.filter(user__email=${JSON.stringify(env.QA_UI_EMAIL)}).order_by('pk').values_list('account_id', flat=True).first())`).trim().split(/\s+/).pop();
const results = [];
const check = (id, pass, detail) => { results.push({ id, pass: !!pass }); process.stderr.write(`${pass ? "PASS" : "FAIL"} ${id} ${detail ? JSON.stringify(detail) : ""}\n`); };

await viewport(1280, 860, false);
await go("/inbox/?view=all", 2500);
const hs = await evaluate("[...document.querySelectorAll('.inbox-row')].map(a => a.getAttribute('href'))");
await clickSel(`.inbox-row[href="${hs[0]}"]`);
await sleep(2500);
const cfg = await evaluate("JSON.parse(document.querySelector('#conversation-pane-inner #chat-config').textContent)");
const feed = new URL(cfg.feedUrl, "http://x").pathname;

// 1. Events reach the page: htmx's SSE extension turns each into an "sse:<name>" DOM event.
await evaluate("(() => { window.__sse = {}; ['message.created', 'conversation.updated'].forEach((n) => document.addEventListener('sse:' + n, () => { window.__sse[n] = (window.__sse[n] || 0) + 1; })); return 0; })()");
await sleep(1500);
// 2. A new message published for this conversation makes the open thread fetch it at once
//    (its own poll is pushed out to a minute, so only the event can explain a fetch).
await evaluate("(() => { const c = Alpine.$data(document.querySelector('#conversation-pane-inner .cv')); c.interval = 60000; c.schedule(); return 0; })()");
await sleep(500);
let before = log.filter((l) => l.includes(feed)).length;
py(`from apps.core.realtime import publish; publish(${accountId}, "message.created", {"conversation_id": ${JSON.stringify(cfg.id)}})`);
await sleep(2000);
let fetched = log.filter((l) => l.includes(feed)).length - before;
const got1 = await evaluate("window.__sse['message.created'] || 0");
check("sse.message-created-arrives-and-refreshes-thread", got1 >= 1 && fetched >= 1, { accountId, got1, fetched, recent: log.slice(-4) });

// 3. An event for another conversation doesn't (the poll pushed out again: the event above
//    rightly reset it to every 3 seconds).
await evaluate("(() => { const c = Alpine.$data(document.querySelector('#conversation-pane-inner .cv')); c.interval = 60000; c.schedule(); return 0; })()");
await sleep(300);
before = log.filter((l) => l.includes(feed)).length;
py(`from apps.core.realtime import publish; publish(${accountId}, "message.created", {"conversation_id": "conv_not_this_one"})`);
await sleep(2000);
fetched = log.filter((l) => l.includes(feed)).length - before;
check("sse.other-conversation-ignored", fetched === 0, { fetched });

// 4. A conversation update refreshes the inbox list (as a background refresh: no toast).
before = log.filter((l) => /GET \/inbox\/(feed\/|\?)/.test(l)).length;
py(`from apps.core.realtime import publish; publish(${accountId}, "conversation.updated", {"conversation_id": ${JSON.stringify(cfg.id)}})`);
await sleep(2500);
const listFetched = log.filter((l) => /GET \/inbox\/(feed\/|\?)/.test(l)).length - before;
const toastCount = await evaluate("Alpine.store('toasts').items.length");
const got2 = await evaluate("window.__sse['conversation.updated'] || 0");
check("sse.conversation-updated-arrives-and-refreshes-list", got2 >= 1 && listFetched >= 1 && toastCount === 0, { got2, listFetched, toastCount, recent: log.slice(-5) });

console.log(JSON.stringify({ pass: results.filter((r) => r.pass).length, fail: results.filter((r) => !r.pass).map((r) => r.id) }));
process.exit(0);
