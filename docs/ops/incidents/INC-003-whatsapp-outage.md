# INC-003 — WhatsApp Outage

**Owner:** On-call operator (see [on-call.md](../on-call.md))
**Severity:** High
**Detection:** Slack `#ops` WhatsApp failure-spike alert, customers reporting undelivered messages

---

## Detect

- Slack alert: "WhatsApp failure spike: X failures in the last 15 minutes"
- Customers report messages not being sent or received
- WhatsApp number health dashboard shows degraded state

## Confirm

1. Check Meta's status page: https://www.metastatus.com/
2. Check the WhatsApp number health dashboard in the Operator Console (`/manage/`)
3. Check for Meta API error codes in Sentry or application logs

## Distinguish: Meta outage vs. our configuration

| Symptom | Likely cause |
|---|---|
| 503/5xx from Meta API | Meta outage — wait |
| 400 with `invalid_parameter` | Our config/template issue |
| 401/403 | Access token expired or revoked |
| Webhook delivery failures only | Our server/WAF issue |

## Act

### Meta outage (their side)

- Do nothing except monitor. Messages will not be delivered until Meta recovers.
- Inform affected pilot customers: "WhatsApp delivery is currently delayed due to a Meta platform issue. We'll update you when it resolves."
- Check Meta's status page for ETA.

### Access token expired

```bash
# The token is stored per-number in the WhatsApp dashboard
# Navigate to /manage/ > WhatsApp > [number] > Connection settings
# Re-authenticate the number via Embedded Signup
```

### Webhook delivery failures (our server)

```bash
# Check nginx and web logs
docker compose logs --tail 100 nginx
docker compose logs --tail 100 web | grep webhook

# Check Cloudflare WAF — if the WAF is blocking webhook traffic,
# temporarily allowlist the Meta IP range or disable WAF on /whatsapp/
```

## Verify Recovery

1. Send a test WhatsApp message from the Operator Console
2. Confirm delivery status updates arrive
3. Check that Slack failure-spike alert has cleared

## Communicate

- Inform affected pilot customers when messages resume
- If messages were lost (not retried): contact Meta support for delivery receipts

## Record

Log the outage duration, root cause, and whether any customer messages were lost.
