# Stage 2: Detailed Implementation Plan

**Start Date:** 2026-10-15  
**Scope:** Data model → orchestration → UI, with compatibility checks between each step.

---

## Three Safeguards (enforced at every step)

### 1. Migration Safety
- Existing `ContentPost` records must remain queryable and retain current publication state
- Before any migration, document how existing data maps to the new schema:
  - Existing `ig_media_id` + `ig_container_id` → ContentPostVariant for "instagram"
  - Existing `fb_post_id` → ContentPostVariant for "facebook"
  - Existing `platform_captions` → per-variant caption
  - Existing `published_at` → variant.published_at
- **Check:** After migration, run `python manage.py migrate` and verify:
  - All existing posts have variants created
  - Publication IDs are preserved
  - No posts have been republished

### 2. Duplicate-Publication Safety
- A timeout on retry does not prove the provider failed
- Before retrying a variant, check if the publication ID already exists on the provider (idempotency check)
- If a variant succeeded, set `attempt_count` and `last_attempt_at` but do NOT republish on parent retry
- **Check:** Unit test: retry a variant that already has a publication_id; verify no provider call is made

### 3. Backward Compatibility
- Existing calendar, post-detail, library views must work unchanged
- Manual `/publishing/posts/<id>/retry/` route must work until variants are fully live
- Existing tasks (`publish_due_posts`, `fetch_fb_post_insights`) must work alongside new variant-based tasks
- **Check:** After each step, run existing post creation → schedule → publish flow; verify no breakage

---

## Step 1: Brief Context and Inheritance (Days 1–2)

### Objective
Add brief fields to `ContentPost`, inherit from plan item, display in composer. Verify existing post creation still works.

### Changes

#### 1a. Model: Add brief context fields to ContentPost
**File:** `apps/publishing/models.py`

Add to `ContentPost` class:
```python
# Brief context — inherited from plan item
brief_objective = models.TextField(blank=True, default="")
brief_audience = models.TextField(blank=True, default="")
brief_problem = models.TextField(blank=True, default="")
brief_offer = models.TextField(blank=True, default="")
brief_message = models.TextField(blank=True, default="")
brief_cta = models.TextField(blank=True, default="")
```

**Rationale:** These six fields cover the brief context shown in wireframes. All blank=True to not break existing posts.

#### 1b. Migration
**Command:** `python manage.py makemigrations publishing`  
**File created:** `apps/publishing/migrations/000X_brief_context.py`

Verify migration:
- Adds six new TEXT columns to content_post table
- All nullable/blank=True (no backfill needed)
- No existing posts are affected

#### 1c. Service: Update plan_item_create_post() to copy brief
**File:** `apps/publishing/views.py`, function `plan_item_create_post()`

Before:
```python
post = ContentPost.objects.create(
    account=account,
    plan_item=item,
    platforms=item.platforms,
    media_type=item.media_type,
    status=ContentPost.Status.DRAFT,
    created_by=request.user,
)
```

After:
```python
post = ContentPost.objects.create(
    account=account,
    plan_item=item,
    platforms=item.platforms,
    media_type=item.media_type,
    status=ContentPost.Status.DRAFT,
    created_by=request.user,
    brief_objective=item.objective,
    brief_audience=item.audience if hasattr(item, "audience") else "",
    brief_problem="",  # Will be captured in UI if plan_item is extended
    brief_offer="",
    brief_message="",
    brief_cta="",
)
```

**Note:** ContentPlanItem currently only has `objective`. If audience/offer/CTA should come from plan item, extend ContentPlanItem in a future step. For now, brief_objective is the key inheritance.

#### 1d. View: Display brief in composer
**File:** `templates/publishing/post_new.html`

Add a brief context panel (collapsible on mobile, sticky on desktop) that displays:
- brief_objective
- brief_audience
- brief_problem
- brief_offer
- brief_message
- brief_cta

If `existing_post.brief_objective` is set, show them; otherwise show placeholder "Add objective from plan item".

#### 1e. View: Allow editing brief in composer
**File:** `apps/publishing/views.py`, function `post_new()` (POST handler)

