// Phase 2 (Content Studio) browser QA: composer on a phone (edit/preview tabs, 16px fields),
// the unsaved-work guard, the confirm and crop dialogs on the shared dialog contract, and
// per-channel outcomes on a post. Every POST is intercepted: nothing is saved or published.
// Usage: node tools/qa/publishing_qa.mjs <repoRoot> <outDir>   (needs QA_UI_* in .env)
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir] = process.argv.slice(2);
const BASE = "http://127.0.0.1:8000";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9337;
const profile = join(outDir, "chrome-profile-p2");
const shots = join(repo, "docs/design/qa/phase2");
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

// ---- Network interception: no POST leaves the browser (nothing is sent to a customer). ----
let rules = [];
const log = [];
ws.addEventListener("message", async (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.method !== "Fetch.requestPaused") return;
  const { requestId, request } = msg.params;
  const rule = rules.find((r) => r.match(request));
  const act = rule ? rule.act : (request.method === "POST" ? { status: 200, body: { ok: true } } : null);
  log.push(`${request.method} ${new URL(request.url).pathname} -> ${act ? (act.fail || act.status) : "pass"}`);
  try {
    if (act && act.delay) { await sleep(act.delay); await send("Fetch.continueRequest", { requestId }); }
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
rules = [{ match: (r) => !/^(127\.0\.0\.1|localhost)$/.test(new URL(r.url).hostname), act: { fail: "BlockedByClient" } }];
const results = [];
const check = (id, pass, detail) => { results.push({ id, pass: !!pass, detail }); process.stderr.write(`${pass ? "PASS" : "FAIL"} ${id} ${detail ? JSON.stringify(detail) : ""}\n`); };
const shot = async (name) => writeFileSync(join(shots, name + ".jpg"), Buffer.from((await send("Page.captureScreenshot", { format: "jpeg", quality: 60 })).data, "base64"));
const cmp = (expr) => evaluate(`(() => { const c = Alpine.$data(document.querySelector('.composer')); return (${expr}); })()`);
const dialogState = () => evaluate(`(() => ({ depth: akDialog.depth, inert: document.querySelectorAll('[data-dialog-inert]').length, focus: (document.activeElement && (document.activeElement.innerText || document.activeElement.id || document.activeElement.tagName) || '').trim().slice(0, 30) }))()`);

// 1. Composer on a 360 phone: edit/preview tabs, fields 16px, no sideways scroll.
await viewport(360, 740, true);
await go("/publishing/new/", 2000);
let st = await evaluate(`(() => ({ tabs: [...document.querySelectorAll('[role=tab]')].map(t => t.innerText.trim()), hscroll: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  small: [...document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=file]), select, textarea')].filter(e => e.offsetParent && parseFloat(getComputedStyle(e).fontSize) < 16).map(e => e.name || e.id) }))()`);
check("composer.phone-layout-360", st.tabs.includes("Preview") && st.hscroll <= 0 && st.small.length === 0, st);
await cmp("(c.caption = 'QA caption for the preview', c.touched && c.touched(), 0)");
await clickSel("[role=tab][aria-selected]:not([aria-selected=true])");
await sleep(400);
st = await evaluate(`(() => ({ previewShown: !!document.querySelector('.cmp-preview') && getComputedStyle(document.querySelector('.cmp-preview')).display !== 'none', editorHidden: getComputedStyle(document.querySelector('.cmp-editor')).display === 'none', text: document.querySelector('.cmp-preview')?.innerText.includes('QA caption') }))()`);
check("composer.preview-tab-360", st.previewShown && st.editorHidden && st.text, st);
await shot("composer-preview-360");

// 2. Leaving with unsaved work asks first (the app's dialog, not the browser's).
await cmp("(c.dirty = true, 0)");
await viewport(1280, 800, false);
await sleep(300);
// Hovering a sidebar link only preloads it: no prompt (it used to ask on hover).
const hoverAt = await evaluate(`(() => { const r = document.querySelector("nav a[href='/inbox/']").getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()`);
const preloadsBefore = log.filter((l) => l.startsWith("GET /inbox/ ")).length;
await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: hoverAt.x, y: hoverAt.y });
await sleep(900);
st = { open: await evaluate("Alpine.store('confirm').open"), preloaded: log.filter((l) => l.startsWith("GET /inbox/ ")).length - preloadsBefore, path: await evaluate("location.pathname") };
check("composer.hover-preload-no-prompt", !st.open && st.preloaded >= 1 && st.path === "/publishing/new/", st);
await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 900, y: 500 });
await clickSel("nav a[href='/dashboard/']");
await sleep(800);
st = await evaluate("({ open: Alpine.store('confirm').open, title: Alpine.store('confirm').title, path: location.pathname })");
check("composer.unsaved-guard-asks", st.open && /leave without saving/i.test(st.title) && st.path === "/publishing/new/", st);
await evaluate("Alpine.store('confirm').respond(false)");
await sleep(300);
st = await evaluate("({ path: location.pathname, open: Alpine.store('confirm').open })");
check("composer.stay-keeps-work", st.path === "/publishing/new/" && !st.open && (await cmp("c.caption")) === "QA caption for the preview", st);
await sleep(400);
st = await evaluate("({ busy: document.getElementById('main').getAttribute('aria-busy'), bar: !!document.querySelector('.is-active[class*=progress], #nav-progress.is-active') })");
check("composer.stay-leaves-page-usable", !st.busy && !st.bar, st);

