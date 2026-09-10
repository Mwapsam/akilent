package akilent

import "fmt"

// ConnectionError means the request never reached the API (DNS, TLS, timeout,
// connection reset) after exhausting retries.
type ConnectionError struct{ Err error }

func (e *ConnectionError) Error() string { return "akilent: connection error: " + e.Err.Error() }
func (e *ConnectionError) Unwrap() error { return e.Err }

// APIError is returned for every non-2xx response and mirrors the API's
// {"error": {...}} envelope.
type APIError struct {
	StatusCode int
	Code       string
	Message    string
	RequestID  string
	DocsURL    string
	RetryAfter int // seconds; 0 if absent
	Body       []byte
}

func (e *APIError) Error() string {
	if e.Code != "" {
		return fmt.Sprintf("akilent: %d %s (code=%s, request_id=%s)", e.StatusCode, e.Message, e.Code, e.RequestID)
	}
	return fmt.Sprintf("akilent: %d %s", e.StatusCode, e.Message)
}

// IsNotFound reports whether err is an APIError with status 404.
func IsNotFound(err error) bool { return statusIs(err, 404) }

// IsConflict reports whether err is an APIError with status 409 (idempotency).
func IsConflict(err error) bool { return statusIs(err, 409) }

// IsRateLimited reports whether err is an APIError with status 429.
func IsRateLimited(err error) bool { return statusIs(err, 429) }

// IsUnauthorized reports whether err is an APIError with status 401.
func IsUnauthorized(err error) bool { return statusIs(err, 401) }

func statusIs(err error, code int) bool {
	ae, ok := err.(*APIError)
	return ok && ae.StatusCode == code
}
