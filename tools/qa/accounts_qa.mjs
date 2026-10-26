// Phase 3 (accounts) browser QA: headless Chrome over the DevTools protocol.
//  - scans the account and settings screens at 390 and 1280, light and dark;
//  - signs up a throwaway account keyboard-only, changes a setting, and turns on two-factor
//    keyboard-only in dark mode (TOTP computed from the key the page shows).
// Safety: requests to any host but the local server are blocked; billing POSTs are faked;
// run the dev server with EMAIL_HOST=127.0.0.1 EMAIL_PORT=9 so no mail can leave.
// Usage: node tools/qa/accounts_qa.mjs <repoRoot> <outDir>   (needs QA_UI_* in .env)
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir] = process.argv.slice(2);
const BASE = "http://127.0.0.1:8000";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9335;
const profile = join(outDir, "chrome-profile-p3");
const shots = join(repo, "docs/design/qa/phase3");
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

import { createHmac } from "node:crypto";

// ---- Network: only the local server; billing POSTs are faked (no checkout is started). ----
let rules = [];
const log = [];
ws.addEventListener("message", async (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.method !== "Fetch.requestPaused") return;
  const { requestId, request } = msg.params;
  const u = new URL(request.url);
  let act = null;
  const rule = rules.find((r) => r.match(request));
  if (rule) act = rule.act;
  else if (u.hostname !== "127.0.0.1" && u.hostname !== "localhost") act = { fail: "BlockedByClient" };
  else if (request.method === "POST" && u.pathname.startsWith("/billing/")) act = { status: 200, body: { ok: true } };
  log.push(`${request.method} ${u.host}${u.pathname} -> ${act ? (act.fail || act.status) : "pass"}`);
  try {
    if (!act) await send("Fetch.continueRequest", { requestId });
    else if (act.fail) await send("Fetch.failRequest", { requestId, errorReason: act.fail });
    else await send("Fetch.fulfillRequest", { requestId, responseCode: act.status,
      responseHeaders: [{ name: "Content-Type", value: act.html !== undefined ? "text/html" : "application/json" }, ...(act.headers || [])],
      body: Buffer.from(act.html !== undefined ? act.html : JSON.stringify(act.body)).toString("base64") });
  } catch (e) { process.stderr.write("fetch handler: " + e.message + "\n"); }
});

const results = [];
const check = (id, pass, detail) => { results.push({ id, pass: !!pass, detail }); process.stderr.write(`${pass ? "PASS" : "FAIL"} ${id} ${detail ? JSON.stringify(detail) : ""}\n`); };
const shot = async (name) => writeFileSync(join(shots, name + ".jpg"), Buffer.from((await send("Page.captureScreenshot", { format: "jpeg", quality: 60 })).data, "base64"));

// Keyboard: real key events, as a keyboard user would press them.
async function press(k) {
  const map = { Tab: ["Tab", 9, ""], Enter: ["Enter", 13, "\r"], Space: [" ", 32, " "], Escape: ["Escape", 27, ""], ArrowDown: ["ArrowDown", 40, ""], ArrowRight: ["ArrowRight", 39, ""] };
  const [keyName, code, text] = map[k];
  await send("Input.dispatchKeyEvent", { type: text ? "keyDown" : "rawKeyDown", key: keyName, code: k === "Space" ? "Space" : keyName, windowsVirtualKeyCode: code, text: text || undefined });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: keyName, code: k === "Space" ? "Space" : keyName, windowsVirtualKeyCode: code });
  await sleep(60);
}
const typeText = async (t) => { await send("Input.insertText", { text: t }); await sleep(60); };
// Where focus is, and whether a keyboard user can see it.
const focusInfo = () => evaluate(`(() => { const a = document.activeElement; if (!a || a === document.body) return { tag: 'BODY' };
  const cs = getComputedStyle(a); const ring = (cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0) || (cs.boxShadow && cs.boxShadow !== 'none');
  return { tag: a.tagName, name: a.getAttribute('name') || '', text: (a.innerText || a.value || a.getAttribute('aria-label') || '').trim().slice(0, 40), ring }; })()`);
