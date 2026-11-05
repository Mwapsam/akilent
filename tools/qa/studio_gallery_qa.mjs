// Content Studio app feel, Phases 4 and 5: Media as a gallery (whole pictures, hover controls,
// shift-click selection and its bar, live search, details with arrow keys, list view, drop
// anywhere), the page shortcuts, the month calendar's "+", the composer's segmented formats,
// and no sideways scroll at 360px. Needs the cs_* features released and a few media files.
// Usage: node tools/qa/studio_overlay_qa.mjs <repoRoot> <outDir>   (needs QA_UI_* in .env)
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir] = process.argv.slice(2);
const BASE = "http://127.0.0.1:8000";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9344;
const profile = join(outDir, "chrome-profile-gallery");
const shots = join(repo, "docs/design/qa/studio-feel");
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
    else if (act.raw) { if (act.wait) await sleep(act.wait); await send("Fetch.fulfillRequest", { requestId, responseCode: 200, responseHeaders: [{ name: "Content-Type", value: act.type }], body: act.raw }); }
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

// Console errors on any page count as failures.
const consoleErrors = [];
ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.method === "Runtime.exceptionThrown") consoleErrors.push(msg.params.exceptionDetails?.exception?.description || msg.params.exceptionDetails?.text);
  if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") consoleErrors.push(msg.params.args.map((a) => a.value || a.description).join(" "));
});
const waitFor = async (expr, ms = 8000) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (await evaluate(expr)) return true; await sleep(50); } return false; };
const activeTab = () => evaluate(`document.querySelector('[data-instant-tabs] a[aria-current="page"]')?.textContent.trim()`);
await viewport(1280, 800, false);


const overlayOpen = () => evaluate(`!document.getElementById('overlay-host').hidden`);
const getCount = (path) => log.filter((l) => l === `GET ${path} -> pass`).length;
const toasts = () => evaluate(`Alpine.store('toasts').items.map((t) => t.type + ': ' + t.message).join(' | ')`);
// The overlay's own plan saves go to the local dev server for real.

// ---- Media as a gallery ----
await viewport(1280, 860, false);
await go("/publishing/media/", 1500);
const g = await evaluate(`({ cards: document.querySelectorAll('#media-results .ml-card').length,
  fit: [...document.querySelectorAll('#media-results .ml-card img.ml-img')].every((i) => getComputedStyle(i).objectFit === 'contain'),
  names: [...document.querySelectorAll('.ml-name')].map((n) => n.textContent),
  policyText: /Everything uploaded in Content Studio lands here/.test(document.getElementById('main').innerText),
  total: (document.querySelector('[data-media-total]') || {}).textContent })`);
await shot("gallery-grid");
check("media.gallery-whole-pictures", g.cards >= 2 && g.fit && !g.policyText && /files?/.test(g.total || ""), g);
check("media.clean-names", !g.names.some((n) => /\.(png|jpe?g)$/i.test(n)), g.names);

// Hover shows the controls (opacity 0 before).
const before = await evaluate(`getComputedStyle(document.querySelector('#media-results .ml-actions')).opacity`);
const fr = await evaluate(`(() => { const r = document.querySelector('#media-results .ml-card .ml-frame').getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()`);
await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: fr.x, y: fr.y });
await sleep(300);
const after = await evaluate(`getComputedStyle(document.querySelector('#media-results .ml-actions')).opacity`);
check("media.controls-on-hover", before === "0" && after === "1", { before, after });

