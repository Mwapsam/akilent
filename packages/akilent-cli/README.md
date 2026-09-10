# akilent-cli

Command-line interface for the [Akilent](https://akilent.com) email API. Pure
Node (>=18), zero dependencies.

```bash
npm install -g akilent-cli
akilent login --key ak_test_xxxxx
akilent send --from you@acme.com --to delivered@sandbox.akilent.test --subject Hi --text "It works"
akilent logs tail
```

## Commands

| Command | Purpose |
|---|---|
| `akilent login --key <ak_...> [--base-url <url>]` | Store credentials in `~/.akilent/config.json` (mode 600). |
| `akilent whoami` | Show the resolved base URL and key suffix. |
| `akilent send --from --to [--subject --text --html --template]` | Send one email (auto `Idempotency-Key`). |
| `akilent messages [--status --to --limit]` | List recent messages, tab-separated. |
| `akilent events <message_id>` | Print a message's lifecycle timeline. |
| `akilent logs tail [--interval <s>]` | Poll for new messages and stream them. |
| `akilent templates pull <slug> [--out file.json]` | Fetch a template as JSON. |
| `akilent templates push <slug> --in file.json` | Update a template from a local JSON file. |
| `akilent webhooks test --event <type> [--data <json>]` | Fan a synthetic event through your live webhook endpoints. |
| `akilent webhooks listen [--port <n>] [--secret <whsec_...>] [--forward-to <url>]` | Run a local receiver that verifies signatures, prints events, and optionally re-POSTs each request to a local URL. Point a webhook endpoint at it via your own tunnel. |

Auth resolution order: `--key` flag → `$AKILENT_API_KEY` → `~/.akilent/config.json`.
Override the config path with `$AKILENT_CONFIG`.

## Development

```bash
node --test "test/*.test.mjs"
```
