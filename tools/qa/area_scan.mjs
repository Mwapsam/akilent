// Area sweep: signs in as the QA user, crawls the app from the sidebar (two levels, GET links
// only, skipping anything that acts: logout, delete, export, connect, oauth...), and scans each
// page at 360 and 1280 in light and dark: contrast, sideways scroll, clipped controls,
// unlabelled fields, phone fields under 16px. Requests to other hosts are blocked; POSTs faked.
// Usage: node tools/qa/area_scan.mjs <repoRoot> <outDir> [areaPrefix...]
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir] = process.argv.slice(2);
const BASE = "http://127.0.0.1:8000";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9336;
const profile = join(outDir, "chrome-profile-scan");
const shots = join(repo, "docs/design/qa/sweep");
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
const prefixes = process.argv.slice(4);
const SKIP = /logout|delete|remove|revoke|disconnect|connect|oauth|export|download|\.csv|\.txt|\.pdf|unsubscribe|impersonat|view-as|stop|toggle|archive|send\/|\/pay\/|checkout|callback|webhook|\/static\/|\/media\/|\/api\/|admin\/|\/feed\/|\/badge\/|\/messages\/$|\/typing\/|\/sse|\?/i;
const found = new Set();
const queue = [];
const add = (href) => {
  if (!href || !href.startsWith("/") || SKIP.test(href) || found.has(href)) return;
  found.add(href); queue.push(href);
};
await viewport(1280, 800, false);
await go("/dashboard/", 1500);
for (const h of await evaluate("[...document.querySelectorAll('nav a[href], aside a[href]')].map(a => a.getAttribute('href'))")) add(h);
// Second level: links inside each page's <main>.
for (const p of [...queue]) {
  await go(p, 700);
  for (const h of await evaluate("[...document.querySelectorAll('#main a[href]')].map(a => a.getAttribute('href'))")) add(h);
  if (found.size > 220) break;
}
const pages = [...found].filter((p) => !prefixes.length || prefixes.some((x) => p.startsWith(x)));
// Pages the crawl can't reach by links (QA_PAGES="/a/ /b/").
for (const p of (process.env.QA_PAGES || "").split(/\s+/).filter(Boolean)) if (!pages.includes(p)) pages.push(p);
process.stderr.write(`pages: ${pages.length}\n`);

const unlabelled = `[...document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea')].filter(e => e.offsetParent)
  .filter(e => !(e.labels && e.labels.length) && !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby') && !e.closest('label') && !e.title).map(e => e.name || e.id || e.type)`;
const clipped = `(() => { const vw = document.documentElement.clientWidth; const out = [];
  document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea').forEach(el => {
    const r = el.getBoundingClientRect(); if (!r.width || !r.height || getComputedStyle(el).visibility === 'hidden') return;
    if (r.left < -1 || r.right > vw + 1) { for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) { const o = getComputedStyle(a).overflowX; if (o !== 'visible') return; }
      out.push((el.innerText || el.getAttribute('aria-label') || el.name || el.tagName).trim().slice(0, 30)); } });
  return out.slice(0, 5); })()`;
const setTheme = (t) => send("Page.addScriptToEvaluateOnNewDocument", { source: `try { localStorage.setItem('akilent:theme', '${t}'); localStorage.setItem('welcomeTourSeen', '1'); } catch (e) {}` });
const rows = [];
for (const [w, h, mob] of [[360, 740, true], [1280, 800, false]]) {
  await viewport(w, h, mob);
  for (const theme of ["light", "dark"]) {
    await setTheme(theme);
    for (const p of pages) {
      try {
        await go(p, 700);
        await evaluate(scanner);
        const r = await evaluate("window.__uiScan({ max: 3 })");
        const row = { w, theme, p, at: await evaluate("location.pathname"), overflow: r.overflow.count, sw: r.overflow.scrollWidth,
          offenders: r.overflow.offenders, contrastFail: r.contrast.failing, ex: r.contrast.examples,
          i16: mob ? r.inputs.under16px : 0, unl: await evaluate(unlabelled), clip: mob ? await evaluate(clipped) : [] };
        if (row.overflow || row.contrastFail || row.i16 || row.unl.length || row.clip.length) rows.push(row);
      } catch (e) { rows.push({ w, theme, p, error: e.message }); }
    }
  }
}
writeFileSync(join(outDir, "area_scan.json"), JSON.stringify({ pages, rows }, null, 1));
const byPage = {};
for (const r of rows) {
  const k = r.p; byPage[k] = byPage[k] || [];
  byPage[k].push(`${r.w}/${r.theme}` + (r.error ? ` ERR ${r.error}` : `${r.overflow ? ` of=${r.sw}` : ""}${r.contrastFail ? ` cf=${r.contrastFail}` : ""}${r.i16 ? ` i16=${r.i16}` : ""}${r.unl.length ? ` unl=${r.unl.join(",")}` : ""}${r.clip.length ? ` clip=${r.clip.join("|")}` : ""}`));
}
console.log(JSON.stringify({ pages: pages.length, pagesWithIssues: Object.keys(byPage).length }));
for (const [k, v] of Object.entries(byPage)) console.log(k, "::", v.join(" ; "));
process.exit(0);
