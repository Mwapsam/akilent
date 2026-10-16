# Staging Validation Checklist — Steps 4.7A–4.7C

**Objective**: Validate that enqueue-failure recovery, reconciliation operations, and polling reliability work together under real Celery, Redis, database, and provider behavior.

**Timeline**: 2–3 days (includes waiting for Instagram polling completion)

**Success Criteria**: All 19 items complete, all blockers identified and documented.

---

## Phase 1: Prepare Staging (1 hour)

### ✓ 1. Confirm staging database and settings

```bash
# Verify environment
echo $ENVIRONMENT  # Should be: staging
echo $DEBUG        # Should be: False or empty (staging=True in code)

# Check database
python manage.py dbshell
SELECT name FROM sqlite_master WHERE type='table' LIMIT 1;
# Should connect successfully

# Verify Redis is running
redis-cli ping
# Should respond: PONG

# Verify Celery worker is running
celery -A automator inspect active
# Should show worker(s) available
```

**Expected State**: Staging environment fully operational, Redis running, Celery workers active.

### ✓ 2. Run reset_staging_publishing --confirm

```bash
# DANGER: This deletes all staging posts and variants
python manage.py reset_staging_publishing --confirm

# Output should show:
# ✓ Deleted X variant records
# ✓ Deleted Y post records
```

**Expected State**: All publishing data cleared, users/accounts/connections preserved.

### ✓ 3. Confirm users, connections, and workspace settings remain intact

```bash
# Check via Django shell or UI
from apps.accounts.models import Account
from apps.instagram.models import InstagramBusinessAccount

Account.objects.all()  # Should have test accounts
InstagramBusinessAccount.objects.all()  # Should have test IG accounts
```

**Expected State**: All pre-staging connections and users present.

---

## Phase 2: Validate Normal Lifecycle (1 hour)

### ✓ 4. Create a post with channel variants

Via UI:
1. Go to `/publishing/posts/new/`
2. Create post with title "Test Post 1"
3. Select channels: Instagram + Facebook
4. Add media (image for both)
5. Add caption
6. Save as draft

**Expected State**: Post created with 2 variants (Instagram + Facebook), status=DRAFT.

### ✓ 5. Submit for approval and approve it

Via UI:
1. On post detail, click "Submit for Review"
2. As reviewer, click "Approve"

**Expected State**: Post status transitions DRAFT → IN_REVIEW → APPROVED.

### ✓ 6. Schedule and publish it

Via UI:
1. Set scheduled_at to 1 minute from now
2. Click "Schedule"
3. Wait for scheduled time or manually trigger `publish_due_posts` task
4. Observe variants transition to PUBLISHING

**Expected State**: Post scheduled, then published. Check variant status in database:
```bash
python manage.py dbshell
SELECT id, platform, status FROM publishing_contentpostvariant WHERE post_id = X;
# Should show: facebook PUBLISHED, instagram PUBLISHED (or PROCESSING for video)
```

### ✓ 7. Verify independent per-channel results

Create second post with same channels, but one deliberately fails:

Via UI or by mocking:
1. Create post with FB + IG channels
2. Schedule it
3. During publish, simulate Facebook API error (or use test account configuration)
4. Observe: IG PUBLISHED, FB FAILED

Via database:
```bash
SELECT platform, status, last_error FROM publishing_contentpostvariant WHERE post_id = X;
# instagram | published | 
# facebook  | failed    | API error details
```

**Expected State**: Per-channel independent outcomes verified. No silent failures.

---

## Phase 3: Test Failure Recovery (2–3 hours)

### ✓ 8. Simulate broker enqueue failure and restore Redis

**Controlled test** (staging only):

```bash
# 1. Stop Redis
redis-cli SHUTDOWN

# 2. Attempt to publish a variant via UI
# Go to a failed variant and click "Retry"
# Observe: Error message shown to user
# "Failed to enqueue retry (broker unreachable). Variant reset to FAILED."

# 3. Verify variant is FAILED, not stuck in PUBLISHING
python manage.py dbshell
SELECT status FROM publishing_contentpostvariant WHERE id = X;
# Should show: failed

# 4. Restart Redis
redis-server  # or your startup command

# 5. Retry the variant again
# Click "Retry" via UI
# Should succeed now (broker available)
```

**Expected State**: 
- Variant stuck in PUBLISHING if retry was lost (DB state after broker down)
- Variant reset to FAILED (enqueue-failure recovery worked)
- Successful retry after broker recovery

### ✓ 9. Verify a failed channel can be retried independently

