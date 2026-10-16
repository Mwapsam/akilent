# Stage 2: Close Core Workflow Gaps — Implementation Checklist

**Timeframe:** Weeks 3–6  
**Objective:** Implement Calendar, Plan/Brief, Composer, and Publish Result screens with real persistence, approval workflows, and per-channel management.

**Reference:**
- Stage 0 Audit: `/c/Users/hp/Desktop/project/automator/docs/design/content-studio-design-system.md`
- Design System: `/c/Users/hp/Desktop/project/automator/docs/design/content-studio-design-system.md`
- Wireframes: `/c/Users/hp/Desktop/project/automator/docs/design/content-studio-wireframes.md`
- Implementation Roadmap: `/c/Users/hp/.claude/plans/how-does-this-fit-graceful-fern.md`

---

## Pre-Implementation Verification

Before starting code:

- [ ] Read Stage 0 audit findings and understand gaps
- [ ] Review existing models in `apps/publishing/models.py`
- [ ] Review existing publishing service in `apps/publishing/services.py`
- [ ] Check existing tests in `apps/publishing/tests.py`
- [ ] Understand current state machine transitions
- [ ] Verify `plan_item_create_post()` flow in `apps/publishing/services.py`
- [ ] Review existing templates in `templates/publishing/`
- [ ] Understand Alpine.js + HTMX integration pattern

---

## 1. Extend Models for Brief Context & Per-Channel Variants

**File:** `apps/publishing/models.py`

### 1.1 Extend ContentPost to store brief context

```python
class ContentPost(models.Model):
    # Existing fields
    campaign = ForeignKey(ContentCampaign, ...)
    plan_item = ForeignKey(ContentPlanItem, ...)

    # NEW: Brief context (copied from plan item at creation time)
    brief_objective = TextField(
        blank=True, help_text="Brief objective/goal from plan item"
    )
    brief_audience = TextField(blank=True, help_text="Target audience from brief")
    brief_problem = TextField(blank=True, help_text="Problem statement from brief")
    brief_offer = TextField(blank=True, help_text="What we're offering from brief")
    brief_message = TextField(blank=True, help_text="Core message/hook from brief")
    brief_cta = TextField(blank=True, help_text="Call-to-action from brief")

    # NEW: Per-channel variants (instead of single scheduled_at)
    # Migrate: scheduled_at will become deprecated
```

### 1.2 Create ContentPostVariant model for per-channel deliverables

```python
class ContentPostVariant(models.Model):
    """
    Per-platform version of a post.
    One ContentPost → multiple ContentPostVariants (one per channel).
    """

    post = ForeignKey(ContentPost, on_delete=models.CASCADE, related_name="variants")
    platform = CharField(
        max_length=20,
        choices=[
            ("facebook", "Facebook"),
            ("instagram", "Instagram"),
            ("email", "Email"),
        ],
    )

    # Caption can differ per platform
    caption = TextField(blank=True, help_text="Platform-specific caption")

    # Media can differ per platform
    media_urls = JSONField(
        default=list, blank=True, help_text="List of media URLs for this platform"
    )

    # Format: feed post, story, reel, carousel, email, etc.
    format_choice = CharField(
        max_length=30,
        choices=[
            ("feed_post", "Feed Post"),
            ("reel", "Reel"),
            ("story", "Story"),
            ("carousel", "Carousel"),
            ("email_campaign", "Email Campaign"),
        ],
        default="feed_post",
    )

    # Scheduling per platform
    scheduled_at = DateTimeField(
        null=True, blank=True, help_text="Scheduled time for this platform"
    )

    # Publication outcome (per platform)
    published_at = DateTimeField(null=True, blank=True)
    publication_id = CharField(
        max_length=255,
        blank=True,
        help_text="Platform post ID (fb_post_id, ig_media_id, etc.)",
    )

    # Error tracking per platform
    last_error = TextField(
        blank=True,
        help_text="Last error message (validation, permission, rate limit, etc.)",
    )
    last_error_type = CharField(
        max_length=50,
        choices=[
            ("validation", "Validation"),
            ("permission", "Permission/Auth"),
            ("rate_limit", "Rate Limit"),
            ("provider_outage", "Provider Outage"),
            ("timeout", "Timeout"),
            ("unknown", "Unknown"),
        ],
        blank=True,
    )
    last_error_at = DateTimeField(null=True, blank=True)

    # Retry tracking
    attempt_count = IntegerField(default=0)
    last_attempt_at = DateTimeField(null=True, blank=True)

    # Platform-specific metadata
    metadata = JSONField(
        default=dict,
        blank=True,
        help_text="Platform-specific config (format rules, aspect ratio checks, etc.)",
    )

    created_at = DateTimeField(auto_now_add=True)
    updated_at = DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("post", "platform")
        ordering = ["platform"]

    def __str__(self):
        return f"{self.post.title} ({self.get_platform_display()})"
```

