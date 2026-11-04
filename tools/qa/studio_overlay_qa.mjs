// Content Studio app-feel, Phase 2: the overlay host. A post opens as a drawer over the list,
// J/K step through the rows, Back and Esc close it with focus back on the row, the URL is the
// real page; "Plan an idea" opens a modal, a refused save stays in it, a save closes it and the
// calendar shows the idea without a reload; the plan item is deleted again from its drawer.
// Usage: node tools/qa/studio_overlay_qa.mjs <repoRoot> <outDir>   (needs QA_UI_* in .env)
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [repo, outDir] = process.argv.slice(2);
const BASE = "http://127.0.0.1:8000";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9342;
const profile = join(outDir, "chrome-profile-overlay");
const shots = join(repo, "docs/design/qa/studio-feel");
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
    else if (act.raw) await send("Fetch.fulfillRequest", { requestId, responseCode: 200, responseHeaders: [{ name: "Content-Type", value: act.type }], body: act.raw });
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

// Console errors on any page count as failures.
const consoleErrors = [];
ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.method === "Runtime.exceptionThrown") consoleErrors.push(msg.params.exceptionDetails?.exception?.description || msg.params.exceptionDetails?.text);
  if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") consoleErrors.push(msg.params.args.map((a) => a.value || a.description).join(" "));
});
const waitFor = async (expr, ms = 8000) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (await evaluate(expr)) return true; await sleep(50); } return false; };
const activeTab = () => evaluate(`document.querySelector('[data-instant-tabs] a[aria-current="page"]')?.textContent.trim()`);
await viewport(1280, 800, false);