// 3. The publish confirm joins the dialog stack: focus in, page inert, Esc closes it alone.
await evaluate("document.querySelector('#main h1, h1') && document.querySelector('#main h1, h1').focus && document.body.focus()");
await cmp("(c.confirmAction = 'publish', c.confirmOpen = true, 0)");
await sleep(500);
const opened = await dialogState();
st = { opened, ids: await evaluate("document.querySelectorAll('#confirm-title').length + '/' + document.querySelectorAll('#cmp-confirm-title').length") };
check("confirm.joins-dialog-stack", opened.depth === 1 && opened.inert > 0 && /not yet/i.test(opened.focus) && st.ids === "1/1", st);
await shot("composer-confirm-1280");
for (const type of ["rawKeyDown", "keyUp"]) await send("Input.dispatchKeyEvent", { type, key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
await sleep(400);
st = { after: await dialogState(), open: await cmp("c.confirmOpen") };
check("confirm.esc-closes-cleanly", !st.open && st.after.depth === 0 && st.after.inert === 0, st);

// 4. The cropper: dialog stack, fits a phone, Esc skips the crop (callback gets null).
await viewport(360, 740, true);
await sleep(300);
const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
await evaluate(`(() => { window.__cropResult = 'pending'; window._igCropOpen(${JSON.stringify(png)}, {}, (r) => { window.__cropResult = r; }); })()`);
await sleep(800);
st = { state: await dialogState(), fits: await evaluate(`(() => { const r = document.getElementById('ig-crop-panel').getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight; })()`) };
st.overHeader = await evaluate("!!document.elementFromPoint(20, 20)?.closest('#ig-crop-modal')");
check("crop.joins-stack-and-fits-360", st.state.depth === 1 && st.state.inert > 0 && st.fits && st.overHeader, st);
await shot("crop-360");
for (const type of ["rawKeyDown", "keyUp"]) await send("Input.dispatchKeyEvent", { type, key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
await sleep(400);
st = { after: await dialogState(), result: await evaluate("window.__cropResult"), hidden: await evaluate("document.getElementById('ig-crop-modal').classList.contains('hidden')") };
check("crop.esc-skips-cleanly", st.hidden && st.result === null && st.after.depth === 0 && st.after.inert === 0, st);

// 4b. Two images queued: skipping the first opens the second as a full dialog (stack, inert
// page, focus inside, Esc works), and skipping that one leaves nothing behind. Uploads are
// stubbed, so nothing is stored.
await evaluate(`(() => { const c = Alpine.$data(document.querySelector('.composer')); window.__uploads = 0; c.uploadFile = () => { window.__uploads++; };
  const b = Uint8Array.from(atob(${JSON.stringify(png.split(",")[1])}), (ch) => ch.charCodeAt(0));
  c.onFiles([new File([b], 'qa1.png', { type: 'image/png' }), new File([b], 'qa2.png', { type: 'image/png' })]); })()`);
await sleep(800);
for (const type of ["rawKeyDown", "keyUp"]) await send("Input.dispatchKeyEvent", { type, key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
await sleep(600);
st = { state: await dialogState(), open: await evaluate("!document.getElementById('ig-crop-modal').classList.contains('hidden')"),
  focusIn: await evaluate("document.getElementById('ig-crop-modal').contains(document.activeElement)"), uploads: await evaluate("window.__uploads") };
check("crop.second-in-queue-is-a-dialog", st.open && st.state.depth === 1 && st.state.inert > 0 && st.focusIn && st.uploads === 1, st);
for (const type of ["rawKeyDown", "keyUp"]) await send("Input.dispatchKeyEvent", { type, key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
await sleep(600);
st = { state: await dialogState(), hidden: await evaluate("document.getElementById('ig-crop-modal').classList.contains('hidden')"), uploads: await evaluate("window.__uploads") };
check("crop.queue-ends-cleanly", st.hidden && st.state.depth === 0 && st.state.inert === 0 && st.uploads === 2, st);

// 4c. Without the shared dialog stack, Esc still skips the crop (the cropper's own fallback).
await evaluate("window.__akDialogSaved = window.akDialog; window.akDialog = undefined; window.__cropResult = 'pending'; window._igCropOpen(" + JSON.stringify(png) + ", {}, (r) => { window.__cropResult = r; })");
await sleep(600);
for (const type of ["rawKeyDown", "keyUp"]) await send("Input.dispatchKeyEvent", { type, key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
await sleep(400);
st = { hidden: await evaluate("document.getElementById('ig-crop-modal').classList.contains('hidden')"), result: await evaluate("window.__cropResult") };
await evaluate("window.akDialog = window.__akDialogSaved");
check("crop.esc-without-dialog-stack", st.hidden && st.result === null, st);

// 5. Native dialogs: none left on the page (prompt/confirm/alert are never called).
await evaluate("window.__native = []; ['alert','confirm','prompt'].forEach(n => { window[n] = function () { window.__native.push(n); return null; }; })");
await evaluate("(() => { const b = document.createElement('button'); b.setAttribute('hx-post', '/publishing/qa-noop/'); b.setAttribute('hx-confirm', 'Sure?'); document.body.appendChild(b); htmx.process(b); b.click(); })()");
await sleep(500);
st = { native: await evaluate("window.__native"), open: await evaluate("Alpine.store('confirm').open") };
check("dialogs.no-native-confirm", st.native.length === 0 && st.open, st);
await evaluate("Alpine.store('confirm').respond(false)");

// 6. A post's page lists each channel's own outcome. (The draft is dropped first: with
// unsaved work the browser's own leave-page prompt would - correctly - hold the navigation.)
await cmp("(c.dirty = false, c.caption = '', 0)");
await viewport(1280, 800, false);
await go("/publishing/library/", 1500);
const postHref = await evaluate("[...document.querySelectorAll('a[href*=\"/publishing/posts/\"]')].map(a => a.getAttribute('href'))[0]");
if (postHref) {
  await go(postHref, 1500);
  st = await evaluate("(() => { const c = document.getElementById('pd-channels'); const card = c && c.closest('section'); return { perChannel: !!card && card.querySelectorAll('.pd-channel').length > 0, text: card ? card.innerText.slice(0, 200) : '' }; })()");
  check("post.per-channel-outcome", st.perChannel, { postHref, ...st });
} else {
  check("post.per-channel-outcome", false, "no post in the library to open");
}

// 7. A page-to-page navigation names #main only while it transitions (no stacking context left).
await go("/publishing/", 1500);
await clickSel("nav a[href='/dashboard/']");
await sleep(2500);
st = await evaluate("({ path: location.pathname, vtClass: document.documentElement.classList.contains('ak-page-vt'), name: getComputedStyle(document.getElementById('main')).viewTransitionName })");
check("nav.transition-name-cleared", st.path === "/dashboard/" && !st.vtClass && (st.name === "none" || !st.name), st);

// 7. Calendar: seven columns where they fit (xl), an agenda below; every entry says its
// status in words; today is marked; no sideways scroll.
const calState = `(() => { const shown = (s) => { const e = document.querySelector(s); return !!e && e.getClientRects().length > 0; };
  const states = [...document.querySelectorAll('.cal-entry')].filter(e => e.offsetParent).map(e => e.querySelector('.cal-state')?.textContent.trim() || '');
  return { grid: shown('.cal-week'), agenda: shown('.cal-agenda'), hscroll: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    entries: states.length, unlabelled: states.filter(s => !s).length, today: !!document.querySelector('.cal-today .cal-date') }; })()`;
await viewport(1280, 860, false);
await go("/publishing/calendar/", 1500);
st = await evaluate(calState);
check("calendar.week-grid-1280", st.grid && !st.agenda && st.hscroll <= 0 && st.unlabelled === 0 && st.today, st);
await viewport(390, 844, true);
await go("/publishing/calendar/", 1500);
st = await evaluate(calState);
check("calendar.agenda-390", !st.grid && st.agenda && st.hscroll <= 0 && st.unlabelled === 0 && st.today, st);
await go("/publishing/calendar/?view=list", 1500);
st = await evaluate(calState);
check("calendar.list-no-overflow-390", st.agenda && st.hscroll <= 0, st);

// 8. A post's page: channels beside the post on a wide screen, above it on a phone.
if (postHref) {
  const pdState = `(() => { const ch = document.getElementById('pd-channels')?.closest('section'); const post = document.getElementById('pd-post')?.closest('section');
    if (!ch || !post) return { missing: true }; const a = ch.getBoundingClientRect(), b = post.getBoundingClientRect();
    return { beside: a.left > b.right - 1, above: a.bottom <= b.top + 1, hscroll: document.documentElement.scrollWidth - document.documentElement.clientWidth, h1: document.querySelector('h1').textContent.trim().slice(0, 40) }; })()`;
  await go(postHref, 1500);
  st = await evaluate(pdState);
  check("post.channels-first-on-phone", st.above && st.hscroll <= 0 && st.h1 && st.h1 !== "Post", st);
  await viewport(1280, 860, false);
  await go(postHref, 1500);
  st = await evaluate(pdState);
  check("post.channels-beside-post-1280", st.beside && st.hscroll <= 0, st);
}

writeFileSync(join(outDir, "publishing_qa_results.json"), JSON.stringify({ results, log: log.slice(-40) }, null, 1));
console.log(JSON.stringify({ pass: results.filter((r) => r.pass).length, fail: results.filter((r) => !r.pass).map((r) => r.id) }));
process.exit(0);
