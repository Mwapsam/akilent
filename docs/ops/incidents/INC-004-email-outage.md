# INC-004 — Email Outage

**Owner:** On-call operator (see [on-call.md](../on-call.md))
**Severity:** High
**Detection:** Slack `#ops` email failure-spike alert, customers reporting undelivered emails

---

## Detect

- Slack alert: "Email failure spike"
- Customers report transactional emails not arriving
- SES bounce/complaint rate is elevated

## Confirm

```bash
# Check email worker logs for errors
docker compose logs --tail 100 celery_worker | grep -i "email\|smtp\|ses"

# Check SES sending limits and reputation in AWS Console
# Services > SES > Sending statistics
```

## Distinguish: Provider vs. configuration

| Symptom | Likely cause |
|---|---|
| SES `Throttling` / `SendingPausedException` | SES sandbox or rate limit |
| SMTP 421/450 | Stalwart temporarily rejecting |
| 5xx from SES | AWS outage |
| High bounce rate → sending paused | Reputation issue |

## Act

### SES rate limit or sandbox

- Check if the account is still in the SES sandbox (can only send to verified addresses).
- Request production access in AWS Console if still in sandbox.
- For rate limits: SES will retry; monitor queue depth.

### SES reputation / sending paused

- Check SES sending statistics for bounce/complaint rates.
- If bounce rate > 5% or complaint rate > 0.1%, SES may pause sending.
- Review recent campaigns for invalid addresses and clean the suppression list.
- Contact AWS Support to request sending to be re-enabled.

### Stalwart SMTP issue

```bash
docker compose logs stalwart 2>/dev/null | tail -50
# Or check the Stalwart admin UI at http://127.0.0.1:8080 (SSH tunnel)
```

## Verify Recovery

1. Send a test email through the Operator Console
2. Confirm it delivers within 2 minutes
3. Slack failure-spike alert clears

## Communicate

- Inform affected pilot customers about delayed transactional emails
- If bulk campaign emails were affected, note which campaigns and affected recipient counts

## Record

Log: outage duration, provider, root cause, emails affected (count), resolution.
