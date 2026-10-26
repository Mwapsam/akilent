// Phase 4 (WhatsApp + Instagram) browser QA: connection states say what they are, template
// statuses are Meta's own with what each means, campaign recipients show what happened to
// each message (never "Sent" while only queued), the campaign page on a 375 phone, and the
// unsaved-work guard on the template builder, campaign composer and comment-rule form.
// Every POST is intercepted: nothing is sent to Meta or a customer.
// Needs WHATSAPP_ENABLED and the local "QA P4" data (a number, four qa_p4_ templates, a
// campaign with one recipient per outcome).
// Usage: node tools/qa/channels_qa.mjs <repoRoot> <outDir> <campaignId>   (QA_UI_* in .env)
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir, campaignId] = process.argv.slice(2);
const BASE = "http://127.0.0.1:8000";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9338;
const profile = join(outDir, "chrome-profile-p4");
const shots = join(repo, "docs/design/qa/phase4");
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
// A dirty page left by one check must not hold the next navigation on the browser's own
// "leave this page?" dialog (headless has nobody to answer it).
ws.addEventListener("message", (ev) => { const m = JSON.parse(ev.data); if (m.method === "Page.javascriptDialogOpening") send("Page.handleJavaScriptDialog", { accept: true }); });
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
const guardAsks = async (from, leaveSel) => {
  await clickSel(leaveSel);
  await sleep(900);
  const st = await evaluate("({ open: Alpine.store('confirm').open, title: Alpine.store('confirm').title || '', path: location.pathname })");
  if (st.open) { await evaluate("Alpine.store('confirm').respond(false)"); await sleep(300); }
  st.stayed = (await evaluate("location.pathname")) === from;
  return st;
};
const leaveFreely = async (from, leaveSel) => {
  await clickSel(leaveSel);
  await sleep(1500);
  return { open: await evaluate("Alpine.store('confirm').open"), path: await evaluate("location.pathname"), from };
};
const hscroll = "document.documentElement.scrollWidth - document.documentElement.clientWidth";
const scanContrast = async () => {
  await evaluate(scanner);
  const r = await evaluate("window.__uiScan()");
  return { failing: r.contrast.failing, ex: r.contrast.examples, overflow: r.overflow.count };
};

// 1. Connection states. A WhatsApp number shows its health; the details grid lines up.
await viewport(1280, 860, false);
await go("/whatsapp/numbers/", 1500);
let st = await evaluate(`(() => { const rows = [...document.querySelectorAll('dl > div.grid')].filter(d => d.offsetParent);
  const aligned = rows.length > 0 && rows.every(d => { const dt = d.querySelector('dt'), dd = d.querySelector('dd'); return dt && dd && Math.abs(dt.getBoundingClientRect().top - dd.getBoundingClientRect().top) < 12; });
  const card = [...document.querySelectorAll('.card h3')].find(h => /\\+260 97 000 0000/.test(h.innerText));
  return { rows: rows.length, aligned, badge: card && card.parentElement.querySelector('.badge')?.innerText.trim(), cols: rows[0] && getComputedStyle(rows[0]).gridTemplateColumns }; })()`);
check("numbers.details-grid-lines-up", st.aligned && st.badge && !/,/.test(st.cols || ","), st);
await go("/instagram/accounts/", 1500);
st = await evaluate(`(() => ({ badges: [...document.querySelectorAll('li.card .badge')].map(b => b.innerText.trim()) }))()`);
check("instagram.connection-badge-truthful", st.badges.length > 0 && !st.badges.includes("Sent") && st.badges.every((b) => ["Connected", "Connection expired", "Setup not finished"].includes(b)), st);

// 2. Template statuses: Meta's own, each with what it means, and how fresh they are.
await go("/email/templates/?channel=whatsapp", 1500);
st = await evaluate(`(() => { const rows = [...document.querySelectorAll('tbody tr')].filter(r => /qa_p4_/.test(r.innerText));
  return { rows: rows.map(r => r.querySelector('[data-label=Status]').innerText.replace(/\\s+/g, ' ').trim()), fresh: /every 30 minutes/.test(document.body.innerText) }; })()`);
const want = [/^Approved Ready to send/, /^Rejected Meta didn.t approve it/, /^Paused by Meta Meta paused it/, /^Pending Meta approval Meta is reviewing it/];
check("templates.status-says-what-it-means", st.rows.length === 4 && want.every((re) => st.rows.some((r) => re.test(r))) && st.fresh, st);

// 3. Campaign recipients: each row is its message's real outcome.
const camp = "/whatsapp/campaigns/" + campaignId + "/";
const rowsState = `(() => Object.fromEntries([...document.querySelectorAll('#recipients tbody tr')].map(r => [r.querySelector('[data-label=Customer]').innerText.trim(), r.querySelector('[data-label=Result]').innerText.trim()])))()`;
await go(camp, 1500);
st = await evaluate(rowsState);
check("campaign.recipients-real-outcome", st["QA P4 Failed"] === "Failed" && st["QA P4 Unconfirmed"] === "Not confirmed" && st["QA P4 Held"] === "Waiting"
  && st["QA P4 Queued"] === "Queued" && st["QA P4 Opted out"] === "Not sent" && !Object.values(st).includes("Sent"), st);
await go(camp + "?outcome=failed", 1500);
st = await evaluate(rowsState);
check("campaign.failed-filter", Object.keys(st).length === 1 && st["QA P4 Failed"] === "Failed", st);
await go(camp + "?outcome=sent", 1500);
st = await evaluate("({ rows: document.querySelectorAll('#recipients tbody tr').length, empty: /No recipients match/.test(document.getElementById('recipients').innerText) })");
check("campaign.sent-filter-empty-until-whatsapp-confirms", st.rows === 0 && st.empty, st);