Add to POST parsing:
```python
brief_objective = request.POST.get("brief_objective", "").strip()
brief_audience = request.POST.get("brief_audience", "").strip()
brief_problem = request.POST.get("brief_problem", "").strip()
brief_offer = request.POST.get("brief_offer", "").strip()
brief_message = request.POST.get("brief_message", "").strip()
brief_cta = request.POST.get("brief_cta", "").strip()
```

Update post before save:
```python
post.brief_objective = brief_objective
post.brief_audience = brief_audience
post.brief_problem = brief_problem
post.brief_offer = brief_offer
post.brief_message = brief_message
post.brief_cta = brief_cta
```

#### 1f. Tests

**New file:** `apps/publishing/tests/test_brief_context.py`

```python
class TestBriefContextInheritance(TestCase):
    def test_plan_item_create_post_copies_objective(self):
        """Brief objective is copied from plan item to post."""
        
    def test_plan_item_without_objective_creates_post_with_empty_brief(self):
        """Plan items without objective create posts with blank brief."""
        
    def test_composer_preserves_brief_on_save(self):
        """Saving a post with brief_objective updates the field."""
        
    def test_brief_context_visible_in_post_detail(self):
        """Post detail shows brief context fields."""
```

**Regression tests:**
```python
class TestLegacyPostCreation(TestCase):
    def test_post_new_without_plan_item_still_works(self):
        """Creating post directly (not from plan item) still works."""
        
    def test_existing_posts_without_brief_can_be_edited(self):
        """Editing an existing post without brief doesn't error."""
        
    def test_calendar_still_lists_posts_after_brief_fields_added(self):
        """Calendar view works with posts that have no brief."""
```

### Compatibility Checks

- ✓ Existing posts remain accessible (no migrations breaking them)
- ✓ plan_item_create_post() still works (tested)
- ✓ post_new() still works with or without plan_item (tested)
- ✓ Calendar and library views unchanged
- ✓ Publishing flow unchanged (no new required fields)

**Verification:** After Step 1, run full publishing workflow (create → edit → schedule → publish) without a plan item. Verify brief fields are optional and don't break the flow.

---

## Step 2: ContentPostVariant Model (Days 3–5)

### Objective
Create one variant per platform with independent schedule, status, publication ID, error tracking. Backfill existing posts. Keep ContentPost as workflow-level coordinator.

### Changes

#### 2a. Model: Create ContentPostVariant
**File:** `apps/publishing/models.py`

```python
class ContentPostVariant(models.Model):
    class Platform(models.TextChoices):
        FACEBOOK = "facebook", "Facebook"
        INSTAGRAM = "instagram", "Instagram"
        EMAIL = "email", "Email"

    class ErrorType(models.TextChoices):
        VALIDATION = "validation", "Validation"
        PERMISSION = "permission", "Permission/Auth"
        RATE_LIMIT = "rate_limit", "Rate Limit"
        PROVIDER_OUTAGE = "provider_outage", "Provider Outage"
        TIMEOUT = "timeout", "Timeout"
        UNKNOWN = "unknown", "Unknown"

    post = models.ForeignKey(
        ContentPost, on_delete=models.CASCADE, related_name="variants"
    )
    platform = models.CharField(max_length=20, choices=Platform.choices)

    # Content (independent per platform)
    caption = models.TextField(blank=True, default="")
    media_urls = models.JSONField(default=list, blank=True)  # Override post.media_urls
    format_choice = models.CharField(
        max_length=30,
        blank=True,
        default="",
        choices=[
            ("feed_post", "Feed Post"),
            ("reel", "Reel"),
            ("story", "Story"),
            ("carousel", "Carousel"),
        ],
    )

    # Scheduling and publication
    scheduled_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    publication_id = models.CharField(max_length=255, blank=True, default="")

    # Error handling
    last_error = models.TextField(blank=True, default="")
    last_error_type = models.CharField(
        max_length=50, choices=ErrorType.choices, blank=True, default=""
    )
    last_error_at = models.DateTimeField(null=True, blank=True)

    # Retry tracking
    attempt_count = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)

    # Metadata for provider-specific state
    metadata = models.JSONField(default=dict, blank=True)  # ig_container_id, etc.

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("post", "platform")
        ordering = ["platform", "-created_at"]

    def __str__(self) -> str:
        return f"{self.post.public_id} / {self.platform}"
```

#### 2b. Migration: Create variant table
**File:** `apps/publishing/migrations/000X_create_content_post_variant.py`