const overlayOpen = () => evaluate(`!document.getElementById('overlay-host').hidden`);
const getCount = (path) => log.filter((l) => l === `GET ${path} -> pass`).length;
const toasts = () => evaluate(`Alpine.store('toasts').items.map((t) => t.type + ': ' + t.message).join(' | ')`);
// The overlay's own plan saves go to the local dev server for real.
const passLocalPlanPosts = { match: (r) => r.method === "POST" && /^\/publishing\/planning\//.test(new URL(r.url).pathname), act: null };

// 1. Library: a post opens as a drawer over the list, without a page load.
await go("/publishing/library/", 1500);
await evaluate(`window.__keep = 'still here'; window.__views = []; window.addEventListener('akilent:pageview', (e) => window.__views.push(e.detail.path)); 0`);
const rows = await evaluate(`[...document.querySelectorAll('#main [data-row] a[data-overlay-open="drawer"]')].map((a) => a.getAttribute('href'))`);
if (rows.length < 2) throw new Error("need at least two posts in the Library, found " + rows.length);
const libGets = getCount("/publishing/library/");
await clickSel(`#main [data-row] a[href="${rows[0]}"]`);
const opened = await waitFor(`!!document.querySelector('#overlay-body [data-post-drawer]')`);
await sleep(300);
const st1 = await evaluate(`({ path: location.pathname, kind: document.getElementById('overlay-host').dataset.kind,
  title: document.getElementById('overlay-title').textContent, keep: window.__keep, views: window.__views.slice(),
  focusInside: document.getElementById('overlay-host').contains(document.activeElement),
  pos: document.querySelector('[data-overlay-position]').textContent, mainInert: document.getElementById('main').closest('[inert]') !== null })`);
await shot("overlay-post-drawer");
check("drawer.opens-in-place", opened && st1.path === rows[0] && st1.kind === "drawer" && st1.keep === "still here", st1);
check("drawer.pageview-recorded", st1.views.includes(rows[0]), st1.views);
check("drawer.focus-inside-and-page-inert", st1.focusInside && st1.mainInert, st1);
check("drawer.position-shown", st1.pos === `1 of ${rows.length}`, st1.pos);

// 2. J steps to the next row; the URL follows without adding history entries.
const histLen = await evaluate("history.length");
await evaluate("document.activeElement.blur(); 0");
await key("j", "KeyJ", 74);
await waitFor(`location.pathname === ${JSON.stringify(rows[1])} && !!document.querySelector('#overlay-body [data-post-drawer]') && document.getElementById('overlay-body').getAttribute('aria-busy') === 'false'`);
const st2 = await evaluate(`({ path: location.pathname, pos: document.querySelector('[data-overlay-position]').textContent, len: history.length })`);
check("drawer.j-steps-to-next-row", st2.path === rows[1] && st2.pos === `2 of ${rows.length}` && st2.len === histLen, st2);

// 3. Back closes it: the list is the same page (not re-fetched), focus is on the row.
await evaluate("history.back(); 0");
await waitFor(`document.getElementById('overlay-host').hidden`);
await sleep(200);
const st3 = await evaluate(`({ path: location.pathname, keep: window.__keep, focus: document.activeElement && document.activeElement.getAttribute('href') })`);
check("drawer.back-closes-without-reload", st3.path === "/publishing/library/" && st3.keep === "still here" && getCount("/publishing/library/") === libGets, { ...st3, gets: getCount("/publishing/library/") - libGets });
check("drawer.focus-returns-to-row", st3.focus === rows[1], st3);

// 4. Esc closes it too.
await clickSel(`#main [data-row] a[href="${rows[0]}"]`);
await waitFor(`!!document.querySelector('#overlay-body [data-post-drawer]')`);
await key("Escape", "Escape", 27);
await waitFor(`document.getElementById('overlay-host').hidden`);
check("drawer.esc-closes", (await evaluate("location.pathname")) === "/publishing/library/" && (await evaluate("window.__keep")) === "still here", {});

// 5. The drawer's URL opened directly is the full post page.
await go(rows[0], 1200);
check("drawer.url-is-full-page", await evaluate(`!!document.querySelector('#main h1') && document.getElementById('overlay-host').hidden`), {});

// 6. Phone: the drawer takes the whole width, nothing scrolls sideways.
await viewport(390, 844, true);
await go("/publishing/library/", 1500);
await clickSel(`#main [data-row] a[href="${rows[0]}"]`);
await waitFor(`!!document.querySelector('#overlay-body [data-post-drawer]')`); await sleep(400);
const phone = await evaluate(`({ w: Math.round(document.querySelector('.ak-overlay-panel').getBoundingClientRect().width), vw: innerWidth, noSideScroll: document.documentElement.scrollWidth <= innerWidth })`);
await shot("overlay-post-drawer-phone");
check("drawer.full-width-on-phone", phone.w === phone.vw && phone.noSideScroll, phone);
await key("Escape", "Escape", 27);
await viewport(1280, 800, false);

// 7. Calendar: "Plan an idea" opens a modal with the day filled in, focus on Topic.
rules.unshift(passLocalPlanPosts);
await go("/publishing/calendar/", 1500);
await evaluate(`window.__keep = 'still here'; 0`);
const add = await evaluate(`(() => { const a = [...document.querySelectorAll('#main a[data-overlay-open="modal"]')].find((x) => x.offsetParent); if (!a) return null; a.setAttribute('data-qa-add', ''); return a.getAttribute('href'); })()`);
if (!add) throw new Error("no empty future day to plan on this week");
const day = new URL(BASE + add).searchParams.get("planned_date");
await clickSel("#main a[data-qa-add]");
await waitFor(`!!document.querySelector('#overlay-body form #plan-topic')`); await sleep(400);
const st7 = await evaluate(`({ kind: document.getElementById('overlay-host').dataset.kind, date: document.getElementById('plan-planned-date').value,
  focus: document.activeElement && document.activeElement.id, title: document.getElementById('overlay-title').textContent })`);
await shot("overlay-add-idea");
check("modal.opens-with-day", st7.kind === "modal" && st7.date === day && st7.title === "Add idea", st7);
check("modal.focus-on-topic", st7.focus === "plan-topic", st7);

// 8. A refused save stays in the modal (422) and says why.
await evaluate(`(() => { const t = document.getElementById('plan-topic'); t.removeAttribute('required'); t.value = '   '; return 0; })()`);
await clickSel("#overlay-body form button[type=submit].btn-primary");
await sleep(900);
const st8 = await evaluate(`(() => { const t = document.getElementById('plan-topic'), save = document.querySelector('#overlay-body .ak-overlay-actions .btn-primary');
  const r = save && save.getBoundingClientRect(), panel = document.querySelector('.ak-overlay-panel').getBoundingClientRect();
  return { open: !document.getElementById('overlay-host').hidden, invalid: t && t.getAttribute('aria-invalid'),
    error: (document.getElementById('plan-topic-error') || {}).textContent, focus: document.activeElement && document.activeElement.id,
    saveInView: !!r && r.bottom <= panel.bottom + 1 && r.top >= panel.top }; })()`);
st8.toast = await toasts();
await shot("overlay-add-idea-refused");
check("modal.refused-save-stays", st8.open && st8.invalid === "true" && /topic/i.test(st8.error || "") && st8.focus === "plan-topic", st8);
check("modal.buttons-pinned-in-view", st8.saveInView, st8);

// 9. A save closes it, the toast says when, the calendar shows the idea, same page.
const topic = "QA overlay idea " + Date.now();
await evaluate(`(() => { document.getElementById('plan-topic').value = ${JSON.stringify(topic)}; return 0; })()`);
await clickSel("#overlay-body form button[type=submit].btn-primary");
await waitFor(`document.getElementById('overlay-host').hidden`);
const shown9 = await waitFor(`[...document.querySelectorAll('#main .cal-plan-topic')].some((e) => e.textContent.includes(${JSON.stringify(topic)}))`);
const st9 = await evaluate(`({ path: location.pathname, keep: window.__keep })`);
st9.toast = await toasts();
await shot("overlay-calendar-after-save");
check("modal.save-closes-and-calendar-updates", shown9 && st9.path === "/publishing/calendar/" && st9.keep === "still here", st9);
check("modal.save-toast", /Planned for/.test(st9.toast), st9.toast);

// 10. The new idea opens in its drawer and is deleted there (this also cleans up).
await evaluate(`(() => { const t = [...document.querySelectorAll('#main .cal-plan-topic')].find((e) => e.textContent.includes(${JSON.stringify(topic)})); t.closest('a').setAttribute('data-qa-plan', ''); return 0; })()`);
await clickSel("#main a[data-qa-plan]");
await waitFor(`!!document.querySelector('#overlay-body button[form=plan-delete-form]')`); await sleep(300);
check("plan.edit-opens-as-drawer", (await evaluate("document.getElementById('overlay-host').dataset.kind")) === "drawer", {});
await clickSel("#overlay-body button[form=plan-delete-form]");
await sleep(500);
await evaluate(`Alpine.store('confirm').respond(true); 0`);
await waitFor(`document.getElementById('overlay-host').hidden`);
const gone = await waitFor(`![...document.querySelectorAll('#main .cal-plan-topic')].some((e) => e.textContent.includes(${JSON.stringify(topic)}))`);
check("plan.delete-in-drawer", gone && (await evaluate("window.__keep")) === "still here", { path: await evaluate("location.pathname") });
rules.shift();

// 11. The other side pages open over their page, each in its shape.
for (const [page, sel, kind, ready] of [
  ["/publishing/media/", '#main a[href$="/edit/"][data-overlay-open="modal"]', "modal", "!!document.querySelector('#overlay-body [x-data^=mediaEditor], #overlay-body #me-seconds')"],
  ["/publishing/media/", '#main a[href="/publishing/media/snippets/"]', "drawer", "!!document.querySelector('#overlay-body [aria-labelledby=sn-templates]')"],
  ["/publishing/calendar/", '#main a[href="/publishing/slots/"]', "drawer", "!!document.querySelector('#overlay-body [aria-labelledby=slots-title]')"],
  ["/publishing/planning/", '#main a[href="/publishing/pillars/"]', "modal", "!!document.querySelector('#overlay-body #pillar-name')"],
  ["/publishing/sales/", '#main a[href="/help/post-results/"]', "modal", "!!document.querySelector('#overlay-body [data-help-body]')"],
]) {
  // A help article for a channel that's switched off here is hidden (404): nothing to open.
  if (page === "/publishing/sales/" && (await evaluate(`fetch('/help/post-results/').then((r) => r.status)`)) === 404) {
    process.stderr.write("SKIP help modal: the article's channel is off on this server" + String.fromCharCode(10));
    continue;
  }
  await go(page, 1400);
  await evaluate(`window.__keep = 'still here'; 0`);
  const has = await evaluate(`!!document.querySelector(${JSON.stringify(sel)})`);
  if (!has) { check(`side.${page}${sel}`, false, "no opener"); continue; }
  await clickSel(sel);
  const ok = await waitFor(ready);
  await sleep(500);
  const st = await evaluate(`({ kind: document.getElementById('overlay-host').dataset.kind, keep: window.__keep, path: location.pathname,
    stage: (() => { const s = document.querySelector('#overlay-body .cropper-container'); return s ? Math.round(s.getBoundingClientRect().height) : null; })() })`);
  const name = sel.match(/href[$]?="([^"]+)"/)[1];
  if (name === "/edit/") { await shot("overlay-media-editor"); check("side.media-editor-cropper-sized", st.stage === null || st.stage > 200, st); }
  check(`side.${name}`, ok && st.kind === kind && st.keep === "still here", st);
  await key("Escape", "Escape", 27);
  await waitFor(`document.getElementById('overlay-host').hidden`);
  check(`side.${name}.closes-back-to-page`, (await evaluate("location.pathname")) === page, {});
}