// 4. The campaign page on a 375 phone: no sideways scroll, rows stack as cards, the long
// name wraps, and text passes contrast in both themes.
await viewport(375, 812, true);
await go(camp, 1500);
st = await evaluate(`(() => ({ hscroll: ${hscroll}, row: getComputedStyle(document.querySelector('#recipients tbody tr')).display, h1: document.querySelector('h1').getBoundingClientRect().right <= innerWidth }))()`);
check("campaign.phone-375", st.hscroll <= 0 && st.row !== "table-row" && st.h1, st);
await shot("campaign-375");
for (const theme of ["light", "dark"]) {
  await evaluate(`document.documentElement.dataset.theme = '${theme}'`);
  await sleep(300);
  st = await scanContrast();
  check(`campaign.contrast-375-${theme}`, st.failing === 0 && st.overflow === 0, st);
}
await evaluate("delete document.documentElement.dataset.theme");

// 5. Unsaved work. The template builder: clean on load, asks once something is typed.
await viewport(1280, 860, false);
await go("/whatsapp/templates/new/", 2500);
st = { dirtyOnLoad: await evaluate("akDirty.any()") };
st.leave = await leaveFreely("/whatsapp/templates/new/", "nav a[href='/dashboard/']");
check("builder.clean-page-leaves-freely", !st.dirtyOnLoad && !st.leave.open && st.leave.path === "/dashboard/", st);
await go("/whatsapp/templates/new/", 2500);
await evaluate(`(() => { document.getElementById('wa-template-form').hidden = false; const n = document.getElementById('tpl-name'); n.value = 'qa_p4_unsaved'; n.dispatchEvent(new Event('input', { bubbles: true })); })()`);
st = await guardAsks("/whatsapp/templates/new/", "nav a[href='/dashboard/']");
st.kept = await evaluate("document.getElementById('tpl-name').value");
check("builder.unsaved-guard-asks", st.open && /leave without saving/i.test(st.title) && st.stayed && st.kept === "qa_p4_unsaved", st);
// Its own submit (hx-boost="false", a real page submit) is not "leaving unsaved".
await evaluate("document.getElementById('wa-form').addEventListener('submit', (e) => e.preventDefault(), { once: true }); document.getElementById('wa-form').dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }))");
st = { guarded: await evaluate("akDirty.any()") };
check("builder.own-submit-not-guarded", !st.guarded, st);

// The campaign composer.
await go("/whatsapp/campaigns/new/", 2000);
st = { dirtyOnLoad: await evaluate("akDirty.any()") };
// A real click (trusted pointer events), as a person would choose another list.
await evaluate(`(() => { const r = [...document.querySelectorAll('#composer-form input[type=radio]:not(:checked):not([disabled])')].find(e => e.offsetParent); if (r) r.id = r.id || 'qa-p4-pick'; return !!r; })()`);
await clickSel("#composer-form input[type=radio]:not(:checked):not([disabled])#qa-p4-pick, #qa-p4-pick");
await sleep(300);
st.dirtyAfter = await evaluate("akDirty.any()");
st.ask = st.dirtyAfter ? await guardAsks("/whatsapp/campaigns/new/", "nav a[href='/dashboard/']") : null;
check("composer.unsaved-guard", !st.dirtyOnLoad && st.dirtyAfter && st.ask.open && st.ask.stayed, st);

// The comment-rule form.
await go("/instagram/rules/reply/new/", 1500);
st = { dirtyOnLoad: await evaluate("akDirty.any()") };
await evaluate(`(() => { const n = document.querySelector('form [name=name]'); n.value = 'QA P4 rule'; n.dispatchEvent(new Event('input', { bubbles: true })); })()`);
st.ask = await guardAsks("/instagram/rules/reply/new/", "nav a[href='/dashboard/']");
check("rule-form.unsaved-guard", !st.dirtyOnLoad && st.ask.open && st.ask.stayed, st);
st = await evaluate("({ labels: [...document.querySelectorAll('form input:not([type=hidden]), form select, form textarea')].filter(e => e.offsetParent).every(e => e.labels && e.labels.length), classes: [...document.querySelectorAll('form textarea')].map(t => t.className) })");
check("rule-form.fields-labelled", st.labels && st.classes.every((c) => /\btextarea\b/.test(c) && !/\binput\b/.test(c)), st);

// 6. The WhatsApp preview is WhatsApp's own screen in both themes.
for (const theme of ["light", "dark"]) {
  await evaluate(`document.documentElement.dataset.theme = '${theme}'`);
  await sleep(200);
  st = await evaluate(`(() => { const d = document.createElement('div'); d.className = 'wa-preview wa-chat wa-chat--compact'; d.innerHTML = '<div class="wa-bubble">x</div>'; document.body.appendChild(d);
    const b = getComputedStyle(d.firstChild); const r = { bg: b.backgroundColor, fg: b.color }; d.remove(); return r; })()`);
  check(`preview.whatsapp-colours-${theme}`, st.bg === (theme === "light" ? "rgb(255, 255, 255)" : "rgb(32, 44, 51)"), st);
}
await evaluate("delete document.documentElement.dataset.theme");

writeFileSync(join(outDir, "channels_qa_results.json"), JSON.stringify({ results, log: log.slice(-40) }, null, 1));
console.log(JSON.stringify({ pass: results.filter((r) => r.pass).length, fail: results.filter((r) => !r.pass).map((r) => r.id) }));
process.exit(0);
