// Landing page browser QA: screenshots at 360 to 1440 (and phone landscape), no sideways
// scroll, the contrast and touch-target scan on the settled page, keyboard (skip link, menu +
// Esc, FAQ, platform tabs), the first paint, reveals, scroll-spy, the header button, the hero
// loop, the pinned "How it works" scene, pricing buttons, analytics events, the OS dark setting,
// reduced motion, no-JavaScript and console errors. Logged out; other hosts blocked.
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

// Console errors and uncaught exceptions anywhere in the run fail the "console" check at the end.
const consoleErrors = [];
ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.method === "Runtime.exceptionThrown") consoleErrors.push(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text);
  if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") consoleErrors.push(msg.params.args.map((a) => a.value || a.description).join(" "));
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
    // Sections off screen aren't drawn (content-visibility: auto); draw them all for the shot.
    await evaluate("document.querySelectorAll('.lp-section').forEach(s => s.style.contentVisibility = 'visible')");
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

// ---- Desktop behaviour (1280 x 800) ----
await viewport(1280, 800, false);
// The headline is in the first paint: no reveal on it, nothing animating it, fully opaque.
const domReady = once("Page.domContentEventFired");
await send("Page.navigate", { url: BASE + "/" });
await domReady;
st = await evaluate("(() => { const h = document.querySelector('h1'); const cs = getComputedStyle(h); return { opacity: cs.opacity, anim: cs.animationName, reveal: !!h.closest('.lp-reveal'), heroReveals: document.querySelectorAll('#hero .lp-reveal').length }; })()");
check("hero.first-paint", st.opacity === "1" && st.anim === "none" && !st.reveal && st.heroReveals === 0, st);
await sleep(1500);
// Jump, don't glide: the page scrolls smoothly, and a check mid-glide reads the wrong section.
await evaluate("document.documentElement.style.scrollBehavior = 'auto'");

// Reveals never leave a block faded: at each section, every block a quarter in view is opaque.
const anchors = ["channels", "how-it-works", "platform", "trust", "pricing", "faq", "developers"];
const faded = [];
for (const id of anchors) {
  await evaluate(`document.getElementById(${JSON.stringify(id)}).scrollIntoView()`);
  await sleep(450);
  const f = await evaluate("[...document.querySelectorAll('.lp-reveal')].filter(e => { const r = e.getBoundingClientRect(); const seen = Math.min(r.bottom, innerHeight) - Math.max(r.top, 0); return r.height && seen / Math.min(r.height, innerHeight) >= .25 && getComputedStyle(e).opacity !== '1'; }).map(e => e.className)");
  if (f.length) faded.push({ id, f });
}
check("motion.no-faded-blocks", faded.length === 0, faded);

// Scroll-spy names the section in view, and nothing over the sections without a nav link.
const spy = {};
for (const id of ["channels", "how-it-works", "platform", "pricing", "trust", "faq"]) {
  await evaluate(`window.scrollTo(0, document.getElementById(${JSON.stringify(id)}).getBoundingClientRect().top + scrollY - 40)`);
  await sleep(250);
  spy[id] = await evaluate("(document.querySelector('nav[aria-label=Primary] a.active') || {}).hash || ''");
}
check("nav.scroll-spy", spy.channels === "#channels" && spy["how-it-works"] === "#how-it-works" && spy.platform === "#platform"
  && spy.pricing === "#pricing" && spy.trust === "" && spy.faq === "", spy);

// The header button keeps one style from the top of the page to the bottom.
const navStyle = "(() => { const b = document.querySelector('.lp-btn-nav'); const cs = getComputedStyle(b); return cs.backgroundColor + '|' + cs.color + '|' + cs.borderColor; })()";
await evaluate("window.scrollTo(0, 0)"); await sleep(200);
const navTop = await evaluate(navStyle);
const navAlong = [];
for (const id of anchors) { await evaluate(`document.getElementById(${JSON.stringify(id)}).scrollIntoView()`); await sleep(150); navAlong.push(await evaluate(navStyle)); }
check("nav.one-button-style", navAlong.every((v) => v === navTop), { navTop, differs: navAlong.filter((v) => v !== navTop) });

