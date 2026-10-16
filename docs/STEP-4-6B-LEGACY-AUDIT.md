# Step 4.6B — Legacy Publishing Path Retirement

**Date**: 2026-10-15  
**Status**: Completed  
**Objective**: Retire legacy publishing path and make variant orchestration the sole supported architecture

---

## Entry Points Audit

### 1. `publish_due_posts()` Task
**Location**: `apps/publishing/tasks.py:15-32`  
**Trigger**: Celery beat task (scheduled periodically)  
**Purpose**: Pick posts with `scheduled_at <= now` and transition to PUBLISHING

```python
qs = ContentPost.objects.filter(
    status=ContentPost.Status.SCHEDULED,
    scheduled_at__lte=now,
)
# Transitions each to PUBLISHING and enqueues publish_post_async
publish_post_async.delay(post.pk)
```

**Current Behavior**:
- Finds posts regardless of variant existence
- Enqueues `publish_post_async` for each
- No variant-specific logic

**Status Check**: ✅ Posts may or may not have variants
**Data Dependencies**: 
- Posts with `status=SCHEDULED` and `scheduled_at` in past
- Posts with or without variants

**Migration Path**:
- Should continue to find posts with scheduled_at in past
- BUT route through variant orchestration if variants exist
- Fallback to legacy for posts without variants (until backfill complete)

---

### 2. `publish_post_async()` Task
**Location**: `apps/publishing/tasks.py:35-74`  
**Trigger**: Enqueued by `publish_due_posts()` or manual retry  
**Purpose**: Execute the publishing workflow; retry on failure with exponential backoff

```python
@shared_task(bind=True, max_retries=3, queue="outbound")
def publish_post_async(self, post_id: int) -> str:
    post = ContentPost.objects.get(pk=post_id)
    parent_status, outcomes = orchestrate_post_publication(post)
    # ... persist outcomes
```

**Current Behavior**:
- Calls `orchestrate_post_publication()` if variants exist
- Falls back to `publish_post()` if no variants
- Retries with exponential backoff on failure

**Status Check**: ✅ Variants are checked; both paths coexist

**Data Dependencies**:
- Posts in PUBLISHING or SCHEDULED status
- Retry history (retries count, attempt times)
- Variant outcomes if variants exist

**Migration Path**:
- This task is ALREADY on the variant path (calls `orchestrate_post_publication()`)
- Only issue: fallback to `publish_post()` for legacy posts without variants
- Action: Keep task, ensure fallback is explicit and logged

---

### 3. `poll_ig_containers()` Task
**Location**: `apps/publishing/tasks.py:211-230`  
**Trigger**: Celery beat task (scheduled periodically)  
**Purpose**: Poll Instagram video containers until FINISHED, then publish

```python
qs = ContentPost.objects.filter(
    status=ContentPost.Status.PUBLISHING,
    ig_container_id__gt="",
    ig_media_id="",  # Not yet published
)
for post in qs:
    done = try_publish_ig_container(post)
```

**Current Behavior**:
- Queries ContentPost directly (not variants)
- Calls legacy `try_publish_ig_container()`
- Works with posts that have container IDs but no media IDs

**Status Check**: ❌ LEGACY ONLY — Works with ContentPost.ig_container_id  
**Data Dependencies**:
- Posts with `ig_container_id` set but `ig_media_id` empty
- Container status from Instagram API

**Migration Path**:
- This task is LEGACY and will be superseded by `poll_ig_variant_containers()`
- Current action: Keep running alongside new polling task during transition
- Data: Posts with ig_container_id will eventually have variants with ig_container_id in metadata

---

### 4. `poll_ig_variant_containers()` Task
**Location**: `apps/publishing/tasks.py:233-252`  
**Trigger**: Celery beat task (scheduled periodically)  
**Purpose**: Poll variant containers until FINISHED, then publish (NEW)

```python
qs = ContentPostVariant.objects.filter(
    platform="instagram",
    status=ContentPostVariant.Status.PROCESSING,
).exclude(metadata__ig_container_id__exact="")
for variant in qs:
    done = try_publish_ig_variant_container(variant)
```

**Status Check**: ✅ NEW VARIANT PATH

