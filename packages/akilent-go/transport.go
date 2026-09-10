package akilent

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

var retryStatuses = map[int]bool{429: true, 500: true, 502: true, 503: true, 504: true}

// do performs an authenticated request with retry/backoff and decodes the JSON
// response into out (may be nil). POST requests get an auto Idempotency-Key
// unless idemKey is non-empty.
func (c *Client) do(ctx context.Context, method, path string, query url.Values, body any, idemKey string, out any) error {
	var payload []byte
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		payload = b
	}

	u := c.cfg.BaseURL + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}

	if method == http.MethodPost && idemKey == "" {
		idemKey = newUUID()
	}

	for attempt := 0; ; attempt++ {
		req, err := http.NewRequestWithContext(ctx, method, u, bytes.NewReader(payload))
		if err != nil {
			return err
		}
		req.Header.Set("Authorization", "Bearer "+c.cfg.APIKey)
		req.Header.Set("User-Agent", userAgent)
		req.Header.Set("Accept", "application/json")
		if payload != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		if idemKey != "" && method == http.MethodPost {
			req.Header.Set("Idempotency-Key", idemKey)
		}

		resp, err := c.cfg.HTTPClient.Do(req)
		if err != nil {
			if attempt < c.cfg.MaxRetries {
				time.Sleep(backoff(attempt + 1))
				continue
			}
			return &ConnectionError{Err: err}
		}

		raw, _ := io.ReadAll(resp.Body)
		resp.Body.Close()

		if retryStatuses[resp.StatusCode] && attempt < c.cfg.MaxRetries {
			time.Sleep(retryDelay(resp, attempt+1))
			continue
		}

		if resp.StatusCode >= 400 {
			return parseAPIError(resp, raw)
		}
		if out != nil && len(raw) > 0 {
			return json.Unmarshal(raw, out)
		}
		return nil
	}
}

func parseAPIError(resp *http.Response, raw []byte) error {
	var env struct {
		Error struct {
			Code      string `json:"code"`
			Message   string `json:"message"`
			RequestID string `json:"request_id"`
			DocsURL   string `json:"docs_url"`
		} `json:"error"`
	}
	_ = json.Unmarshal(raw, &env)

	msg := env.Error.Message
	if msg == "" {
		msg = fmt.Sprintf("HTTP %d", resp.StatusCode)
	}
	reqID := env.Error.RequestID
	if reqID == "" {
		reqID = resp.Header.Get("X-Request-Id")
	}
	ra := 0
	if v := resp.Header.Get("Retry-After"); v != "" {
		if n, err := strconv.Atoi(strings.TrimSpace(v)); err == nil {
			ra = n
		}
	}
	return &APIError{
		StatusCode: resp.StatusCode,
		Code:       env.Error.Code,
		Message:    msg,
		RequestID:  reqID,
		DocsURL:    env.Error.DocsURL,
		RetryAfter: ra,
		Body:       raw,
	}
}

func backoff(attempt int) time.Duration {
	d := 0.5 * math.Pow(2, float64(attempt-1))
	if d > 8 {
		d = 8
	}
	return time.Duration(d * float64(time.Second))
}

func retryDelay(resp *http.Response, attempt int) time.Duration {
	if v := resp.Header.Get("Retry-After"); v != "" {
		if n, err := strconv.Atoi(strings.TrimSpace(v)); err == nil {
			return time.Duration(n) * time.Second
		}
	}
	return backoff(attempt)
}

func newUUID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	s := hex.EncodeToString(b[:])
	return s[0:8] + "-" + s[8:12] + "-" + s[12:16] + "-" + s[16:20] + "-" + s[20:32]
}
