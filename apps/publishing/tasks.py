"""Celery tasks for content publishing."""

from __future__ import annotations

import logging
from functools import partial

from django.db import transaction
from django.utils import timezone

from celery import shared_task

logger = logging.getLogger(__name__)


DISPATCH_GRACE_MINUTES = 15


@shared_task(queue="outbound")
def publish_due_posts() -> int:
    """Publish every channel whose own scheduled time has passed.

    Each variant is dispatched on its own, so Facebook and Instagram can go out at
    different times. A post scheduled only at post level (no SCHEDULED variant) still
    publishes through the whole-post path.
    """
    from datetime import timedelta

    from django.db.models import Q

    from .models import ContentPost, ContentPostVariant

    now = timezone.now()
    count = 0
    due_variants = ContentPostVariant.objects.select_for_update(
        skip_locked=True, of=("self",)
    ).filter(
        Q(dispatched_at__isnull=True)
        | Q(dispatched_at__lt=now - timedelta(minutes=DISPATCH_GRACE_MINUTES)),
        status=ContentPostVariant.Status.SCHEDULED,
        scheduled_at__lte=now,
        post__status__in=(ContentPost.Status.SCHEDULED, ContentPost.Status.PUBLISHING),
    )
    with transaction.atomic():
        for variant in due_variants:
            variant.dispatched_at = now
            variant.save(update_fields=["dispatched_at", "updated_at"])
            # After commit, robust: a broker failure leaves dispatched_at set, and the
            # variant is picked up again once the grace window passes.
            transaction.on_commit(
                partial(publish_variant_async.delay, variant.pk, scheduled=True),
                robust=True,
            )
            count += 1

    qs = (
        ContentPost.objects.select_for_update(skip_locked=True)
        .filter(status=ContentPost.Status.SCHEDULED, scheduled_at__lte=now)
        .exclude(variants__status=ContentPostVariant.Status.SCHEDULED)
        .exclude(variants__status=ContentPostVariant.Status.PUBLISHED)
    )
    with transaction.atomic():
        for post in qs:
            post.status = ContentPost.Status.PUBLISHING
            post.save(update_fields=["status", "updated_at"])
            # Enqueue after commit so one broker failure cannot roll back the batch; a
            # post whose enqueue fails is picked up by recover_unstarted_publishing_posts.
            transaction.on_commit(
                partial(publish_post_async.delay, post.pk), robust=True
            )
            count += 1
    return count


UNSTARTED_POST_GRACE_MINUTES = 15


@shared_task(queue="outbound")
def recover_unstarted_publishing_posts() -> int:
    """Re-enqueue posts left in PUBLISHING whose publish task never ran.

    A failed or lost enqueue leaves the parent PUBLISHING with no variant ever claimed,
    and nothing else would move it. Re-enqueueing is safe: publish_post_async re-checks
    the status and every variant is claimed atomically before any provider call.
    """
    from datetime import timedelta

    from .models import ContentPost, ContentPostVariant

    now = timezone.now()
    unstarted = (ContentPostVariant.Status.DRAFT, ContentPostVariant.Status.SCHEDULED)
    pks = list(
        ContentPost.objects.filter(
            status=ContentPost.Status.PUBLISHING,
            updated_at__lt=now - timedelta(minutes=UNSTARTED_POST_GRACE_MINUTES),
        )
        .exclude(
            variants__status__in=[
                s for s in ContentPostVariant.Status if s not in unstarted
            ]
        )
        .values_list("pk", flat=True)
    )
    # Bump updated_at so each post is retried at most once per grace window.
    ContentPost.objects.filter(pk__in=pks).update(updated_at=now)
    for pk in pks:
        logger.warning("Re-enqueueing unstarted publishing post=%s", pk)
        try:
            publish_post_async.delay(pk)
        except Exception:
            logger.exception("Re-enqueue failed for unstarted post=%s", pk)
    return len(pks)


