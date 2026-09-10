package akilent

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strconv"
	"strings"
	"time"
)

// ErrSignatureVerification is returned by VerifyWebhookSignature on any mismatch.
var ErrSignatureVerification = errors.New("akilent: webhook signature verification failed")

const webhookToleranceSeconds = 300

// VerifyWebhookSignature checks an inbound X-Akilent-Signature header
// (t=<unix>,v1=<hex>) against the exact raw request body.
func VerifyWebhookSignature(payload []byte, header, secret string) error {
	return verifyWebhookSignature(payload, header, secret, webhookToleranceSeconds, time.Now())
}

func verifyWebhookSignature(payload []byte, header, secret string, tolerance int, now time.Time) error {
	var ts, v1 string
	for _, part := range strings.Split(header, ",") {
		if i := strings.IndexByte(part, '='); i > 0 {
			switch part[:i] {
			case "t":
				ts = part[i+1:]
			case "v1":
				v1 = part[i+1:]
			}
		}
	}
	if ts == "" || v1 == "" {
		return ErrSignatureVerification
	}
	tsInt, err := strconv.ParseInt(ts, 10, 64)
	if err != nil {
		return ErrSignatureVerification
	}
	if d := now.Unix() - tsInt; d > int64(tolerance) || d < -int64(tolerance) {
		return ErrSignatureVerification
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(ts + "."))
	mac.Write(payload)
	expected := hex.EncodeToString(mac.Sum(nil))
	if !hmac.Equal([]byte(expected), []byte(v1)) {
		return ErrSignatureVerification
	}
	return nil
}
