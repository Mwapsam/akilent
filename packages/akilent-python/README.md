# akilent (Python)

Official Python SDK for the [Akilent](https://akilent.com) email API.

```bash
pip install akilent
```

```python
from akilent import Akilent

client = Akilent(api_key="ak_live_...")           # or ak_test_... for the sandbox

msg = client.messages.send(
    from_="billing@acme.com",
    to="customer@example.com",
    subject="Your receipt",
    text="Thanks for your payment.",
)
print(msg["public_id"], msg["status"])

for event in client.messages.events(msg["public_id"]):
    print(event["type"], event["occurred_at"])
```

## Features

- Every endpoint of the versioned `/api/v1` surface: `messages`, `templates`,
  `campaigns`, `request_logs`.
- Automatic `Idempotency-Key` on every POST (override per call).
- Typed exceptions (`AuthenticationError`, `ConflictError`, `RateLimitError`, …)
  carrying `code`, `request_id`, and `docs_url` from the error envelope.
- Transparent retry with backoff on `429` and `5xx` (honours `Retry-After`).
- `client.messages.iter(...)` transparently pages.
- `akilent.webhooks.verify(payload, header, secret)` for inbound webhooks.

## Sandbox / test mode

Create a test key (`ak_test_...`) in the dashboard. Test sends never deliver,
never touch quota or reputation, and the recipient's local-part picks the
outcome: `delivered@`, `bounce@`, `complaint@`, `open@`, `click@`, `fail@`.

## Development

```bash
python -m venv .venv && . .venv/Scripts/activate
pip install -e ".[dev]"
pytest
```