@shared_task(bind=True, max_retries=3, queue="outbound")
def publish_post_async(self, post_id: int) -> str:
    from django.db import transaction

    from .models import ContentPost
    from .services import orchestrate_post_publication

    with transaction.atomic():
        try:
            post = ContentPost.objects.select_for_update().get(pk=post_id)
        except ContentPost.DoesNotExist:
            return "not_found"
        if post.status not in (
            ContentPost.Status.PUBLISHING,
            ContentPost.Status.SCHEDULED,
        ):
            return "skipped"
        if not post.is_approval_valid():
            post.status = ContentPost.Status.DRAFT
            post.error = "Approval is missing or no longer matches the content; reapprove before publishing."
            post.save(update_fields=["status", "error"])
            return "approval_required"

    try:
        _, outcomes = orchestrate_post_publication(post)

        # The parent status was persisted by refresh_parent_post; read it, don't rewrite it.
        post.refresh_from_db(fields=["status"])
        if post.status == ContentPost.Status.PUBLISHED:
            return "published"
        if any(o["status"] == ContentPost.Status.PUBLISHED for o in outcomes):
            return "partial_success"
        return post.status
    except Exception as exc:
        logger.exception("publish_post_async failed for post=%s", post_id)
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=60 * (2**self.request.retries), exc=exc) from exc
        from .models import ContentPostVariant
        from .services import refresh_parent_post

        unstarted = (
            ContentPostVariant.Status.DRAFT,
            ContentPostVariant.Status.SCHEDULED,
        )
        if post.variants.exclude(status__in=unstarted).exists():
            # Some channel ran; its variant holds the real outcome, so derive the parent
            # from the variants instead of overwriting a partial success with FAILED.
            refresh_parent_post(post)
            post.refresh_from_db(fields=["status"])
            return post.status
        post.status = ContentPost.Status.FAILED
        post.error = str(exc)[:500]
        post.save(update_fields=["status", "error"])
        return "failed"


@shared_task(bind=True, max_retries=3, queue="outbound")
def publish_variant_async(self, variant_id: int, scheduled: bool = False) -> str:
    """Publish a single ContentPostVariant: a scheduled channel or a per-channel retry.

    ``scheduled`` marks a dispatch from publish_due_posts. It publishes only a variant
    that is still SCHEDULED and due, so a channel cancelled or moved after dispatch is
    left alone. If its approval no longer holds, only that channel goes back to draft;
    another channel of the post may already be live.

    Safeguards:
    - Acquires database lock before validating state (Phase A2)
    - Validates approval at execution time
    - Prevents retry of published/cancelled/reconciling variants
    - Preserves attempt history
    - Handles task enqueue failures gracefully
    """
    from django.db import transaction

    from .models import ContentPostVariant
    from .services import publish_variant

    # Phase A2: All database claims must be wrapped in transaction.atomic()
    # to avoid TransactionManagementError on PostgreSQL.
    with transaction.atomic():
        try:
            variant = ContentPostVariant.objects.select_for_update().get(pk=variant_id)
        except ContentPostVariant.DoesNotExist:
            return "not_found"

        post = variant.post

        # Safeguard: don't retry published, cancelled, reconciling, or processing variants
        if variant.status in (
            ContentPostVariant.Status.PUBLISHED,
            ContentPostVariant.Status.CANCELLED,
            ContentPostVariant.Status.RECONCILING,
            ContentPostVariant.Status.PROCESSING,
        ):
            return f"skipped_{variant.status}"

        if scheduled:
            from .services import refresh_parent_post, variant_is_due

            if variant.status != ContentPostVariant.Status.SCHEDULED:
                return "skipped_not_scheduled"
            if not variant_is_due(variant):
                return "not_due"
            if not post.is_approval_valid():
                variant.status = ContentPostVariant.Status.DRAFT
                variant.last_error = (
                    "Approval is missing or no longer matches the content; "
                    "reapprove, then schedule this channel again."
                )
                variant.last_error_type = ContentPostVariant.ErrorType.VALIDATION
                variant.last_error_at = timezone.now()
                variant.save(
                    update_fields=[
                        "status",
                        "last_error",
                        "last_error_type",
                        "last_error_at",
                    ]
                )
                refresh_parent_post(post)
                return "approval_required"

        # Safeguard: check approval at execution time (may have changed since enqueue)
        if not post.is_approval_valid():
            variant.status = ContentPostVariant.Status.FAILED
            variant.last_error = "Post approval is invalid or not present"
            variant.last_error_type = ContentPostVariant.ErrorType.VALIDATION
            variant.last_error_at = timezone.now()
            variant.save(
                update_fields=[
                    "status",
                    "last_error",
                    "last_error_type",
                    "last_error_at",
                ]
            )
            return "approval_invalid"

    try:
        result = publish_variant(variant)
    except Exception as exc:
        logger.exception("publish_variant_async failed for variant=%s", variant_id)
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=60 * (2**self.request.retries), exc=exc) from exc
        # Max retries exceeded - make sure variant is marked failed
        variant.refresh_from_db()
        if variant.status not in (
            ContentPostVariant.Status.PUBLISHED,
            ContentPostVariant.Status.PROCESSING,
            ContentPostVariant.Status.RECONCILING,
        ):
            variant.status = ContentPostVariant.Status.FAILED
            variant.last_error = str(exc)[:500]
            variant.last_error_type = ContentPostVariant.ErrorType.UNKNOWN
            variant.last_error_at = timezone.now()
            variant.save(
                update_fields=[
                    "status",
                    "last_error",
                    "last_error_type",
                    "last_error_at",
                ]
            )
        return "failed"

    if result["success"]:
        return "published"
    if result.get("held"):
        return "held_channel_off"
    if result.get("status") == ContentPostVariant.Status.SCHEDULED:
        # Deferred (Facebook's daily Reel limit): the scheduler sends it at its new time.
        return "deferred"
    if result.get("status") == ContentPostVariant.Status.PROCESSING:
        return "processing"
    if result.get("reconciling"):
        return "reconciling"
    # Validation and permission failures will not succeed on retry; only transient ones may.
    if result.get("error_type") in (
        ContentPostVariant.ErrorType.VALIDATION,
        ContentPostVariant.ErrorType.PERMISSION,
    ):
        return "failed"
    if self.request.retries < self.max_retries:
        raise self.retry(
            countdown=60 * (2**self.request.retries),
            exc=Exception(result.get("error", "Unknown error")),
        )
    return "failed"


