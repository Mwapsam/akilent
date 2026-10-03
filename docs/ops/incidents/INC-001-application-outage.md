# INC-001 — Application Outage

**Owner:** On-call operator (see [on-call.md](../on-call.md))
**Severity:** Critical
**Detection:** Uptime monitor alerts on `https://akilent.com/healthz` returning non-200, or Sentry error-rate spike

---

## Detect

- Uptime monitor fires → Slack `#ops` alert
- Users cannot load the dashboard or login page
- `/healthz` returns 503 or times out

## Confirm

1. `curl -f https://akilent.com/healthz` — confirm 503 or timeout
2. Check Slack `#ops` for any other concurrent alerts (queue failure? deploy just ran?)
3. Check `docker compose ps` on the VPS (SSH in) — which containers are up/down/restarting?

## Act

### If a deployment just ran (within the last 30 minutes)

```bash
# The deploy workflow auto-rolls back on health failure, but verify it completed
git log --oneline -3
# If auto-rollback didn't fire, trigger a manual redeploy of the previous commit:
PREV=$(git log --oneline | sed -n '2p' | awk '{print $1}')
git reset --hard "$PREV"
docker compose --profile prod build
docker compose --profile prod run --rm web python manage.py migrate --noinput
docker compose --profile prod run --rm web python manage.py collectstatic --noinput
docker compose --profile prod up -d --remove-orphans
```

### If no recent deployment

```bash
# Check web container logs
docker compose logs --tail 100 web

# Restart the web container
docker compose restart web

# If that doesn't help, check database
docker compose logs --tail 50 db
docker compose restart db
sleep 10
docker compose restart web
```

### If database is the cause

See [INC-002 — Database Failure](INC-002-database-failure.md).

## Verify Recovery

1. `curl -f https://akilent.com/healthz` returns `{"ok":true,"db":true,"cache":true}`
2. Manual login works in browser
3. Uptime monitor clears
4. Sentry error rate returns to baseline

## Communicate

- **< 5 minutes:** No communication required while investigating
- **> 5 minutes:** Post to Slack `#ops`: "We're investigating an outage affecting [feature]. ETA: [X minutes]."
- **Resolved:** Post: "The outage affecting [feature] is resolved as of [time]. [Brief cause if known]."
- If pilot customers are affected: contact them directly with a brief note

## Record

Create a post-incident note in Slack `#ops` or a doc covering:
- When it was detected, when resolved
- Root cause
- What was done
- What will prevent recurrence