Via UI:
1. On post with multi-channel partial success (one FAILED)
2. Click "Retry" on only the failed channel
3. Successful channel remains unchanged

**Expected State**: Only failed variant retried, others untouched.

### ✓ 10. Simulate an ambiguous publication timeout

Mock the provider publish endpoint to timeout:

```python
# In test or via Django shell:
from unittest.mock import patch
from apps.publishing.services import publish_variant

with patch("apps.publishing.providers.instagram.InstagramPublishProvider.publish_container") as mock:
    mock.side_effect = TimeoutError("Final publish timed out")
    # Trigger publish for a variant
    
# Expected: Variant enters RECONCILING state
python manage.py dbshell
SELECT status, last_error FROM publishing_contentpostvariant WHERE id = X;
# Should show: reconciling | "timed out; may be in provider"
```

**Expected State**: Variant in RECONCILING, not silently marked FAILED or PUBLISHED.

### ✓ 11. Resolve a reconciliation case with provider evidence

Via UI:
1. Go to `/publishing/reconciliation/`
2. Click on a RECONCILING variant
3. Investigate: check Instagram/Facebook provider account
4. If published: click "Confirm Published", enter publication ID and URL
5. If not published: click "Retry"

**Expected State**: Variant transitions to PUBLISHED (if confirmed) or FAILED (if retry). Audit log created.

```bash
python manage.py dbshell
SELECT action, performed_by, provider_publication_id FROM publishing_reconciliationlog WHERE variant_id = X;
# Should show: confirmed_published | user | publication_id
```

---

## Phase 4: Test Instagram Polling (1–2 days)

### ✓ 12. Publish a test video and confirm PROCESSING

Via UI:
1. Create post with Instagram video
2. Upload video (or use test media)
3. Schedule and publish

**Expected State**: Variant status = PROCESSING (waiting for Instagram transcoding)

```bash
SELECT status, metadata FROM publishing_contentpostvariant WHERE platform='instagram' AND post_id = X;
# Should show: processing | {"ig_container_id": "abc123", "last_poll_attempt": "2026-10-15T..."}
```

### ✓ 13. Verify persisted container ID and polling timestamps

Check metadata is recording poll attempts:

```bash
# Observe metadata updates as polling task runs
# Run task manually to see timestamp update:
python manage.py shell
from apps.publishing.tasks import poll_ig_variant_containers
poll_ig_variant_containers()

# Check metadata again:
SELECT metadata FROM publishing_contentpostvariant WHERE id = X;
# "last_poll_attempt" timestamp should be recent (current time)
```

**Expected State**: `last_poll_attempt` updated on each polling run (every 30 seconds). Container ID persisted.

### ✓ 14. Restart a worker and confirm polling resumes

**Critical test** (verifies worker recovery):

```bash
# 1. Identify a video still in PROCESSING state
SELECT id, metadata FROM publishing_contentpostvariant WHERE status='processing';
# Note the metadata["last_poll_attempt"] timestamp

# 2. Kill the Celery worker
pkill -f "celery -A automator"

# 3. Wait 30+ seconds (ensure no polling happens)

# 4. Restart the worker
celery -A automator worker -Q outbound -l info &

# 5. Wait for next poll cycle (30 seconds)

# 6. Check variant metadata again
SELECT metadata FROM publishing_contentpostvariant WHERE id = X;
# "last_poll_attempt" should be RECENT (after restart), not stale
```

**Expected State**: Polling resumes after worker restart. Container ID not lost. Polling timestamp updated to post-restart time.

### ✓ 15. Verify final publication and no duplicate posts

Once Instagram finishes transcoding (hours or days later):

```bash
# Check variant is PUBLISHED
SELECT status, publication_id FROM publishing_contentpostvariant WHERE id = X;
# Should show: published | ig_media_xyz

# Verify no duplicates in provider
# Go to Instagram account, search for video
# Should appear exactly once
```

**Expected State**: Video published once, variant marked PUBLISHED with provider ID. No duplicates.

---

## Phase 5: Verify Operations (1 hour)

### ✓ 16. Confirm variant polling runs every 30 seconds

Monitor Celery task logs:

```bash
# In worker output, should see:
# [tasks] Received task: apps.publishing.tasks.poll_ig_variant_containers...
# Repeated every ~30 seconds

# Or check Celery inspector:
celery -A automator inspect scheduled
# Should show poll-ig-variant-containers with next run time ~30s away
```

**Expected State**: Polling task running on schedule, no errors.

### ✓ 17. Confirm stale-claim and exhaustion tasks run on schedule

Monitor Celery Beat:

