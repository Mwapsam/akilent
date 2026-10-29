// Phase 6 (automations + forms) browser QA: each status is labelled as the backend has it;
// the editor starts once (one save per edit), asks before an in-app navigation drops unsaved
// edits, keeps edits and says why when a save fails (server error, offline then back online),
// keeps edits made during a save unsaved, saves pending edits before Publish, and the form
// builder does the same; pause and resume really run and report their outcome. Save, publish
// and form-save POSTs are answered by the harness (nothing is written); pause/resume go to the
// dev server against the QA workflow from p6_seed.py. Other hosts are blocked.
// Usage: node tools/qa/automation_qa.mjs <repoRoot> <outDir> <workflowSlug> <formId>   (QA_UI_* in .env)
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir, wfSlug, formId] = process.argv.slice(2);
const BASE = "http://127.0.0.1:8000";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9346;
const profile = join(outDir, "chrome-profile-p6");
const shots = join(repo, "docs/design/qa/phase6");
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

const saves = () => log.filter((l) => /\/automations\/[^/]+\/save\/ ->/.test(l)).length;
const wfData = `Alpine.$data(document.querySelector('[x-data="workflowEditor()"]'))`;
const SAVE = (r) => r.method === "POST" && /\/automations\/[^/]+\/save\/$/.test(new URL(r.url).pathname);
const PUBLISH = (r) => r.method === "POST" && /\/publish\/$/.test(new URL(r.url).pathname);
const okSave = { status: 200, body: { ok: true, errors: [], status: "published", version: 1, has_changes: true } };
const blockOthers = { match: (r) => !/^(127\.0\.0\.1|localhost)$/.test(new URL(r.url).hostname), act: { fail: "BlockedByClient" } };
const passReal = { match: (r) => r.method === "POST" && /\/(pause|resume)\/$/.test(new URL(r.url).pathname), act: null };
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
const statusText = "(document.querySelector('[x-data=\"workflowEditor()\"] [role=status]') || {}).textContent";

// 1. Every page, desktop and phone: no sideways scroll, no contrast failures, no tiny targets.
for (const [w, h, mobile] of [[1280, 800, false], [390, 844, true]]) {
  await viewport(w, h, mobile);
  for (const path of ["/automations/", `/automations/${wfSlug}/`, `/automations/${wfSlug}/runs/`, `/automations/${wfSlug}/runs/?status=failed`,
    `/automations/${wfSlug}/stats/`, "/inbox/forms/", `/inbox/forms/${formId}/`]) {
    await go(path, 1500);
    const sc = await scan();
    const hs = await evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth");
    check(`scan.${w}.${path}`, hs <= 0 && sc.overflow === 0 && sc.contrast === 0 && sc.under24 === 0, { hs, ...sc });
    await shot(`p6-${w}-${path.replace(/[^a-z0-9]+/gi, "_")}`);
  }
}
await viewport(1280, 800, false);

// 2. Status labels on the list match the workflow (seeded as published, no pending edits).
await go("/automations/");
let st = await evaluate(`(() => { const c = document.getElementById('running-${wfSlug}'); return { badge: c && c.querySelector('h3 .badge').textContent.trim(), pending: !!c && /Unpublished changes/.test(c.textContent) }; })()`);
check("list.status-on", st.badge === "On" && !st.pending, st);

// 3. The editor starts once: one edit makes one save.
rules = [{ match: SAVE, act: okSave }, blockOthers];
await go(`/automations/${wfSlug}/`, 1500);
let before = saves();
await evaluate(`${wfData}.priority = 7`);
await sleep(3600);
check("editor.one-save-per-edit", saves() - before === 1, { saves: saves() - before });

// 4. Unsaved edits hold an in-app navigation and ask; "Stay" keeps them.
rules = [{ match: SAVE, act: { status: 200, delay: 60000, body: okSave.body } }, blockOthers];
await go(`/automations/${wfSlug}/`, 1500);
await evaluate(`${wfData}.priority = 8`);
await sleep(300);
st = { label: (await evaluate(statusText) || "").trim() };
await clickSel('nav a[href="/automations/"]').catch(() => clickSel('a[href="/automations/"]'));
await sleep(700);
st.asked = await evaluate("Alpine.store('confirm').open && Alpine.store('confirm').title");
await evaluate("Alpine.store('confirm').respond(false)");
await sleep(400);
st.stayed = await evaluate(`location.pathname === '/automations/${wfSlug}/' && ${wfData}.priority === 8`);
check("editor.leave-asks-and-stay-keeps", st.label === "Unsaved changes" && st.asked === "Leave without saving?" && st.stayed, st);

