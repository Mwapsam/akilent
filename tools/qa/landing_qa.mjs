// Landing page browser QA: screenshots at 360 to 1440 (and phone landscape), no sideways
// scroll, the contrast and touch-target scan on the settled page, keyboard (skip link, menu +
// Esc, FAQ), reduced motion and no-JavaScript. Logged out; other hosts blocked.
// Usage: node tools/qa/landing_qa.mjs <repoRoot> <outDir>
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir] = process.argv.slice(2);
const BASE = "http://127.0.0.1:8000";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9344;
const profile = join(outDir, "chrome-profile-landing");
const shots = join(repo, "docs/design/qa/landing");
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

// Logged out, every request to another host blocked (fonts and CSS are local).
await send("Fetch.enable", { patterns: [{ urlPattern: "*", requestStage: "Request" }] });
rules = [{ match: (r) => !/^(127\.0\.0\.1|localhost)$/.test(new URL(r.url).hostname), act: { fail: "BlockedByClient" } }];
await send("Network.clearBrowserCookies");
const results = [];
const check = (id, pass, detail) => { results.push({ id, pass: !!pass, detail }); process.stderr.write(`${pass ? "PASS" : "FAIL"} ${id} ${detail ? JSON.stringify(detail) : ""}\n`); };
const shot = async (name, full) => {
  let clip;
  if (full) {
    const m = await send("Page.getLayoutMetrics");
    // At 1x: a phone's 3x pixel ratio made a full-page shot tens of thousands of pixels tall.
    const dpr = await evaluate("window.devicePixelRatio");
    clip = { x: 0, y: 0, width: m.cssContentSize.width, height: Math.min(m.cssContentSize.height, 16000), scale: 1 / dpr };
  }
  writeFileSync(join(shots, name + ".jpg"), Buffer.from((await send("Page.captureScreenshot", { format: "jpeg", quality: 70, ...(full ? { clip, captureBeyondViewport: true } : {}) })).data, "base64"));
};
// Reveal everything (as scrolling would) so the scan and the full-page shot see the settled page.
const settle = async () => {
  await evaluate("(async () => { const h = document.documentElement.scrollHeight; for (let y = 0; y < h; y += 500) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 40)); } document.querySelectorAll('.lp-reveal').forEach(e => e.classList.add('in')); window.scrollTo(0, 0); })()");
  await sleep(1300);
};
const scan = async () => { await evaluate(scanner); const r = await evaluate("window.__uiScan()"); return { contrast: r.contrast.failing, ex: r.contrast.examples, overflow: r.overflow.count, offenders: r.overflow.offenders, under24: r.targets.under24, small: r.targets.examples }; };

const widths = [[360, 740, true], [390, 844, true], [844, 390, true], [768, 1024, true], [1024, 768, false], [1280, 800, false], [1440, 900, false]];
for (const [w, h, mobile] of widths) {
  await viewport(w, h, mobile);
  await go("/", 1500);
  await shot(`landing-${w}x${h}-top`);
  await settle();
  const st = await evaluate("({ hscroll: document.documentElement.scrollWidth - document.documentElement.clientWidth, h1: Math.round(parseFloat(getComputedStyle(document.querySelector('h1')).fontSize)) })");
  const sc = await scan();
  check(`layout.${w}x${h}`, st.hscroll <= 0 && sc.overflow === 0, { ...st, overflow: sc.overflow, offenders: sc.offenders });
  check(`contrast.${w}x${h}`, sc.contrast === 0, sc.ex);
  // Links inside a sentence are exempt from the 24px target size (WCAG 2.5.8, inline exception).
  const smallTargets = (sc.small || []).filter((t) => t.belowAA && !/^a\.lp-link /.test(t.el));
  check(`targets.${w}x${h}`, smallTargets.length === 0, smallTargets);
  await shot(`landing-${w}x${h}-full`, true);
}

// A real Enter (with its character), which is what activates a <button> or a <summary>.
const pressEnter = async () => {
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: String.fromCharCode(13) });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
};
// Keyboard: the skip link comes first and shows; the menu opens and Esc closes it; FAQ opens with Enter.
await viewport(390, 844, true);
await go("/", 1200);
await key("Tab", "Tab", 9);
let st = await evaluate("({ el: document.activeElement.className, visible: document.activeElement.getBoundingClientRect().top >= 0 })");
check("keyboard.skip-link-first", /lp-skip/.test(st.el) && st.visible, st);
await evaluate("document.getElementById('menu-button').focus()");
await pressEnter();
await sleep(400);
st = { open: await evaluate("document.getElementById('menu-button').getAttribute('aria-expanded')") };
for (const type of ["rawKeyDown", "keyUp"]) await send("Input.dispatchKeyEvent", { type, key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
await sleep(400);
st.after = await evaluate("document.getElementById('menu-button').getAttribute('aria-expanded')");
st.focus = await evaluate("document.activeElement.id");
check("keyboard.menu-esc", st.open === "true" && st.after === "false" && st.focus === "menu-button", st);
await evaluate("document.querySelector('#faq summary').focus()");
await pressEnter();
await sleep(300);
st = { open: await evaluate("document.querySelector('#faq details').open"), ring: await evaluate("getComputedStyle(document.activeElement).outlineStyle") };
check("keyboard.faq-enter", st.open === true && st.ring !== "none", st);

// Reduced motion: nothing hidden waiting for an animation.
await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
await viewport(1280, 800, false);
await go("/", 1200);
st = await evaluate("({ hidden: [...document.querySelectorAll('.lp-reveal')].filter(e => getComputedStyle(e).opacity !== '1').length, anim: [...document.querySelectorAll('.lp-float, .lp-window')].filter(e => getComputedStyle(e).animationName !== 'none').length })");
check("motion.reduced", st.hidden === 0 && st.anim === 0, st);
await send("Emulation.setEmulatedMedia", { features: [] });

// No JavaScript: everything is visible (the hidden start state is only under html.js).
await send("Emulation.setScriptExecutionDisabled", { value: true });
await go("/", 1200);
await send("Emulation.setScriptExecutionDisabled", { value: false });
st = await evaluate("({ js: document.documentElement.classList.contains('js'), hidden: [...document.querySelectorAll('.lp-reveal')].filter(e => getComputedStyle(e).opacity !== '1').length })");
check("nojs.everything-visible", !st.js && st.hidden === 0, st);

writeFileSync(join(outDir, "landing_qa_results.json"), JSON.stringify({ results }, null, 1));
console.log(JSON.stringify({ pass: results.filter((r) => r.pass).length, fail: results.filter((r) => !r.pass).map((r) => r.id) }));
process.exit(0);