### 1.3 Add approval workflow fields to ContentPost

```python
class ContentPost(models.Model):
    # Existing fields
    ...

    # NEW: Team review & approval
    owner = ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="owned_posts"
    )
    reviewer = ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_posts",
        help_text="User assigned to review this post",
    )

    # Approval flow states
    APPROVAL_STATUS_CHOICES = [
        ("draft", "Draft"),
        ("in_review", "In Review"),
        ("changes_requested", "Changes Requested"),
        ("approved", "Approved"),
    ]
    approval_status = CharField(
        max_length=20,
        choices=APPROVAL_STATUS_CHOICES,
        default="draft",
        help_text="Approval state (separate from publication status)",
    )

    # Feedback from reviewer
    reviewer_feedback = TextField(
        blank=True, help_text="Comments from reviewer on changes requested"
    )
    reviewed_at = DateTimeField(
        null=True, blank=True, help_text="When reviewer last acted on this post"
    )

    # Flags
    needs_creator_attention = BooleanField(
        default=False, help_text="Set when reviewer requests changes"
    )
```

### 1.4 Create ApprovalLog model for audit trail

```python
class ApprovalLog(models.Model):
    """
    Approval history for a post (audit trail).
    """

    post = ForeignKey(
        ContentPost, on_delete=models.CASCADE, related_name="approval_logs"
    )
    actor = ForeignKey(User, on_delete=models.SET_NULL, null=True)
    action = CharField(
        max_length=50,
        choices=[
            ("submitted_review", "Submitted for Review"),
            ("approved", "Approved"),
            ("changes_requested", "Changes Requested"),
            ("scheduled", "Scheduled"),
            ("published", "Published"),
            ("failed", "Failed"),
        ],
    )
    feedback = TextField(blank=True)
    created_at = DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.post.title} - {self.get_action_display()} by {self.actor}"
```

### 1.5 Migrate existing posts and add default values

- [ ] Write migration to add new fields to ContentPost
- [ ] Backfill brief context from linked plan items
- [ ] Create ContentPostVariant for each existing post (one per currently-published platform)
- [ ] Backfill variant.published_at, variant.publication_id from post.fb_post_id / ig_media_id
- [ ] Backfill variant.scheduled_at from post.scheduled_at
- [ ] Set approval_status = "published" for existing published posts

**Test:**
- [ ] `ContentPost.variants.all()` returns variants
- [ ] `variant.platform` distinguishes channels
- [ ] `variant.publication_id` preserved from old post data
- [ ] Brief context displays in admin

---

## 2. Service Layer: Brief Inheritance & Variant Creation

**File:** `apps/publishing/services.py`

### 2.1 Update `plan_item_create_post()` to inherit brief