// Shift-click ticks a range; the selection bar shows the count.
const boxes = await evaluate(`[...document.querySelectorAll('#media-results .ml-check input')].slice(0, 3).map((i) => { const r = i.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })`);
for (const [i, mods] of [[0, 0], [2, 8]]) {
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: boxes[i].x, y: boxes[i].y });
  for (const type of ["mousePressed", "mouseReleased"]) await send("Input.dispatchMouseEvent", { type, x: boxes[i].x, y: boxes[i].y, button: "left", clickCount: 1, modifiers: mods });
  await sleep(150);
}
const sel = await evaluate(`({ bar: getComputedStyle(document.querySelector('.ml-bulkbar')).display !== 'none', text: document.querySelector('.ml-bulkbar').innerText.split(String.fromCharCode(10))[0], ticked: document.querySelectorAll('.ml-card.is-selected').length })`);
await shot("gallery-selected");
check("media.shift-click-range-and-bar", sel.bar && sel.ticked === 3 && /3 selected/.test(sel.text), sel);
await key("Escape", "Escape", 27);
await sleep(200);
check("media.esc-clears-selection", (await evaluate(`document.querySelectorAll('.ml-card.is-selected').length`)) === 0, {});

// Live search: no button, the URL follows, focus stays in the box.
const mediaGets = log.filter((l) => l.startsWith("GET /publishing/media/ ")).length;
await evaluate(`(() => { const i = document.querySelector('input[name=q]'); i.focus(); i.value = 'linen'; i.dispatchEvent(new Event('input', { bubbles: true })); return 0; })()`);
await waitFor(`location.search.includes('q=linen') && document.querySelectorAll('#media-results .ml-card').length === 1`);
const ls = await evaluate(`({ q: location.search, cards: document.querySelectorAll('#media-results .ml-card').length, focus: document.activeElement && document.activeElement.name })`);
check("media.live-search", ls.cards === 1 && ls.focus === "q" && /q=linen/.test(ls.q), ls);
await evaluate(`(() => { const i = document.querySelector('input[name=q]'); i.value = ''; i.dispatchEvent(new Event('input', { bubbles: true })); return 0; })()`);
await waitFor(`!location.search.includes('q=linen')`);
await sleep(300);

// Details drawer: arrows step, a post opened from it loads in place, Back returns, Close leaves.
await evaluate("document.activeElement.blur(); 0");
const firstHref = await evaluate(`document.querySelector('#media-results .ml-card .ml-open').getAttribute('href')`);
await clickSel('#media-results .ml-card .ml-open');
await waitFor("!!document.querySelector('#overlay-body .md')");
await sleep(400);
const d1 = await evaluate(`({ path: location.pathname, pos: document.querySelector('[data-overlay-position]').textContent, focus: document.activeElement.className })`);
await key("ArrowRight", "ArrowRight", 39);
await waitFor(`location.pathname !== ${JSON.stringify(firstHref)} && document.getElementById('overlay-body').getAttribute('aria-busy') === 'false'`);
const d2 = await evaluate(`({ path: location.pathname, pos: document.querySelector('[data-overlay-position]').textContent })`);
await shot("gallery-detail");
check("media.details-arrows-step", d1.path === firstHref && /md-preview/.test(d1.focus) && d2.pos.startsWith("2 of"), { d1, d2 });
await key("Escape", "Escape", 27);
await waitFor(`document.getElementById('overlay-host').hidden`);
check("media.details-close-back-to-grid", (await evaluate("location.pathname")) === "/publishing/media/", {});

// List view.
await evaluate(`document.querySelector('input[name=view][value=list]').click(); 0`);
await waitFor(`!!document.querySelector('.ml-table')`);
check("media.list-view", (await evaluate(`location.search.includes('view=list') && document.querySelectorAll('.ml-table tbody tr').length > 1`)), {});
await shot("gallery-list");
await evaluate(`document.querySelector('input[name=view][value=grid]').click(); 0`);
await waitFor(`!!document.querySelector('.ml-grid .ml-card')`);