@shared_task(queue="outbound")
def fetch_fb_post_insights(post_id: int) -> str:
    """Kept so tasks queued before the metrics rework still run: reads the post's
    Facebook channel through the per-channel collector."""
    from .models import ContentPostVariant

    variant = ContentPostVariant.objects.filter(
        post_id=post_id,
        platform=ContentPostVariant.Platform.FACEBOOK,
        status=ContentPostVariant.Status.PUBLISHED,
    ).first()
    if variant is None:
        return "no_fb_post"
    return collect_variant_metrics_task(variant.pk)


METRICS_LEASE = 30  # minutes a queued read holds its slot before it may be queued again


@shared_task(queue="outbound")
def post_first_comment_task(variant_id: int) -> str:
    """Post a published channel's first comment (see services.post_first_comment)."""
    from .services import post_first_comment

    return post_first_comment(variant_id)


@shared_task(queue="outbound")
def collect_variant_metrics_task(variant_id: int) -> str:
    """Read one published channel's performance numbers."""
    from datetime import timedelta

    from .metrics_collector import (
        RATE_LIMIT_BACKOFF,
        MetricsRateLimited,
        collect_variant_metrics,
    )
    from .models import ContentPostVariant

    try:
        variant = ContentPostVariant.objects.select_related("post").get(pk=variant_id)
    except ContentPostVariant.DoesNotExist:
        return "not_found"
    if (
        variant.status != ContentPostVariant.Status.PUBLISHED
        or not variant.publication_id
    ):
        return "not_published"
    try:
        collect_variant_metrics(variant)
    except MetricsRateLimited:
        ContentPostVariant.objects.filter(pk=variant.pk).update(
            metrics_next_at=timezone.now() + RATE_LIMIT_BACKOFF
        )
        logger.warning("Metrics read rate-limited for variant=%s", variant_id)
        return "rate_limited"
    except Exception:
        # Unexpected: leave metrics_next_at on its lease so the sweep tries again.
        logger.exception("Metrics read failed for variant=%s", variant_id)
        ContentPostVariant.objects.filter(pk=variant.pk).update(
            metrics_next_at=timezone.now() + timedelta(minutes=METRICS_LEASE)
        )
        return "error"
    return "ok"