```python
def plan_item_create_post(plan_item):
    """
    Create a post from a plan item, inheriting brief context.
    """
    post = ContentPost.objects.create(
        title=plan_item.title,
        account=plan_item.account,
        campaign=plan_item.campaign,
        plan_item=plan_item,
        owner=plan_item.owner,  # creator is typically the plan owner
        # Inherit brief context from plan item
        brief_objective=plan_item.objective or "",
        brief_audience=plan_item.target_audience or "",
        brief_problem=plan_item.problem_statement or "",
        brief_offer=plan_item.offer or "",
        brief_message=plan_item.message_hook or "",
        brief_cta=plan_item.call_to_action or "",
        # Approval starts at draft
        approval_status="draft",
    )

    # Create variants for each requested platform
    for platform in plan_item.platforms or ["facebook", "instagram"]:
        ContentPostVariant.objects.create(
            post=post,
            platform=platform,
            format_choice=plan_item.desired_formats.get(platform, "feed_post"),
        )

    return post
```

### 2.2 Create `publish_post_by_variant()` to handle per-channel publishing

```python
def publish_post_by_variant(variant, force=False):
    """
    Publish a single platform variant.
    Handles platform-specific provider calls and error classification.
    """
    post = variant.post
    variant.attempt_count += 1
    variant.last_attempt_at = now()

    try:
        if variant.platform == "facebook":
            result = _publish_facebook_variant(variant)
        elif variant.platform == "instagram":
            result = _publish_instagram_variant(variant)
        elif variant.platform == "email":
            result = _publish_email_variant(variant)
        else:
            raise ValueError(f"Unknown platform: {variant.platform}")

        # Success
        variant.published_at = now()
        variant.publication_id = result["publication_id"]
        variant.last_error = ""
        variant.last_error_type = ""
        variant.save()

        # Log approval
        ApprovalLog.objects.create(
            post=post,
            actor=post.owner,
            action="published",
        )

        # Schedule metrics collection
        schedule_metrics_collection(variant)

        return {"success": True, "platform": variant.platform}

    except ValidationError as e:
        variant.last_error = str(e)
        variant.last_error_type = "validation"
        variant.last_error_at = now()
        variant.save()
        return {"success": False, "error_type": "validation", "message": str(e)}

    except PermissionError as e:
        variant.last_error = str(e)
        variant.last_error_type = "permission"
        variant.last_error_at = now()
        variant.save()
        return {"success": False, "error_type": "permission", "message": str(e)}

    except RateLimitError as e:
        variant.last_error = str(e)
        variant.last_error_type = "rate_limit"
        variant.last_error_at = now()
        variant.save()

        # Schedule retry
        if variant.attempt_count < 3:
            schedule_retry(variant, delay_seconds=60 * (2**variant.attempt_count))

        return {"success": False, "error_type": "rate_limit", "message": str(e)}

    except ProviderOutageError as e:
        variant.last_error = str(e)
        variant.last_error_type = "provider_outage"
        variant.last_error_at = now()
        variant.save()
        return {"success": False, "error_type": "provider_outage", "message": str(e)}

    except Exception as e:
        variant.last_error = str(e)
        variant.last_error_type = "unknown"
        variant.last_error_at = now()
        variant.save()
        return {"success": False, "error_type": "unknown", "message": str(e)}
```

### 2.3 Update `publish_post()` to orchestrate variants

```python
def publish_post(post):
    """
    Publish all variants of a post.
    Handles partial success (one platform succeeds, another fails).
    """
    variants = post.variants.filter(
        status="approved"
    )  # or "scheduled" if past scheduled_at
    results = []

    for variant in variants:
        result = publish_post_by_variant(variant)
        results.append(result)

    # Update post status
    post.status = "publishing"
    post.save()

    # Check outcomes
    all_success = all(r["success"] for r in results)
    any_failed = any(not r["success"] for r in results)

    if all_success:
        post.status = "published"
        post.published_at = now()
    elif any_failed and any(r["success"] for r in results):
        # Partial success
        post.status = "published"  # Some platforms succeeded
        post.partial_failure = True
    else:
        # All failed
        post.status = "failed"
        post.partial_failure = False

    post.save()

    return {
        "post_id": post.id,
        "all_success": all_success,
        "partial": any_failed and any(r["success"] for r in results),
        "variant_results": results,
    }
```

**Test:**
- [ ] `plan_item_create_post()` copies brief context to new post
- [ ] `publish_post_by_variant()` handles each error type correctly
- [ ] Partial success (one platform fails) doesn't block other platforms
- [ ] Retry logic uses exponential backoff
- [ ] `ApprovalLog` records each action

