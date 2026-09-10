# akilent (PHP)

Official PHP SDK for the Akilent email API. PHP 8.1+, zero runtime dependencies
(uses `ext-curl`).

```bash
composer require akilent/akilent
```

```php
use Akilent\Client;

$client = new Client(getenv('AKILENT_API_KEY'));

$client->messages->send(
    from: 'billing@acme.com',
    to: 'user@example.com',
    subject: 'Your receipt',
    text: 'Thanks for your purchase!',
);
```

## Features

- `messages`, `templates`, `campaigns`, `contacts`, `events`, `workflows`, `requestLogs` namespaces
- Automatic `Idempotency-Key` on every POST (override with `idempotencyKey:`)
- Typed exceptions carrying `code`, `requestId`, `docsUrl` (`ValidationError`,
  `AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `ConflictError`,
  `RateLimitError`, `ServerError`)
- Retry with backoff on 429 / 5xx
- `$client->messages->iterate()` — generator that pages through results
- `Akilent\Webhooks::verify($rawBody, $header, $secret)` — signature verifier

## Configuration

```php
new Client($apiKey, baseUrl: 'https://akilent.com', timeout: 30, maxRetries: 2);
```

## Development

```bash
composer install
composer test
```

Tests stub `Transport::$sender`, so no network access is required.
