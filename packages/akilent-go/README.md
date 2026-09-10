# akilent (Go)

Official Go SDK for the Akilent email API. Go 1.21+, no dependencies outside the
standard library.

```bash
go get github.com/akilent/akilent-go
```

```go
package main

import (
	"context"
	"log"
	"os"

	"github.com/akilent/akilent-go"
)

func main() {
	client := akilent.New(os.Getenv("AKILENT_API_KEY"))

	msg, err := client.Messages.Send(context.Background(), akilent.SendParams{
		From:    "billing@acme.com",
		To:      "user@example.com",
		Subject: "Your receipt",
		Text:    "Thanks for your purchase!",
	})
	if err != nil {
		log.Fatal(err)
	}
	log.Println(msg.PublicID, msg.Status)
}
```

## Features

- `client.Messages`, `.Templates`, `.Campaigns`, `.Contacts`, `.Events`,
  `.Workflows`, `.RequestLogs`
- Automatic `Idempotency-Key` on every POST (`SendParams.IdempotencyKey` to override)
- `*APIError` with `Code`, `RequestID`, `DocsURL`, `RetryAfter`; helpers
  `akilent.IsNotFound(err)`, `IsConflict`, `IsRateLimited`, `IsUnauthorized`
- Retry with backoff on 429 / 5xx
- `akilent.VerifyWebhookSignature(body, header, secret)` — signature verifier
- Every method takes a `context.Context`

## Configuration

```go
client := akilent.NewWithConfig(akilent.Config{
	APIKey:     "ak_live_…",
	BaseURL:    "https://akilent.com",
	MaxRetries: 2,
	HTTPClient: myHTTPClient,
})
```

## Development

```bash
go test ./...
```