---

## 3. Views Layer: Approval Workflow & Composer Updates

**File:** `apps/publishing/views.py`

### 3.1 Create approval endpoints

- [ ] `post_submit_for_review()` — move post from draft → in-review, assign reviewer
- [ ] `post_request_changes()` — reviewer sends feedback, move post → changes-requested
- [ ] `post_approve()` — reviewer approves, move post → approved
- [ ] `post_approval_history()` — show timeline of approvals

### 3.2 Update `post_new()` (composer) to show brief context

- [ ] Display brief panel (sidebar on desktop, collapse on mobile)
- [ ] Show brief fields: objective, audience, offer, message, CTA
- [ ] Link back to full plan item
- [ ] Show channel selection at top (checkboxes or tabs)

### 3.3 Update `post_detail()` to show per-channel outcomes

- [ ] Display publishing results per channel (one status per platform)
- [ ] Show publication IDs as clickable links (View on Facebook, View on Instagram)
- [ ] Show per-channel errors with classification (validation, permission, rate-limit, provider outage)
- [ ] Add [Fix and retry] button for failed variants

**Test:**
- [ ] Approval workflow transitions work (draft → review → approved → scheduled)
- [ ] Reviewer feedback is displayed
- [ ] Partial success shows which platform failed
- [ ] Error classification is accurate
- [ ] Brief context is visible in composer

---

## 4. Frontend: Template & Component Updates

**Files:** `templates/publishing/*.html`, `templates/components/`

### 4.1 Create new template components

- [ ] `_status_indicator.html` — status dot + label (draft, in-review, changes-requested, approved, scheduled, published, failed)
- [ ] `_channel_status.html` — per-platform publication result (📘 Published, 📸 Failed, etc.)
- [ ] `_brief_context_panel.html` — brief sidebar/accordion in composer
- [ ] `_metric_card.html` — metric with state (available, pending, unsupported, stale, failed)
- [ ] `_approval_feedback.html` — reviewer feedback panel
- [ ] `_approval_history.html` — timeline of approvals
- [ ] `_validation_error.html` — error classification with recovery action

### 4.2 Update `post_new.html` (composer)

- [ ] Add brief context panel (sticky on desktop, collapsible on mobile)
- [ ] Add channel selector at top (checkboxes: 📘 Facebook, 📸 Instagram, 📧 Email)
- [ ] Add channel tabs to switch between variants [📘] [📸] [📧]
- [ ] Each tab shows caption, media, links for that platform
- [ ] Show validation errors inline beside fields
- [ ] Show platform-specific warnings (e.g., "Reel max 90 min", "Aspect ratio 0.8:1 to 1.91:1")
- [ ] AI caption suggestions with [Use] [Regen] buttons
- [ ] Bottom buttons: [Save draft] [Request review] [Schedule] [Publish now]
- [ ] Unsaved changes indicator (with auto-save feedback)

### 4.3 Update `calendar.html`

- [ ] Add status legend (draft, in-review, approved, scheduled, published, failed)
- [ ] Color-code calendar items by status (left border color)
- [ ] Show channel icons (📘 📸 📧) on cards
- [ ] Click card to open detail view
- [ ] Drag-to-reschedule (if date/time picker interactive)

### 4.4 Update `post_detail.html`

- [ ] Show approval status with feedback if applicable
- [ ] Per-channel publishing results (one section per platform)
  - [ ] Status indicator (◆ Published, ◆ Failed, ◆ Scheduled)
  - [ ] Publication ID and link to platform
  - [ ] Error message with classification and recovery action [Fix and retry]
- [ ] Performance metrics (separate section)
  - [ ] Metric cards showing state (available, pending, unsupported, stale, failed)
  - [ ] [Refresh all] button to manually trigger metric collection
- [ ] Approval history timeline (who approved, when, feedback)

### 4.5 Add CSS for new status colors

