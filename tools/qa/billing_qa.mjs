// Phase 5 (billing) browser QA: plan cards show exactly what checkout charges, the plans page
// on a 360 phone, one click starts one checkout, the pending-payment page says what the
// payment record says (pending, can't check, failed, needs another step, stopped checking,
// not found), the bank-transfer form sends once, and a transfer in review isn't asked for again.
// No payment is made: checkout is answered by the harness, every POST is intercepted, other
// hosts are blocked; the only real request that writes is the local manual checkout (a
// pending manual payment row). Needs the state file from p5_seed.py (setup) and, for part B,
// its "review" mode.
// Usage: QA_PART=A|B node tools/qa/billing_qa.mjs <repoRoot> <outDir> <stateJson>   (QA_UI_* in .env)
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir, statePath] = process.argv.slice(2);
const BASE = "http://127.0.0.1:8000";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9339;
const profile = join(outDir, "chrome-profile-p5");
const shots = join(repo, "docs/design/qa/phase5");
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
const state = JSON.parse(readFileSync(statePath, "utf8"));
const part = process.env.QA_PART || "A";
const hscroll = "document.documentElement.scrollWidth - document.documentElement.clientWidth";
const scanPage = async () => {
  await evaluate(scanner);
  const r = await evaluate("window.__uiScan()");
  return { contrast: r.contrast.failing, ex: r.contrast.examples, overflow: r.overflow.count, small: r.inputs.under16px };
};
const cardPrice = (slug) => `(() => { const a = [...document.querySelectorAll('a[href*="plan=${slug}&"]')][0]; const card = a && a.closest('[x-data*=prices]');
  return card ? card.querySelector('[x-text="prices[period]"]').innerText.trim() : null; })()`;