// The hero loop plays while the scene is on screen and stops when it isn't.
await evaluate("window.scrollTo(0, 0)");
await sleep(5200);
const playing = await evaluate("document.querySelector('.lp-scene').classList.contains('play')");
await evaluate("window.scrollTo(0, document.body.scrollHeight)");
await sleep(600);
const stopped = await evaluate("(() => { const c = document.querySelector('.lp-scene').classList; return !c.contains('play') && !['s1','s2','s3','s4','s5'].some(k => c.contains(k)); })()");
check("hero.loop-stops-off-screen", playing && stopped, { playing, stopped });

// How it works: pinned on desktop, one picture at a time, the one for the step in the middle.
const pinned = [];
const stepIds = await evaluate("[...document.querySelectorAll('.lp-step')].map(s => s.id)");
for (const id of stepIds) {
  await evaluate(`(() => { const s = document.getElementById(${JSON.stringify(id)}); scrollTo(0, s.getBoundingClientRect().top + scrollY - innerHeight / 2 + s.offsetHeight / 2); })()`);
  await sleep(700);
  pinned.push(await evaluate(`(() => { const how = document.querySelector('.lp-how'); const shown = [...document.querySelectorAll('.lp-how-stage > .lp-step-art')].filter(a => getComputedStyle(a).opacity === '1'); const stage = document.querySelector('.lp-how-stage'); return { id: ${JSON.stringify(id)}, pinned: how.classList.contains('is-pinned'), shown: shown.length, active: (document.querySelector('.lp-step.is-active') || {}).id, stageTop: stage ? Math.round(stage.getBoundingClientRect().top) : null, inView: !!stage && stage.getBoundingClientRect().top >= 0 && stage.getBoundingClientRect().bottom <= innerHeight }; })()`));
}
// The frame scrolls in with the first step and out with the last; at every step it is whole on screen.
check("how.pinned-scene", pinned.every((x) => x.pinned && x.shown === 1 && x.active === x.id && x.inView), pinned);
const howHeight = await evaluate("Math.round(document.getElementById('how-it-works').getBoundingClientRect().height)");
// Was 2,610px before the pinned scene; the plan aims for about 1,400.
check("how.height", howHeight <= 1600, { howHeight });

// Platform tabs: one panel open; arrow keys move between tabs; #content-studio opens its tab.
await evaluate("document.querySelector('.lp-ptabs [role=tab]').focus()");
await key("ArrowRight", "ArrowRight", 39);
await sleep(100);
st = await evaluate("({ tablist: !document.querySelector('.lp-ptabs').hidden, open: [...document.querySelectorAll('.lp-panel')].filter(p => !p.hidden).map(p => p.id), focused: document.activeElement.getAttribute('aria-controls'), selected: document.querySelector('[role=tab][aria-selected=true]').getAttribute('aria-controls') })");
check("platform.tabs-keyboard", st.tablist && st.open.length === 1 && st.open[0] === st.focused && st.selected === st.focused, st);
const hasStudio = await evaluate("!!document.getElementById('content-studio')");
if (hasStudio) {
  await go("/?from=qa#content-studio", 900);
  await evaluate("document.documentElement.style.scrollBehavior = 'auto'");
  st = await evaluate("({ open: [...document.querySelectorAll('.lp-panel')].filter(p => !p.hidden).map(p => p.id), top: Math.round(document.querySelector('.lp-ptabs').getBoundingClientRect().top) })");
  check("platform.old-anchor-content-studio", st.open.length === 1 && st.open[0] === "content-studio" && st.top >= 0 && st.top < 200, st);
}
st = await evaluate("['channels','how-it-works','content-studio','features','pricing','faq','developers'].filter(id => !document.getElementById(id))");
check("anchors.old-addresses", st.length === 0 || (st.length === 1 && st[0] === "content-studio" && !hasStudio), { missing: st });