- [ ] Add `.status-dot`, `.status-indicator` classes to `/assets/app.css`
- [ ] Define color pairs for each status (dot color + label color)
- [ ] Add `.channel-status`, `.metric-card` component classes
- [ ] Test dark mode contrast

**Test:**
- [ ] Brief context visible in composer on desktop
- [ ] Channel tabs work (switch between platforms without losing work)
- [ ] Validation errors appear beside fields
- [ ] Approval feedback displays when post is in-review
- [ ] Status colors match design spec

---

## 5. Task Updates: Publishing & Metrics

**File:** `apps/publishing/tasks.py`

### 5.1 Update `publish_due_posts()`

- [ ] Query `ContentPostVariant` where `scheduled_at <= now()` and status pending
- [ ] Call `publish_post_by_variant()` for each variant (not full post)
- [ ] Handle partial success gracefully

### 5.2 Add `schedule_metrics_collection()` task

- [ ] After successful publish, schedule metric collection task
- [ ] For Facebook: delay 15 min after publish
- [ ] For Instagram: delay 2 hours (insights take longer)
- [ ] Use ExponentialBackoff for retries

### 5.3 Update `fetch_fb_post_insights()`

- [ ] Move to generic `collect_metrics_for_variant()`
- [ ] Accept ContentPostVariant instead of ContentPost
- [ ] Store metrics in new `ContentPerformanceSnapshot` model (defer to Stage 3)

**Test:**
- [ ] Metrics collection scheduled correctly per platform
- [ ] Retry logic works (exponential backoff, max 3 retries)

---

## 6. Permissions & Validation

**File:** `apps/publishing/views.py` (decorator/middleware)

- [ ] Only post owner can edit draft
- [ ] Only assigned reviewer can approve
- [ ] Creator can request changes from reviewer feedback
- [ ] Scheduled posts cannot be edited (must reopen or cancel)
- [ ] Failed variants can be retried only by owner or reviewer

**Test:**
- [ ] Non-owner cannot edit post
- [ ] Non-assigned reviewer cannot approve
- [ ] Approval history reflects actual permissions

---

## 7. Tests: Unit & Integration

**File:** `apps/publishing/tests.py`

### 7.1 Model tests

- [ ] `ContentPostVariant` creation and platform uniqueness
- [ ] Brief context inheritance from plan item
- [ ] Approval status transitions
- [ ] `ApprovalLog` record creation

### 7.2 Service tests

- [ ] `plan_item_create_post()` with brief and variants
- [ ] `publish_post_by_variant()` with each error type
- [ ] Partial success (one platform fails)
- [ ] Retry logic (exponential backoff, max attempts)
- [ ] Error classification (validation vs. permission vs. rate-limit)

### 7.3 View tests

- [ ] Approval workflow (draft → review → approved → scheduled)
- [ ] Reviewer feedback displayed
- [ ] Brief context visible in composer (GET /publishing/new/)
- [ ] Post detail shows per-channel outcomes
- [ ] Permissions enforced (non-owner cannot edit)

### 7.4 End-to-end task tests

- [ ] Create post → submit for review → approve → schedule → publish (happy path)
- [ ] Create post → submit → reviewer requests changes → creator edits → resubmit → approve → schedule → publish (with feedback)
- [ ] Create post for 2 channels → publish → one succeeds, one fails (partial success)
- [ ] Retry failed variant → succeeds

**Acceptance criteria:**
- [ ] All new tests pass
- [ ] Existing tests remain passing
- [ ] Coverage > 80% for modified files

---

## 8. Database Migrations

```bash
# Create migrations (will be generated by Django)
python manage.py makemigrations publishing

# Review migrations for safety
# - No destructive changes without data preservation
# - Data backfill for existing posts/variants

# Test migrations locally
python manage.py migrate

# Verify data integrity post-migration
```

- [ ] Existing posts have variants
- [ ] Brief context filled from plan items
- [ ] Publication IDs preserved
- [ ] Approval status defaults to "published" for historical posts

---

## 9. Validation & Acceptance Criteria

### Task Success (from wireframes)

