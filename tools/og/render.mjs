// Draws static/img/seo/og-default.png (1200 x 630, the default share image) from
// tools/og/og-default.html in headless Chrome. Run from the repo root after changing the line:
//   node tools/og/render.mjs
import { spawn } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
const html = path.resolve("tools/og/og-default.html").replace(/\\/g, "/");
const out = "static/img/seo/og-default.png";
const chrome = spawn(process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe", ["--headless=new", "--remote-debugging-port=9366", "--user-data-dir=" + process.env.TEMP + "/cdp-og", "--no-first-run", "--allow-file-access-from-files", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let url;
for (let i = 0; i < 50 && !url; i++) { try { url = (await (await fetch("http://127.0.0.1:9366/json/list")).json()).find((t) => t.type === "page")?.webSocketDebuggerUrl; } catch {} await sleep(200); }
const ws = new WebSocket(url); await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let id = 0; const pend = new Map();
ws.addEventListener("message", (e) => { const m = JSON.parse(e.data); if (pend.has(m.id)) { pend.get(m.id)(m.result); pend.delete(m.id); } });
const send = (method, params = {}) => new Promise((r) => { pend.set(++id, r); ws.send(JSON.stringify({ id, method, params })); });
await send("Emulation.setDeviceMetricsOverride", { width: 1200, height: 630, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: "file:///" + html }); await sleep(1500);
await send("Runtime.evaluate", { expression: "document.fonts.ready", awaitPromise: true });
const d = await send("Page.captureScreenshot", { format: "png", clip: { x: 0, y: 0, width: 1200, height: 630, scale: 1 } });
fs.writeFileSync(out, Buffer.from(d.data, "base64"));
chrome.kill(); process.exit(0);