if (part === "A") {
  // 1. Plans: the price on a card is exactly what checkout charges, for every term.
  await viewport(1280, 860, false);
  await go("/billing/plans/", 1500);
  let st = { periods: {} };
  for (const [value, label] of [["monthly", "Monthly"], ["quarterly", "Quarterly"], ["annually", "Annually"], ["biennial", "Every 2 years"]]) {
    const clicked = await evaluate(`(() => { const b = [...document.querySelectorAll('[aria-label="Billing period"] button')].find(b => b.innerText.trim() === ${JSON.stringify(label)}); if (!b) return false; b.click(); return true; })()`);
    if (!clicked) continue;
    await sleep(250);
    st.periods[value] = { shown: await evaluate(cardPrice("starter")), want: state.prices.starter[value],
      pressed: await evaluate(`[...document.querySelectorAll('[aria-label="Billing period"] button[aria-pressed=true]')].map(b => b.innerText.trim()).join()`) };
  }
  const rows = Object.values(st.periods);
  check("plans.card-price-is-checkout-price", rows.length >= 3 && rows.every((r) => r.shown === r.want), st);
  check("plans.period-selector-pressed-state", Object.entries(st.periods).every(([, r]) => r.pressed.split(",").length === 1), st);
  for (const theme of ["light", "dark"]) {
    await evaluate(`document.documentElement.dataset.theme = '${theme}'`);
    await sleep(250);
    st = await scanPage();
    check(`plans.scan-1280-${theme}`, st.contrast === 0 && st.overflow === 0, st);
  }
  await evaluate("delete document.documentElement.dataset.theme");

  // 2. Plans on a 360 phone: no sideways scroll (R1: the compare table used to widen it to 440).
  await viewport(360, 740, true);
  await go("/billing/plans/", 1500);
  st = await evaluate(`(() => ({ hscroll: ${hscroll}, layout: document.documentElement.scrollWidth, compare: !!document.getElementById('h-compare') }))()`);
  check("plans.phone-360-no-sideways-scroll", st.hscroll <= 0, st);
  for (const theme of ["light", "dark"]) {
    await evaluate(`document.documentElement.dataset.theme = '${theme}'`);
    await sleep(250);
    st = await scanPage();
    check(`plans.scan-360-${theme}`, st.contrast === 0 && st.overflow === 0, st);
  }
  await evaluate("delete document.documentElement.dataset.theme");
  await shot("plans-360");

  // 3. One click starts one checkout. Checkout itself is answered here (it would create a
  // payment and go to the provider): slow, so a second click lands while it's in flight.
  await viewport(1280, 860, false);
  await go("/billing/plans/", 1500);
  // Answered here after 2.5s: never reaches the server, so no payment is made and no provider called.
  const holdCheckout = { match: (r) => new URL(r.url).pathname === "/billing/checkout/", act: { delay: 2500, status: 200, html: "<!doctype html><title>QA checkout stub</title><p>stub" } };
  rules.unshift(holdCheckout);
  const before = log.filter((l) => l.startsWith("GET /billing/checkout/")).length;
  const sel = 'a[data-busy-link][href*="plan=starter&"]';
  await evaluate(`document.querySelector('${sel}').scrollIntoView({ block: 'center' })`);
  // Both clicks happen in the page, back to back, before the browser leaves it (a CDP mouse
  // click only returns once the navigation has finished).
  st = { under: await evaluate(`(() => { const r = document.querySelector('${sel}').getBoundingClientRect(); const e = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return e && e.closest('a') === document.querySelector('${sel}'); })()`) };
  st.busy = await evaluate(`(() => { const a = document.querySelector('${sel}'); a.click(); const busy = a.getAttribute('aria-busy'); a.click(); return busy; })()`);
  await sleep(3500);
  st.requests = log.filter((l) => l.startsWith("GET /billing/checkout/")).length - before;
  check("plans.one-click-one-checkout", st.busy === "true" && st.requests === 1, st);
  rules.splice(rules.indexOf(holdCheckout), 1);
  // Back from the provider (a restored page) is usable again.
  await go("/billing/plans/", 1200);
  st = { busyAfterReturn: await evaluate(`document.querySelectorAll('a[data-busy-link][aria-busy=true]').length`) };
  check("plans.busy-cleared-on-return", st.busyAfterReturn === 0, st);

  // 4. The pending page says what the payment record says, as it changes.
  const ret = "/billing/return/manual/?ref=" + encodeURIComponent(state.ref);
  const statusPath = "/billing/payments/" + state.ref + "/status/";
  const statusRule = { match: (r) => new URL(r.url).pathname === statusPath, act: { status: 200, body: { status: "pending" } } };
  rules.unshift(statusRule);
  await go(ret, 1200);
  const heading = "document.querySelector('#main h1, main h1, h1') && [...document.querySelectorAll('h1')].map(h => h.innerText.trim()).filter(Boolean).pop()";
  st = { h1: await evaluate(heading) };
  check("pending.says-confirming", st.h1 === "Confirming your payment", st);
  statusRule.act = { status: 500, body: { error: "x" } };
  await sleep(3500);
  st = { unreachable: await evaluate("/can't check right now/i.test(document.body.innerText)"), h1: await evaluate(heading) };
  check("pending.check-failure-said", st.unreachable && st.h1 === "Confirming your payment", st);
  statusRule.act = { status: 200, body: { status: "failed" } };
  await sleep(3500);
  st = { h1: await evaluate(heading), text: await evaluate("document.body.innerText.includes(\"You haven't been charged\")") };
  check("pending.failed-said", st.h1 === "The payment didn't go through" && st.text, st);
  statusRule.act = { status: 200, body: { status: "requires_action" } };
  await go(ret, 1200);
  await sleep(3500);
  st = { h1: await evaluate(heading) };
  check("pending.requires-action-said", st.h1 === "The payment needs another step", st);
  statusRule.act = { status: 200, body: { status: "pending" } };
  await go(ret, 1200);
  await evaluate("(() => { const el = document.querySelector('[x-data*=gaveUp]'); Alpine.$data(el).gaveUp = true; })()");
  await sleep(300);
  st = { h1: await evaluate(heading), noPayAgain: await evaluate("document.body.innerText.includes(\"Don't pay again\")") };
  check("pending.stopped-checking-said", st.h1 === "Not confirmed yet" && st.noPayAgain, st);
  rules.splice(rules.indexOf(statusRule), 1);
  await go("/billing/return/manual/?ref=QA-P5-unknown", 1200);
  st = { h1: await evaluate(heading), polls: log.filter((l) => l.includes("QA-P5-unknown/status")).length };
  check("pending.unknown-payment-said", st.h1 === "We couldn't find this payment" && st.polls === 0, st);
  await viewport(360, 740, true);
  await go(ret, 1200);
  st = await scanPage();
  check("pending.scan-360", st.contrast === 0 && st.overflow === 0, st);

  // 5. Bank transfer: the form sends once however often it's pressed. (The submit is a POST,
  // answered here; the GET to checkout is real and only creates a local manual payment.)
  await viewport(390, 844, true);
  await go("/billing/checkout/?plan=starter&period=monthly&method=manual", 1500);
  st = { title: await evaluate("document.title") };
  await evaluate("document.querySelector('form[data-busy-submit] [name=reference]').value = 'QA P5 double submit'");
  const submits = () => log.filter((l) => l.startsWith("POST /billing/manual/submit/")).length;
  const s0 = submits();
  const slow = { match: (r) => r.method === "POST" && new URL(r.url).pathname === "/billing/manual/submit/", act: { delay: 2500, status: 200, html: "<!doctype html><title>QA submit stub</title>" } };
  rules.unshift(slow);
  st.disabled = await evaluate(`(() => { const b = document.querySelector('form[data-busy-submit] [type=submit]'); b.click(); const d = b.disabled && b.getAttribute('aria-busy') === 'true'; b.click(); b.form.requestSubmit && (() => { try { b.form.requestSubmit(); } catch (e) {} })(); return d; })()`);
  await sleep(3200);
  st.posts = submits() - s0;
  check("transfer.one-submit", st.disabled === true && st.posts === 1, st);
  rules.splice(rules.indexOf(slow), 1);
} else {
  // 6. A bank transfer in review: the page says so and asks for no second payment.
  await viewport(1280, 860, false);
  await go("/billing/plans/", 1500);
  const st = await evaluate(`(() => ({ badge: [...document.querySelectorAll('.badge')].map(b => b.innerText.trim()).find(t => /review/i.test(t)),
    note: document.body.innerText.includes("You don't need to pay again"), ref: document.body.innerText.includes('QA P5 TX-0001'),
    checkoutLinks: document.querySelectorAll('a[href*="/billing/checkout/"]').length, inReview: [...document.querySelectorAll('span')].filter(s => s.innerText.trim() === 'Bank transfer in review').length }))()`);
  // The badge stays the subscription's own status (here a trial); "Payment in review" only
  // replaces Incomplete, which pytest covers.
  check("plans.transfer-in-review", st.note && st.ref && st.checkoutLinks === 0 && st.inReview > 0, st);
  await shot("plans-transfer-in-review");
}

writeFileSync(join(outDir, `billing_qa_results_${part}.json`), JSON.stringify({ results, log: log.slice(-40) }, null, 1));
console.log(JSON.stringify({ part, pass: results.filter((r) => r.pass).length, fail: results.filter((r) => !r.pass).map((r) => r.id) }));
process.exit(0);
