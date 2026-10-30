// Phase 9 browser QA, in three modes:
//   public   — signed out: help, docs, legal, the "for" pages and the email unsubscribe page
//   user     — the QA user: message and request logs, and the email pages
//   operator — the local test operator (QA_UI_OP_* in .env): the Operator Console
// Every page passes the layout, contrast and target scan at desktop and phone width in both
// themes, and the first Tab reaches a skip link that moves focus into the content. Nothing is
// posted; other hosts are blocked.
// Usage: node tools/qa/phase9_qa.mjs <repoRoot> <outDir> <public|user|operator>   (QA_UI_* in .env)
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir, mode = "public"] = process.argv.slice(2);
const BASE = "http://127.0.0.1:8000";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9349;
const profile = join(outDir, "chrome-profile-p9");
const shots = join(repo, "docs/design/qa/phase9");
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
await evaluate(`(() => { const f=document.querySelector('input[type=password]').form; f.querySelector('input[name=username]').value=${JSON.stringify(mode === "operator" ? env.QA_UI_OP_EMAIL : env.QA_UI_EMAIL)};
  f.querySelector('input[type=password]').value=${JSON.stringify(mode === "operator" ? env.QA_UI_OP_PASSWORD : env.QA_UI_PASSWORD)}; })()`);
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


const scan = async () => {
  await evaluate(scanner);
  const r = await evaluate("window.__uiScan()");
  return { contrast: r.contrast.failing, ex: r.contrast.examples, overflow: r.overflow.count, offenders: r.overflow.offenders, under24: r.targets.under24, small: r.targets.examples.filter((t) => t.belowAA) };
};
if (mode === "public") {
  // Signed out: these pages are for visitors and customers, not the workspace.
  await send("Network.clearBrowserCookies");
}
const PAGES = {
  public: ["/help/", "/help/post-results/", "/docs/", "/docs/authentication/", "/privacy/", "/terms/", "/cookies/",
    "/data-deletion/", "/for/shared-inbox/", "/email/t/unsub/not-a-real-token/"],
  user: ["/logs/messages/", "/logs/requests/", "/email/campaigns/", "/email/domains/", "/email/templates/", "/email/assets/", "/email/webhooks/"],
  operator: ["/manage/", "/manage/businesses/", "/manage/payments/", "/manage/plans/", "/manage/testimonials/", "/manage/ux-events/",
    "/manage/pilot/", "/manage/health/", "/manage/audit/", "/manage/settings/", "/manage/chatbots/"],
}[mode];

// 1. Every page at desktop and phone width, in both themes: no sideways scroll, no contrast
//    failures, no targets under 24px.
// The public shell is always light (data-theme="light" on <html>): visitors never see it dark.
for (const theme of mode === "public" ? ["light"] : ["dark", "light"]) {
  for (const [w, h, mobile] of [[1280, 800, false], [390, 844, true]]) {
    await viewport(w, h, mobile);
    for (const path of PAGES) {
      await go(path, 1400);
      const status = await evaluate("document.title");
      await evaluate(`document.documentElement.setAttribute('data-theme', '${theme}')`);
      // Long enough for the theme's colour transitions to finish, or the scan reads a mid-fade colour.
      await sleep(800);
      const sc = await scan();
      const hs = await evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth");
      const where = await evaluate("location.pathname");
      check(`scan.${mode}.${theme}.${w}.${path}`, where.startsWith(path.split("?")[0].replace(/not-a-real-token\/$/, "")) && hs <= 0 && sc.overflow === 0 && sc.contrast === 0 && sc.under24 === 0, { where, title: status, hs, ...sc });
      if (theme === "dark" || mode === "public") await shot(`p9-${mode}-${w}-${path.replace(/[^a-z0-9]+/gi, "_")}`);
    }
  }
}
await viewport(1280, 800, false);

// 2. Keyboard: the first Tab lands on the skip link, which moves focus to the main content.
for (const path of PAGES.slice(0, 3)) {
  await go(path, 1200);
  await key("Tab", "Tab", 9);
  await sleep(150);
  const first = await evaluate("({ text: (document.activeElement.textContent || '').trim(), href: document.activeElement.getAttribute('href') })");
  let landed = null;
  if (/^#/.test(first.href || "")) {
    await key("Enter", "Enter", 13);
    await sleep(250);
    landed = await evaluate("document.activeElement && (document.activeElement.id || document.activeElement.tagName)");
  }
  check(`keyboard.skip-link.${mode}.${path}`, /skip/i.test(first.text) && !!landed && landed !== "BODY", { first, landed });
}

writeFileSync(join(outDir, `phase9_${mode}_qa_results.json`), JSON.stringify({ results, log }, null, 1));
console.log(JSON.stringify({ mode, pass: results.filter((r) => r.pass).length, fail: results.filter((r) => !r.pass).map((r) => r.id) }));
process.exit(0);
