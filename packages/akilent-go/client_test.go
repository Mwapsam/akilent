package akilent

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync/atomic"
	"testing"
	"time"
)

func newTestClient(h http.HandlerFunc) (*Client, *httptest.Server) {
	srv := httptest.NewServer(h)
	c := NewWithConfig(Config{APIKey: "ak_test_abc", BaseURL: srv.URL})
	return c, srv
}

func TestSendPostsWithAutoIdempotencyKey(t *testing.T) {
	var gotAuth, gotIdem, gotBody string
	c, srv := newTestClient(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotIdem = r.Header.Get("Idempotency-Key")
		buf := make([]byte, r.ContentLength)
		r.Body.Read(buf)
		gotBody = string(buf)
		w.WriteHeader(202)
		fmt.Fprint(w, `{"id":1,"public_id":"msg_1","status":"queued"}`)
	})
	defer srv.Close()

	msg, err := c.Messages.Send(context.Background(), SendParams{From: "a@x.com", To: "b@y.com", Text: "hi"})
	if err != nil {
		t.Fatal(err)
	}
	if msg.PublicID != "msg_1" {
		t.Fatalf("public_id = %q", msg.PublicID)
	}
	if gotAuth != "Bearer ak_test_abc" {
		t.Fatalf("auth = %q", gotAuth)
	}
	if gotIdem == "" {
		t.Fatal("missing Idempotency-Key")
	}
	if want := `"from":"a@x.com"`; !contains(gotBody, want) {
		t.Fatalf("body %q missing %q", gotBody, want)
	}
}

func TestListSendsQueryParams(t *testing.T) {
	var gotQuery string
	c, srv := newTestClient(func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.RawQuery
		fmt.Fprint(w, `{"data":[],"total":0}`)
	})
	defer srv.Close()

	_, err := c.Messages.List(context.Background(), map[string]string{"status": "delivered"})
	if err != nil {
		t.Fatal(err)
	}
	if !contains(gotQuery, "status=delivered") {
		t.Fatalf("query = %q", gotQuery)
	}
}

func TestNotFoundReturnsTypedError(t *testing.T) {
	c, srv := newTestClient(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(404)
		fmt.Fprint(w, `{"error":{"code":"not_found","message":"nope","request_id":"req_9"}}`)
	})
	defer srv.Close()

	_, err := c.Messages.Get(context.Background(), "msg_missing")
	if !IsNotFound(err) {
		t.Fatalf("expected IsNotFound, got %v", err)
	}
	ae := err.(*APIError)
	if ae.Code != "not_found" || ae.RequestID != "req_9" {
		t.Fatalf("bad APIError: %+v", ae)
	}
}

func TestConflictOnIdempotencyReuse(t *testing.T) {
	c, srv := newTestClient(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(409)
		fmt.Fprint(w, `{"error":{"code":"idempotency_key_reuse","message":"reused"}}`)
	})
	defer srv.Close()

	_, err := c.Messages.Send(context.Background(), SendParams{From: "a@x.com", To: "b@y.com", Text: "hi", IdempotencyKey: "k1"})
	if !IsConflict(err) {
		t.Fatalf("expected IsConflict, got %v", err)
	}
}

func TestRetriesOn503(t *testing.T) {
	var calls int32
	c, srv := newTestClient(func(w http.ResponseWriter, r *http.Request) {
		if atomic.AddInt32(&calls, 1) < 2 {
			w.WriteHeader(503)
			return
		}
		fmt.Fprint(w, `{"data":[]}`)
	})
	defer srv.Close()

	if _, err := c.Messages.List(context.Background(), nil); err != nil {
		t.Fatal(err)
	}
	if calls != 2 {
		t.Fatalf("calls = %d, want 2", calls)
	}
}

func TestVerifyWebhookSignature(t *testing.T) {
	secret := "whsec_test"
	body := []byte(`{"event":"message.sent"}`)
	ts := strconv.FormatInt(time.Now().Unix(), 10)
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(ts + "."))
	mac.Write(body)
	header := "t=" + ts + ",v1=" + hex.EncodeToString(mac.Sum(nil))

	if err := VerifyWebhookSignature(body, header, secret); err != nil {
		t.Fatalf("verify failed: %v", err)
	}
	if err := VerifyWebhookSignature(append(body, 'x'), header, secret); err == nil {
		t.Fatal("expected verification failure on tampered body")
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