const noRing = [];
// Tab forward until the focused element matches; records any focus stop with no visible ring.
async function tabTo(pred, max = 60) {
  for (let i = 0; i < max; i++) {
    await press("Tab");
    const f = await focusInfo();
    if (f.tag !== "BODY" && !f.ring) noRing.push(f);
    if (pred(f)) return f;
  }
  throw new Error("tabTo: not reached");
}

// A keyboard user's first stop in the app shell: the skip link, then into the page.
async function skipToMain() {
  // A first-run dialog (the welcome tour) holds focus: Esc closes it, as a person would.
  if (await evaluate("!!document.activeElement && !!document.activeElement.closest('[role=dialog]')")) { await press("Escape"); await sleep(300); }
  await evaluate("document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0)");
  await press("Tab");
  const f = await focusInfo();
  if (/skip/i.test(f.text)) { await press("Enter"); await sleep(150); }
  return f;
}

function totp(base32) {
  const alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const ch of base32.replace(/=+$/, "").toUpperCase()) bits += alpha.indexOf(ch).toString(2).padStart(5, "0");
  const bytes = Buffer.from(bits.match(/.{8}/g).map((b) => parseInt(b, 2)));
  const counter = Buffer.alloc(8); counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 1000 / 30)));
  const h = createHmac("sha1", bytes).update(counter).digest();
  const o = h[h.length - 1] & 15;
  return String(((h.readUInt32BE(o) & 0x7fffffff) % 1000000)).padStart(6, "0");
}

await send("Page.enable"); await send("Runtime.enable");
await send("Fetch.enable", { patterns: [{ urlPattern: "*", requestStage: "Request" }] });
mkdirSync(shots, { recursive: true });

// ---------------------------------------------------------------------------------------
// 1. Sign up keyboard-only (light theme, 390 phone width), a throwaway account.
// ---------------------------------------------------------------------------------------
await viewport(390, 844, true);
await go("/signup/", 1500);
const cfg = await evaluate("JSON.parse(document.getElementById('signup-wizard-config').textContent)");
await evaluate("document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0)");
const firstChannel = await tabTo((f) => f.tag === "BUTTON" && f.text && !/sign in|log in/i.test(f.text) && f.text.length > 2);
await press("Space");
let st = await evaluate("[...Alpine.$data(document.querySelector('[x-data=\"signupWizard()\"]')).channels]");
check("signup.channel-by-keyboard", st.length === 1, { firstChannel, st });
const toNext = () => tabTo((f) => f.tag === "BUTTON" && /^(continue|next)/i.test(f.text));
await toNext(); await press("Enter"); await sleep(400);
// Step 1: a plan that needs no payment if there is one (price 0), else a trial.
const wiz = "Alpine.$data(document.querySelector('[x-data=\"signupWizard()\"]'))";
const plans = await evaluate(`${wiz}.filteredPlans.map(p => ({ slug: p.slug, name: p.name, price: p.price ?? p.priceMonthly ?? null, trial: p.trialDays }))`);
const pick = plans.find((p) => Number(p.price) === 0) || plans.find((p) => p.trial) || plans[0];
await tabTo((f) => f.tag === "BUTTON" && f.text.startsWith(pick.name));
await press("Space");
st = await evaluate(`${wiz}.plan`);
check("signup.plan-by-keyboard", st === pick.slug, { plans, picked: pick.slug, st });
await toNext(); await press("Enter"); await sleep(400);
// Step 2+: details. Throwaway values; nothing is stored or reused after this run.
const stamp = Date.now().toString(36);
const email = `qa-signup-${stamp}@akilent-qa.invalid`;
const pw = "Qa!" + createHmac("sha256", stamp).update("pw").digest("hex").slice(0, 14);
const fields = { first_name: "QA", last_name: "Signup", email, phone: "+260971000999", password1: pw, password2: pw,
  company_name: "QA Signup " + stamp, address_line1: "1 Test Road", city: "Lusaka", country: "Zambia" };