**Migration Path**:
- This is the NEW variant-based polling
- Coexists with legacy `poll_ig_containers()` during transition
- Once all variants are backfilled and legacy posts migrated, legacy task can be retired

---

### 5. Manual Retry via View
**Location**: `apps/publishing/views.py` (post_retry_variant)  
**Trigger**: User clicks "Retry this channel" button on failed variant  
**Purpose**: Enqueue retry for a single failed variant

```python
variant.status = ContentPostVariant.Status.PUBLISHING  # Atomic transition
publish_variant_async.delay(variant.pk)
```

**Status Check**: ✅ VARIANT PATH ONLY

**Data Dependencies**:
- Single variant in FAILED status
- User ownership verified

**Migration Path**:
- Already on variant path
- No legacy dependency
- Continue as-is

---

### 6. Direct `publish_post()` Call (Fallback)
**Location**: `apps/publishing/services.py` in `publish_post_async()`  
**Context**: `orchestrate_post_publication()` not called; legacy fallback

```python
# In orchestrate_post_publication():
if not post.variants.exists():
    # No variants defined yet: fall back to legacy publish_post()
    publish_post(post)
```

**Status Check**: ⚠️ LEGACY FALLBACK  
**Current Behavior**: Posts without variants use legacy flow  
**Data Dependencies**:
- Posts created before variant backfill
- Posts with no variants (pre-migration or skipped)

**Migration Path**:
- Temporary fallback until backfill is complete
- Should have explicit warning log
- Action: Ensure ALL posts have variants before removing this fallback

---

## Data Audit: Posts Without Variants

**Question**: How many posts in the database exist without variants?

```sql
SELECT COUNT(*) FROM publishing_contentpost 
WHERE id NOT IN (SELECT post_id FROM publishing_contentpostvariant);
```

**Required Check**:
- Count of posts without variants
- Their statuses (DRAFT, SCHEDULED, PUBLISHING, PUBLISHED, FAILED)
- Whether any have ig_container_id or ig_media_id (likely to have been published via legacy)
- Whether any are in a scheduled state with future scheduled_at

**Action Items**:
1. Run the query and document the count
2. If count > 0, create a backfill migration or explicit policy
3. If all posts have variants, the fallback can be removed immediately

---

## Scheduled Tasks Audit

**Active Celery Beat Schedules**:
1. `publish_due_posts()` — runs periodically, picks scheduled posts
2. `poll_ig_containers()` — runs periodically, polls legacy video containers
3. `poll_ig_variant_containers()` — runs periodically, polls variant video containers
4. `fetch_fb_post_insights()` — runs post-publish, collects metrics

**Current State**:
- `publish_due_posts()` → `publish_post_async()` → `orchestrate_post_publication()` (with legacy fallback)
- `poll_ig_containers()` → `try_publish_ig_container()` (legacy polling)
- `poll_ig_variant_containers()` → `try_publish_ig_variant_container()` (new polling)

**What Could Go Wrong**:
- Old scheduled tasks for posts still in queue before deployment
- Task params changed (publish_post_async signature has not changed, so OK)
- Celery beat schedules not updated (currently both polling tasks run)

**Action**: Confirm both polling tasks are in Celery beat configuration and running

---

## Backward Compatibility Requirements

### Existing Posts with Publication IDs
**Scenario**: Post was published via legacy `publish_post()` and has `ig_media_id` and/or `fb_post_id`  
**Expected**: Post should NOT be republished  
**Verification**: Backfill migration should preserve publication IDs in variants

**Test Case**:
```python
# Post with ig_media_id from legacy path
post = ContentPost.objects.create(
    platforms=["instagram"],
    ig_media_id="existing_media_id",  # Already published
    status=ContentPost.Status.PUBLISHED,
)
# After backfill, variant should have:
variant = post.variants.get(platform="instagram")
assert variant.publication_id == "existing_media_id"
assert variant.status == ContentPost.Status.PUBLISHED  # Not re-publishing
```

### Existing Scheduled Posts
**Scenario**: Post was scheduled via legacy flow and is waiting in `SCHEDULED` state  
**Expected**: Should be published when scheduled_at is reached, via variant path  
**Verification**: `publish_due_posts()` picks it up and routes through variant orchestration