@shared_task(queue="outbound")
def collect_due_metrics() -> int:
    """Queue a read for every published channel whose next planned read has come."""
    from datetime import timedelta

    from django.db.models import Q

    from .models import ContentPostVariant

    now = timezone.now()
    count = 0
    with transaction.atomic():
        due = ContentPostVariant.objects.select_for_update(skip_locked=True).filter(
            # Or never read and never planned: a channel live before reads were planned
            # (or repaired to published later) had no reach at all in the Library.
            Q(metrics_next_at__lte=now)
            | Q(metrics_next_at__isnull=True, metrics_collected_at__isnull=True),
            status=ContentPostVariant.Status.PUBLISHED,
            platform__in=(
                ContentPostVariant.Platform.FACEBOOK,
                ContentPostVariant.Platform.INSTAGRAM,
            ),
        )[:200]
        for variant in due:
            # Lease the slot: the read moves metrics_next_at on, and a lost enqueue
            # comes round again once the lease runs out.
            ContentPostVariant.objects.filter(pk=variant.pk).update(
                metrics_next_at=now + timedelta(minutes=METRICS_LEASE)
            )
            transaction.on_commit(
                partial(collect_variant_metrics_task.delay, variant.pk), robust=True
            )
            count += 1
    return count


@shared_task(queue="outbound")
def reconcile_stale_publishing_variants() -> dict:
    """Find and escalate variants stuck in PUBLISHING state for more than 6 hours.

    This is the stale-claim reconciler: if a variant is PUBLISHING but the worker
    never picked up the task (enqueue failed or broker unreachable), it will be stuck
    forever. This task finds such variants and escalates them to RECONCILING after a
    timeout, requiring manual intervention.

    Returns: dict with counts of stale variants found and escalated
    """
    from datetime import timedelta

    from django.db.models import Q

    from .models import ContentPostVariant
    from .services import drop_claim_flag

    now = timezone.now()
    stale_threshold = now - timedelta(hours=6)

    # Find variants stuck in PUBLISHING with no recent attempt
    # A variant is considered stale if:
    # 1. Status is PUBLISHING
    # 2. last_attempt_at is old (or None, meaning no attempt has been made yet)
    # IMPORTANT: Use last_attempt_at, not last_error_at. last_error_at only records errors,
    # not active publishing attempts. A variant with a 7-hour-old error that was just retried
    # should NOT be escalated immediately.
    stale_qs = ContentPostVariant.objects.filter(
        status=ContentPostVariant.Status.PUBLISHING,
    ).filter(Q(last_attempt_at__lte=stale_threshold) | Q(last_attempt_at__isnull=True))

    count_escalated = 0
    stale_found = stale_qs.count()
    stale_filter = Q(last_attempt_at__lte=stale_threshold) | Q(
        last_attempt_at__isnull=True
    )
    for pk in list(stale_qs.values_list("pk", flat=True)[:100]):  # Bounded per run
        # Re-check under a row lock: a worker may have claimed or finished the variant
        # since the query ran, and its state must not be overwritten.
        with transaction.atomic():
            variant = (
                ContentPostVariant.objects.select_for_update()
                .filter(pk=pk, status=ContentPostVariant.Status.PUBLISHING)
                .filter(stale_filter)
                .first()
            )
            if variant is None:
                continue
            logger.warning(
                "Escalating stale variant to RECONCILING: variant=%s post=%s platform=%s last_error_at=%s",
                variant.pk,
                variant.post_id,
                variant.platform,
                variant.last_error_at,
            )
            variant.status = ContentPostVariant.Status.RECONCILING
            variant.last_error = "Stuck in PUBLISHING for 6+ hours (enqueue may have failed); manual reconciliation needed"
            variant.last_error_type = ContentPostVariant.ErrorType.UNKNOWN
            variant.last_error_at = now
            drop_claim_flag(variant)
            variant.save(
                update_fields=[
                    "status",
                    "last_error",
                    "last_error_type",
                    "last_error_at",
                    "metadata",
                ]
            )
        count_escalated += 1

    if count_escalated > 0:
        logger.info(
            "reconcile_stale_publishing_variants escalated %d variants to RECONCILING",
            count_escalated,
        )

    return {
        "stale_found": stale_found,
        "escalated": count_escalated,
    }


