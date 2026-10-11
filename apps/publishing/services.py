"""Publishing service: orchestrates IG and FB publish calls."""

from __future__ import annotations

import logging

from django.utils import timezone

from .models import ContentPost, FacebookPageConnection
from .providers.facebook import FacebookPublishError, FacebookPublishProvider
from .providers.instagram import (
    CONTAINER_STATUS_ERROR,
    CONTAINER_STATUS_EXPIRED,
    CONTAINER_STATUS_FINISHED,
    InstagramPublishError,
    InstagramPublishProvider,
)

logger = logging.getLogger(__name__)


def _full_caption_for(post: ContentPost, platform: str) -> str:
    """Return platform-specific caption with hashtags appended."""
    base = post.caption_for(platform)
    tags = " ".join(f"#{t.lstrip('#')}" for t in post.hashtags)
    if tags:
        return f"{base}\n\n{tags}".strip()
    return base


def _ig_account(post: ContentPost):
    """Return the active InstagramBusinessAccount for this post's account, or None."""
    return post.account.instagram_accounts.filter(is_active=True).first()


def _ig_provider_for_post(post: ContentPost) -> InstagramPublishProvider | None:
    ig = _ig_account(post)
    if ig is None or not ig.access_token:
        return None
    return InstagramPublishProvider(ig.access_token)


def _fb_provider_for_post(
    post: ContentPost,
) -> tuple[FacebookPublishProvider, str] | None:
    try:
        conn: FacebookPageConnection = post.account.facebook_page_connection
    except Exception:
        return None
    if conn.status != FacebookPageConnection.Status.ACTIVE or not conn.access_token:
        return None
    return FacebookPublishProvider(conn.page_id, conn.access_token), conn.page_id


def publish_post(post: ContentPost) -> None:
    """Publish a ContentPost to all its platforms. Updates post in place."""
    errors = []

    if "instagram" in post.platforms:
        try:
            _publish_instagram(post)
        except InstagramPublishError as exc:
            errors.append(f"Instagram: {exc}")
            logger.warning("IG publish failed for post=%s: %s", post.pk, exc)

    if "facebook" in post.platforms:
        try:
            _publish_facebook(post)
        except FacebookPublishError as exc:
            errors.append(f"Facebook: {exc}")
            logger.warning("FB publish failed for post=%s: %s", post.pk, exc)

    if errors:
        post.error = "; ".join(errors)
        post.status = ContentPost.Status.FAILED
    else:
        post.status = ContentPost.Status.PUBLISHED
        post.published_at = timezone.now()

    post.save(
        update_fields=[
            "status",
            "published_at",
            "ig_media_id",
            "ig_container_id",
            "fb_post_id",
            "error",
        ]
    )


def _publish_instagram(post: ContentPost) -> None:
    provider = _ig_provider_for_post(post)
    if provider is None:
        raise InstagramPublishError("Instagram not connected.")

    ig = _ig_account(post)
    ig_user_id = ig.instagram_business_account_id if ig else ""  # type: ignore[union-attr]
    caption = _full_caption_for(post, "instagram")

    mt = post.media_type
    url = post.media_urls[0] if post.media_urls else ""

    if mt == ContentPost.MediaType.IMAGE:
        container_id = provider.create_image_container(ig_user_id, url, caption)
        post.ig_container_id = container_id
        ig_media_id = provider.publish_container(ig_user_id, container_id)
        post.ig_media_id = ig_media_id

    elif mt in (ContentPost.MediaType.VIDEO, ContentPost.MediaType.REEL):
        container_id = provider.create_video_container(ig_user_id, url, caption)
        post.ig_container_id = container_id
        # Leave status=PUBLISHING; poll_ig_containers will finish it

    elif mt == ContentPost.MediaType.STORY:
        if url.lower().endswith((".mp4", ".mov")):
            container_id = provider.create_story_video_container(ig_user_id, url)
        else:
            container_id = provider.create_story_image_container(ig_user_id, url)
        post.ig_container_id = container_id

    elif mt == ContentPost.MediaType.CAROUSEL:
        children = []
        for item_url in post.media_urls:
            child_id = provider.create_carousel_item_container(ig_user_id, item_url)
            children.append(child_id)
        container_id = provider.create_carousel_container(ig_user_id, children, caption)
        post.ig_container_id = container_id
        ig_media_id = provider.publish_container(ig_user_id, container_id)
        post.ig_media_id = ig_media_id


def _publish_facebook(post: ContentPost) -> None:
    result = _fb_provider_for_post(post)
    if result is None:
        raise FacebookPublishError("Facebook Page not connected.")
    provider, _page_id = result

    mt = post.media_type
    caption = _full_caption_for(post, "facebook")
    url = post.media_urls[0] if post.media_urls else ""

    if mt == ContentPost.MediaType.TEXT:
        post_id = provider.create_text_post(caption)
    elif mt == ContentPost.MediaType.IMAGE:
        post_id = provider.create_photo_post(url, caption)
    elif mt in (ContentPost.MediaType.VIDEO, ContentPost.MediaType.REEL):
        post_id = provider.create_video_post(url, caption)
    else:
        post_id = provider.create_photo_post(url, caption)

    post.fb_post_id = post_id


def try_publish_ig_container(post: ContentPost) -> bool:
    """Poll the container and publish once FINISHED. Returns True if published."""
    provider = _ig_provider_for_post(post)
    if provider is None:
        return False

    status = provider.poll_container_status(post.ig_container_id)

    if status == CONTAINER_STATUS_FINISHED:
        ig = _ig_account(post)
        ig_user_id = ig.instagram_business_account_id if ig else ""  # type: ignore[union-attr]
        ig_media_id = provider.publish_container(ig_user_id, post.ig_container_id)
        post.ig_media_id = ig_media_id
        if "facebook" not in post.platforms or post.fb_post_id:
            post.status = ContentPost.Status.PUBLISHED
            post.published_at = timezone.now()
        post.save(update_fields=["ig_media_id", "status", "published_at"])
        return True

    if status in (CONTAINER_STATUS_ERROR, CONTAINER_STATUS_EXPIRED):
        post.status = ContentPost.Status.FAILED
        post.error = f"IG container {status.lower()}"
        post.save(update_fields=["status", "error"])

    return False
