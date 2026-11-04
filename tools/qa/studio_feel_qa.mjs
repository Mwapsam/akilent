// Content Studio app-feel, Phase 1: instant tabs, in-place swaps for Media and the composer,
// skeletons only on slow loads, tab revert on failure, today on the month calendar.
// Usage: node tools/qa/studio_feel_qa.mjs <repoRoot> <outDir>   (needs QA_UI_* in .env)
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir] = process.argv.slice(2);
const BASE = "http://127.0.0.1:8000";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9341;
const profile = join(outDir, "chrome-profile-feel");
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

// 1. A tab click shows at once, and the page swaps in place (no full load).
await go("/publishing/", 1500);
await evaluate("window.__keep = 'still here'; performance.clearMarks(); 0");
await clickSel("[data-instant-tabs] a[href='/publishing/planning/']");
const lag = await evaluate(`(() => { const c = performance.getEntriesByName('ak-tab-click')[0], a = performance.getEntriesByName('ak-tab-active')[0]; return c && a ? a.startTime - c.startTime : null; })()`);
await waitFor("location.pathname === '/publishing/planning/' && !document.querySelector('#main[aria-busy]')");
await sleep(600); // let the swap settle before the next click
check("tab.instant-under-50ms", lag !== null && lag < 50, { lag });
check("tab.swap-not-reload", (await evaluate("window.__keep")) === "still here" && (await activeTab()) === "Planning", { tab: await activeTab() });

// 2. Media and the composer no longer force a full page load.
for (const [sel, href, ready] of [
  ["[data-instant-tabs] a[href='/publishing/media/']", "/publishing/media/", "!!document.querySelector('[x-data^=mediaLibrary]')"],
  ["[data-instant-tabs] a[href='/publishing/']", "/publishing/", "true"],
  ["#main a.btn[href='/publishing/new/']", "/publishing/new/", "!!document.querySelector('.composer') && !!Alpine.$data(document.querySelector('.composer'))"],
]) {
  await clickSel(sel);
  const ok = await waitFor(`location.pathname === ${JSON.stringify(href)} && ${ready}`);
  await sleep(600);
  check(`swap.${href}`, ok && (await evaluate("window.__keep")) === "still here", { path: await evaluate("location.pathname") });
}

// 2b. The composer reached in place has its own styles and <main> layout (it came unstyled:
// its <style> lived only in <head>, and <main> kept the previous page's classes).
const composerLook = await evaluate(`(() => { const bar = document.querySelector('.cmp-bar'), pv = document.querySelector('.cmp-preview');
  return { bar: bar && getComputedStyle(bar).position, barBorder: bar && getComputedStyle(bar).borderTopStyle,
    preview: pv && getComputedStyle(pv).position, main: document.getElementById('main').className }; })()`);
await shot("composer-after-swap");
check("composer.styled-after-swap", composerLook.bar === "sticky" && composerLook.barBorder === "solid" && composerLook.preview === "sticky" && /composer-main/.test(composerLook.main), composerLook);

// 3. The composer's crop dialog is wired after an in-place swap (akilent.onPage).
await evaluate(`(() => { const c = document.createElement('canvas'); c.width = 400; c.height = 400; c.getContext('2d').fillRect(0,0,400,400);
  window.__cropResult = 'pending'; window._igCropOpen(c.toDataURL(), { ratio: 1 }, (r) => { window.__cropResult = r === null ? 'skipped' : 'cropped'; }); return 0; })()`);
await waitFor("!document.getElementById('ig-crop-modal').classList.contains('hidden')");
await clickSel("#ig-crop-skip");
check("composer.crop-wired-after-swap", await waitFor("window.__cropResult === 'skipped'"), { result: await evaluate("window.__cropResult") });

