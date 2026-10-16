# Stage 2: Targeted Backend Verification

**Audit Date:** 2026-10-15  
**Scope:** Verify eight locked design decisions against current ContentPost, ContentPlanItem, publishing tasks, and services.

---

## Verification Matrix

| # | Decision | Current State | Classification | Smallest Safe Change | Risk / Note |
|---|----------|---------------|-----------------|----------------------|-------------|
| **1** | **Post vs channel status** — Separate workflow status from per-channel publishing status | ContentPost.status is shared; one ig_media_id + fb_post_id per post (not per-channel). No per-channel status tracking. | **Backend change required** | Create ContentPostVariant model (post FK, platform CharField, status/identifier/error fields). Keep ContentPost.status for workflow. Map old posts on migration. | Significant change; enables independent channel outcomes. Plan 2–3 migrations (create variant model, backfill, drop redundant post fields). |
| **2** | **Channel-specific content** — Store caption, media, format per platform independently | platform_captions (JSON dict) exists ✓. media_urls shared list ⚠️. media_type shared, no per-channel format selection. No per-platform media selection. | **UI-only + minor backend** | Add ContentPostVariant.caption, media_urls, format_choice fields. platform_captions on ContentPost can remain for backward compatibility during backfill. Templates read from variant. | Safe. Variant model solves this. Composer must save per-variant. |
| **3** | **Partial publishing** — If FB succeeds, IG fails: preserve FB result, retry only IG | publish_post() treats errors as all-or-nothing. If IG fails, whole post status = FAILED. No per-channel retry. | **Backend change required** | Add ContentPostVariant.last_error, last_error_type, attempt_count, last_attempt_at. Refactor publish_post_by_variant(). publish_due_posts() queries ContentPostVariant, not ContentPost. | Medium effort. Must rewrite publish_post() orchestration to per-variant. Significant test impact. |
| **4** | **Brief inheritance** — Audience, objective, offer, CTA carried from plan → post | ContentPlanItem has objective only. ContentPost has no brief fields. plan_item_create_post() copies only platforms + media_type. No brief visible in composer. | **Backend change required** | Add ContentPost fields: brief_objective, brief_audience, brief_problem, brief_offer, brief_message, brief_cta (all TextField, blank=True). plan_item_create_post() copies plan_item.objective → post.brief_objective. Composer displays these in context panel. | Small data model change. Requires migration. Composer template changes. Medium test impact. |
| **5** | **Approval workflow** — Review, changes, feedback, solo bypass | Status transitions exist (_TRANSITIONS dict in views.py). No reviewer/owner fields. No feedback/approval_history views. No permissions enforcement on transitions. | **Backend change required + UI** | Add ContentPost fields: owner FK, reviewer FK, approval_status CharField (draft/in-review/changes-requested/approved), reviewer_feedback, reviewed_at. Create ApprovalLog model (post FK, action, user, timestamp, notes). Implement views: post_submit_for_review(), post_request_changes(), post_approve(). Enforce permissions on backend. | Medium data model change. UI-heavy. Tests required for permission enforcement. |
| **6** | **Metrics** — Show availability (available/pending/unsupported/stale/failed), source channel, collection timestamp | fb_reach, fb_impressions, fb_insights_fetched_at exist for FB only ✓. No Instagram metrics collection. No "unsupported" / "stale" / "failed" indicator. All metrics hardcoded to post, not per-variant. | **Backend change required + UI** | Defer detailed metric model to Stage 3 (ContentPerformanceSnapshot, capability registry). For Stage 2: Move FB metrics to ContentPostVariant. Add metric_fetched_at per variant. Implement fetch_metrics_for_variant(). UI shows {available, pending, unsupported, stale, failed} states based on timestamps and error flags. | Deferred architecture. Stage 2 focuses on infrastructure (variant model). Stage 3 builds trustworthy metric collection. |
| **7** | **Scheduling** — Workspace timezone, explicit display, valid transitions | ContentPost.scheduled_at is DateTimeField, no timezone context. post_new() view uses parse_datetime() + timezone.make_aware(). No timezone validation or display in templates. | **UI-only + config** | Verify settings.TIME_ZONE is set correctly. Templates use get_current_timezone() when rendering scheduled_at. Add timezone name to composer and calendar templates. Validate future scheduling in views before saving. No backend change needed if Django timezone handling is correct. | Low risk. Verify existing Django timezone config is correct. Add UI clarification only. |
| **8** | **Channels + capabilities** — Derive formats/actions from actual provider capabilities, not hardcoded | Channels hardcoded to ["instagram", "facebook"] in templates. Media types hardcoded in choices. No capability registry. No "unsupported for this account" states. | **Backend change required (deferred to Stage 3)** | Stage 2: Accept that Facebook + Instagram are pilot channels with assumed capabilities. Stage 3: Build capability registry (config or model) and query before enabling UI actions. Stage 2 can hardcode "facebook" and "instagram" are available if connected. Defer whatsapp/email unless scope includes them. | Deferred. Stage 2 assumes FB/IG are operational once connected. Stage 3 validates metrics / formats per account config. Not blocking Stage 2. |

