// Phase 8 (contacts, interested customers, orders, starter packs, chatbots, support) browser QA:
// every page passes the layout, contrast and target scan at desktop and phone width in both
// themes; text fields are 16px on a phone (no iOS zoom); the CRM pipeline is one column on a
// phone; contacts go list → search → profile → delete (typed confirmation); orders say what the
// payment records say; the lead's current status is pressed; starter packs ask before automations
// go live; a chatbot allowed on no site says so. Every POST is answered by the harness (nothing is
// written); other hosts are blocked. Data: p8_seed.py.
// Usage: node tools/qa/small_areas_qa.mjs <repoRoot> <outDir> '<state JSON from p8_seed.py>'   (QA_UI_* in .env)
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir, stateArg] = process.argv.slice(2);
const BASE = "http://127.0.0.1:8000";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9348;
const profile = join(outDir, "chrome-profile-p8");
const shots = join(repo, "docs/design/qa/phase8");
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


const S = JSON.parse(stateArg);
const scan = async () => {
  await evaluate(scanner);
  const r = await evaluate("window.__uiScan()");
  return { contrast: r.contrast.failing, ex: r.contrast.examples, overflow: r.overflow.count, offenders: r.overflow.offenders, under24: r.targets.under24, small: r.targets.examples.filter((t) => t.belowAA) };
};
const blockOthers = { match: (r) => !/^(127\.0\.0\.1|localhost)$/.test(new URL(r.url).hostname), act: { fail: "BlockedByClient" } };
// Not counting the page's own usage beacon (/internal/ux-event/), which is never a change.
const posts = () => log.filter((l) => l.startsWith("POST ") && !l.includes("/internal/ux-event/")).length;
const PAGES = [
  "/contacts/", `/contacts/${S.contact}/`, "/contacts/lists/", `/contacts/lists/${S.list}/`, "/contacts/segments/", "/contacts/fields/",
  "/orders/", "/orders/?view=all", `/orders/${S.failed}/`, `/orders/${S.cancelled}/`, `/orders/${S.paid}/`,
  "/sales/", `/sales/leads/${S.lead}/`, "/automations/templates/",
  "/chatbot/", `/chatbot/${S.bot}/`, "/chatbot/new/", "/support/", "/support/portal/", "/support/portal/new/",
];

// 1. Every page at desktop and phone width, in both themes: no sideways scroll, no contrast
//    failures, no targets under 24px.
for (const theme of ["dark", "light"]) {
  for (const [w, h, mobile] of [[1280, 800, false], [390, 844, true]]) {
    await viewport(w, h, mobile);
    for (const path of PAGES) {
      await go(path, 1500);
      await evaluate(`document.documentElement.setAttribute('data-theme', '${theme}')`);
      // Long enough for the theme's colour transitions to finish, or the scan reads a mid-fade colour.
      await sleep(800);
      const sc = await scan();
      const hs = await evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth");
      check(`scan.${theme}.${w}.${path}`, hs <= 0 && sc.overflow === 0 && sc.contrast === 0 && sc.under24 === 0, { hs, ...sc });
      if (theme === "dark") await shot(`p8-${w}-${path.replace(/[^a-z0-9]+/gi, "_")}`);
    }
  }
}

// 2. Phone: every text field is at least 16px (iOS zooms the page on smaller ones).
await viewport(390, 844, true);
for (const path of ["/chatbot/new/", "/support/portal/new/", `/sales/leads/${S.lead}/`, `/support/`]) {
  await go(path, 1300);
  const small = await evaluate(`Array.from(document.querySelectorAll('main input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=color]), main select, main textarea'))
    .filter(e => e.offsetParent && parseFloat(getComputedStyle(e).fontSize) < 16).map(e => (e.name || e.id) + ':' + getComputedStyle(e).fontSize)`);
  check(`phone.no-zoom-fields.${path}`, small.length === 0, { small });
}

// 3. CRM pipeline on a phone: one stage per row, not squeezed columns.
await go("/sales/", 1300);
let st = await evaluate(`(() => { const g = document.querySelector('.pipeline-grid'); return g ? getComputedStyle(g).gridTemplateColumns.split(' ').length : null; })()`);
check("crm.pipeline-one-column-on-phone", st === null || st === 1, { columns: st });
await viewport(1280, 800, false);