// 11b. A media server without CORS headers (CloudFront today): the editor can't read the picture
// back, so it shows it as a preview only instead of a broken image. The picture is on
// media.qa.invalid (a test asset), another origin, so the browser checks CORS for it.
{
  const png = readFileSync(join(repo, "static/img/demo/leather-sandals.png")).toString("base64");
  // An https host (the page's CSP allows https images) that's answered here, never with CORS
  // headers; the request never leaves the browser.
  rules.unshift({ match: (r) => new URL(r.url).host === "media.qa.invalid", act: { raw: png, type: "image/png" } });
  await go("/publishing/media/", 1400);
  const editHref = await evaluate(`(() => { let el = document.querySelector('#main img[src*="leather-sandals"], #main [style*="leather-sandals"]'); while (el && !(el.querySelector && el.querySelector('a[href$="/edit/"]'))) el = el.parentElement; const a = el && el.querySelector('a[href$="/edit/"]'); return a ? a.getAttribute('href') : null; })()`);
  if (!editHref) { check("editor.no-cors-shows-preview", false, "no localhost picture in the grid"); }
  else {
    await clickSel(`#main a[href="${editHref}"]`);
    await waitFor("!!document.querySelector('#overlay-body [x-data^=mediaEditor]')");
    const shownOk = await waitFor(`(() => { const i = document.querySelector('#overlay-body img[data-media-preview]'); return i && !i.hasAttribute('crossorigin') && i.complete && i.naturalWidth > 0; })()`);
    const cors = await evaluate(`(() => { const i = document.querySelector('#overlay-body img[data-media-preview]');
      return { msg: (document.querySelector('#overlay-body [role=alert]') || {}).textContent, saveDisabled: document.querySelector('#overlay-body button.btn-primary').disabled,
        img: i && { src: i.getAttribute('src'), co: i.getAttribute('crossorigin'), complete: i.complete, w: i.naturalWidth, h: Math.round(i.getBoundingClientRect().height), display: getComputedStyle(i).display } }; })()`);
    await shot("overlay-media-editor-no-cors");
    check("editor.no-cors-shows-preview", shownOk && /preview only/.test(cors.msg || "") && cors.saveDisabled, cors);
    await key("Escape", "Escape", 27);
    await waitFor(`document.getElementById('overlay-host').hidden`);
  }
  rules.shift();
}

