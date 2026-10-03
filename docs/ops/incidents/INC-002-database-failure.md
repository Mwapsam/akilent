# INC-002 — Database Failure

**Owner:** On-call operator (see [on-call.md](../on-call.md))
**Severity:** Critical
**Detection:** `/healthz` returns `{"db": false}` (HTTP 503), or Sentry database connection errors

---

## Detect

- `/healthz` returns 503 with `"db": false`
- Django logs show `OperationalError` or `ProgrammingError`
- Sentry fires on database exceptions

## Confirm

```bash
# Check if the db container is running
docker compose ps db

# Check db container logs
docker compose logs --tail 100 db

# Try to connect directly
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1"
```

## Act

### Container is stopped or crashing

```bash
docker compose restart db
# Wait 15 seconds, then check web
sleep 15
docker compose restart web
```

### Container is running but connections are exhausted

```bash
# Check active connections
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT count(*), state FROM pg_stat_activity GROUP BY state"

# Terminate idle connections if they are exhausting max_connections
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'idle' AND query_start < now() - interval '5 minutes'"
```

### Data corruption suspected

**DO NOT restart or write to the database until the situation is understood.**

1. Stop the web and Celery containers to prevent further writes:
   ```bash
   docker compose stop web events celery_worker celery_ai celery_beat
   ```
2. Create an emergency backup of the current state (even if corrupted):
   ```bash
   docker compose exec backup backup.sh dump
   ```
3. Contact the backup file (see [backups.md](../backups.md)) and restore from the last known-good dump to a scratch DB first, inspect rows, then decide whether to restore.
4. See [backups.md](../backups.md) for the full restore procedure.

## Verify Recovery

1. `docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM accounts_account"` returns a number
2. `/healthz` returns `{"ok":true,"db":true}`
3. Login and dashboard load

## Communicate

- If data was lost: inform affected pilot customers immediately with what was lost and when
- All database incidents must be recorded regardless of severity

## Record

Document the incident with: detection time, cause, data affected (if any), recovery steps taken.
