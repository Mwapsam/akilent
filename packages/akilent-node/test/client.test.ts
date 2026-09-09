import { createHmac } from "node:crypto";
import { describe, expect, it } from "vitest";

import { Akilent, AuthenticationError, ConflictError, webhooks } from "../src/index.js";

function stubFetch(handler: (req: Request) => Response | Promise<Response>) {
  return (input: URL | RequestInfo, init?: RequestInit) =>
    Promise.resolve(handler(new Request(input as RequestInfo, init)));
}

const make = (handler: Parameters<typeof stubFetch>[0]) =>
  new Akilent({ apiKey: "ak_test_x", baseUrl: "https://api.test", fetch: stubFetch(handler) as typeof fetch });

describe("messages.send", () => {
  it("sets bearer auth + auto idempotency key and parses the body", async () => {
    let seen: { auth: string | null; idem: string | null; body: unknown } | undefined;
    const client = make(async (req) => {
      seen = {
        auth: req.headers.get("authorization"),
        idem: req.headers.get("idempotency-key"),
        body: await req.json(),
      };
      return new Response(JSON.stringify({ id: 1, public_id: "msg_1", status: "queued" }), {
        status: 202,
        headers: { "content-type": "application/json" },
      });
    });

    const out = await client.messages.send({ from: "a@acme.com", to: "b@x.com", text: "hi" });
    expect(out.public_id).toBe("msg_1");
    expect(seen?.auth).toBe("Bearer ak_test_x");
    expect(seen?.idem).toBeTruthy();
    expect((seen?.body as { from: string }).from).toBe("a@acme.com");
  });

  it("forwards an explicit idempotency key", async () => {
    const client = make((req) => {
      expect(req.headers.get("idempotency-key")).toBe("abc");
      return new Response("{}", { status: 202 });
    });
    await client.messages.send({ from: "a@a.com", to: "b@b.com", idempotencyKey: "abc" });
  });
});

describe("errors", () => {
  it("maps 401 to AuthenticationError with envelope fields", async () => {
    const client = make(
      () =>
        new Response(
          JSON.stringify({ error: { code: "authentication_failed", message: "bad", request_id: "req_9" } }),
          { status: 401, headers: { "content-type": "application/json" } },
        ),
    );
    await expect(client.messages.list()).rejects.toMatchObject({
      name: "AuthenticationError",
      statusCode: 401,
      code: "authentication_failed",
      requestId: "req_9",
    });
    await expect(client.messages.list()).rejects.toBeInstanceOf(AuthenticationError);
  });

  it("maps 409 to ConflictError", async () => {
    const client = make(
      () =>
        new Response(JSON.stringify({ error: { code: "idempotency_key_reuse", message: "x" } }), {
          status: 409,
        }),
    );
    await expect(client.messages.send({ from: "a@a.com", to: "b@b.com" })).rejects.toBeInstanceOf(
      ConflictError,
    );
  });

  it("retries once on 429 then succeeds", async () => {
    let n = 0;
    const client = make(() => {
      n += 1;
      if (n === 1) return new Response("{}", { status: 429, headers: { "retry-after": "0" } });
      return new Response(JSON.stringify({ data: [], total: 0 }), { status: 200 });
    });
    const out = await client.messages.list();
    expect(n).toBe(2);
    expect(out.total).toBe(0);
  });
});

describe("messages.iterate", () => {
  it("pages through all results", async () => {
    const client = make((req) => {
      const offset = Number(new URL(req.url).searchParams.get("offset") ?? "0");
      const body =
        offset === 0
          ? { data: [{ id: "m1" }, { id: "m2" }], total: 3 }
          : { data: [{ id: "m3" }], total: 3 };
      return new Response(JSON.stringify(body), { status: 200 });
    });
    const ids: string[] = [];
    for await (const m of client.messages.iterate({}, 2)) ids.push(m.id);
    expect(ids).toEqual(["m1", "m2", "m3"]);
  });
});

describe("webhooks.verify", () => {
  it("accepts a valid signature and rejects a bad one", () => {
    const secret = "whsec_test";
    const body = '{"event":"message.delivered"}';
    const ts = Math.floor(Date.now() / 1000);
    const sig = createHmac("sha256", secret).update(`${ts}.${body}`).digest("hex");
    expect(webhooks.verify(body, `t=${ts},v1=${sig}`, secret)).toBe(true);
    expect(() => webhooks.verify(body, `t=${ts},v1=deadbeef`, secret)).toThrow(
      webhooks.SignatureVerificationError,
    );
  });
});