const filled = [];
for (let stepGuard = 0; stepGuard < 6; stepGuard++) {
  const names = await evaluate(`[...document.querySelectorAll('input:not([type=hidden]), select, textarea')].filter(e => e.offsetParent && e.name).map(e => e.name)`);
  const todo = names.filter((n) => fields[n] !== undefined && !filled.includes(n));
  if (!todo.length) break;
  for (const n of todo) {
    await tabTo((f) => f.name === n);
    const tag = await evaluate("document.activeElement.tagName");
    if (tag === "SELECT") {
      await evaluate(`(() => { const s = document.activeElement; const o = [...s.options].find(o => o.text === ${JSON.stringify(fields[n])} || o.value === ${JSON.stringify(fields[n])}) || s.options[1]; s.value = o.value; s.dispatchEvent(new Event('change', { bubbles: true })); })()`);
    } else {
      await typeText(fields[n]);
    }
    filled.push(n);
  }
  const lastStep = await evaluate(`${wiz}.step === ${wiz}.stepLabels.length - 1`);
  if (lastStep) break;
  await toNext(); await press("Enter"); await sleep(400);
}
await shot("signup-last-step-390");
const before = await evaluate("location.pathname");
await tabTo((f) => f.tag === "BUTTON" && /create|sign up|finish/i.test(f.text));
const nav = once("Page.loadEventFired", 15000).catch(() => null);
await press("Enter");
await nav; await sleep(1500);
st = { from: before, path: await evaluate("location.pathname"), toasts: await evaluate("window.Alpine && Alpine.store('toasts') ? Alpine.store('toasts').items.map(t => t.type + ': ' + t.message) : []"), msgs: await evaluate("[...document.querySelectorAll('.alert, [role=status], [role=alert]')].map(e => e.innerText.trim()).filter(Boolean).slice(0, 4)"), errors: await evaluate("[...document.querySelectorAll('.field-error')].map(e => e.innerText.trim()).filter(Boolean)") };
check("signup.completes-keyboard-only", st.path !== "/signup/" && !st.errors.length, { ...st, filled });
await shot("signup-after-390");

// ---------------------------------------------------------------------------------------
// 2. Onboarding: the first setup page loads for the new account, primary action reachable.
// ---------------------------------------------------------------------------------------
await go("/onboarding/", 1500);
st = await evaluate(`(() => ({ path: location.pathname, h1: document.querySelector('#main h1, h1')?.innerText.trim(), hscroll: document.documentElement.scrollWidth - document.documentElement.clientWidth }))()`);
check("onboarding.loads-390", !!st.h1 && st.hscroll <= 0, st);
await shot("onboarding-390");

// ---------------------------------------------------------------------------------------
// 3. Two-factor on, keyboard-only, in the dark theme (on the throwaway account).
// ---------------------------------------------------------------------------------------
const setTheme = (t) => send("Page.addScriptToEvaluateOnNewDocument", { source: `try { localStorage.setItem('akilent:theme', '${t}'); } catch (e) {}` });
await setTheme("dark");
await viewport(1280, 800, false);
await go("/settings/security/2fa/", 1500);
const secret = await evaluate("document.getElementById('setup-key')?.textContent.trim()");
check("2fa.setup-key-shown", !!secret && /^[A-Z2-7]+=*$/.test(secret), { len: secret && secret.length });
st = await evaluate("({ alt: document.querySelector('img[src^=\"data:image/svg+xml\"]')?.alt, theme: document.documentElement.dataset.theme })");
check("2fa.qr-has-alt", !!st.alt && st.theme === "dark", st);
const skip = await skipToMain();
check("keyboard.skip-link-first", /skip/i.test(skip.text) && skip.ring, skip);
await tabTo((f) => f.name === "token");
await typeText(totp(secret));
const nav2 = once("Page.loadEventFired", 15000).catch(() => null);
await press("Enter");
await nav2; await sleep(1500);
st = await evaluate(`(() => ({ path: location.pathname, text: document.querySelector('#main')?.innerText.slice(0, 400) }))()`);
const on = /recovery code/i.test(st.text) || /two-factor authentication is\s+enabled/i.test(st.text);
check("2fa.enabled-keyboard-dark", on, { path: st.path, head: st.text.slice(0, 160) });
await shot("2fa-after-dark-1280");
await go("/settings/security/", 1500);
st = await evaluate("(document.querySelector('#main')?.innerText.match(/Authenticator app[\\s\\S]*?(Configured|Not set up)/) || [])[1]");
check("security.overview-says-on", st === "Configured", st);