---

## Summary: Backend Changes Required for Stage 2

### Must-have model changes:
1. **ContentPostVariant** — New model (post FK, platform, caption, media_urls, format_choice, scheduled_at, published_at, publication_id, last_error, last_error_type, attempt_count, last_attempt_at, metadata, timestamps)
2. **ContentPost extensions** — Add brief context fields (brief_objective, brief_audience, brief_problem, brief_offer, brief_message, brief_cta), owner, reviewer, approval_status, reviewer_feedback, reviewed_at
3. **ApprovalLog** — New model for audit trail (post FK, action, user, timestamp, notes)

### Must-have service changes:
1. Refactor `publish_post()` → `publish_post_by_variant()` with per-variant error handling
2. Refactor `publish_due_posts()` to query ContentPostVariant, not ContentPost
3. Update `_publish_instagram()` and `_publish_facebook()` to accept variant and store results per-variant
4. Create `plan_item_create_post()` service to inherit brief context from plan item
5. Create approval transition service (draft → review, review → approved, review → changes)

### Must-have view changes:
1. `post_submit_for_review()` — Draft → In-Review, assign reviewer
2. `post_request_changes()` — In-Review → Draft, capture feedback
3. `post_approve()` — In-Review → Approved, optionally auto-schedule
4. Approval history and reviewer feedback display on post_detail
5. Brief context panel in composer

### Must-have permissions:
1. Only post owner can edit drafts
2. Only assigned reviewer can approve
3. Creator can submit for review or edit when in changes-requested state
4. Scheduled/published posts cannot be edited

### Test coverage gaps:
- **Current:** No existing tests in apps/publishing/tests
- **Required:** Unit tests for model changes, service layer with each error type, view permission enforcement, end-to-end workflow (create → review → approve → schedule → publish)

---

## Recommended Implementation Order (from user feedback)

1. **Foundation & Calendar** — Verify calendar filters, navigation, status indicators work. Link to existing posts/plan items. ✓ Already works; no changes needed.

2. **Brief-to-Composer handoff** — Add brief context fields to ContentPost, update plan_item_create_post() to copy brief, display in composer. (Smallest change, unblocks composer UX)

3. **Per-channel deliverables** — Create ContentPostVariant model, backfill from existing posts, update composer to save variants. (Medium change, enables channel independence)

4. **Review and scheduling** — Add approval fields (owner, reviewer, approval_status), create approval views, enforce permissions. (UI-heavy, high test impact)

5. **Publishing result and recovery** — Refactor publish_post() → per-variant, handle partial success, classify errors. (Complex service change, many tests)

6. **Performance and Overview** — Move FB metrics to variant model, prepare for Stage 3 metric collection. (Deferred to Stage 3 for full implementation)

---

## Stage 3 (Deferred, weeks 7–10): Metric trustworthiness
- ContentPerformanceSnapshot model
- Capability registry (versioned, provider-aware)
- Metric collectors per platform
- Collection timestamp and availability states
- Performance UI with honest data states

---

## Verified Safe Assumptions

✓ Facebook OAuth and token management works (tested in callbacks)  
✓ Instagram token refresh and webhook processing exists (apps/instagram/)  
✓ Media upload and S3 storage works  
✓ Async publishing with retries works (tasks.py)  
✓ Basic calendar and post listing works  
✓ Platform-specific caption overrides already supported  

---

## Blockers / Dependencies

**None blocking Stage 2 implementation:**
- Capability registry is a Stage 3 concern (assume FB/IG connected once OAuth succeeds)
- Email channel is out of scope for pilot
- WhatsApp is conversion-flow only (not Content Studio publishing)
- Contact Intelligence / recommendations deferred to Phase 2

