# INC-006 — Security Incident

**Owner:** On-call operator — escalate immediately (see [on-call.md](../on-call.md))
**Severity:** Critical
**Detection:** Unusual access patterns, Sentry anomaly, Cloudflare WAF alert, customer report

---

## Detect

- Unusual authentication failures in PlatformAuditLog
- Unexpected admin access in logs
- Customer reports data they shouldn't have seen
- Cloudflare WAF fires on anomalous traffic

## Immediate Response (first 15 minutes)

**Do not attempt to investigate while the attacker may still have access.**

1. **Rotate all credentials immediately:**
   - Django secret key (`DJANGO_SECRET_KEY`) — forces all sessions to expire
   - Database password
   - Redis password
   - RabbitMQ credentials
   - WhatsApp webhook secret
   - Stripe webhook secret
   - Any AI provider API keys

2. **Invalidate all active sessions:**
   After rotating `DJANGO_SECRET_KEY` and redeploying, all user sessions are invalidated.

3. **Take the application offline if active exfiltration is suspected:**
   ```bash
   docker compose stop web events
   ```

4. **Preserve evidence:**
   ```bash
   # Save current application logs before any restart
   docker compose logs web > /tmp/incident-web-$(date +%Y%m%d%H%M%S).log
   docker compose logs nginx > /tmp/incident-nginx-$(date +%Y%m%d%H%M%S).log
   ```

## Investigate

- Check `PlatformAuditLog` for unusual operator actions
- Check Nginx access logs for unusual endpoints or IPs
- Check Sentry for unusual error patterns
- Check Cloudflare Access logs (if available) for the affected time window
- Identify the entry point (compromised credentials? vulnerable endpoint? insider?)

## Contain and Recover

1. Fix the vulnerability or revoke compromised credentials
2. Redeploy with new credentials via the standard deploy pipeline
3. Verify all sessions are invalidated (changed secret key handles this)
4. Monitor for repeat access attempts

## Notify

If customer data was accessed or exfiltrated:
- Notify affected pilot customers promptly and directly
- Document what data was accessed, when, and by whom (as far as known)
- Consult the privacy policy obligations around data breach notification

## Record

A security incident record must document:
- When it was detected and how
- What was accessed and by whom (if known)
- What credentials were rotated
- What was fixed
- Whether customers were notified and when