@shared_task(queue="outbound")
def poll_ig_containers() -> int:
    """DEPRECATED: Legacy Instagram container polling.

    This task is retired as of Step 4.6B. Variant-based publishing uses
    poll_ig_variant_containers() instead. This function remains for backward
    compatibility with existing scheduled posts but will not be called by
    any active workflow.

    Returns 0 (no work performed).
    """
    logger.warning(
        "poll_ig_containers called but is deprecated. "
        "Use poll_ig_variant_containers for new publishing workflows."
    )
    return 0


@shared_task(queue="outbound")
def poll_ig_variant_containers() -> int:
    """Poll in-progress IG video containers in variants and publish when FINISHED.

    Updates last_poll_attempt timestamp in metadata for exhaustion detection.
    """
    from .models import ContentPostVariant
    from .services import refresh_parent_post, try_publish_ig_variant_container

    # Find variants with container IDs in PROCESSING state
    qs = ContentPostVariant.objects.filter(
        platform="instagram",
        status=ContentPostVariant.Status.PROCESSING,
    ).exclude(metadata__ig_container_id__exact="")

    count = 0
    for pk in qs.values_list("pk", flat=True):
        try:
            # Record the poll attempt under a row lock, merged into the current metadata,
            # so a concurrent publish claim written to metadata is never overwritten.
            with transaction.atomic():
                variant = (
                    ContentPostVariant.objects.select_for_update()
                    .filter(pk=pk, status=ContentPostVariant.Status.PROCESSING)
                    .first()
                )
                if variant is None:
                    continue
                variant.metadata = {
                    **(variant.metadata or {}),
                    "last_poll_attempt": timezone.now().isoformat(),
                }
                variant.save(update_fields=["metadata"])

            done = try_publish_ig_variant_container(variant)
            if done:
                count += 1
            refresh_parent_post(variant.post)
        except Exception:
            logger.exception("poll_ig_variant_containers error for variant=%s", pk)
    return count


@shared_task(queue="outbound")
def escalate_exhausted_processing_variants(max_age_hours: int = 48) -> dict:
    """Escalate variants stuck in PROCESSING to RECONCILING after timeout.

    A variant in PROCESSING for more than max_age_hours likely has a problem:
    - Container did not finish (provider issue)
    - Polling was interrupted (worker crash)
    - Provider API became unavailable

    Escalation to RECONCILING allows operators to investigate and resolve.
    Preserves all metadata (container_id, poll history) for recovery.

    B2 Implementation: Uses processing_started_at (provider operation duration) as the clock,
    not polling activity. This allows distinguishing between:
    - A genuinely overdue provider operation (escalate)
    - A variant whose polling was interrupted but provider is still working (don't escalate)

    Args:
        max_age_hours: Maximum allowed time in PROCESSING state (default 48h)

    Returns: dict with counts of exhausted and escalated variants
    """
    from datetime import timedelta

    from django.db import transaction
    from django.db.models import Q

    from .models import ContentPostVariant

    now = timezone.now()
    exhaustion_threshold = now - timedelta(hours=max_age_hours)

    # Find variants stuck in PROCESSING longer than the threshold.
    # processing_started_at indicates when the provider first entered async processing.
    # This is independent of polling attempts; it measures provider operation duration.
    stale_filter = ContentPostVariant.objects.filter(
        # Instagram video containers, and Facebook Reels / video stories (the only
        # Facebook channels that process; they carry their video id).
        Q(platform="instagram")
        | Q(platform="facebook", metadata__has_key="fb_video_id"),
        status=ContentPostVariant.Status.PROCESSING,
        processing_started_at__lte=exhaustion_threshold,
    )
    with transaction.atomic():
        candidate_pks = list(
            stale_filter.select_for_update(skip_locked=True).values_list(
                "pk", flat=True
            )[:100]
        )

    exhausted_found = stale_filter.count()
    count_escalated = 0
    for pk in candidate_pks:
        # Atomic recheck: variant may have reached a terminal state since query
        with transaction.atomic():
            variant = ContentPostVariant.objects.select_for_update().get(pk=pk)
            if variant.status != ContentPostVariant.Status.PROCESSING:
                # Already transitioned (published, failed, etc.) — skip escalation
                continue
            if variant.processing_started_at is None:
                # Defensive: should not happen, but skip if clock is missing
                continue
            if variant.processing_started_at > exhaustion_threshold:
                # Processing was restarted after the query (e.g., retry) — not exhausted
                continue

            logger.warning(
                "Escalating exhausted PROCESSING variant to RECONCILING: "
                "variant=%s post=%s platform=%s processing_started_at=%s",
                variant.pk,
                variant.post.pk,
                variant.platform,
                variant.processing_started_at,
            )

            variant.status = ContentPostVariant.Status.RECONCILING
            variant.last_error = (
                f"Provider processing exceeded {max_age_hours} hours. "
                "Operation may have stalled; manual investigation required."
            )
            variant.last_error_type = ContentPostVariant.ErrorType.TIMEOUT
            variant.last_error_at = now
            # Preserve metadata (container_id, poll history) for recovery
            variant.save(
                update_fields=[
                    "status",
                    "last_error",
                    "last_error_type",
                    "last_error_at",
                ]
            )
            count_escalated += 1

    if count_escalated > 0:
        logger.info(
            "escalate_exhausted_processing_variants escalated %d variants to RECONCILING",
            count_escalated,
        )

    return {
        "exhausted_found": exhausted_found,
        "escalated": count_escalated,
    }