- [ ] User can create plan item (brief) without repeating information
- [ ] User can create post from brief and see all brief context while composing
- [ ] User can produce two channel variants (FB & IG) with different captions/media
- [ ] User can submit post for review and see reviewer feedback
- [ ] User can approve post and move to schedule
- [ ] User can schedule post and see per-channel publication results
- [ ] User can identify which channel failed and recover (fix + retry)

### Context Continuity

- [ ] Brief objective, audience, offer, CTA visible in composer
- [ ] Channel selections survive navigation (back/forward)
- [ ] Unsaved changes protected (warn on exit)
- [ ] Draft auto-saves every 30 seconds

### State Completeness

- [ ] Draft: gray status, [Save] [Request review] buttons
- [ ] In-review: blue status, [Approve] [Request changes] buttons (if reviewer)
- [ ] Changes-requested: amber status, creator sees feedback
- [ ] Approved: teal status, [Schedule] button enabled
- [ ] Scheduled: blue status, countdown to publication
- [ ] Publishing: amber status, animated pulse
- [ ] Published: teal status, per-channel outcomes visible
- [ ] Failed: coral status, error message + recovery action [Fix and retry]

### Recovery

- [ ] Clear error messages (validation, permission, rate-limit, outage)
- [ ] [Fix and retry] button for validation errors
- [ ] [Fix and retry] button for rate-limit (with retry count)
- [ ] Partial success accurate (one platform published, one failed)
- [ ] No manual DB edits needed for normal operations

### Consistency

- [ ] Status colors match across calendar, composer, detail
- [ ] Approval workflow flows same everywhere
- [ ] Channel icons (📘 📸 📧) consistent
- [ ] Error classification (validation, permission, rate-limit) consistent

### Responsive/Accessibility

- [ ] Composer works on mobile (single column, tabs for channels)
- [ ] Calendar works on mobile (day view, tap to detail)
- [ ] All buttons have focus states
- [ ] Labels for form fields
- [ ] Color contrast meets WCAG AA
- [ ] No color-only indicators (status dot + label)

### Real Integration

- [ ] Test against Facebook test page/token
- [ ] Test against Instagram test account
- [ ] Partial success tested (publish to FB, fail on IG)
- [ ] Error handling tested (revoke token, check permission error)

### Regression

- [ ] Existing publishing still works
- [ ] Existing metrics collection still works
- [ ] Existing calendar view still works
- [ ] No breaking changes to ContentPost API

---

## 10. Sign-off Checklist

Before moving to Stage 3:

- [ ] All Stage 2 code complete and tested
- [ ] Migrations run successfully in test/staging
- [ ] Data backfill verified (variants created, brief context filled)
- [ ] Task-based usability test with representative user
- [ ] All acceptance criteria met
- [ ] No regressions in existing workflows
- [ ] Code review passed
- [ ] Documentation updated
- [ ] Commit message references this checklist

---

## Known Constraints & Decisions

1. **Brief context is read-only in composer** — Users see brief but cannot edit it there; changes go back to plan item
2. **Variants are created at post creation time** — Cannot add/remove platforms after post is created (can archive if needed later)
3. **Approval is optional** — Solo creators have no reviewer; workflow skips approval steps
4. **Scheduled-at is per-variant** — Different platforms can publish at different times
5. **Partial success is the default** — Assume one platform may fail; UI handles this gracefully

---

## Stage 2 Completion

**Definition:** Stage 2 is complete when:

1. All five screens (Calendar, Plan/Brief, Composer, Post Detail, Overview) are implemented with real persistence
2. Brief context inheritance works end-to-end
3. Per-channel variants are created, scheduled, and published separately
4. Approval workflow (draft → review → approved → scheduled) is functional
5. Error recovery paths are clear (error message + recovery action)
6. Partial success is handled gracefully (one platform succeeds, one fails)
7. Task-based usability tests pass
8. All automated tests pass
9. Zero regressions in existing publishing workflows

**Next:** Move to Stage 3 — Make performance trustworthy (ContentPerformanceSnapshot, metric capability registry, collectors, performance UI).
