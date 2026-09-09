import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { parseArgs, main } from "../src/cli.mjs";

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

test("missing key is a friendly error", async () => {
  process.env.AKILENT_CONFIG = join(mkdtempSync(join(tmpdir(), "akilent-cli-")), "none.json");
  delete process.env.AKILENT_API_KEY;
  const c = capture();
  const rc = await main(["messages"], c.io);
  assert.equal(rc, 1);
  assert.match(c.errs.join("\n"), /No API key/);
  delete process.env.AKILENT_CONFIG;
});