// Drop anywhere shows the drop overlay (a synthetic drag with files).
await evaluate(`(() => { const dt = new DataTransfer(); dt.items.add(new File(['x'], 'a.png', { type: 'image/png' })); window.dispatchEvent(new DragEvent('dragenter', { dataTransfer: dt, bubbles: true })); return 0; })()`);
await sleep(200);
const dropShown = await evaluate(`getComputedStyle(document.querySelector('.ml-drop')).display !== 'none'`);
await evaluate(`(() => { const dt = new DataTransfer(); window.dispatchEvent(new DragEvent('dragleave', { dataTransfer: dt, bubbles: true })); return 0; })()`);
await sleep(200);
check("media.drop-anywhere-overlay", dropShown && (await evaluate(`getComputedStyle(document.querySelector('.ml-drop')).display === 'none'`)), {});

// ---- Shortcuts ----
await evaluate("document.activeElement.blur(); 0");
await key("j", "KeyJ", 74);
await sleep(100);
const j1 = await evaluate(`!!document.activeElement.closest('[data-row]')`);
await key("/", "Slash", 191);
await sleep(100);
const slash = await evaluate(`document.activeElement && document.activeElement.name`);
await evaluate("document.activeElement.blur(); 0");
await key("?", "Slash", 191);
await sleep(400);
const pal = await evaluate(`(() => { const i = document.querySelector('input[aria-label="Search pages and actions"]'); return { open: !!i && i.offsetParent !== null, q: i && i.value, rows: document.querySelectorAll('#cmdk-results [role=option]').length }; })()`);
check("shortcuts.j-slash-question", j1 && slash === "q" && pal.open && pal.q === "Shortcut" && pal.rows >= 5, { j1, slash, pal });
await key("Escape", "Escape", 27);
await sleep(300);

// ---- Calendar month: + on a day, ideas draggable ----
await go("/publishing/calendar/?view=month", 1500);
const cal = await evaluate(`({ add: document.querySelectorAll('.cal-m-add').length, addOpacity: (() => { const a = document.querySelector('.cal-m-add'); return a && getComputedStyle(a).opacity; })() })`);
const day = await evaluate(`(() => { const d = document.querySelector('.cal-m-add').closest('.cal-m-day').getBoundingClientRect(); return { x: d.left + d.width / 2, y: d.top + d.height / 2 }; })()`);
await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: day.x, y: day.y });
await sleep(250);
const shownOnHover = await evaluate(`(() => { const d = document.elementFromPoint(${day.x}, ${day.y}).closest('.cal-m-day'); return getComputedStyle(d.querySelector('.cal-m-add')).opacity; })()`);
check("calendar.plus-on-hover", cal.add > 5 && cal.addOpacity === "0" && shownOnHover === "1", { ...cal, shownOnHover });
await evaluate(`document.querySelector('.cal-m-day:hover .cal-m-add, .cal-m-add').click(); 0`);
await waitFor("!!document.querySelector('#overlay-body [name=topic]')");
const modal = await evaluate(`({ kind: document.getElementById('overlay-host').dataset.kind, date: (document.querySelector('#overlay-body [name=planned_date]') || {}).value })`);
check("calendar.plus-opens-add-idea", modal.kind === "modal" && /^\d{4}-\d\d-\d\d$/.test(modal.date || ""), modal);
await key("Escape", "Escape", 27);
await waitFor(`document.getElementById('overlay-host').hidden`);

// ---- Composer: the formats as a segmented control, 3 to a row on a phone ----
await go("/publishing/new/", 1500);
const fmt = await evaluate(`({ n: document.querySelectorAll('#media-type input[type=radio]').length, checked: (document.querySelector('#media-type input:checked') || {}).value })`);
await evaluate(`document.querySelector('#media-type input[value=reel]').click(); 0`);
await sleep(200);
const fmt2 = await evaluate(`({ checked: document.querySelector('#media-type input:checked').value, hint: document.querySelector('.cmp-hint.mb-3') && document.querySelector('.cmp-hint.mb-3').textContent })`);
check("composer.format-segmented", fmt.n === 6 && fmt.checked === "image" && fmt2.checked === "reel", { fmt, fmt2 });

