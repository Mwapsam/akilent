import { createHmac, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";

const TOLERANCE_SECONDS = 300;

/** Verify an `X-Akilent-Signature` header (`t=<unix>,v1=<hex>`) over the raw body. */
export function verifySignature(rawBody, header, secret, toleranceSeconds = TOLERANCE_SECONDS) {
  if (!header) return { ok: false, reason: "no signature header" };
  const parts = {};
  for (const p of header.split(",")) {
    const i = p.indexOf("=");
    if (i > 0) parts[p.slice(0, i)] = p.slice(i + 1);
  }
  const timestamp = Number(parts.t);
  const received = parts.v1;
  if (!Number.isFinite(timestamp) || !received) return { ok: false, reason: "malformed header" };
  if (Math.abs(Date.now() / 1000 - timestamp) > toleranceSeconds) {
    return { ok: false, reason: "timestamp outside tolerance" };
  }
  const body = Buffer.isBuffer(rawBody) ? rawBody : Buffer.from(rawBody);
  const signed = Buffer.concat([Buffer.from(`${timestamp}.`), body]);
  const expected = createHmac("sha256", secret).update(signed).digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(received);
  if (a.length !== b.length || !timingSafeEqual(a, b)) return { ok: false, reason: "signature mismatch" };
  return { ok: true };
}

/**
 * Run a local HTTP server that receives Akilent webhooks, verifies their
 * signature, pretty-prints them, and optionally forwards the raw request.
 *
 * @returns {Promise<{close: () => void, port: number}>}
 */
export function listen({ port = 4318, secret = null, forwardTo = null, out = console.log } = {}) {
  return new Promise((resolve) => {
    const server = createServer((req, res) => {
      const chunks = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", async () => {
        const raw = Buffer.concat(chunks);
        const event = req.headers["x-akilent-event"] || "(unknown)";
        const sig = req.headers["x-akilent-signature"];

        let verdict = "not checked (no --secret)";
        if (secret) {
          const v = verifySignature(raw, sig, secret);
          verdict = v.ok ? "signature OK" : `SIGNATURE INVALID — ${v.reason}`;
        }

        let body;
        try {
          body = JSON.parse(raw.toString("utf8") || "{}");
        } catch {
          body = raw.toString("utf8");
        }
        out(`\n[${new Date().toISOString()}] ${event}  (${verdict})`);
        out(typeof body === "string" ? body : JSON.stringify(body, null, 2));

        if (forwardTo) {
          try {
            const fwd = await fetch(forwardTo, {
              method: "POST",
              headers: {
                "content-type": req.headers["content-type"] || "application/json",
                "x-akilent-event": String(event),
                ...(sig ? { "x-akilent-signature": String(sig) } : {}),
              },
              body: raw,
            });
            out(`  → forwarded to ${forwardTo} (${fwd.status})`);
          } catch (e) {
            out(`  → forward to ${forwardTo} failed: ${e.message}`);
          }
        }

        res.writeHead(200, { "content-type": "application/json" });
        res.end('{"received":true}');
      });
    });
    server.listen(port, () => resolve({ close: () => server.close(), port: server.address().port }));
  });
}