- Create table with all fields from 2a
- Add unique_together constraint

#### 2c. Migration: Backfill existing posts
**File:** `apps/publishing/migrations/000X_backfill_variants.py`

```python
def backfill_variants(apps, schema_editor):
    ContentPost = apps.get_model("publishing", "ContentPost")
    ContentPostVariant = apps.get_model("publishing", "ContentPostVariant")

    for post in ContentPost.objects.all():
        if "instagram" in (post.platforms or []):
            ContentPostVariant.objects.get_or_create(
                post=post,
                platform="instagram",
                defaults={
                    "caption": post.caption_for("instagram"),
                    "media_urls": post.media_urls,
                    "published_at": post.published_at if post.ig_media_id else None,
                    "publication_id": post.ig_media_id,
                    "metadata": {"ig_container_id": post.ig_container_id},
                },
            )
        if "facebook" in (post.platforms or []):
            ContentPostVariant.objects.get_or_create(
                post=post,
                platform="facebook",
                defaults={
                    "caption": post.caption_for("facebook"),
                    "media_urls": post.media_urls,
                    "published_at": post.published_at if post.fb_post_id else None,
                    "publication_id": post.fb_post_id,
                },
            )
```

#### 2d. Tests

**New file:** `apps/publishing/tests/test_variants.py`

```python
class TestVariantModel(TestCase):
    def test_variant_unique_together_post_platform(self):
        """Cannot create two variants for same post+platform."""
        
    def test_backfill_creates_variants_for_published_posts(self):
        """Migration creates variants with publication_ids from existing posts."""
        
    def test_backfill_preserves_facebook_publication_id(self):
        """Facebook variant.publication_id == old post.fb_post_id."""
        
    def test_backfill_preserves_instagram_publication_id(self):
        """Instagram variant.publication_id == old post.ig_media_id."""
        
    def test_variant_caption_falls_back_to_post_caption(self):
        """If variant.caption is empty, post.caption_for(platform) is used."""
```

**Regression tests:**
```python
class TestVariantBackwardCompatibility(TestCase):
    def test_existing_posts_without_variants_can_be_edited(self):
        """Posts created before variant backfill can still be saved."""
        
    def test_post_detail_works_with_and_without_variants(self):
        """Post detail view doesn't error if variants don't exist yet."""
        
    def test_calendar_still_lists_posts_before_variant_migration(self):
        """Calendar query works before variant table exists."""
```

### Compatibility Checks