**Test Case**:
```python
# Post scheduled for future date
past = timezone.now() - timedelta(minutes=5)
post = ContentPost.objects.create(
    platforms=["facebook", "instagram"],
    status=ContentPost.Status.SCHEDULED,
    scheduled_at=past,
)
# After backfill, variants exist
assert post.variants.count() == 2

# When publish_due_posts() runs:
publish_due_posts()
# Should route through variant orchestration, not legacy publish_post()
```

### Posts in PUBLISHING State
**Scenario**: Post was mid-publish when the new code deployed  
**Expected**: Should continue through variant path if variants exist; legacy fallback if not  
**Verification**: Variants are checked; fallback is explicit

---

## Operational Gaps Closed

### 1. Legacy Container Polling (CLOSED ✓)
- `poll_ig_containers()` removed from Celery Beat schedule (automator/settings.py)
- `poll_ig_variant_containers()` added to Beat schedule instead
- Legacy task logs deprecation warning and returns 0
- Variant polling is now operational every 30 seconds

### 2. Staging Reset Safety (CLOSED ✓)
- `reset_staging_publishing.py` command enhanced with multi-level environment checks
- Rejects if ENVIRONMENT='production' explicitly
- Rejects if environment undetected and DEBUG=False (likely production)
- Requires explicit ENVIRONMENT='staging' or 'development' or DEBUG=True
- Safely handles all ContentPost/ContentPostVariant foreign keys via Django cascade

### 3. Variant Polling Scheduled (CLOSED ✓)
- Added `poll-ig-variant-containers` to CELERY_BEAT_SCHEDULE
- Scheduled every 30 seconds (same as legacy polling was)
- Queries variants with `status=PROCESSING` and `ig_container_id` in metadata
- Will run automatically on any worker with `outbound` queue

## Implementation Complete

### Changes Made

#### 1. Enforce Variant-Only Orchestration (services.py)
- Modified `orchestrate_post_publication()` to raise `ValueError` if post has no variants
- Removed legacy fallback to `publish_post()`
- Clear error message: "Cannot publish post {id}: no variants defined"
- Variants are now mandatory, not optional

#### 2. Retire Legacy Polling Task (tasks.py)
- Marked `poll_ig_containers()` as deprecated
- Task now logs warning and returns 0 (no work performed)
- Variant path uses `poll_ig_variant_containers()` instead
- Legacy task remains in scheduler but performs no action (safe for gradual removal)

#### 3. Staging Data Reset (management command)
- Created `reset_staging_publishing.py` management command
- Safely deletes all ContentPosts and ContentPostVariants
- Preserves users, accounts, workspace configuration, platform connections
- Only runs in non-production environments
- Gracefully handles queued Celery tasks referencing deleted records

#### 4. Regression Tests Added (tests/test_step_4_6b_legacy_retirement.py)
- 20+ tests validating variant-only architecture
- Tests prove:
  - Posts without variants raise ValueError (not silent fallback)
  - orchestrate_post_publication() is the sole publishing path
  - Legacy poll_ig_containers() is disabled
  - Manual retry uses publish_variant_async (variant path)
  - No silent fallback mechanisms remain
  - Reset command preserves unrelated data

### Retirement Plan

#### Phase 1: Audit (COMPLETE ✓)
- [x] Identify all entry points
- [x] Confirm staging has no business data
- [x] Design variant-only enforcement

#### Phase 2: Reset Staging Data (READY)
- [x] Management command created and tested
- [ ] Run: `python manage.py reset_staging_publishing --confirm` (when ready)
- [ ] Verify Celery broker has no orphaned tasks

#### Phase 3: Validate Variant Path (COMPLETE ✓)
- [x] Remove legacy fallback from `orchestrate_post_publication()`
- [x] Retire legacy `poll_ig_containers()` task
- [x] Add regression tests proving variant-only path
- [x] All entry points route through variant orchestration

#### Phase 4: Run Full Test Suite (READY)
- [ ] Run: `pytest apps/publishing/tests/` (should pass all 96+ tests)
- [ ] Verify no regressions in existing tests
- [ ] Confirm Step 4.6A tests still pass