// 12. The composer keeps what you typed while its templates drawer opens and closes.
await go("/publishing/new/", 1500);
const manage = await evaluate(`!!document.querySelector('#main a[href="/publishing/media/snippets/"]')`);
if (manage) {
  await evaluate(`(() => { const t = document.querySelector('#main textarea'); t.value = 'Unsaved words'; t.dispatchEvent(new Event('input', { bubbles: true })); return 0; })()`);
  await evaluate(`document.querySelector('#main a[href="/publishing/media/snippets/"]').scrollIntoView({ block: 'center' }); 0`);
  await clickSel('#main a[href="/publishing/media/snippets/"]');
  await waitFor("!!document.querySelector('#overlay-body [aria-labelledby=sn-templates]')");
  await key("Escape", "Escape", 27);
  await waitFor(`document.getElementById('overlay-host').hidden`);
  check("composer.keeps-work-under-drawer", (await evaluate(`document.querySelector('#main textarea').value`)) === "Unsaved words" && (await evaluate("location.pathname")) === "/publishing/new/", {});
} else {
  check("composer.keeps-work-under-drawer", false, "Manage templates link hidden (cs_media_library off?)");
}

check("console.no-errors", consoleErrors.filter((e) => !/favicon|ERR_BLOCKED_BY_CLIENT|status of 422|leather-sandals|CORS/.test(e)).length === 0, consoleErrors.slice(0, 5));
const failed = results.filter((r) => !r.pass);
writeFileSync(join(outDir, "studio_overlay_qa.json"), JSON.stringify({ results, log }, null, 2));
console.log(`${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