@shared_task(queue="outbound")
def recover_abandoned_publish_claims() -> dict:
    """Recover Instagram publish claims whose worker died before persisting an outcome.

    An expired claim means the remote publish outcome is unknown, so the variant moves to
    RECONCILING and is never reset to a publishable state. Clearing the claim makes any late
    write from the original worker fail its token check.
    """
    from datetime import datetime

    from django.db import transaction

    from .models import ContentPostVariant
    from .services import (
        CLAIM_IN_FLIGHT_KEY,
        CLAIM_TOKEN_KEY,
        PUBLISH_CLAIM_AT_KEY,
        PUBLISH_CLAIM_KEY,
        PUBLISH_CLAIM_LEASE,
        refresh_parent_post,
    )

    cutoff = timezone.now() - PUBLISH_CLAIM_LEASE
    recovered = 0

    # Instagram container claims: PROCESSING with a claim token and claim timestamp.
    ig_candidates = list(
        ContentPostVariant.objects.filter(
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            metadata__has_key=PUBLISH_CLAIM_KEY,
        ).values_list("pk", flat=True)
    )
    for pk in ig_candidates:
        with transaction.atomic():
            variant = ContentPostVariant.objects.select_for_update().get(pk=pk)
            metadata = variant.metadata or {}
            claimed_at_raw = metadata.get(PUBLISH_CLAIM_AT_KEY)
            if (
                variant.status != ContentPostVariant.Status.PROCESSING
                or not metadata.get(PUBLISH_CLAIM_KEY)
            ):
                continue
            if not claimed_at_raw or datetime.fromisoformat(claimed_at_raw) > cutoff:
                continue

            variant.status = ContentPostVariant.Status.RECONCILING
            variant.processing_started_at = None
            variant.last_error = "Publish attempt abandoned before its outcome was recorded; reconcile before retrying"
            variant.last_error_type = ContentPostVariant.ErrorType.TIMEOUT
            variant.last_error_at = timezone.now()
            variant.metadata = {
                k: v
                for k, v in metadata.items()
                if k not in (PUBLISH_CLAIM_KEY, PUBLISH_CLAIM_AT_KEY)
            }
            variant.save(
                update_fields=[
                    "status",
                    "processing_started_at",
                    "last_error",
                    "last_error_type",
                    "last_error_at",
                    "metadata",
                ]
            )
            refresh_parent_post(variant.post)
            recovered += 1

    # Synchronous (Facebook) claims: PUBLISHING with a claim token, stamped by last_attempt_at.
    fb_candidates = list(
        ContentPostVariant.objects.filter(
            status=ContentPostVariant.Status.PUBLISHING,
            last_attempt_at__lte=cutoff,
            metadata__has_key=CLAIM_TOKEN_KEY,
        ).values_list("pk", flat=True)
    )
    for pk in fb_candidates:
        with transaction.atomic():
            variant = ContentPostVariant.objects.select_for_update().get(pk=pk)
            metadata = variant.metadata or {}
            if (
                variant.status != ContentPostVariant.Status.PUBLISHING
                or not metadata.get(CLAIM_TOKEN_KEY)
            ):
                continue
            variant.status = ContentPostVariant.Status.RECONCILING
            variant.last_error = "Publish attempt abandoned before its outcome was recorded; reconcile before retrying"
            variant.last_error_type = ContentPostVariant.ErrorType.TIMEOUT
            variant.last_error_at = timezone.now()
            variant.metadata = {
                k: v
                for k, v in metadata.items()
                if k not in (CLAIM_IN_FLIGHT_KEY, CLAIM_TOKEN_KEY)
            }
            variant.save(
                update_fields=[
                    "status",
                    "last_error",
                    "last_error_type",
                    "last_error_at",
                    "metadata",
                ]
            )
            refresh_parent_post(variant.post)
            recovered += 1

    return {"recovered": recovered}