// ---------------------------------------------------------------------------------------
// 4. A settings change by keyboard: profile first name, saved and announced, then put back.
// ---------------------------------------------------------------------------------------
await go("/settings/", 1500);
const oldName = await evaluate("document.querySelector('input[name=first_name]')?.value");
await skipToMain();
await tabTo((f) => f.name === "first_name");
await evaluate("document.activeElement.select()");
await typeText("QA Renamed");
// The save is a boosted (in-place) form: no load event, so wait briefly either way.
const nav3 = once("Page.loadEventFired", 15000).catch(() => null);
await press("Enter");
await Promise.race([nav3, sleep(2500)]); await sleep(500);
st = await evaluate(`(() => ({ value: document.querySelector('input[name=first_name]')?.value, said: [...document.querySelectorAll('[role=status], [role=alert], .alert')].map(e => e.innerText.trim()).filter(Boolean).slice(0, 3), toasts: (window.Alpine && Alpine.store('toasts')) ? Alpine.store('toasts').items.map(t => t.type + ': ' + t.message) : [] }))()`);
check("settings.change-saved-and-said", st.value === "QA Renamed" && (st.said.length + st.toasts.length) > 0, st);
await evaluate(`(() => { const i = document.querySelector('input[name=first_name]'); i.value = ${JSON.stringify(oldName || "QA")}; i.form.requestSubmit(); })()`);
await sleep(1800);

check("keyboard.focus-always-visible", noRing.length === 0, noRing.slice(0, 8));

// ---------------------------------------------------------------------------------------
// 5. Scan every account screen: 390 + 1280, light + dark (as the throwaway account).
// ---------------------------------------------------------------------------------------
const PAGES = ["/settings/", "/settings/security/", "/settings/security/2fa/", "/settings/security/passkeys/", "/settings/security/email-codes/",
  "/settings/team/", "/settings/business/", "/settings/hours/", "/settings/integrations/", "/settings/api-keys/", "/settings/ai/",
  "/settings/ai/knowledge/", "/settings/ai/playground/", "/settings/tools/", "/settings/ai/autopilot/", "/onboarding/", "/channels/"];
const scan = [];
const unlabelled = `[...document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea')].filter(e => e.offsetParent)
  .filter(e => !(e.labels && e.labels.length) && !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby') && !e.closest('label')).map(e => e.name || e.id || e.type)`;
for (const [w, h, mob] of [[390, 844, true], [1280, 800, false]]) {
  await viewport(w, h, mob);
  for (const theme of ["light", "dark"]) {
    await setTheme(theme);
    for (const p of PAGES) {
      await go(p, 1000);
      await evaluate(scanner);
      const r = await evaluate("window.__uiScan({ max: 4 })");
      const row = { w, theme, p, path: await evaluate("location.pathname"), overflow: r.overflow.count, contrastFail: r.contrast.failing, ex: r.contrast.examples,
        under44: mob ? r.targets.under44 : null, tex: mob ? r.targets.examples : null, inputsUnder16: mob ? r.inputs.under16px : null, unlabelled: await evaluate(unlabelled) };
      scan.push(row);
      if (row.overflow || row.contrastFail || row.unlabelled.length || (mob && row.inputsUnder16)) process.stderr.write("SCAN " + JSON.stringify(row) + "\n");
    }
  }
}
const bad = scan.filter((r) => r.overflow || r.contrastFail || r.unlabelled.length || r.inputsUnder16);
check("scan.accounts-clean", bad.length === 0, bad.map((r) => `${r.w}/${r.theme}${r.p}: of=${r.overflow} cf=${r.contrastFail} unl=${r.unlabelled.join(",")} i16=${r.inputsUnder16}`).slice(0, 20));
await viewport(390, 844, true); await go("/settings/team/", 1000); await shot("team-390-dark");

writeFileSync(join(outDir, "accounts_qa_results.json"), JSON.stringify({ results, scan, signupEmail: email, log: log.slice(-60) }, null, 1));
console.log(JSON.stringify({ pass: results.filter((r) => r.pass).length, fail: results.filter((r) => !r.pass).map((r) => r.id), signupEmail: email }));
process.exit(0);