// ---- Phone: no sideways scroll on Media, the composer and the calendar ----
await viewport(360, 780, true);
const wide = [];
for (const p of ["/publishing/media/", "/publishing/new/", "/publishing/calendar/", "/publishing/sales/", "/publishing/analytics/"]) {
  await go(p, 1300);
  const w = await evaluate("document.documentElement.scrollWidth");
  if (w > 360) wide.push([p, w]);
}
await go("/publishing/media/", 1300);
await shot("gallery-phone");
check("phone.no-sideways-scroll", wide.length === 0, wide);
await viewport(1280, 860, false);

// ---- Phase 3: live filters, chips, remove in place with the server's Undo ----
await go("/publishing/library/", 1500);
const libGets = log.filter((l) => l.startsWith("GET /publishing/library/")).length;
await evaluate(`(() => { const i = document.getElementById('library-q'); i.focus(); i.value = 'zzz-no-such-caption'; i.dispatchEvent(new Event('input', { bubbles: true })); return 0; })()`);
await waitFor(`location.search.includes('q=zzz') && !!document.querySelector('#library-results [data-chip-clear="q"]')`);
const lib = await evaluate(`({ q: location.search, focus: document.activeElement && document.activeElement.id, empty: /No posts match/.test(document.getElementById('library-results').innerText), button: [...document.querySelectorAll('form[data-live-filter] button')].some((b) => b.textContent.trim() === 'Filter') })`);
check("library.live-search-keeps-focus", /q=zzz/.test(lib.q) && lib.focus === "library-q" && lib.empty && !lib.button, lib);
await evaluate(`document.querySelector('#library-results [data-chip-clear="q"]').click(); 0`);
await waitFor(`!location.search.includes('q=zzz')`);
const cleared = await evaluate(`({ q: document.getElementById('library-q').value, search: location.search, rows: document.querySelectorAll('#library-results tbody tr').length })`);
check("library.chip-clears-filter", cleared.q === "" && cleared.rows > 0, cleared);
await evaluate(`(() => { const s = document.getElementById('library-show'); s.value = 'published'; s.dispatchEvent(new Event('change', { bubbles: true })); return 0; })()`);
await waitFor(`location.search.includes('show=published')`);
check("library.show-applies-on-change", (await evaluate(`!!document.querySelector('#library-results [data-chip-clear="show bucket status"]')`)), {});

rules.unshift({ match: (r) => r.method === "POST" && /^\/publishing\/media\/\d+\/(restore\/)?$/.test(new URL(r.url).pathname), act: null });
await go("/publishing/media/", 1500);
await evaluate(`Alpine.store('toasts').items = []; 0`);
const linen = await evaluate(`(() => { const img = document.querySelector('#media-results img[src*="linen-dress"]'); const li = img && img.closest('[data-asset]'); return li ? li.getAttribute('data-asset') : null; })()`);
if (!linen) check("media.archive-in-place-with-undo", false, "no linen-dress card");
else {
  await evaluate(`(() => { const li = document.querySelector('[data-asset="${linen}"]'); li.querySelector('.ml-more').open = true; li.querySelector('button[value=remove]').click(); return 0; })()`);
  // The confirm says what will happen (archived: published posts use it), then yes.
  await waitFor(`/archived/.test(Alpine.store('confirm').message || '')`, 3000);
  await evaluate(`Alpine.store('confirm').respond(true); 0`);
  await waitFor(`!document.querySelector('[data-asset="${linen}"]')`);
  const t1 = await evaluate(`Alpine.store('toasts').items.map((t) => ({ m: t.message, a: t.action && t.action.label }))`);
  const gone = await evaluate(`!document.querySelector('[data-asset="${linen}"]') && location.pathname === '/publishing/media/'`);
  await evaluate(`(() => { const s = Alpine.store('toasts'); const t = s.items.find((i) => i.action && i.action.label === 'Undo'); if (t) s.run(t); return 0; })()`);
  await waitFor(`!!document.querySelector('[data-asset="${linen}"]')`);
  const back = await evaluate(`!!document.querySelector('[data-asset="${linen}"]')`);
  await shot("phase3-undo");
  check("media.archive-in-place-with-undo", gone && t1.some((t) => /^Archived/.test(t.m) && t.a === "Undo") && back, { t1, gone, back });
}
rules.shift();