- ✓ Existing posts remain queryable (no changes to ContentPost table)
- ✓ Backfill is idempotent (get_or_create prevents duplicates)
- ✓ All existing publication IDs are preserved
- ✓ old code reading `post.fb_post_id` still works (field not removed)
- ✓ Variant model is optional (queries that don't join variants still work)

**Verification:** After Step 2:
1. Run `python manage.py migrate`
2. Verify all existing posts have variants created
3. Verify variant.publication_id matches old post identifiers
4. Manually publish a new post (not yet using variants in orchestration); verify post creates variant

---

## Step 3: Approval Workflow (Days 6–8)

### Objective
Add owner/reviewer fields, approval status, transitions with permissions. Tests for valid state changes.

### Changes (planned, not implemented in this message)

#### 3a. Model: Extend ContentPost with approval fields
```python
owner = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, ...)
reviewer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, ...)
approval_status = models.CharField(max_length=25, choices=[
    ("draft", "Draft"),
    ("in_review", "In Review"),
    ("changes_requested", "Changes Requested"),
    ("approved", "Approved"),
], default="draft")
reviewer_feedback = models.TextField(blank=True, default="")
reviewed_at = models.DateTimeField(null=True, blank=True)
```

#### 3b. Model: Create ApprovalLog for audit trail
```python
class ApprovalLog(models.Model):
    post = ForeignKey(ContentPost, ...)
    action = CharField(
        choices=[
            ("submitted_review", "Submitted for Review"),
            ("approved", "Approved"),
            ("changes_requested", "Changes Requested"),
            ("scheduled", "Scheduled"),
            ("published", "Published"),
            ("failed", "Failed"),
        ]
    )
    user = ForeignKey(User, ...)
    timestamp = DateTimeField(auto_now_add=True)
    notes = TextField(blank=True)
```

#### 3c. Views: Approval transitions
- `post_submit_for_review()` — Draft → In-Review, assign reviewer
- `post_request_changes()` — In-Review → Draft, capture feedback
- `post_approve()` — In-Review → Approved

#### 3d. Permissions: Enforce on backend
- Only owner can edit draft
- Only assigned reviewer can approve
- Creator can request changes from reviewer feedback

#### 3e. Templates
- Brief context panel shows approval status
- Composer shows "Request review" button
- Post detail shows reviewer feedback and approval history

### Compatibility Checks
- ✓ Existing posts without approval fields still work (all optional, default="draft")
- ✓ Solo creators can still publish without review
- ✓ Approval is opt-in (not mandatory)

---

## Step 4: Per-Variant Publishing Orchestration (Days 9–12)

### Objective
Refactor `publish_post()` to process each variant independently. Keep existing provider integrations.

### Changes (planned, not implemented in this message)

#### 4a. Service: Create publish_post_by_variant()
```python
def publish_post_by_variant(variant: ContentPostVariant) -> bool:
    """Publish one variant. Returns True on success."""
    # Call appropriate provider based on variant.platform
    # Store publication_id, error, error_type on variant
    # Handle each error type separately
    # Return success/failure
```

#### 4b. Service: Update publish_post() to use variants
```python
def publish_post(post: ContentPost) -> None:
    """Orchestrate variants. Handle partial success."""
    for variant in post.variants.all():
        try:
            publish_post_by_variant(variant)
        except Exception as exc:
            # Error is captured on variant; continue to next
    # Update post.status based on variant outcomes
```

#### 4c. Task: Update publish_due_posts()
```python
def publish_due_posts() -> int:
    """Query variants with scheduled_at <= now."""
    variants = ContentPostVariant.objects.filter(
        scheduled_at__lte=timezone.now(),
        published_at__isnull=True,  # Not yet published
    )
    for variant in variants:
        publish_post_async.delay(variant.pk)
```

#### 4d. Reconciliation: Handle provider timeouts
```python
def publish_post_by_variant(variant: ContentPostVariant) -> bool:
    """Before retrying, check if publication_id already exists."""
    if variant.publication_id:
        # Already published; don't republish
        return True
    # ... normal publishing flow
```

### Compatibility Checks
- ✓ Existing `/publishing/posts/<id>/retry/` route still works
- ✓ Manual retry calls the new orchestration
- ✓ Partial success is recorded per-variant, not on post
- ✓ Old tasks can coexist with new variant-based tasks during transition

---

## Step 5: Per-Channel Recovery and Result UI (Days 13–14)

### Objective
Show independent channel outcomes, allow correction and retry of failed variants.

### Changes (planned, not implemented in this message)

#### 5a. Template: Post detail shows per-variant results
- For each variant: status (published/failed), publication ID, error message
- If failed: [Fix and retry] button
- If partially successful: [Publish Instagram] button for unpublished variant

#### 5b. View: Post detail updated
- Render variants instead of single status
- Show per-platform metrics (Stage 3)

---

## Definition of Done

✅ User can:
1. Create plan item with objective
2. Create post from plan (objective inherited)
3. Edit brief context in composer
4. Select platforms (Facebook, Instagram)
5. Edit independent captions/media per platform (via variants)
6. Submit for review (owner/reviewer assignments)
7. Approve and schedule
8. Publish with independent outcomes per channel
9. See which channel succeeded/failed
10. Retry only failed channels

✅ No regressions:
- Calendar still works
- Library still works
- Post creation without plan item still works
- Existing published posts still show results
- Facebook/Instagram publishing still works

✅ Safeguards in place:
- Existing records survive all migrations
- No accidental republishing of succeeded channels
- Backward compatibility maintained at each step

---

## Timeline

| Step | Days | Owner | Reviewer |
|------|------|-------|----------|
| 1. Brief context | 1–2 | Implementation | Safeguard check |
| 2. Variant model | 3–5 | Implementation | Migration + backfill verification |
| 3. Approval workflow | 6–8 | Implementation + UI | Permission enforcement tests |
| 4. Per-variant orchestration | 9–12 | Implementation | Reconciliation + retry tests |
| 5. Result UI | 13–14 | UI + templates | Integration tests |
| **Total** | **14 days** | | |