// 5. A failed save keeps the edits and says so (an HTML 500, which used to read as "offline").
rules = [{ match: SAVE, act: { status: 500, html: "<h1>Server Error</h1>" } }, blockOthers];
await go(`/automations/${wfSlug}/`, 1500);
await evaluate(`${wfData}.priority = 9`);
await evaluate(`${wfData}.save()`);
await sleep(1200);
st = await evaluate(`(() => { const d = ${wfData}; const a = document.querySelector('p.alert-danger[role=alert]'); return { msg: d.saveError, dirty: d.dirty, kept: d.priority === 9, shown: !!a && getComputedStyle(a).display !== 'none' }; })()`);
check("editor.server-error-keeps-edits", /Your changes are still here/.test(st.msg) && !/offline/i.test(st.msg) && st.dirty && st.kept && st.shown, st);
await shot("p6-editor-save-failed");

// 6. Offline: says so; back online, the edits save by themselves.
// The harness answers saves itself, so "offline" is both: the browser says offline and the save fails to connect.
rules = [{ match: SAVE, act: { fail: "InternetDisconnected" } }, blockOthers];
await send("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
await evaluate(`${wfData}.save()`);
await sleep(800);
st = { msg: await evaluate(`${wfData}.saveError`) };
rules = [{ match: SAVE, act: okSave }, blockOthers];
before = saves();
await send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
await evaluate("window.dispatchEvent(new Event('online'))");
await sleep(1500);
st.retried = saves() - before; st.dirty = await evaluate(`${wfData}.dirty`); st.err = await evaluate(`${wfData}.saveError`);
check("editor.offline-then-saves", /offline/i.test(st.msg) && st.retried >= 1 && !st.dirty && !st.err, st);

// 7. An edit made while a save is on its way stays unsaved afterwards.
rules = [{ match: SAVE, act: { ...okSave, delay: 1500 } }, blockOthers];
await evaluate(`${wfData}.priority = 10; ${wfData}.save()`);
await sleep(300);
await evaluate(`${wfData}.priority = 11`);
await sleep(1700);
st = await evaluate(`({ dirty: ${wfData}.dirty, saved: ${wfData}.saved })`);
check("editor.edit-during-save-stays-unsaved", st.dirty === true && st.saved === false, st);
await sleep(4500);

// 8. Publish with an edit still pending: saved first, then published, with no browser prompt.
rules = [{ match: SAVE, act: { ...okSave, delay: 600 } }, { match: PUBLISH, act: { status: 200, html: "<p>published</p>" } }, blockOthers];
await go(`/automations/${wfSlug}/`, 1500);
await evaluate("Alpine.store('wfChanges').pending = true");
await sleep(200);
const mark = log.length; dialogs = 0;
await evaluate(`${wfData}.priority = 12`);
await sleep(100);
await clickSel("form[data-wf-publish] button");
await sleep(2500);
const order = log.slice(mark).filter((l) => /\/(save|publish)\//.test(l)).map((l) => (/publish/.test(l) ? "publish" : "save"));
check("editor.publish-saves-first", order[0] === "save" && order.includes("publish") && dialogs === 0, { order, dialogs });

// 8b. The Code tab: Save clears "unsaved" and stays quiet (reading the code back in isn't an
// edit); Publish from it saves first and goes; broken code stops Publish and says why.
rules = [{ match: SAVE, act: okSave }, { match: PUBLISH, act: { status: 200, html: "<p>published</p>" } }, blockOthers];
await go(`/automations/${wfSlug}/`, 1500);
await evaluate(`(() => { const d = ${wfData}; d.advanced = true; d.syncToCode(); d.tab = 'code'; })()`);
await sleep(300);
before = saves();
await evaluate(`${wfData}.save()`);
await sleep(6500);
st = await evaluate(`({ dirty: ${wfData}.dirty, err: ${wfData}.saveError })`);
st.saves = saves() - before;
check("editor.code-tab-save-settles", st.dirty === false && !st.err && st.saves === 1, st);
await evaluate("Alpine.store('wfChanges').pending = true");
await sleep(200);
let mk = log.length;
await clickSel("form[data-wf-publish] button");
await sleep(2000);
let ord = log.slice(mk).filter((l) => /\/(save|publish)\//.test(l)).map((l) => (/publish/.test(l) ? "publish" : "save"));
check("editor.code-tab-publish", ord[0] === "save" && ord.includes("publish"), { ord });
await go(`/automations/${wfSlug}/`, 1500);
await evaluate(`(() => { const d = ${wfData}; d.advanced = true; d.tab = 'code'; d.codeText = '{ not json'; Alpine.store('wfChanges').pending = true; })()`);
await sleep(200);
mk = log.length;
await clickSel("form[data-wf-publish] button");
await sleep(1200);
ord = log.slice(mk).filter((l) => /\/(save|publish)\//.test(l));
st = { posts: ord.length, err: await evaluate(`${wfData}.saveError`), shown: await evaluate("(() => { const a = document.querySelector('p.alert-danger[role=alert]'); return !!a && getComputedStyle(a).display !== 'none'; })()") };
check("editor.broken-code-stops-publish-and-says-why", st.posts === 0 && /mistake/.test(st.err) && st.shown, st);

// 9. Pause and resume really run, report back, and the list says so.
rules = [passReal, blockOthers];
await go("/automations/", 1500);
await clickSel(`#running-${wfSlug} form[action$="/pause/"] button`);
await sleep(2500);
st = await evaluate(`(() => { const c = document.getElementById('running-${wfSlug}'); return { badge: c && c.querySelector('h3 .badge').textContent.trim() }; })()`);
check("list.pause", st.badge === "Paused", st);
await evaluate("window.__sawOn = false; window.__onPoll = setInterval(() => { if (document.body.innerText.includes('is on.')) window.__sawOn = true; }, 100)");
await clickSel(`#running-${wfSlug} form[action$="/resume/"]:not([data-confirm]) button`);
await sleep(2500);
st = await evaluate(`(() => { const c = document.getElementById('running-${wfSlug}'); return { badge: c && c.querySelector('h3 .badge').textContent.trim(), toast: window.__sawOn || document.body.innerText.includes('is on.') }; })()`);
check("list.resume", st.badge === "On" && st.toast, st);

// 10. Form builder: one save per edit, failures say so with Try again, leaving asks.
const fb = `Alpine.$data(document.querySelector('[x-data="formBuilder()"]'))`;
const FSAVE = (r) => r.method === "POST" && /\/inbox\/forms\/\d+\/save\/$/.test(new URL(r.url).pathname);
const fsaves = () => log.filter((l) => /\/inbox\/forms\/\d+\/save\/ ->/.test(l)).length;
rules = [{ match: FSAVE, act: { status: 200, body: { ok: true, questions: [], problems: [] } } }, blockOthers];
await go(`/inbox/forms/${formId}/`, 1500);
before = fsaves();
await evaluate(`${fb}.questions[0].label = 'Your full name'; ${fb}.changed()`);
await sleep(2200);
check("form.one-save-per-edit", fsaves() - before === 1, { saves: fsaves() - before });
// 10b. An edit during a slow save waits for it: never two form saves in flight at once.
rules = [{ match: FSAVE, act: { status: 200, delay: 2500, body: { ok: true, questions: [], problems: [] } } }, blockOthers];
await go(`/inbox/forms/${formId}/`, 1500);
before = fsaves();
// void: evaluate would otherwise wait for the save to finish.
await evaluate(`${fb}.questions[0].label = 'Full name'; void ${fb}.save()`);
await sleep(300);
await evaluate(`${fb}.questions[0].label = 'Full name please'; ${fb}.changed()`);
await sleep(1800);
st = { during: fsaves() - before };
await sleep(3500);
st.after = fsaves() - before; st.state = await evaluate(`${fb}.saveState`);
check("form.one-save-in-flight", st.during === 1 && st.after === 2 && st.state === "saved", st);

// 10c. Keys from the server land on the right questions: a blank question (dropped by the
// server) and a question added while the save was on its way don't take another's key.
rules = [{ match: FSAVE, act: { status: 200, delay: 1200, body: { ok: true, questions: [{ key: "your_name" }, { key: "budget" }], problems: [] } } }, blockOthers];
await go(`/inbox/forms/${formId}/`, 1500);
await evaluate(`(() => { const d = ${fb}; const blank = d.prepare({}); const b = d.prepare({ label: 'Budget' }); d.questions = [blank, d.questions[0], b]; window.__q = { blank, b }; d.save(); })()`);
await sleep(300);
await evaluate(`(() => { const d = ${fb}; const x = d.prepare({ label: 'Extra' }); d.questions.unshift(x); window.__q.x = x; })()`);
await sleep(1800);
st = await evaluate("({ blank: window.__q.blank.key, budget: window.__q.b.key, extra: window.__q.x.key })");
check("form.keys-land-on-the-right-question", st.blank === "" && st.budget === "budget" && st.extra === "", st);

await go(`/inbox/forms/${formId}/`, 1500);
rules = [{ match: FSAVE, act: { status: 500, html: "<h1>Server Error</h1>" } }, blockOthers];
await evaluate(`${fb}.questions[0].label = 'Name'; ${fb}.changed()`);
await sleep(2200);
st = await evaluate(`({ state: ${fb}.saveState, label: ${fb}.saveLabel(), retry: !!Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === 'Try again' && b.offsetParent) })`);
await clickSel('a[href="/inbox/"]').catch(() => {});
await sleep(700);
st.asked = await evaluate("Alpine.store('confirm').open && Alpine.store('confirm').title");
await evaluate("Alpine.store('confirm').respond(false)");
check("form.failure-retry-and-leave-asks", st.state === "error" && /still here/.test(st.label) && st.retry && st.asked === "Leave without saving?", st);
await shot("p6-form-save-failed");

writeFileSync(join(outDir, "automation_qa_results.json"), JSON.stringify({ results, log }, null, 1));
console.log(JSON.stringify({ pass: results.filter((r) => r.pass).length, fail: results.filter((r) => !r.pass).map((r) => r.id) }));
process.exit(0);