// 4. Contacts (the reference): list → search → profile → delete asks you to type "delete".
await go(`/contacts/?q=${encodeURIComponent("QA P8")}`, 1500);
st = { found: await evaluate(`!!document.querySelector('a[href="/contacts/${S.contact}/"]')`) };
await go(`/contacts/${S.contact}/`, 1300);
st.profile = await evaluate("document.querySelector('h1') && document.querySelector('h1').textContent.trim()");
await go(`/contacts/${S.contact}/delete/`, 1300);
st.disabledUntilTyped = await evaluate(`(() => { const b = Array.from(document.querySelectorAll('main button')).find(x => /Delete for good/.test(x.textContent)); return b && b.disabled; })()`);
check("contacts.list-search-detail-delete", st.found && /QA P8/.test(st.profile || "") && st.disabledUntilTyped === true, st);

// 4b. The profile's order count opens that customer's orders, not everyone's.
await go(`/contacts/${S.contact}/`, 1300);
await clickSel(`a[href*="contact=${S.contact}"]`);
await sleep(1500);
st = await evaluate(`({ chip: !!document.querySelector('[data-orders=for-contact]'), rows: Array.from(document.querySelectorAll('table tbody tr a[href^="/orders/"]')).map(a => a.textContent.trim()) })`);
check("contacts.order-count-opens-their-orders", st.chip && st.rows.length === 3 && st.rows.includes(S.paid) && st.rows.includes(S.failed) && st.rows.includes(S.cancelled), st);

// 5. Orders: Needs attention is the current tab; a failed payment says so and offers a new link;
//    a cancelled order offers nothing to collect.
await go("/orders/", 1300);
st = { tab: await evaluate(`(document.querySelector('nav[aria-label="Order views"] [aria-current=page]') || {}).textContent`) };
await go(`/orders/${S.failed}/`, 1300);
st.failed = await evaluate(`({ alert: !!document.querySelector('[data-payment=failed]'), again: /Request payment again/.test(document.body.textContent), link: /Payment link created/.test(document.body.textContent), badge: (document.querySelector('h1 + p .badge') || {}).textContent })`);
await go(`/orders/${S.cancelled}/`, 1300);
st.cancelled = await evaluate(`({ said: !!document.querySelector('[data-payment=cancelled]'), offer: /Request payment/.test(document.body.textContent) })`);
await shot("p8-order-failed-payment");
check("orders.truthful-payment", /Needs attention/.test(st.tab || "") && st.failed.alert && st.failed.again && !st.failed.link && st.failed.badge === "Awaiting payment" && st.cancelled.said && !st.cancelled.offer, st);

// 6. Interested customers: the current status is pressed, not the page's amber action.
await go(`/sales/leads/${S.lead}/`, 1300);
st = await evaluate(`(() => { const b = document.querySelector('main button[name=status][aria-pressed=true]'); return { pressed: b && b.textContent.trim(), amber: b && b.classList.contains('btn-primary'), named: !!document.querySelector('label[for=deal-title]') && !!document.getElementById('deal-title') }; })()`);
check("crm.status-pressed-and-fields-named", st.pressed === "New" && !st.amber && st.named, st);

// 7. Starter packs: Activate says the automations go live, and Cancel sends nothing.
await go("/automations/templates/", 1300);
let before = posts();
const hasPack = await evaluate("!!document.querySelector('form[data-confirm] button')");
if (hasPack) {
  await clickSel("form[data-confirm] button");
  await sleep(500);
  st = { asked: await evaluate("Alpine.store('confirm').open && Alpine.store('confirm').message") };
  await shot("p8-starter-confirm");
  await evaluate("Alpine.store('confirm').respond(false)");
  await sleep(300);
  st.sent = posts() - before;
  check("starters.ask-before-going-live", /(go live straight away|changes you made to them are replaced)/.test(st.asked || "") && st.sent === 0, st);
} else {
  check("starters.ask-before-going-live", false, { reason: "no starter packs listed" });
}

// 8. A chatbot allowed on no site doesn't just say Active.
await go("/chatbot/", 1300);
st = await evaluate(`(() => { const a = Array.from(document.querySelectorAll('a[href="/chatbot/${S.bot}/"]'))[0]; return a && (a.querySelector('[data-bot-status]') || {}).textContent; })()`);
check("chatbot.no-sites-says-so", /no sites allowed/.test(st || ""), { badge: st });

writeFileSync(join(outDir, "small_areas_qa_results.json"), JSON.stringify({ results, log }, null, 1));
console.log(JSON.stringify({ pass: results.filter((r) => r.pass).length, fail: results.filter((r) => !r.pass).map((r) => r.id) }));
process.exit(0);