@shared_task(queue="outbound")
def alert_publishing_problems() -> dict:
    """Email Owners and Admins about channels that failed or need a check (see alerts)."""
    from .alerts import send_due_alerts

    return send_due_alerts()


@shared_task(queue="outbound")
def archive_stale_bio_tiles() -> int:
    """Archive bio-page tiles older than their page's limit (pinned tiles stay)."""
    from .bio import archive_stale_tiles

    return archive_stale_tiles()


@shared_task(queue="outbound")
def check_bio_links() -> int:
    """Re-read each live bio page's Instagram website field, so the page can warn
    when the bio doesn't point at it."""
    from .bio import check_instagram_website
    from .models import BioPage

    return sum(
        1
        for page in BioPage.objects.filter(is_active=True).select_related("account")
        if check_instagram_website(page)
    )


@shared_task(queue="media", soft_time_limit=300, time_limit=330)
def probe_media(check_id: int) -> str:
    """Hash a media file and, for a video, read it with ffprobe (apps.publishing.media_checks).
    Runs on its own worker so large files never hold up publishing."""
    from .media_checks import run
    from .models import MediaCheck

    check = MediaCheck.objects.filter(pk=check_id).first()
    if check is None:
        return "not_found"
    return run(check).status


@shared_task(queue="outbound")
def alert_media_check_backlog() -> int:
    """Media checks stuck behind a queue: email each business's Owners once a day.
    Operators see the same in the console (apps.publishing.api.attention_reasons)."""
    from django.core.cache import cache

    from apps.accounts.notifications import notify_team

    from .media_checks import backlog

    stuck = backlog()
    if not stuck.exists():
        return 0
    logger.error(
        "Media checks are behind: %s pending, oldest since %s",
        stuck.count(),
        stuck.first().created_at.isoformat(),
    )
    sent = 0
    for account_id in stuck.values_list("account_id", flat=True).distinct():
        if not cache.add(f"media-backlog-alert:{account_id}", 1, timeout=86400):
            continue
        from apps.accounts.models import Account

        account = Account.objects.get(pk=account_id)
        try:
            notify_team(
                account,
                to="owners",
                subject="Your media checks are running slowly",
                text=(
                    "Akilent checks each video before it can be scheduled, and those "
                    "checks are taking longer than usual. Your posts are saved: you can "
                    "keep them as drafts and schedule them once the check finishes. "
                    "We're looking into it."
                ),
                path="/publishing/library/",
                reason=(
                    "You're getting this because you're an Owner or Admin of "
                    f"{account.company_name} in Akilent."
                ),
            )
            sent += 1
        except Exception:
            logger.exception("media backlog alert failed for account=%s", account_id)
    return sent


