// Phase 7 (Insights) browser QA: Insights, the policy pages and the dashboard pass the layout,
// contrast and target scan at desktop and phone width in both themes; the page and each insight
// say how fresh they are; a week with nothing to measure is a break in the chart (a dash in its
// table, "No data" on hover), never a zero; removing a goal or deleting a policy asks first; the
// policy editor's fields are named and leaving it with a change asks; a failed load of the
// dashboard's panels shows an error with Try again, which then loads them. Every POST is answered
// by the harness (nothing is written); other hosts are blocked. Data: p7_seed.py.
// Usage: node tools/qa/insights_qa.mjs <repoRoot> <outDir> <policyId> <gapIndex> <missingIndex>   (QA_UI_* in .env)
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir, policyId, gapArg, missingArg] = process.argv.slice(2);
const gapIndex = Number(gapArg), missingIndex = Number(missingArg);
const BASE = "http://127.0.0.1:8000";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9347;
const profile = join(outDir, "chrome-profile-p7");
const shots = join(repo, "docs/design/qa/phase7");
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
    // A delay alone lets the request through late; a delay with an answer gives that answer late.
    if (act && act.delay) await sleep(act.delay);
    if (act && act.delay && !act.status && !act.fail) await send("Fetch.continueRequest", { requestId });
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


let dialogs = 0;
ws.addEventListener("message", (ev) => {
  const m = JSON.parse(ev.data);
  if (m.method === "Page.javascriptDialogOpening") { dialogs++; send("Page.handleJavaScriptDialog", { accept: true }).catch(() => {}); }
});
const scan = async () => {
  await evaluate(scanner);
  const r = await evaluate("window.__uiScan()");
  return { contrast: r.contrast.failing, ex: r.contrast.examples, overflow: r.overflow.count, offenders: r.overflow.offenders, under24: r.targets.under24, small: r.targets.examples };
};
const blockOthers = { match: (r) => !/^(127\.0\.0\.1|localhost)$/.test(new URL(r.url).hostname), act: { fail: "BlockedByClient" } };
const PAGES = ["/insights/", "/insights/policies/", `/insights/policies/${policyId}/edit/`, "/dashboard/"];

// 1. Every page, desktop and phone, dark (the QA user's theme) and light: no sideways scroll,
//    no contrast failures, no tiny targets.
for (const theme of ["dark", "light"]) {
  for (const [w, h, mobile] of [[1280, 800, false], [390, 844, true]]) {
    await viewport(w, h, mobile);
    for (const path of PAGES) {
      await go(path, 1800);
      await evaluate(`document.documentElement.setAttribute('data-theme', '${theme}')`);
      await sleep(300);
      const sc = await scan();
      const hs = await evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth");
      check(`scan.${theme}.${w}.${path}`, hs <= 0 && sc.overflow === 0 && sc.contrast === 0 && sc.under24 === 0, { hs, ...sc });
      await shot(`p7-${theme}-${w}-${path.replace(/[^a-z0-9]+/gi, "_")}`);
    }
  }
}
await viewport(1280, 800, false);

// 2. Freshness: the page and each insight say when they're from.
await go("/insights/", 1500);
let st = await evaluate(`({ asOf: (document.querySelector('[data-insights="as-of"]') || {}).textContent, spotted: !!document.querySelector('[data-insight="spotted"] time'), momentum: (document.querySelector('[data-momentum="as-of"]') || {}).textContent })`);
check("fresh.as-of-and-spotted", /Figures as of/.test(st.asOf || "") && st.spotted && /Counted up to the week of/.test(st.momentum || ""), st);

// 3. The reply chart: the week with no reply to time and the missing week are breaks, not zeros.
const replyChart = `document.querySelector('#momentum-h').closest('section').querySelector('figure.chart')`;
st = await evaluate(`(() => { const f = ${replyChart}; const path = f.querySelector('svg path[fill="none"]').getAttribute('d');
  const cells = Array.from(f.querySelectorAll('.chart-table tbody tr')).map(tr => tr.children[1].textContent.trim());
  return { runs: (path.match(/M/g) || []).length, dots: f.querySelectorAll('svg path[stroke-width="7"]').length, cells }; })()`);
check("chart.gaps-not-zeros", st.runs === 3 && st.dots === 3 && st.cells[gapIndex] === "—" && st.cells[missingIndex] === "—" && !st.cells.includes("0"), st);

