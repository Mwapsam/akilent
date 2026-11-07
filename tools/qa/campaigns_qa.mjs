// Campaigns plan browser QA: drives locally installed Chrome (headless) over the DevTools
// protocol. Every POST is intercepted unless a mode lets one through, so nothing is sent.
// Usage: node tools/qa/campaigns_qa.mjs <repoRoot> <outDir>
//   QA_BLANKS=1 Phase B: "Fill in the blanks" shows the text box only for fixed text, and the
//               fallback (filled in, required) only for a customer detail;
// Needs a dev server with WHATSAPP_ENABLED=True (QA_BASE, default :8001) and QA_UI_EMAIL /
// QA_UI_PASSWORD in .env.
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir] = process.argv.slice(2);
const BASE = process.env.QA_BASE || "http://127.0.0.1:8001";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9336;
const profile = join(outDir, "chrome-profile-campaigns");
const shots = join(repo, "docs/design/qa/campaigns");
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
  if (!p) throw new Error("no element " + sel + " (rows: " + JSON.stringify(await evaluate("[...document.querySelectorAll('.inbox-row')].map(a => a.getAttribute('href'))")) + ", path: " + (await evaluate("location.pathname")) + ", requests: " + JSON.stringify(log.slice(-8)) + ")");
  for (const type of ["mousePressed", "mouseReleased"]) await send("Input.dispatchMouseEvent", { type, x: p.x, y: p.y, button: "left", clickCount: 1 });
}
async function key(k, code, keyCode) {
  for (const type of ["keyDown", "keyUp"]) await send("Input.dispatchKeyEvent", { type, key: k, code, windowsVirtualKeyCode: keyCode });
}
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
const shot0 = async (name) => writeFileSync(join(shots, name + ".jpg"), Buffer.from((await send("Page.captureScreenshot", { format: "jpeg", quality: 60 })).data, "base64"));

const composer = (expr) => evaluate(`(() => { const c = Alpine.$data(document.getElementById('composer-form')); return (${expr}); })()`);
// The composer at step 2 with the first audience and the first template that has blanks.
async function toBlanks() {
  await go("/whatsapp/campaigns/new/", 1500);
  const pick = await evaluate(`(() => { const a = document.querySelector('input[name=audience]'); const t = [...document.querySelectorAll('input[name=template_id]:not(:disabled)')]
    .find(i => document.querySelector('[name^="var__' + i.value + '__"]')); return { audience: a && a.value, template: t && t.value }; })()`);
  if (!pick.audience || !pick.template) { console.log("FAIL no audience or no template with blanks", JSON.stringify(pick)); process.exit(1); }
  await composer(`(c.audience = ${JSON.stringify(pick.audience)}, c.template = ${JSON.stringify(pick.template)}, c.go(2))`);
  await sleep(500);
  return pick.template;
}
const blankState = (tpl) => evaluate(`(() => { const box = [...document.querySelectorAll('[data-blank]')].find(b => b.dataset.blank.startsWith(${JSON.stringify(tpl + "__")}));
  const shown = (s) => { const e = box.querySelector(s); return !!e && !!e.offsetParent; };
  const fb = box.querySelector('[name^="fb__"]');
  return { source: box.querySelector('[data-blank-source]') ? box.querySelector('[data-blank-source]').value : null, literalBox: shown('[name^="lit__"]'), literalWarning: shown('[data-literal-warning]'),
    fallback: shown('[name^="fb__"]'), fallbackValue: fb && fb.value, fallbackRequired: !!fb && fb.required }; })()`);

if (process.env.QA_BLANKS) {
  const out = {};
  await viewport(1280, 900, false);
  const tpl = await toBlanks();
  out.start = await blankState(tpl);
  await evaluate(`(() => { const s = [...document.querySelectorAll('[data-blank-source]')].find(e => e.name.startsWith(${JSON.stringify("var__" + tpl + "__")})); s.value = 'literal'; s.dispatchEvent(new Event('change', { bubbles: true })); })()`);
  await sleep(250);
  out.fixedText = await blankState(tpl);
  await evaluate(`(() => { const s = [...document.querySelectorAll('[data-blank-source]')].find(e => e.name.startsWith(${JSON.stringify("var__" + tpl + "__")})); s.value = 'first_name'; s.dispatchEvent(new Event('change', { bubbles: true })); })()`);
  await sleep(250);
  out.backToName = await blankState(tpl);
  await sleep(1500); // the preview re-reads after a change (hx-trigger on the form)
  out.preview = await evaluate(`(() => { const p = document.querySelector('#estimate'); return p ? p.innerText.replace(/\s+/g, ' ').slice(0, 160) : null; })()`);
  await shot0("blanks-1280");
  console.log(JSON.stringify(out, null, 1));
  process.exit(0);
}