@shared_task(queue="outbound")
def poll_fb_video_variants() -> int:
    """Finish Facebook Reels and video stories once Meta has processed them."""
    from .models import ContentPostVariant
    from .services import refresh_parent_post, try_finish_fb_video_variant

    qs = ContentPostVariant.objects.filter(
        platform=ContentPostVariant.Platform.FACEBOOK,
        status=ContentPostVariant.Status.PROCESSING,
    ).exclude(metadata__fb_video_id__exact="")
    count = 0
    for pk in qs.values_list("pk", flat=True):
        try:
            with transaction.atomic():
                variant = (
                    ContentPostVariant.objects.select_for_update()
                    .filter(pk=pk, status=ContentPostVariant.Status.PROCESSING)
                    .first()
                )
                if variant is None:
                    continue
                variant.metadata = {
                    **(variant.metadata or {}),
                    "last_poll_attempt": timezone.now().isoformat(),
                }
                variant.save(update_fields=["metadata"])
            if try_finish_fb_video_variant(variant):
                count += 1
            refresh_parent_post(variant.post)
        except Exception:
            logger.exception("poll_fb_video_variants error for variant=%s", pk)
    return count


@shared_task(queue="outbound")
def collect_account_metrics() -> int:
    """Read each connected channel's settled days (Phase 2b, ``cs_analytics``). Does
    nothing until released, so no new calls reach Meta before then."""
    from django.db.models import Q

    from apps.accounts.models import Account

    from . import account_metrics
    from .metrics_collector import MetricsRateLimited

    if not account_metrics.is_released():
        return 0
    count = 0
    accounts = Account.objects.filter(
        Q(instagram_accounts__is_active=True)
        | Q(facebook_page_connection__status="active")
    ).distinct()
    for account in accounts:
        for platform in account_metrics.PLATFORMS:
            if account_metrics.connection(account, platform) is None:
                continue
            for day in account_metrics.days_to_collect(account, platform):
                try:
                    account_metrics.collect_day(account, platform, day)
                    count += 1
                except MetricsRateLimited:
                    logger.info("Account metrics rate limited; trying later")
                    return count
                except Exception:
                    logger.exception(
                        "account metrics failed account=%s %s %s",
                        account.pk,
                        platform,
                        day,
                    )
    return count


@shared_task(queue="outbound")
def send_weekly_reports() -> int:
    """The weekly email (``cs_analytics``), Monday from 08:00 on each business's clock,
    once a week."""
    from . import account_metrics

    if not account_metrics.is_released():
        return 0
    return account_metrics.send_weekly_reports()


@shared_task(queue="outbound")
def run_recurring_posts() -> int:
    """Make the copies of recurring posts due within a day (``cs_recurring``)."""
    from . import recurring

    if not recurring.is_released():
        return 0
    return len(recurring.run_due())


@shared_task(queue="media")
def grab_cover_frame(asset_pk: int, seconds: float, user_pk: int | None = None) -> int:
    """Take one frame of a library video as a Reel cover (``cs_media_library``). On
    the media queue with ffprobe, so it never delays publishing. Returns the new
    asset's id, or 0; a failure is kept for the library page to show."""
    from django.contrib.auth.models import User
    from django.core.cache import cache

    from . import media_library
    from .models import MediaAsset

    asset = MediaAsset.objects.filter(pk=asset_pk).select_related("account").first()
    if asset is None:
        return 0
    key = media_library.cover_status_key(asset)
    try:
        cover = media_library.grab_cover(
            asset, seconds, User.objects.filter(pk=user_pk).first() if user_pk else None
        )
    except media_library.LibraryError as exc:
        cache.set(key, {"error": str(exc)}, 3600)
        return 0
    except Exception:
        logger.exception("cover frame failed for asset=%s", asset_pk)
        cache.set(key, {"error": "Couldn't take a frame. Try again."}, 3600)
        return 0
    cache.set(key, {"cover": cover.pk}, 3600)
    return cover.pk


@shared_task(queue="media", soft_time_limit=120, time_limit=150)
def fill_asset_details(asset_id: int) -> str:
    """A media file's thumbnail, size and dimensions (media_library.fill_details)."""
    from . import media_library
    from .models import MediaAsset

    asset = MediaAsset.objects.select_related("account").filter(pk=asset_id).first()
    if asset is None:
        return "not_found"
    return ",".join(media_library.fill_details(asset)) or "nothing"
