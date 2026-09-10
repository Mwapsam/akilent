import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { parseArgs, main } from "../src/cli.mjs";
import { listen, verifySignature } from "../src/webhooks.mjs";
import { createHmac } from "node:crypto";
import { request as httpRequest } from "node:http";

const REAL_FETCH = globalThis.fetch;

function postRaw(url, headers, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const req = httpRequest(
      { hostname: u.hostname, port: u.port, path: u.pathname, method: "POST", headers },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => resolve({ status: res.statusCode, body: Buffer.concat(chunks).toString() }));
      },
    );
    req.on("error", reject);
    req.end(body);
  });
}

function capture() {
  const lines = [];
  const errs = [];
  return {
    io: { out: (s) => lines.push(String(s)), err: (s) => errs.push(String(s)) },
    lines,
    errs,
  };
}

test("parseArgs splits positionals, valued flags, and boolean flags", () => {
  const { positional, flags } = parseArgs(["send", "--from", "a@x.com", "--json"]);
  assert.deepEqual(positional, ["send"]);
  assert.equal(flags.from, "a@x.com");
  assert.equal(flags.json, true);
});

test("login writes config and send reads it back", async () => {
  const dir = mkdtempSync(join(tmpdir(), "akilent-cli-"));
  process.env.AKILENT_CONFIG = join(dir, "config.json");

  const c1 = capture();
  const rc = await main(["login", "--key", "ak_test_abc", "--base-url", "https://api.test"], c1.io);
  assert.equal(rc, 0);
  assert.match(c1.lines.join("\n"), /Saved credentials/);

  // stub fetch for the send call
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify({ public_id: "msg_1", status: "queued" }), { status: 202 });
  };

  const c2 = capture();
  const rc2 = await main(["send", "--from", "a@x.com", "--to", "b@y.com", "--text", "hi"], c2.io);
  assert.equal(rc2, 0);
  assert.match(c2.lines.join("\n"), /msg_1/);
  assert.equal(calls[0].init.headers.authorization, "Bearer ak_test_abc");
  assert.equal(calls[0].url, "https://api.test/api/v1/messages");
  assert.ok(calls[0].init.headers["idempotency-key"]);

  delete process.env.AKILENT_CONFIG;
});

test("webhooks test posts to the test endpoint", async () => {
  process.env.AKILENT_API_KEY = "ak_test_xyz";
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ event: "contact.created", deliveries: 1 }), { status: 202 });
  };
  const c = capture();
  const rc = await main(["webhooks", "test", "--event", "contact.created", "--data", '{"id":"con_1"}'], c.io);
  assert.equal(rc, 0);
  assert.ok(calls[0].url.endsWith("/api/v1/webhooks/test"));
  assert.deepEqual(calls[0].body, { event: "contact.created", data: { id: "con_1" } });
  delete process.env.AKILENT_API_KEY;
  globalThis.fetch = REAL_FETCH;
});

test("webhooks listen verifies a signed request and forwards it", async () => {
  const secret = "whsec_test";
  const forwarded = [];
  const { close, port } = await listen({
    port: 0,
    secret,
    forwardTo: "http://forward.invalid/hook",
    out: () => {},
  });
  globalThis.fetch = async (url, init) => {
    forwarded.push({ url: String(url), body: init.body.toString() });
    return new Response("{}", { status: 200 });
  };
  try {
    const body = JSON.stringify({ event: "message.sent", data: {} });
    const ts = Math.floor(Date.now() / 1000);
    const sig = `t=${ts},v1=${createHmac("sha256", secret).update(`${ts}.${body}`).digest("hex")}`;
    const res = await postRaw(
      `http://localhost:${port}/`,
      { "content-type": "application/json", "x-akilent-event": "message.sent", "x-akilent-signature": sig },
      body,
    );
    assert.equal(res.status, 200);
    assert.equal(verifySignature(body, sig, secret).ok, true);
    assert.equal(forwarded.length, 1);
    assert.equal(forwarded[0].url, "http://forward.invalid/hook");
  } finally {
    close();
    globalThis.fetch = REAL_FETCH;
  }
});

test("missing key is a friendly error", async () => {
  process.env.AKILENT_CONFIG = join(mkdtempSync(join(tmpdir(), "akilent-cli-")), "none.json");
  delete process.env.AKILENT_API_KEY;
  const c = capture();
  const rc = await main(["messages"], c.io);
  assert.equal(rc, 1);
  assert.match(c.errs.join("\n"), /No API key/);
  delete process.env.AKILENT_CONFIG;
});
