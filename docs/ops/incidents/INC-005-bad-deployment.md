# INC-005 — Bad Deployment

**Owner:** On-call operator (see [on-call.md](../on-call.md))
**Severity:** High
**Detection:** Automated rollback fires in GitHub Actions, or `/healthz` returns 503 after a deploy

---

## Detect

- GitHub Actions deploy job fails with "Health check failed; rolling back"
- Uptime monitor fires within minutes of a deployment
- Sentry spikes with a new exception type immediately after deployment

## Confirm

```bash
# On the VPS: check what commit is running
git log --oneline -3

# Check health
curl -f http://127.0.0.1:8000/healthz

# Check web container logs for the failure
docker compose logs --tail 200 web
```

## Act

### Automated rollback fired and succeeded

- The deployment script already rolled back to `$PREV`.
- Verify the application is healthy (`curl /healthz`).
- Investigate the failure in the GitHub Actions logs before re-deploying.

### Automated rollback fired but failed

```bash
# Manual rollback
PREV=$(git log --oneline | sed -n '2p' | awk '{print $1}')
git reset --hard "$PREV"
docker compose --profile prod build
docker compose --profile prod run --rm web python manage.py migrate --noinput
docker compose --profile prod run --rm web python manage.py collectstatic --noinput
docker compose --profile prod up -d --remove-orphans

# Verify
curl -f http://127.0.0.1:8000/healthz
```

### Application is running but behaving incorrectly (not failing health check)

- Revert the commit causing the issue via a new Git commit (do not force-push main).
- Push the revert through CI normally — the deploy pipeline will pick it up.

## Notes

- **Migrations are NOT reversed on rollback.** Rolled-back code must be compatible with the already-applied migrations. This is safe only when all migrations are additive (new tables, nullable columns, no destructive schema changes).
- If a migration removed or renamed a column that the rolled-back code now needs: the rollback will succeed for reads/writes not touching that column, but you must assess data impact before proceeding.

## Verify Recovery

1. `/healthz` returns 200 with `{"ok":true,"db":true}`
2. Login and dashboard load
3. Check Sentry — new errors should stop

## Communicate

- Post in Slack `#ops`: "Bad deployment detected at [time]; rolled back to [commit]. Investigating root cause."
- If pilot customers were affected: brief note with resolution time

## Record

Document: commit SHA deployed, failure mode, rollback method, root cause analysis, fix applied.
