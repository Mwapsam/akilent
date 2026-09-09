# akilent (Node / TypeScript)

Official SDK for the [Akilent](https://akilent.com) email API. Zero runtime
dependencies — uses the global `fetch` (Node 18+).

```bash
npm install akilent
```

```ts
import { Akilent } from "akilent";

const client = new Akilent({ apiKey: process.env.AKILENT_API_KEY! }); // ak_live_… or ak_test_…

const msg = await client.messages.send({
  from: "billing@acme.com",
  to: "customer@example.com",
  subject: "Your receipt",
  text: "Thanks for your payment.",
});

for await (const m of client.messages.iterate({ status: "delivered" })) {
  console.log(m.id, m.to);
}
```

## Features

- Full `/api/v1` surface: `messages`, `templates`, `campaigns`, `requestLogs`.
- Automatic `Idempotency-Key` on every POST (override per call).
- Typed errors (`AuthenticationError`, `ConflictError`, `RateLimitError`, …)
  with `code`, `requestId`, `docsUrl`.
- Retry with backoff on `429`/`5xx` (honours `Retry-After`).
- `client.messages.iterate()` async paging.
- `import { webhooks } from "akilent"` → `webhooks.verify(body, header, secret)`.

## Sandbox

Use an `ak_test_` key; nothing is delivered and the recipient local-part picks
the simulated outcome: `delivered@`, `bounce@`, `complaint@`, `open@`, `click@`,
`fail@`.

## Development

```bash
npm install
npm test        # vitest
npm run build   # tsc -> dist/
```
