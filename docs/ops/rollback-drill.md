# Rollback Drill Procedure

**Purpose:** Verify that the automated rollback in `deploy.yml` works before a real deployment failure happens in production.

**Frequency:** Once before the pilot launch, then after any significant changes to the deploy pipeline.

**Required:** SSH access to the VPS, write access to the GitHub repository.

---

## Pre-drill checklist

- [ ] The application is healthy: `curl https://akilent.com/healthz` returns 200
- [ ] No pilot customers are actively using the application (schedule for off-peak)
- [ ] You have noted the current commit SHA: `git rev-parse HEAD`

---

## Step 1 — Identify the known-good commit

```bash
# On your local machine
GOOD=$(git rev-parse HEAD)
echo "Known-good commit: $GOOD"
```

## Step 2 — Create a controlled failure

Create a branch with a deliberate health-check failure:

```bash
git checkout -b test/rollback-drill
```

In `apps/core/views.py`, temporarily modify `healthz` to return 503:

```python
# TEMPORARY — rollback drill only, revert immediately
def healthz(request):
    from django.http import JsonResponse

    return JsonResponse(
        {"ok": False, "db": False, "cache": False, "beat": False, "drill": True},
        status=503,
    )
```

```bash
git add apps/core/views.py
git commit -m "chore: rollback drill — intentional healthz failure (revert immediately)"
git push origin test/rollback-drill
```

Then merge this to `main` via a PR, or push directly:

```bash
git checkout main
git merge test/rollback-drill
git push origin main
```

## Step 3 — Observe the deploy pipeline

1. Watch the GitHub Actions deploy job
2. Expected: after ~90 seconds, the deploy detects the health check failure and logs "Health check failed; rolling back to $PREV"
3. The automatic rollback runs the previous commit

## Step 4 — Verify the rollback succeeded

```bash
# On the VPS
git log --oneline -3
# The top commit should be the known-good one (not the drill commit)

curl -f https://akilent.com/healthz
# Must return: {"ok": true, "db": true, "cache": true, "beat": true}
```

Also verify:
- [ ] Workers are running: Pilot Command Center shows all queues healthy
- [ ] SSE is reachable: open a browser tab and check the network tab for the `/events/stream/` connection
- [ ] WhatsApp numbers show as connected (if enabled)
- [ ] Sentry captured the 503 error from the drill commit

## Step 5 — Confirm the drill commit is gone

```bash
git log --oneline | grep "rollback drill"
# Should not appear in the current HEAD chain
```

## Step 6 — Record the result

Fill in this table and keep it here:

| Date | Conducted by | Good commit | Drill commit | Auto-rollback fired? | Manual intervention needed? | Notes |
|---|---|---|---|---|---|---|
| _[date]_ | _[name]_ | _[SHA]_ | _[SHA]_ | Yes / No | Yes / No | |

---

## If the automatic rollback fails

If the deploy job did not roll back, or the application is still unhealthy:

```bash
# SSH to the VPS
cd "$DEPLOY_PATH"

# Force rollback to known-good commit
git reset --hard "$GOOD"
docker compose --profile prod build
docker compose --profile prod run --rm web python manage.py migrate --noinput
docker compose --profile prod run --rm web python manage.py collectstatic --noinput
docker compose --profile prod up -d --remove-orphans

# Verify
curl -f http://127.0.0.1:8000/healthz
```

If manual intervention was needed: investigate why the automatic rollback failed before the launch. The drill result is **NO-GO** until the automated rollback works without intervention.