// A copy of a file already in the library: asked "keep both?" before it's added; Skip adds nothing.
await go("/publishing/media/", 1500);
const uploadsBefore = log.filter((l) => /^POST \/publishing\/posts\/media\//.test(l)).length;
const dupName = await evaluate(`(async () => {
  const img = document.querySelector('#media-results img[src*="/media/publishing/9/demo/"]');
  if (!img) return null;
  const name = img.closest('[data-asset]').querySelector('.ml-name').getAttribute('title');
  const blob = await (await fetch(img.getAttribute('src'))).blob();
  const file = new File([blob], name.replace(/(\\.[a-z]+)$/i, ' (2)$1'), { type: blob.type });
  Alpine.$data(document.querySelector('[x-data^="mediaLibrary"]')).onFiles([file]);
  return file.name;
})()`);
if (!dupName) check("media.duplicate-asked-before-upload", false, "no demo picture to copy");
else {
  const asked = await waitFor(`(() => { const a = document.querySelector('.ml-ask'); return a && getComputedStyle(a).display !== 'none'; })()`, 6000);
  const text = await evaluate(`(document.querySelector('.ml-ask') || {}).innerText || ''`);
  await shot("gallery-duplicate-ask");
  await evaluate(`[...document.querySelectorAll('.ml-ask button')].find((b) => b.textContent.trim() === 'Skip').click(); 0`);
  await waitFor(`!document.querySelector('.ml-ask')`, 4000);
  await sleep(300);
  const uploadsAfter = log.filter((l) => /^POST \/publishing\/posts\/media\//.test(l)).length;
  check("media.duplicate-asked-before-upload", asked && /Looks like/.test(text) && uploadsAfter === uploadsBefore, { dupName, text, uploads: uploadsAfter - uploadsBefore });
}

// ---- Review round: the date popover, the toolbar under the header, the heading's ring, focus after Cancel ----
await viewport(1280, 860, false);
await go("/publishing/library/", 1500);
const panelShown = `(() => { const p = document.querySelector('form[data-live-filter] details.pop .pop-panel'); return !!p && getComputedStyle(p).display !== 'none'; })()`;
await evaluate(`document.querySelector('form[data-live-filter] details.pop summary').click(); 0`);
await sleep(200);
const openedWide = await evaluate(`({ shown: ${panelShown}, wide: document.documentElement.scrollWidth - innerWidth })`);
await evaluate(`[...document.querySelectorAll('form[data-live-filter] .pop-item')].find((b) => b.textContent.trim() === 'Last 7 days').click(); 0`);
await waitFor(`location.search.includes('date_from=')`);
await sleep(300);
const preset = await evaluate(`({ shown: ${panelShown}, trigger: document.querySelector('form[data-live-filter] details.pop summary').innerText.trim(), chip: (document.querySelector('#library-results [data-chip-clear="date_from date_to"]') || {}).innerText })`);
await evaluate(`document.querySelector('form[data-live-filter] details.pop summary').click(); 0`);
await sleep(150);
await key("Escape", "Escape", 27);
await sleep(150);
const afterEsc = await evaluate(panelShown);
await evaluate(`document.querySelector('form[data-live-filter] details.pop summary').click(); 0`);
await sleep(150);
await evaluate(`document.querySelector('h1').click(); 0`);
await sleep(150);
const afterOutside = await evaluate(panelShown);
check("library.date-popover-closes-and-names-preset", openedWide.shown && openedWide.wide <= 0 && !preset.shown && preset.trigger === "Last 7 days" && /Last 7 days/.test(preset.chip || "") && !afterEsc && !afterOutside, { openedWide, preset, afterEsc, afterOutside });
// Removing the date chip resets the trigger too (it sits outside the swapped region).
await evaluate(`document.querySelector('#library-results [data-chip-clear="date_from date_to"]').click(); 0`);
await waitFor(`!location.search.includes('date_from=2')`);
await sleep(300);
const reset = await evaluate(`({ trigger: document.querySelector('form[data-live-filter] details.pop summary').innerText.trim(), from: document.getElementById('library-from').value })`);
check("library.date-chip-resets-trigger", reset.trigger === "Any time" && reset.from === "", reset);

await go("/publishing/media/", 1500);
await evaluate(`window.scrollTo(0, 900); 0`);
await sleep(300);
const bar = await evaluate(`({ toolbarTop: Math.round(document.querySelector('.ml-toolbar').getBoundingClientRect().top), headerBottom: Math.round(document.querySelector('header.sticky, body > div header').getBoundingClientRect().bottom) })`);
check("media.toolbar-below-header", bar.toolbarTop >= bar.headerBottom - 1, bar);
await evaluate(`window.scrollTo(0, 0); 0`);
const group = await evaluate(`(() => { const g = document.querySelector('.ml-group'); const c = document.querySelector('.ml-card .ml-check'); if (!g || !c) return { none: true }; const a = g.getBoundingClientRect(), b = c.getBoundingClientRect(); return { overlap: !(a.bottom <= b.top || b.bottom <= a.top || a.right <= b.left || b.right <= a.left), position: getComputedStyle(g).position }; })()`);
check("media.date-label-clear-of-cards", group.none || (!group.overlap && group.position !== "sticky"), group);

// After an in-place page change focus goes to the heading, without a visible ring.
await clickSel('[data-instant-tabs] a[href="/publishing/library/"]');
await waitFor(`location.pathname === '/publishing/library/'`);
await sleep(600);
const ring = await evaluate(`(() => { const a = document.activeElement; return { tag: a && a.tagName, outline: a && getComputedStyle(a).outlineStyle }; })()`);
check("nav.heading-focus-without-ring", ring.tag === "BODY" || ring.outline === "none", ring);

// Cancelling a confirm puts focus back on the card's menu, not <body>.
await go("/publishing/media/", 1500);
await evaluate(`(() => { const li = document.querySelector('#media-results [data-asset]'); const m = li.querySelector('.ml-more'); m.open = true; const b = m.querySelector('button[value=remove]'); b.focus(); b.click(); return 0; })()`);
await waitFor(`!!Alpine.store('confirm').message`, 3000);
await evaluate(`Alpine.store('confirm').respond(false); 0`);
await sleep(400);
const focusAfter = await evaluate(`(() => { const a = document.activeElement; return { tag: a.tagName, inCard: !!(a.closest && a.closest('[data-asset]')) }; })()`);
check("confirm.cancel-keeps-focus", focusAfter.tag !== "BODY" && focusAfter.inCard, focusAfter);

// A file a draft needs still lists Remove in its menu, disabled, with the reason.
const blocked = await evaluate(`(() => { const b = document.querySelector('#media-results .ml-more button[disabled]'); if (!b) return { none: true }; const why = document.getElementById(b.getAttribute('aria-describedby')); return { label: b.textContent.trim(), why: why && why.textContent.trim() }; })()`);
check("media.blocked-remove-says-why", !blocked.none && blocked.label === "Remove" && /draft/.test(blocked.why || ""), blocked);

check("console.no-errors", consoleErrors.filter((e) => !/favicon|ERR_BLOCKED_BY_CLIENT|status of 4\d\d|leather-sandals|CORS/.test(e)).length === 0, consoleErrors.slice(0, 5));
const failed = results.filter((r) => !r.pass);
writeFileSync(join(outDir, "studio_gallery_qa.json"), JSON.stringify({ results, log }, null, 2));
console.log(`${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
