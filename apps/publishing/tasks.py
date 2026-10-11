"""Celery tasks for content publishing."""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(queue="outbound")
def publish_due_posts() -> int:
    """Pick ContentPosts whose scheduled_at has passed and publish them."""
    from .models import ContentPost

    now = timezone.now()
    qs = ContentPost.objects.select_for_update(skip_locked=True).filter(
        status=ContentPost.Status.SCHEDULED,
        scheduled_at__lte=now,
    )
    count = 0
    with transaction.atomic():
        for post in qs:
            post.status = ContentPost.Status.PUBLISHING
            post.save(update_fields=["status"])
            publish_post_async.delay(post.pk)
            count += 1
    return count


@shared_task(bind=True, max_retries=3, queue="outbound")
def publish_post_async(self, post_id: int) -> str:
    from django.db import transaction

    from .models import ContentPost
    from .services import publish_post

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

    try:
        publish_post(post)
        return "published"
    except Exception as exc:
        logger.exception("publish_post_async failed for post=%s", post_id)
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=60 * (2**self.request.retries), exc=exc) from exc
        post.status = ContentPost.Status.FAILED
        post.error = str(exc)[:500]
        post.save(update_fields=["status", "error"])
        return "failed"


@shared_task(queue="outbound")
def fetch_fb_post_insights(post_id: int) -> str:
    """Fetch reach/impressions for a published Facebook post and store them.

    Scheduled ~15 minutes after publish to allow Meta's insights to populate.
    Uses pages_read_engagement scope on the stored page access token.
    """
    from django.utils import timezone

    from .models import ContentPost
    from .services import _fb_provider_for_post

    try:
        post = ContentPost.objects.get(pk=post_id)
    except ContentPost.DoesNotExist:
        return "not_found"

    if not post.fb_post_id:
        return "no_fb_post"

    result = _fb_provider_for_post(post)
    if result is None:
        return "no_connection"

    provider, _ = result
    try:
        insights = provider.fetch_post_insights(post.fb_post_id)
    except Exception as exc:
        logger.error(
            "fetch_fb_post_insights failed for post=%s fb_post_id=%s: %s",
            post_id,
            post.fb_post_id,
            exc,
        )
        return "error"

    post.fb_reach = insights.get("reach")
    post.fb_impressions = insights.get("impressions")
    post.fb_insights_fetched_at = timezone.now()
    post.save(update_fields=["fb_reach", "fb_impressions", "fb_insights_fetched_at"])
    logger.info(
        "fetch_fb_post_insights ok post=%s reach=%s impressions=%s",
        post_id,
        post.fb_reach,
        post.fb_impressions,
    )
    return "ok"


@shared_task(queue="outbound")
def poll_ig_containers() -> int:
    """Poll in-progress IG video containers and publish them when FINISHED."""
    from .models import ContentPost
    from .services import try_publish_ig_container

    qs = ContentPost.objects.filter(
        status=ContentPost.Status.PUBLISHING,
        ig_container_id__gt="",
        ig_media_id="",
    )
    count = 0
    for post in qs:
        try:
            done = try_publish_ig_container(post)
            if done:
                count += 1
        except Exception:
            logger.exception("poll_ig_containers error for post=%s", post.pk)
    return count