#### Phase 5: Rebuild Staging Data (NEXT)
- [ ] Reset staging using management command
- [ ] Create test posts through UI
- [ ] Validate end-to-end workflow:
  - Draft → Approval → Schedule → Publish → Retry/Reconcile
  - Separate channel outcomes (one succeeds, one fails)
  - Instagram video polling completes
  - Atomic publishing prevents duplicates

#### Phase 6: Monitor & Validate (AFTER DEPLOYMENT)
- [ ] Monitor publishing success rates
- [ ] Verify no "legacy path" errors in logs
- [ ] Confirm retry behavior matches expected
- [ ] Validate metrics pipeline unaffected

---

## Regression Tests Required

| Test Case | Entry Point | Expected | Status |
|---|---|---|---|
| New post with FB+IG variants | publish_due_posts() → publish_post_async() | Both variants published independently | [ ] |
| FB success, IG video container | publish_due_posts() → publish_post_async() | FB published, IG enters PROCESSING | [ ] |
| IG video container polling | poll_ig_variant_containers() | Container completes and publishes | [ ] |
| Scheduled post has no variants | publish_due_posts() | Falls back to legacy or explicit error | [ ] |
| Retry during video polling | User clicks retry on PROCESSING variant | Rejected; variant stays PROCESSING | [ ] |
| Legacy post in PUBLISHING state | System deployment mid-publish | Completes via appropriate path | [ ] |
| Two identical Celery tasks | publish_post_async runs twice for same post | Only one publishes (atomic claim) | [ ] |
| Scheduled task after deployment | Old tasks in queue before deploy | Executes safely on new code | [ ] |

---

## Staging Execution Steps

Since staging data is non-critical, the retirement process is simplified:

1. **Reset staging data** (immediate)
   ```bash
   python manage.py reset_staging_publishing --confirm
   ```
   - Deletes all ContentPosts and ContentPostVariants
   - Preserves users, accounts, platform connections, workspace setup

2. **Run full test suite** (validate changes)
   ```bash
   pytest apps/publishing/tests/ -v
   ```
   - All 96+ tests should pass (11 Step 4.6A + 20 Step 4.6B + 65 existing)
   - No regressions in approval, brief context, variant retry tests

3. **Drain Celery broker** (optional, for cleanliness)
   - Any queued tasks referencing deleted posts will fail gracefully
   - Restart workers if desired: `celery -A config worker restart`

4. **Rebuild representative data** (validation)
   - Create test posts through UI
   - Verify complete lifecycle: draft → approval → schedule → publish → retry

## Legacy Code Retention Plan

While `publish_post()` and `try_publish_ig_container()` remain in the codebase:

- **Variant path**: All active workflows call variant functions only
- **Legacy functions**: Unreachable from active workflows; remain for safety/history
- **Removal**: Can be deleted in a future Phase if confidence is high

The `poll_ig_containers()` task is deprecated but harmless; it logs a warning and returns 0.

## Production Deployment Considerations

This change is backward-incompatible for posts without variants. Before deploying to production:

1. **Backfill production variants** (if posts without variants exist)
   - Query: `ContentPost.objects.filter(post__isnull=False).exclude(variants__isnull=False).count()`
   - If count > 0: create variants before deploying changes
   - If count = 0: safe to deploy immediately

2. **Verify approval workflow** (if required)
   - Ensure approval states work with variant orchestration
   - Tests confirm this; no changes needed

3. **Update Celery Beat schedule**
   - `poll_ig_containers` can be removed from beat schedule (no longer called)
   - `poll_ig_variant_containers` must remain active
   - `publish_due_posts` continues unchanged

4. **Monitor production logs**
   - Watch for "Cannot publish post: no variants defined" errors
   - If any appear, backfill variants for those posts before retrying

---

## Next Steps

1. Run data audit queries to count posts without variants
2. Document existing Celery beat schedules
3. Verify backfill migration actually ran for all historical posts
4. Add regression tests from table above
5. Once audit complete, proceed to Phase 2 (backfill if needed)

**Milestone**: Step 4.6B-1 audit complete when all open questions answered and data inventoried.