// Pricing buttons line up across the cards.
st = await evaluate("[...document.querySelectorAll('.lp-plan .lp-btn')].map(b => Math.round(b.getBoundingClientRect().top - b.closest('.plans').getBoundingClientRect().top))");
check("pricing.buttons-aligned", st.length > 0 && Math.max(...st) - Math.min(...st) <= 1, st);

// Analytics: every call to action sends cta_click with its name and place (gtag stubbed; the
// navigation itself is cancelled after the page's own handler has run).
await evaluate("window.__ev = []; window.gtag = (...a) => window.__ev.push(a); window.addEventListener('click', e => e.preventDefault())");
for (const sel of ["#hero [data-cta=start_trial]", "#hero [data-cta=book_demo]", ".lp-plan [data-cta=plan]", ".lp-final [data-cta=book_demo]", ".lp-header [data-cta=start_trial]"]) await clickSel(sel);
st = await evaluate("window.__ev.map(a => [a[0], a[1], a[2].cta, a[2].location, a[2].plan || ''].join(':'))");
check("analytics.cta-click", st.length === 5 && st.every((e) => e.startsWith("event:cta_click:")) && st.some((e) => /:plan:pricing:\w/.test(e)), st);

// The OS dark setting changes nothing: the page has one palette, and it passes contrast.
await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
await go("/", 1200);
await settle();
const dark = await scan();
check("contrast.os-dark", dark.contrast === 0, dark.ex);
await send("Emulation.setEmulatedMedia", { features: [] });

// Reduced motion: nothing hidden waiting for an animation, no loop, no pinned scene.
await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
await go("/", 1200);
await sleep(4500);
st = await evaluate("({ hidden: [...document.querySelectorAll('.lp-reveal')].filter(e => getComputedStyle(e).opacity !== '1').length, anim: [...document.querySelectorAll('.lp-float, .lp-window')].filter(e => getComputedStyle(e).animationName !== 'none').length, loop: document.querySelector('.lp-scene').classList.contains('play'), pinned: document.querySelector('.lp-how').classList.contains('is-pinned') })");
check("motion.reduced", st.hidden === 0 && st.anim === 0 && !st.loop && !st.pinned, st);
await send("Emulation.setEmulatedMedia", { features: [] });

// No JavaScript: everything is visible (the hidden start state is only under html.js).
await send("Emulation.setScriptExecutionDisabled", { value: true });
await go("/", 1200);
await send("Emulation.setScriptExecutionDisabled", { value: false });
st = await evaluate("({ js: document.documentElement.classList.contains('js'), hidden: [...document.querySelectorAll('.lp-reveal')].filter(e => getComputedStyle(e).opacity !== '1').length, tabs: getComputedStyle(document.querySelector('.lp-ptabs')).display, panels: [...document.querySelectorAll('.lp-panel')].filter(p => getComputedStyle(p).display === 'none').length, arts: document.querySelectorAll('.lp-step .lp-step-art').length === document.querySelectorAll('.lp-step').length, finalFrame: [...document.querySelectorAll('.lp-scene .k4, .lp-scene .k5')].every(e => getComputedStyle(e).opacity === '1') })");
check("nojs.everything-visible", !st.js && st.hidden === 0 && st.tabs === "none" && st.panels === 0 && st.arts && st.finalFrame, st);

check("console.no-errors", consoleErrors.length === 0, consoleErrors.slice(0, 5));

writeFileSync(join(outDir, "landing_qa_results.json"), JSON.stringify({ results }, null, 1));
console.log(JSON.stringify({ pass: results.filter((r) => r.pass).length, fail: results.filter((r) => !r.pass).map((r) => r.id) }));
process.exit(0);
