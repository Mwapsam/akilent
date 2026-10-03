# INC-007 — Queue / Worker Failure

**Owner:** On-call operator (see [on-call.md](../on-call.md))
**Severity:** High — background tasks stop silently; application remains online
**Detection:** Pilot Command Center shows queue as "late", Slack failure alerts stop firing (beat dead), or customer reports that automations / scheduled messages are not running

---

## Detect

- Pilot Command Center (`/manage/`) shows one or more queues as late
- `/healthz` returns `"beat": false` — celery_beat is dead
- Customers report automations or scheduled messages not executing
- WhatsApp outbound messages pile up (drain_outbound_queue not running)

## Confirm

```bash
# Check which containers are running
docker compose ps

# Check worker logs
docker compose logs --tail 100 celery_worker
docker compose logs --tail 100 celery_ai
docker compose logs --tail 100 celery_beat

# Check RabbitMQ queue depths (SSH tunnel to port 15672, or):
docker compose exec rabbitmq rabbitmq-diagnostics -q list_queues
```

## Act

### Worker container stopped or crashing

```bash
# Restart the affected worker
docker compose restart celery_worker
# or
docker compose restart celery_ai
# or (if beat is dead)
docker compose restart celery_beat
```

### Queue backlog is very large

- Do not drain artificially — let workers process normally.
- Identify why the backlog grew: was a worker down for a long time? Did a task keep failing and retrying?
- If tasks are stuck in a retry loop, check Sentry for the underlying error and fix it.

### RabbitMQ container stopped

```bash
docker compose restart rabbitmq
sleep 20  # wait for rabbit to fully initialize
docker compose restart celery_worker celery_ai celery_beat
```

### Beat is dead (`/healthz` shows `"beat": false`)

```bash
docker compose restart celery_beat
# The beat-heartbeat key will refresh within 2 minutes
```

**Impact of beat death:** All periodic tasks stop until beat restarts:
- WhatsApp outbound draining (every 10 seconds)
- Workflow due-job scanning (every 60 seconds)
- Failure spike alerting (every 15 minutes)
- Worker heartbeats (every 60 seconds)
- Scheduled messages and follow-ups

## Verify Recovery

1. `/healthz` returns `"beat": true` within 3 minutes of restarting beat
2. Pilot Command Center shows all queues as healthy
3. RabbitMQ management shows queue depths draining

## Communicate

If automated messages or workflows were delayed:
- Inform affected pilot customers of the approximate delay window
- Clarify which messages were delayed vs. skipped (RabbitMQ retries durable tasks; tasks that expired during the outage may have been dropped)

## Record

Document: which queues/workers were affected, duration, root cause, messages lost (if any).