// 4. A slow page shows the tab's skeleton under the tabs; a fast one never does.
await go("/publishing/", 1500);
await evaluate(`window.__t0 = performance.now(); window.__skel = 0; document.addEventListener('htmx:afterSettle', () => { window.__loadMs = window.__loadMs || performance.now() - window.__t0; }, { once: true }); new MutationObserver(() => { if (document.querySelector('[data-page-skeleton]')) window.__skel++; }).observe(document.body, { childList: true, subtree: true }); 0`);
await evaluate("window.__t0 = performance.now(); 0");
await clickSel("[data-instant-tabs] a[href='/publishing/planning/']");
await waitFor("location.pathname === '/publishing/planning/'"); await sleep(300);
// Clicks during a page's view transition are ignored by the browser: wait for it to end.
await waitFor("!document.documentElement.classList.contains('ak-page-vt') && !document.querySelector('#main[aria-busy]')"); await sleep(150);
// A skeleton only for a load slower than 150ms (timed from the click to the swap).
const fast = { seen: await evaluate("window.__skel"), ms: await evaluate("Math.round(window.__loadMs || 0)") };
check("skeleton.only-on-slow-loads", fast.ms >= 150 || fast.seen === 0, fast);
rules.unshift({ match: (r) => r.method === "GET" && new URL(r.url).pathname === "/publishing/library/", act: { delay: 1200 } });
await clickSel("[data-instant-tabs] a[href='/publishing/library/']");
await sleep(500);
const mid = await evaluate(`({ skel: !!document.querySelector('[data-page-skeleton]'), tab: document.querySelector('[data-instant-tabs] a[aria-current="page"]')?.textContent.trim() })`);
await shot("skeleton-library");
await waitFor("location.pathname === '/publishing/library/' && !document.querySelector('[data-page-skeleton]')");
check("skeleton.on-slow-load", mid.skel && mid.tab === "Library", mid);
rules.shift();

// 5. A failed navigation puts the previous tab back.
rules.unshift({ match: (r) => r.method === "GET" && new URL(r.url).pathname === "/publishing/sales/", act: { status: 500, html: "<p>boom</p>" } });
await clickSel("[data-instant-tabs] a[href='/publishing/sales/']");
await sleep(800);
check("tab.reverts-on-error", (await activeTab()) === "Library" && (await evaluate("location.pathname")) === "/publishing/library/", { tab: await activeTab() });
rules.shift();

// 5b. Leaving the composer puts the normal <main> classes back, and the tab row sits at the
// same height on every tab (a header with a subtitle or buttons used to push it down).
await go("/publishing/new/", 1500);
await clickSel("#shell-nav-desktop a[href='/publishing/']"); // the composer has no tabs: the sidebar link
await waitFor("location.pathname === '/publishing/' && !document.querySelector('#main[aria-busy]')"); await sleep(600);
check("main.classes-restored-after-composer", await evaluate(`!/composer-main/.test(document.getElementById('main').className) && /max-w-7xl/.test(document.getElementById('main').className)`), await evaluate("document.getElementById('main').className"));
const tabTops = {};
for (const href of ["/publishing/", "/publishing/library/", "/publishing/calendar/", "/publishing/planning/", "/publishing/channels/", "/publishing/sales/", "/publishing/media/"]) {
  await waitFor("!document.documentElement.classList.contains('ak-page-vt') && !document.querySelector('#main[aria-busy]')"); await sleep(150);
  await clickSel(`[data-instant-tabs] a[href='${href}']`).catch(() => {});
  await waitFor(`location.pathname === ${JSON.stringify(href)} && !document.querySelector('#main[aria-busy]')`); await sleep(500);
  tabTops[href] = await evaluate(`Math.round(document.querySelector('[data-instant-tabs]').getBoundingClientRect().top + scrollY)`);
}
check("tabs.same-height-on-every-tab", new Set(Object.values(tabTops)).size === 1, tabTops);

// 5c. Quick tab clicks skip view transitions without unhandled errors (checked at the end).
for (const href of ["/publishing/library/", "/publishing/calendar/", "/publishing/planning/", "/publishing/"]) {
  await clickSel(`[data-instant-tabs] a[href='${href}']`).catch(() => {});
  await sleep(60);
}
await waitFor("location.pathname === '/publishing/' && !document.querySelector('#main[aria-busy]')"); await sleep(800);

// 6. Month calendar: today stands out.
await go("/publishing/calendar/?view=month", 1500);
const today = await evaluate(`(() => { const d = document.querySelector('.cal-m-day.cal-today'); if (!d) return null; const n = d.querySelector('.cal-m-num'); return { bg: getComputedStyle(d).backgroundColor, pill: getComputedStyle(n).backgroundColor }; })()`);
check("calendar.today-highlighted", today && today.bg !== "rgba(0, 0, 0, 0)" && today.pill !== "rgba(0, 0, 0, 0)", today);
await shot("calendar-month");

check("console.no-errors", consoleErrors.filter((e) => !/favicon|ERR_BLOCKED_BY_CLIENT|status of 500/.test(e)).length === 0, consoleErrors.slice(0, 5));
const failed = results.filter((r) => !r.pass);
writeFileSync(join(outDir, "studio_feel_qa.json"), JSON.stringify({ results, log }, null, 2));
console.log(`${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