```bash
# Stale-claim reconciler (Step 4.7A) - runs hourly
# Should see in logs: "reconcile_stale_publishing_variants"

# Exhaustion detector (Step 4.7C) - runs every 2 hours
# Should see in logs: "escalate_exhausted_processing_variants"

# Verify scheduled:
celery -A automator inspect scheduled | grep reconcile
celery -A automator inspect scheduled | grep escalate
```

**Expected State**: Both tasks scheduled and running without errors.

### ✓ 18. Inspect errors, stuck variants, and reconciliation cases

Query production data:

```bash
# Stuck variants (PUBLISHING > 6 hours)
SELECT COUNT(*) FROM publishing_contentpostvariant 
WHERE status='publishing' AND last_error_at < datetime('now', '-6 hours');
# Should be 0 after stale reconciler runs

# Reconciling variants (waiting for operator)
SELECT COUNT(*) FROM publishing_contentpostvariant WHERE status='reconciling';
# Should be any that hit timeouts during test

# Errors by type
SELECT last_error_type, COUNT(*) FROM publishing_contentpostvariant 
WHERE status='failed' GROUP BY last_error_type;
# Shows error distribution
```

**Expected State**: No unexplained stuck variants. Errors classified correctly.

### ✓ 19. Capture results and remaining issues in the staging report

Create document:

```markdown
# Staging Validation Report — Steps 4.7A–4.7C

**Date**: 2026-10-15
**Tested by**: [Name]
**Environment**: Staging

## Summary
- ✅ All 19 checklist items completed
- [X] Issue(s) found: [description]
- [X] Blockers: [list]

## Phase 1: Preparation
- ✅ Environment verified
- ✅ Reset successful
- ✅ Connections preserved

## Phase 2: Normal Lifecycle
- ✅ Post created with variants
- ✅ Approval workflow passed
- ✅ Schedule published correctly
- ✅ Per-channel independence verified

## Phase 3: Failure Recovery
- ✅ Enqueue-failure reset to FAILED
- ✅ Redis recovery successful
- ✅ Reconciliation workflow works
- Issue: [if any]

## Phase 4: Polling
- ✅ Video entered PROCESSING
- ✅ Container ID persisted
- ✅ Worker restart recovery passed
- ⏳ Final publication (awaiting IG transcoding)

## Phase 5: Operations
- ✅ Polling runs every 30 seconds
- ✅ Reconciliation tasks scheduled
- ✅ No stuck variants observed
- Metrics: [success rate, timing, etc.]

## Remaining Issues
- [List any bugs, edge cases, performance issues]

## Recommendation
- Ready for production migration: [YES/NO]
- Required fixes before prod: [list]
```

**Expected State**: Report captures all findings, blockers identified.

---

## Precautions

### Redis Outage Test
- Perform **only in staging**
- Stop Redis, attempt retry, observe DB state
- Restart Redis promptly
- Verify actual DB state (not just UI message)
- Check variant status via `manage.py dbshell`

### Worker Restart Test
- Restart while video is **actively polling** (PROCESSING state)
- Not after publication is complete
- Verify `last_poll_attempt` updates after restart
- Confirm no missed polls

---

## Success Criteria

All 19 items must be ✅ before proceeding to Step 4.7D (Production Migration).

**Blockers for production**:
- Any stuck variants not recovered by reconciliation
- Duplicate publications
- Worker restart losses
- Task scheduling failures

**Minor issues** (acceptable for pilot):
- Transient provider errors (expected)
- Slow polling (depends on IG)
- Manual operator actions for reconciliation (by design)

---

## Timeline Estimate

| Phase | Duration | Notes |
|---|---|---|
| Phase 1 (Prepare) | 1 hour | Quick |
| Phase 2 (Normal) | 1 hour | Quick |
| Phase 3 (Failure) | 2–3 hours | Controlled tests |
| Phase 4 (Polling) | 1–2 days | Waiting for IG |
| Phase 5 (Ops) | 1 hour | Verification |
| **Total** | **2–3 days** | Parallelizable with Phase 5 |

---

## What Comes After?

Once staging passes:

1. **Step 4.7D — Production Migration**
   - Query production for posts without variants
   - Plan backfill migration if needed
   - Document deployment sequence

2. **Step 4.7E — Observability**
   - Add metrics for stuck claims, exhaustion, errors
   - Configure alerts
   - Build operational dashboards

3. **Production Deployment**
   - Execute pre-deployment audit
   - Deploy with monitoring
   - Monitor first week closely
   - Iterate on alerts and dashboards