// 4. Hovering a gap says "No data" and draws no marker.
await evaluate(`(() => { const f = ${replyChart}; f.scrollIntoView({ block: 'center' }); Alpine.$data(f).active = ${gapIndex}; })()`);
await sleep(300);
st = await evaluate(`(() => { const f = ${replyChart}; return { value: f.querySelector('.chart-tooltip-value').textContent.trim(), r: f.querySelector('g circle').getAttribute('r') }; })()`);
check("chart.gap-tooltip", st.value === "No data" && st.r === "0", st);
await shot("p7-chart-gap-tooltip");
await evaluate(`Alpine.$data(${replyChart}).active = null`);

// 5. Removing a goal asks first; Cancel keeps it (and nothing is sent).
// Not counting the page's own usage beacon (/internal/ux-event/), which is never a change.
const posts = () => log.filter((l) => l.startsWith("POST ") && !l.includes("/internal/ux-event/")).length;
let before = posts();
await clickSel('form[action$="/delete/"][data-confirm] button');
await sleep(500);
st = { asked: await evaluate("Alpine.store('confirm').open && Alpine.store('confirm').message") };
await shot("p7-goal-remove-confirm");
await evaluate("Alpine.store('confirm').respond(false)");
await sleep(300);
st.sent = posts() - before;
check("goal.remove-asks", /Remove the Leads this month goal\?/.test(st.asked || "") && st.sent === 0, st);

// 6. Deleting a policy asks the same way.
await go("/insights/policies/", 1500);
before = posts();
await clickSel('form[data-confirm] button.btn-danger-ghost');
await sleep(500);
st = { asked: await evaluate("Alpine.store('confirm').open && Alpine.store('confirm').message") };
await evaluate("Alpine.store('confirm').respond(false)");
await sleep(300);
st.sent = posts() - before;
check("policy.delete-asks", /Delete the policy/.test(st.asked || "") && st.sent === 0, st);

// 7. The policy editor: the number field has a name, and leaving with a change asks.
await go(`/insights/policies/${policyId}/edit/`, 1500);
st = await evaluate(`(() => { const i = document.querySelector('input[name=days]'); return { name: i.labels && i.labels[0] && i.labels[0].textContent.replace(/\\s+/g, ' ').trim() }; })()`);
await evaluate(`(() => { const i = document.querySelector('input[name=days]'); i.value = '45'; i.dispatchEvent(new Event('input', { bubbles: true })); })()`);
await clickSel('a[aria-label="Back to policies"]');
await sleep(700);
st.asked = await evaluate("Alpine.store('confirm').open && Alpine.store('confirm').title");
await evaluate("Alpine.store('confirm').respond(false)");
await sleep(300);
st.stayed = await evaluate("location.pathname");
check("policy.editor-named-and-guarded", /Customer inactive for/.test(st.name || "") && st.asked === "Leave without saving?" && /\/edit\/$/.test(st.stayed), st);

// 8. The dashboard's panels: a failed load shows an error with Try again, not a skeleton forever;
//    Try again then loads them.
const PANELS = (r) => r.method === "GET" && new URL(r.url).pathname === "/dashboard/panels/";
rules = [{ match: PANELS, act: { status: 500, html: "<h1>Server Error</h1>" } }, blockOthers];
await go("/dashboard/", 2200);
st = await evaluate(`(() => { const r = document.getElementById('dashboard-panels'); return { busy: r.getAttribute('aria-busy'), title: (r.querySelector('[data-error-title]') || {}).textContent, retry: !!r.querySelector('[data-error-retry]'), skeleton: !!r.querySelector('.skeleton, .skeleton-stat') }; })()`);
await shot("p7-dashboard-panels-failed");
rules = [blockOthers];
await clickSel('#dashboard-panels [data-error-retry]');
await sleep(2500);
st.after = await evaluate(`(() => { const r = document.getElementById('dashboard-panels'); return { exists: !!r, error: !!(r && r.querySelector('[data-error-title]')), busy: r && r.getAttribute('aria-busy') }; })()`);
check("dashboard.panels-fail-then-retry", st.busy === null && st.title === "Couldn't load these panels" && st.retry && !st.skeleton && !st.after.error, st);

writeFileSync(join(outDir, "insights_qa_results.json"), JSON.stringify({ results, log }, null, 1));
console.log(JSON.stringify({ pass: results.filter((r) => r.pass).length, fail: results.filter((r) => !r.pass).map((r) => r.id) }));
process.exit(0);
