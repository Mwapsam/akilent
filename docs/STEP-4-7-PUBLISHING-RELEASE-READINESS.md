# Step 4.7 — Publishing Release Readiness

**Date**: 2026-10-15  
**Status**: Planning  
**Objective**: Close remaining operational gaps to declare publishing pipeline production-ready

---

## Completed: Step 4.6B Legacy Retirement

✓ Variant orchestration is mandatory (no silent fallback)  
✓ Legacy polling task deprecated and removed from Beat schedule  
✓ 111 regression tests pass (no regressions)  
✓ Staging reset command created and environment-guarded  

**Next milestone**: Staging lifecycle validation (in parallel with this work)

---

## Release Blocker 1: Enqueue-Failure Recovery

**Problem**: If Celery enqueue fails, variant may be stuck in PUBLISHING status forever.

**Example**: 
```
variant.status = PUBLISHING
variant.save()
publish_variant_async.delay(variant.pk)  # ← Broker unreachable, task not queued
# Variant is PUBLISHING forever; no worker will pick it up
```

**Solution Approach**:

1. **Atomic variant claiming with task enqueue verification**
   - Claim variant atomically (`status = PUBLISHING`)
   - Attempt to enqueue task
   - If enqueue fails: immediately reset variant to PUBLISHING (for manual retry)
   - If enqueue succeeds: variant remains PUBLISHING until worker picks it up

2. **Implement in `publish_variant_async()` task function**
   ```python
   @shared_task(bind=True, max_retries=3, queue="outbound")
   def publish_variant_async(self, variant_id: int) -> str:
       try:
           variant = ContentPostVariant.objects.select_for_update().get(pk=variant_id)
       except ContentPostVariant.DoesNotExist:
           return "not_found"
       
       # Safeguard: don't retry published/cancelled/reconciling/processing variants
       if variant.status in (...):
           return f"skipped_{variant.status}"
       
       # Validate approval at execution time
       if post.approval_required and not post.is_approval_valid():
           # Mark failed
           return "approval_invalid"
       
       try:
           result = publish_variant(variant)
           # Handle result...
       except Exception as exc:
           # Attempt retry; if max retries exceeded, mark FAILED
   ```

3. **Test coverage**:
   - Enqueue succeeds → variant proceeds normally
   - Enqueue fails → variant remains PUBLISHING (can be retried)
   - Variant stuck in PUBLISHING for X hours → alert triggered

**Files to modify**:
- apps/publishing/tasks.py — publish_variant_async() safety improvements
- apps/publishing/tests/test_enqueue_failure_recovery.py — NEW (4-6 tests)

---

## Release Blocker 2: Reconciliation Operations

**Problem**: Ambiguous timeout (API accepted request but response timed out) leaves variant in RECONCILING. Need explicit recovery path.

**Example**:
```
variant.status = RECONCILING
variant.last_error = "Final publication timed out; may be in provider"
# How does operator recover? How do we know if it was actually published?
```

**Solution Approach**:

1. **Reconciliation view/API**
   - List all variants in RECONCILING state
   - Show container_id and timestamp
   - Option: "Mark as published (if confirmed in provider)" → set status to PUBLISHED, set publication_id
   - Option: "Retry publication attempt" → reset to PUBLISHING for retry

2. **Manual provider check before reconciling**
   - When variant enters RECONCILING, log container_id and variant_id
   - Operator checks Instagram/Facebook API: is media actually published?
   - If yes: operator marks as published manually
   - If no: operator triggers retry

3. **Automatic reconciliation (optional, Phase 2)**
   - Add a background task that polls media state for RECONCILING variants
   - If media found: automatically mark PUBLISHED
   - If not found after timeout: mark FAILED

**Files to modify**:
- apps/publishing/views.py — NEW reconciliation view
- apps/publishing/urls.py — route for reconciliation
- apps/publishing/tests/test_reconciliation.py — NEW (4-6 tests)

---

## Release Blocker 3: Polling Reliability

**Problem**: Polling must survive worker restarts and handle exhausted retries gracefully.

**Solution Approach**:

1. **Persistence across worker restarts**
   - Variant metadata stores container_id and last_poll_attempt timestamp
   - `poll_ig_variant_containers()` task is idempotent:
     ```python
     qs = ContentPostVariant.objects.filter(
         platform="instagram",
         status=PROCESSING,
     ).exclude(metadata__ig_container_id__exact="")
     ```
   - Even if worker restarts mid-poll, next run picks up from metadata
   - ✓ Already implemented; verify in test

2. **Exhaustion handling**
   - If variant remains PROCESSING after N days, mark as RECONCILING
   - Add metadata field: `last_poll_attempt` timestamp
   - In polling task: if `now - last_poll_attempt > 2 days`, mark RECONCILING instead of looping

3. **Test coverage**:
   - Polling survives worker restart (container_id persisted in metadata)
   - Polling times out gracefully (variant marked RECONCILING after N retries)
   - No infinite loops in PROCESSING state

**Files to modify**:
- apps/publishing/services.py — add exhaustion check to try_publish_ig_variant_container()
- apps/publishing/models.py — ContentPostVariant.metadata schema documentation
- apps/publishing/tests/test_polling_reliability.py — NEW (3-5 tests)

---

## Release Blocker 4: Production Data Migration

**Problem**: Deploying variant-only code breaks any posts without variants.

**Solution Approach**:

1. **Pre-deployment audit**
   ```sql
   SELECT COUNT(*) FROM publishing_contentpost 
   WHERE id NOT IN (SELECT DISTINCT post_id FROM publishing_contentpostvariant);
   ```
   - If count = 0: safe to deploy immediately
   - If count > 0: backfill variants before deploying code

2. **Backfill migration** (if needed)
   - For each post without variants:
     - Create one variant per platform in post.platforms
     - Copy publication_id, publication_url, status from post
     - If post.status = PUBLISHED and post has publication IDs, variant.status = PUBLISHED
     - If post.status = PUBLISHING, variant.status = PROCESSING (for in-flight IG videos)

3. **Deployment order**:
   - Step 1: Deploy backfill migration (safe on production)
   - Step 2: Deploy variant-only code (publish_post() raises ValueError)
   - Step 3: Remove legacy functions and polling task (optional, cleanup)

**Files to create** (if count > 0):
- apps/publishing/migrations/000X_backfill_variants_for_posts.py — NEW

---

## Release Blocker 5: Observability

**Problem**: Stuck variants, repeated failures, and reconciliation states must be monitored.

**Solution Approach**:

1. **Metrics to track**
   - Variants stuck in PUBLISHING > 1 hour (should progress to PUBLISHED or FAILED)
   - Variants in RECONCILING state (need manual intervention)
   - Variants entering FAILED after max retries (per error type)
   - Polling task completion rate (publish rate from PROCESSING)

2. **Alert conditions**
   - Any variant in PROCESSING for > 2 days → alert "Instagram container polling exhausted"
   - Any variant in RECONCILING for > 1 day → alert "Reconciliation needed"
   - Any variant in PUBLISHING for > 6 hours → alert "Stuck publishing variant"

3. **Dashboard views**:
   - Variants by status (published, failed, reconciling, processing, publishing)
   - Recent errors by type (validation, provider, timeout, unknown)
   - Publishing success rate per day
   - Polling completion rate

4. **Logging points**:
   - When variant enters PROCESSING (container created)
   - When polling detects FINISHED (before final publish)
   - When variant enters RECONCILING (ambiguous timeout)
   - When variant max retries exceeded

**Files to modify**:
- apps/publishing/services.py — add logging
- apps/publishing/tasks.py — add metric collection
- NEW: apps/publishing/metrics.py — metric definitions
- NEW: monitoring dashboard spec

---

## Staging Validation Test Matrix

Before declaring production-ready, validate these scenarios in staging:

| Scenario | Entry Point | Expected Result | Test |
|---|---|---|---|
| **Normal flow** | | | |
| Facebook post | UI → Schedule → Publish | Published immediately | ✓ |
| Instagram image | UI → Schedule → Publish | Published immediately | ✓ |
| Instagram video | UI → Schedule → Publish | PROCESSING (awaiting polling) | ✓ |
| **Error handling** | | | |
| Publish before approval | publish_post_async | FAILED (approval required) | ✓ |
| Missing platform connection | UI → Publish | FAILED (no provider) | ✓ |
| Rate limit (FB) | Retry failed variant | Exponential backoff, max 3 retries | ✓ |
| **Partial success** | | | |
| FB succeeds, IG fails | Multi-channel post | FB published, IG failed | ✓ |
| Retry failed channel only | Manual retry on IG | IG retries, FB unchanged | ✓ |
| **Video polling** | | | |
| Container completes | Poll task runs | Variant published automatically | ✓ |
| Container times out | Poll after 2 days | Variant marked RECONCILING | ✓ |
| Container fails | Poll detects ERROR | Variant marked FAILED | ✓ |
| **Atomicity** | | | |
| Concurrent publish attempt | Celery runs task twice | Only one variant.status=PUBLISHED | ✓ |
| Worker restart mid-poll | Worker crashes | Polling resumes from metadata | ✓ |
| Enqueue fails | Broker unreachable | Variant remains PUBLISHING, can retry | ✓ |
| **Reconciliation** | | | |
| Ambiguous timeout | Publish API times out | Variant enters RECONCILING | ✓ |
| Operator confirms published | Reconciliation UI | Variant marked PUBLISHED | ✓ |
| Operator triggers retry | Reconciliation UI | Variant reset to PUBLISHING | ✓ |

---

## Implementation Plan

### Phase A: Enqueue-Failure Recovery (1-2 days)
- Implement atomic claiming with task enqueue safeguards
- Add tests for enqueue failure scenarios
- Validate in staging

### Phase B: Reconciliation Operations (1-2 days, parallel)
- Build reconciliation view/API
- Add manual intervention flow
- Document operator workflow

### Phase C: Polling Reliability (1 day, depends on Phase A)
- Add exhaustion detection to polling task
- Implement timeout logic (PROCESSING → RECONCILING after N days)
- Add reliability tests

### Phase D: Staging Execution (2-3 days, parallel with Phases B-C)
- Reset staging data
- Run full regression test suite
- Execute validation test matrix (table above)
- Verify metrics collection and alerts

### Phase E: Production Preparation (1 day)
- Audit production for posts without variants
- Create backfill migration if needed
- Document deployment sequence

### Phase F: Post-Deployment Monitoring (1+ weeks)
- Monitor publishing success rates
- Track stuck variants and reconciliation states
- Respond to operator alerts
- Validate metrics accuracy

---

## Success Criteria

✓ Step 4.6B: Legacy retirement complete and tested  
✓ Step 4.7A: Enqueue failure recovery implemented and tested  
✓ Step 4.7B: Reconciliation operations available  
✓ Step 4.7C: Polling reliability verified  
✓ Staging: All scenarios in test matrix validated  
✓ Production: Data migrated, workers configured, alerts active  

When all are complete, publishing pipeline is **release-ready**.

---

## Known Limitations (Acceptable for Pilot)

- No automatic reconciliation (Phase 2; manual intervention acceptable)
- No advanced retry strategies (constant exponential backoff is sufficient)
- No cross-platform approval (each variant approved independently, acceptable for single-creator model)
- Operator must check provider API manually for RECONCILING (acceptable; rare edge case)

---

## Appendix: Enqueue-Failure Recovery Code Outline

```python
# In publish_variant_async(self, variant_id)

with transaction.atomic():
    variant = ContentPostVariant.objects.select_for_update().get(pk=variant_id)
    
    # Safeguards...
    if variant.status in (...):
        return f"skipped_{variant.status}"
    
    # Claim atomically
    variant.status = ContentPostVariant.Status.PUBLISHING
    variant.attempt_count += 1
    variant.last_attempt_at = timezone.now()
    variant.save(...)

# Perform external work (outside transaction)
try:
    result = publish_variant(variant)
except Exception as exc:
    # Enqueue failures reset variant to allow manual retry
    if "broker" in str(exc).lower() or "connection" in str(exc).lower():
        variant.status = ContentPostVariant.Status.PUBLISHING  # Reset for retry
        variant.save(update_fields=["status"])
        raise
    # Other exceptions: mark failed or retry
```

---
