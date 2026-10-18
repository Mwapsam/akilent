"""Publishing service: orchestrates IG and FB publish calls."""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.utils import timezone

from .models import ContentPost, ContentPostVariant, FacebookPageConnection
from .providers.facebook import FacebookPublishError, FacebookPublishProvider
from .providers.instagram import (
    CONTAINER_STATUS_ERROR,
    CONTAINER_STATUS_EXPIRED,
    CONTAINER_STATUS_FINISHED,
    InstagramPublishError,
    InstagramPublishProvider,
)

logger = logging.getLogger(__name__)

# Instagram accepted aspect ratio range: width/height must be between 0.8 (4:5)
# and 1.91 (1.91:1). A 1% tolerance covers minor rounding in image dimensions.
_IG_RATIO_MIN = 0.8 * 0.99
_IG_RATIO_MAX = 1.91 * 1.01


# ---------------------------------------------------------------------------
# Phase A1: Atomic variant initialization
# ---------------------------------------------------------------------------


def initialize_post_with_variants(
    post: ContentPost, platforms: list[str]
) -> list[ContentPostVariant]:
    """Create or update variants for a post, ensuring one exists per platform.

    Atomically creates the post's variants within a transaction. Idempotent:
    if a variant for a platform already exists, it is preserved and not
    recreated. This allows multiple creation paths (post_new, plan_item,
    workflow, API) to safely initialize variants without race conditions.

    Args:
        post: The ContentPost to initialize (must already be saved to database)
        platforms: List of platform choices (e.g., ["facebook", "instagram"])

    Returns:
        List of ContentPostVariant instances (newly created or existing)

    Raises:
        ValueError: If platforms list is empty or contains invalid platform codes
        IntegrityError: If database constraints are violated (rolled back by caller)
    """
    from django.db import transaction

    if not platforms:
        raise ValueError("At least one platform must be selected")

    # Validate platform choices
    valid_platforms = {code for code, _ in ContentPostVariant.Platform.choices}
    invalid = set(platforms) - valid_platforms
    if invalid:
        raise ValueError(
            f"Invalid platforms: {invalid}. Valid choices: {valid_platforms}"
        )

    Status = ContentPostVariant.Status
    editable = {Status.DRAFT, Status.SCHEDULED, Status.FAILED, Status.CANCELLED}
    preserved = {
        Status.PUBLISHING,
        Status.PROCESSING,
        Status.PUBLISHED,
        Status.RECONCILING,
    }

    variants = []
    with transaction.atomic():
        existing = {
            v.platform: v
            for v in ContentPostVariant.objects.select_for_update().filter(post=post)
        }
        for platform in platforms:
            caption = post.caption_for(platform)
            variant = existing.get(platform)
            if variant is None:
                variant = ContentPostVariant.objects.create(
                    post=post,
                    platform=platform,
                    caption=caption,
                    media_urls=post.media_urls or [],
                    scheduled_at=post.scheduled_at,
                    status=Status.DRAFT,
                )
                logger.info(
                    "Created variant: post=%s platform=%s variant_id=%s",
                    post.pk,
                    platform,
                    variant.pk,
                )
            elif variant.status in editable:
                variant.caption = caption
                variant.media_urls = post.media_urls or []
                variant.scheduled_at = post.scheduled_at
                if variant.status == Status.CANCELLED:
                    variant.status = Status.DRAFT
                variant.save(
                    update_fields=["caption", "media_urls", "scheduled_at", "status"]
                )
            variants.append(variant)

        for platform, variant in existing.items():
            if platform in platforms:
                continue
            if variant.status in editable and variant.status != Status.CANCELLED:
                variant.status = Status.CANCELLED
                variant.save(update_fields=["status"])
            elif variant.status in preserved:
                logger.warning(
                    "Deselected platform kept: post=%s platform=%s status=%s",
                    post.pk,
                    platform,
                    variant.status,
                )

    return variants


def _ig_permalink(provider, media_id: str) -> str:
    """Fetch the media's real permalink. A lookup failure must not fail the publication."""
    try:
        return provider.get_permalink(media_id)
    except Exception:
        logger.warning(
            "IG permalink lookup failed for media=%s", media_id, exc_info=True
        )
        return ""


class _IGPublishOutcomeUnknown(Exception):
    """The final media_publish call failed without a definitive answer from Meta."""

    def __init__(self, container_id: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.container_id = container_id


def _ig_publish_container(provider, ig_user_id: str, container_id: str) -> str:
    """Run the final media_publish call.

    Only InstagramPublishError is a definitive rejection from Meta. Anything else
    (connection reset, unreadable response, missing id) may have published the media,
    so it is raised as an unknown outcome to be reconciled, never blindly retried.
    """
    try:
        return provider.publish_container(ig_user_id, container_id)
    except InstagramPublishError:
        raise
    except Exception as exc:
        raise _IGPublishOutcomeUnknown(container_id, exc) from exc


_IG_RATIO_CHECK_MAX_BYTES = 8 * 1024 * 1024  # Instagram's own image size limit


def _is_own_media_url(url: str) -> bool:
    """Whether ``url`` points at our media storage or our own site.

    Only these are fetched server-side; fetching arbitrary user-supplied URLs would let
    a user make the server request internal addresses (SSRF).
    """
    from urllib.parse import urlparse

    from django.conf import settings

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    hosts = {
        urlparse(settings.SITE_URL).hostname,
        urlparse(settings.MEDIA_URL).hostname,
    }
    return parsed.hostname.lower() in {h.lower() for h in hosts if h}


def _validate_ig_image_ratio(url: str) -> None:
    """Raise InstagramPublishError before the API call if the aspect ratio is wrong.

    Best effort: images that are not on our own media host, too large, or unreadable
    are left for Instagram to judge.
    """
    if not url or not _is_own_media_url(url):
        return
    try:
        import io

        import requests as _req
        from PIL import Image

        with _req.get(url, timeout=10, stream=True, allow_redirects=False) as resp:
            resp.raise_for_status()
            body = bytearray()
            for chunk in resp.iter_content(64 * 1024):
                body.extend(chunk)
                if len(body) > _IG_RATIO_CHECK_MAX_BYTES:
                    return
        img = Image.open(io.BytesIO(bytes(body)))
        w, h = img.size
        if h == 0:
            return
        ratio = w / h
        if not (_IG_RATIO_MIN <= ratio <= _IG_RATIO_MAX):
            raise InstagramPublishError(
                f"Unsupported aspect ratio ({w}x{h}, ratio {ratio:.2f}). "
                "Instagram accepts: square (1:1), portrait (4:5 = 0.8), "
                "landscape (1.91:1). Crop the image before publishing.",
                error_type=ContentPostVariant.ErrorType.VALIDATION,
            )
    except InstagramPublishError:
        raise
    except Exception as exc:
        # If we can't fetch or open the image, skip the check and let the API decide.
        logger.debug("_validate_ig_image_ratio: could not check %s: %s", url, exc)


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
    if ig is None or not ig.access_token or ig.token_expired:
        return None
    return InstagramPublishProvider(ig.access_token)


IG_RECONNECT_MESSAGE = (
    "Instagram connection expired. Reconnect Instagram, then retry this post."
)
FB_RECONNECT_MESSAGE = (
    "Facebook Page connection expired. Reconnect it in Content Studio → Channels, "
    "then retry this post."
)


def _mark_token_invalid(post: ContentPost, platform: str) -> None:
    """Record that Meta rejected the stored token, so the UI asks for a reconnect."""
    if platform == "instagram":
        ig = _ig_account(post)
        if ig and not ig.token_expired:
            type(ig).objects.filter(pk=ig.pk).update(token_expired=True)
            logger.warning("Instagram token invalid for account=%s", post.account_id)
    else:
        updated = FacebookPageConnection.objects.filter(
            account_id=post.account_id, status=FacebookPageConnection.Status.ACTIVE
        ).update(status=FacebookPageConnection.Status.EXPIRED)
        if updated:
            logger.warning("Facebook token invalid for account=%s", post.account_id)


def _provider_error_result(exc, post: ContentPost, platform: str) -> dict:
    """Failure result for a definitive Graph API error."""
    if exc.token_invalid:
        _mark_token_invalid(post, platform)
    message = str(exc)
    if exc.token_invalid:
        message = (
            IG_RECONNECT_MESSAGE if platform == "instagram" else FB_RECONNECT_MESSAGE
        )
    return {
        "success": False,
        "publication_id": "",
        "publication_url": "",
        "error": message,
        "error_type": exc.error_type,
        "error_at": timezone.now(),
        "reconciling": False,
    }


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
    """Publish a ContentPost to all its platforms. Updates post in place.

    For videos, creates container and leaves post PUBLISHING for async polling.
    For images and other media, publishes directly and marks PUBLISHED.
    """
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

    # Determine final status based on errors and async work
    if errors:
        # Only set FAILED if we had errors AND no async work in progress
        has_unfinished_video = (
            "instagram" in post.platforms
            and post.ig_container_id
            and not post.ig_media_id
        )
        if has_unfinished_video:
            # Video container created; polling finishes it, but the real errors stay visible
            post.status = ContentPost.Status.PUBLISHING
            post.error = "; ".join(errors)
        else:
            post.status = ContentPost.Status.FAILED
            post.error = "; ".join(errors)
    else:
        # No errors; check if we have unfinished async work
        has_unfinished_video = (
            "instagram" in post.platforms
            and post.ig_container_id
            and not post.ig_media_id
        )
        if has_unfinished_video:
            # Video is processing; keep in PUBLISHING until polling completes
            post.status = ContentPost.Status.PUBLISHING
        else:
            # Everything is done synchronously
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

    if post.fb_post_id and post.status == ContentPost.Status.PUBLISHED:
        from .tasks import fetch_fb_post_insights

        fetch_fb_post_insights.apply_async(args=[post.pk], countdown=900)


def _wait_for_ig_container(
    provider: InstagramPublishProvider,
    container_id: str,
    *,
    max_attempts: int = 10,
    delay: float = 2.0,
) -> None:
    """Poll until the container is FINISHED (or raise on ERROR/EXPIRED)."""
    import time

    for _ in range(max_attempts):
        status = provider.poll_container_status(container_id)
        if status == CONTAINER_STATUS_FINISHED:
            return
        if status in (CONTAINER_STATUS_ERROR, CONTAINER_STATUS_EXPIRED):
            raise InstagramPublishError(
                f"Instagram could not process the media (container {status.lower()}).",
                error_type=ContentPostVariant.ErrorType.VALIDATION,
            )
        time.sleep(delay)
    raise InstagramPublishError(
        "Container did not finish processing in time.",
        error_type=ContentPostVariant.ErrorType.TIMEOUT,
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
        _validate_ig_image_ratio(url)
        container_id = provider.create_image_container(ig_user_id, url, caption)
        post.ig_container_id = container_id
        _wait_for_ig_container(provider, container_id)
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
        _wait_for_ig_container(provider, container_id)
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


PUBLISH_CLAIM_KEY = "publish_claim"
PUBLISH_CLAIM_AT_KEY = "publish_claim_at"
# Longer than any single Graph call chain (30s timeouts, a few requests) plus task scheduling
# delay, so a live worker is never mistaken for an abandoned one.
PUBLISH_CLAIM_LEASE = timedelta(minutes=30)


def _claim_publish(variant_pk: int) -> str | None:
    """Atomically claim a PROCESSING container for one publish attempt. Returns a token or None."""
    import secrets

    from django.db import transaction

    token = secrets.token_hex(8)
    with transaction.atomic():
        v = ContentPostVariant.objects.select_for_update().get(pk=variant_pk)
        if v.status != ContentPostVariant.Status.PROCESSING:
            return None
        if (v.metadata or {}).get(PUBLISH_CLAIM_KEY):
            return None
        v.metadata = {
            **(v.metadata or {}),
            PUBLISH_CLAIM_KEY: token,
            PUBLISH_CLAIM_AT_KEY: timezone.now().isoformat(),
        }
        v.save(update_fields=["metadata"])
        return token


def _persist_publish_outcome(
    variant_pk: int, token: str | None, status: str, **fields
) -> bool:
    """Write an outcome only if the variant is still PROCESSING and, when a claim was taken,
    this attempt still owns it. A stale worker cannot overwrite a later outcome."""
    from django.db import transaction

    with transaction.atomic():
        v = ContentPostVariant.objects.select_for_update().get(pk=variant_pk)
        if v.status != ContentPostVariant.Status.PROCESSING:
            return False
        if token is not None and (v.metadata or {}).get(PUBLISH_CLAIM_KEY) != token:
            return False

        metadata = {
            k: val
            for k, val in (v.metadata or {}).items()
            if k not in (PUBLISH_CLAIM_KEY, PUBLISH_CLAIM_AT_KEY)
        }
        v.status = status
        v.processing_started_at = None
        if status == ContentPostVariant.Status.PUBLISHED:
            v.publication_id = fields["publication_id"]
            v.publication_url = fields["publication_url"]
            v.published_at = timezone.now()
            v.last_error = ""
            v.last_error_type = ""
            v.last_error_at = None
            metadata = {}
        else:
            v.last_error = fields["error"]
            v.last_error_type = fields["error_type"]
            v.last_error_at = timezone.now()
        v.metadata = metadata
        v.save(
            update_fields=[
                "status",
                "publication_id",
                "publication_url",
                "published_at",
                "last_error",
                "last_error_type",
                "last_error_at",
                "processing_started_at",
                "metadata",
            ]
        )
        return True


def try_publish_ig_variant_container(variant: ContentPostVariant) -> bool:
    """Poll a PROCESSING container and publish it once, under an atomic claim.

    The claim is taken in a short transaction; Graph API calls run outside it. A publish
    request whose outcome is unknown moves the variant to RECONCILING and is never retried
    automatically. Outcomes are persisted only if this attempt still owns the variant.
    """
    post = variant.post
    container_id = (variant.metadata or {}).get("ig_container_id", "")

    if not container_id:
        _persist_publish_outcome(
            variant.pk,
            None,
            ContentPostVariant.Status.FAILED,
            error="Missing container ID for polling",
            error_type=ContentPostVariant.ErrorType.UNKNOWN,
        )
        return False

    provider = _ig_provider_for_post(post)
    if provider is None:
        return False

    try:
        status = provider.poll_container_status(container_id)
    except InstagramPublishError as exc:
        if exc.token_invalid:
            _mark_token_invalid(post, "instagram")
        logger.warning(
            "poll_container_status refused for variant=%s container=%s: %s",
            variant.pk,
            container_id,
            exc,
        )
        return False
    except Exception:
        logger.exception(
            "poll_container_status failed for variant=%s container=%s",
            variant.pk,
            container_id,
        )
        return False

    if status == CONTAINER_STATUS_FINISHED:
        token = _claim_publish(variant.pk)
        if token is None:
            return False
        try:
            ig = _ig_account(post)
            ig_user_id = ig.instagram_business_account_id if ig else ""
            ig_media_id = provider.publish_container(ig_user_id, container_id)
        except Exception as exc:
            logger.exception(
                "publish_container outcome unknown for variant=%s container=%s",
                variant.pk,
                container_id,
            )
            _persist_publish_outcome(
                variant.pk,
                token,
                ContentPostVariant.Status.RECONCILING,
                error=f"Final publication outcome unknown: {exc}",
                error_type=ContentPostVariant.ErrorType.TIMEOUT,
            )
            return False
        _persist_publish_outcome(
            variant.pk,
            token,
            ContentPostVariant.Status.PUBLISHED,
            publication_id=ig_media_id,
            publication_url=_ig_permalink(provider, ig_media_id),
        )
        return True

    if status in (CONTAINER_STATUS_ERROR, CONTAINER_STATUS_EXPIRED):
        _persist_publish_outcome(
            variant.pk,
            None,
            ContentPostVariant.Status.FAILED,
            error=f"IG container {status.lower()}",
            error_type=ContentPostVariant.ErrorType.PROVIDER_OUTAGE,
        )
        return False

    return False


CLAIM_IN_FLIGHT_KEY = "claim_in_flight"
CLAIM_TOKEN_KEY = "claim_token"


def drop_claim_flag(variant: ContentPostVariant) -> None:
    variant.metadata = {
        k: v for k, v in (variant.metadata or {}).items() if k != CLAIM_IN_FLIGHT_KEY
    }


def _claim_blocked(variant: ContentPostVariant) -> bool:
    """True when another attempt owns this variant.

    A retry click sets PUBLISHING before enqueueing, and that state alone must not
    block the queued attempt. Only an in-flight claim (flagged under the lock) does.
    """
    if variant.status in (
        ContentPostVariant.Status.PUBLISHED,
        ContentPostVariant.Status.RECONCILING,
        ContentPostVariant.Status.PROCESSING,
    ):
        return True
    return variant.status == ContentPostVariant.Status.PUBLISHING and bool(
        (variant.metadata or {}).get(CLAIM_IN_FLIGHT_KEY)
    )


def publish_variant(variant: ContentPostVariant) -> dict:
    """Publish a single ContentPostVariant to its platform.

    Returns dict with keys:
    - success (bool): publication succeeded
    - publication_id (str): provider ID if successful
    - publication_url (str): public URL if successful
    - error (str): human-readable error message if failed
    - error_type (str): classification (validation, permission, rate_limit, provider_outage, timeout, unknown)
    - error_at (datetime): when error occurred
    - reconciling (bool): ambiguous timeout, requires reconciliation before retry

    This function:
    1. Validates approval if required
    2. Uses variant's platform-specific caption, media, format
    3. Calls the appropriate provider adapter
    4. Persists outcome on the variant
    5. Does NOT hold DB lock during external API calls

    Safe for concurrent calls: uses atomic transaction for claiming,
    releases lock before API calls, then persists results atomically.
    """
    from django.db import transaction

    post = variant.post

    # Validate approval before publishing (Step 3 integration)
    if not post.is_approval_valid():
        return {
            "success": False,
            "publication_id": "",
            "publication_url": "",
            "error": "Post approval is invalid or not present",
            "error_type": ContentPostVariant.ErrorType.VALIDATION,
            "error_at": timezone.now(),
            "reconciling": False,
        }

    # Validate variant state: only publish if not already published, reconciling, or processing
    if _claim_blocked(variant):
        return {
            "success": False,
            "publication_id": variant.publication_id or "",
            "publication_url": variant.publication_url or "",
            "error": f"Variant is already {variant.status}",
            "error_type": ContentPostVariant.ErrorType.VALIDATION,
            "error_at": timezone.now(),
            "reconciling": False,
        }

    # Phase A4: Claim the variant atomically and recheck status under lock
    with transaction.atomic():
        # Lock the post and all its variants. The approval check and the payload sent
        # to the provider both come from this locked snapshot, so the approved content
        # is the content published.
        post = ContentPost.objects.select_for_update().get(pk=post.pk)
        variants = list(post.variants.select_for_update().order_by("platform"))
        variant = next(v for v in variants if v.pk == variant.pk)
        variant.post = post

        # IMPORTANT: Recheck status after acquiring lock. A concurrent caller may have
        # already published, published, or moved to reconciling. This prevents duplicate
        # external publication when two tasks race to claim the same variant.
        if _claim_blocked(variant):
            return {
                "success": False,
                "publication_id": variant.publication_id or "",
                "publication_url": variant.publication_url or "",
                "error": f"Variant is already {variant.status}",
                "error_type": ContentPostVariant.ErrorType.VALIDATION,
                "error_at": timezone.now(),
                "reconciling": False,
            }

        if not post.is_approval_valid(variants):
            return {
                "success": False,
                "publication_id": "",
                "publication_url": "",
                "error": "Post approval is invalid or not present",
                "error_type": ContentPostVariant.ErrorType.VALIDATION,
                "error_at": timezone.now(),
                "reconciling": False,
            }

        # Claim: mark as PUBLISHING and record attempt
        variant.attempt_count += 1
        variant.last_attempt_at = timezone.now()
        variant.status = ContentPostVariant.Status.PUBLISHING
        variant.processing_started_at = None
        claim_token = secrets.token_hex(8)
        variant.metadata = {
            **(variant.metadata or {}),
            CLAIM_IN_FLIGHT_KEY: True,
            CLAIM_TOKEN_KEY: claim_token,
        }
        variant.save(
            update_fields=[
                "status",
                "attempt_count",
                "last_attempt_at",
                "processing_started_at",
                "metadata",
            ]
        )

    # Perform the external API call (outside transaction to avoid lock hold)
    try:
        if variant.platform == "facebook":
            result = _publish_variant_facebook(variant, post)
        elif variant.platform == "instagram":
            result = _publish_variant_instagram(variant, post)
        else:
            result = {
                "success": False,
                "publication_id": "",
                "publication_url": "",
                "error": f"Unknown platform: {variant.platform}",
                "error_type": ContentPostVariant.ErrorType.VALIDATION,
                "error_at": timezone.now(),
                "reconciling": False,
            }
    except Exception as exc:
        logger.exception(
            "publish_variant failed for variant=%s platform=%s",
            variant.pk,
            variant.platform,
        )
        result = {
            "success": False,
            "publication_id": "",
            "publication_url": "",
            "error": str(exc),
            "error_type": ContentPostVariant.ErrorType.UNKNOWN,
            "error_at": timezone.now(),
            "reconciling": False,
        }

    # Persist the outcome atomically, only if this attempt still owns the variant. A recovered
    # (RECONCILING) or superseded claim must not be overwritten by a late worker.
    with transaction.atomic():
        variant.refresh_from_db()
        if (variant.metadata or {}).get(CLAIM_TOKEN_KEY) != claim_token or (
            variant.status != ContentPostVariant.Status.PUBLISHING
        ):
            logger.warning(
                "Discarding stale publish outcome: variant=%s status=%s",
                variant.pk,
                variant.status,
            )
            return {
                "success": variant.status == ContentPostVariant.Status.PUBLISHED,
                "publication_id": variant.publication_id or "",
                "publication_url": variant.publication_url or "",
                "error": variant.last_error or "",
                "error_type": variant.last_error_type or "",
                "error_at": variant.last_error_at,
                "reconciling": variant.status == ContentPostVariant.Status.RECONCILING,
                "status": variant.status,
            }
        variant.metadata = {
            k: v
            for k, v in (variant.metadata or {}).items()
            if k not in (CLAIM_IN_FLIGHT_KEY, CLAIM_TOKEN_KEY)
        }

        if result["success"]:
            variant.status = ContentPostVariant.Status.PUBLISHED
            variant.publication_id = result.get("publication_id", "")
            variant.publication_url = result.get("publication_url", "")
            variant.published_at = timezone.now()
            variant.last_error = ""
            variant.last_error_type = ""
            variant.last_error_at = None
            variant.processing_started_at = (
                None  # Clear processing clock on terminal state
            )
            variant.metadata = {}  # Clear processing metadata on success
        else:
            # Failed or reconciling
            if result.get("reconciling"):
                variant.status = ContentPostVariant.Status.RECONCILING
                variant.processing_started_at = None  # Clear on reconciliation
                # Keep provider evidence (e.g. the IG container id) for the operator.
                variant.metadata = {
                    **(variant.metadata or {}),
                    **result.get("metadata", {}),
                }
            elif result.get("metadata", {}).get("ig_container_id"):
                # Video container created; waiting for polling
                variant.status = ContentPostVariant.Status.PROCESSING
                # Track when processing began (for exhaustion detection)
                if not variant.processing_started_at:
                    variant.processing_started_at = timezone.now()
                # Preserve any existing metadata and merge in new provider data
                variant.metadata = {
                    **(variant.metadata or {}),
                    **result.get("metadata", {}),
                }
            else:
                variant.status = ContentPostVariant.Status.FAILED
                variant.processing_started_at = None  # Clear on failure

            variant.last_error = result.get("error", "Unknown error")
            variant.last_error_type = result.get(
                "error_type", ContentPostVariant.ErrorType.UNKNOWN
            )
            variant.last_error_at = result.get("error_at", timezone.now())

        variant.save(
            update_fields=[
                "status",
                "publication_id",
                "publication_url",
                "published_at",
                "last_error",
                "last_error_type",
                "last_error_at",
                "processing_started_at",
                "metadata",
            ]
        )

    refresh_parent_post(post)

    # Phase A3: Return the persisted variant state, not the temporary provider result.
    # This ensures callers always get authoritative DB state and can rely on it for
    # aggregating parent post outcomes. The variant has been reloaded and saved above.
    return {
        "success": variant.status == ContentPostVariant.Status.PUBLISHED,
        "publication_id": variant.publication_id or "",
        "publication_url": variant.publication_url or "",
        "error": variant.last_error or "",
        "error_type": variant.last_error_type or "",
        "error_at": variant.last_error_at,
        "reconciling": variant.status == ContentPostVariant.Status.RECONCILING,
        "status": variant.status,
    }


def _publish_variant_facebook(variant: ContentPostVariant, post: ContentPost) -> dict:
    """Publish a single variant to Facebook.

    Returns dict with success, publication_id, publication_url, error, error_type, error_at, reconciling.
    """
    result_tuple = _fb_provider_for_post(post)
    if result_tuple is None:
        return {
            "success": False,
            "publication_id": "",
            "publication_url": "",
            "error": FB_RECONNECT_MESSAGE
            if FacebookPageConnection.objects.filter(
                account_id=post.account_id,
                status=FacebookPageConnection.Status.EXPIRED,
            ).exists()
            else "Facebook Page not connected",
            "error_type": ContentPostVariant.ErrorType.PERMISSION,
            "error_at": timezone.now(),
            "reconciling": False,
        }

    provider, _page_id = result_tuple

    # Use variant's caption if available, fall back to post caption
    caption = variant.caption if variant.caption else post.caption_for("facebook")
    # Append hashtags (from post, not variant)
    tags = " ".join(f"#{t.lstrip('#')}" for t in post.hashtags)
    if tags:
        caption = f"{caption}\n\n{tags}".strip()

    # Use variant's media or fall back to post media
    url = (
        variant.media_urls[0]
        if variant.media_urls
        else (post.media_urls[0] if post.media_urls else "")
    )

    mt = post.media_type

    try:
        if mt == ContentPost.MediaType.TEXT:
            post_id = provider.create_text_post(caption)
        elif mt == ContentPost.MediaType.IMAGE:
            post_id = provider.create_photo_post(url, caption)
        elif mt in (ContentPost.MediaType.VIDEO, ContentPost.MediaType.REEL):
            post_id = provider.create_video_post(url, caption)
        else:
            post_id = provider.create_photo_post(url, caption)

        return {
            "success": True,
            "publication_id": post_id,
            "publication_url": f"https://facebook.com/{post_id}",  # Best-effort URL
            "error": "",
            "error_type": "",
            "error_at": None,
            "reconciling": False,
        }
    except FacebookPublishError as exc:
        return _provider_error_result(exc, post, "facebook")
    except Exception as exc:
        # Only FacebookPublishError is a definitive answer from Meta. Anything else raised by
        # the publish request (timeout, reset, unreadable response) may have been accepted
        # remotely, so the outcome is unknown and must be reconciled, never blindly retried.
        is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
        return {
            "success": False,
            "publication_id": "",
            "publication_url": "",
            "error": f"Publication outcome unknown: {exc}",
            "error_type": ContentPostVariant.ErrorType.TIMEOUT
            if is_timeout
            else ContentPostVariant.ErrorType.UNKNOWN,
            "error_at": timezone.now(),
            "reconciling": True,
        }


def _publish_variant_instagram(variant: ContentPostVariant, post: ContentPost) -> dict:
    """Publish a single variant to Instagram.

    Returns dict with success, publication_id, publication_url, error, error_type, error_at, reconciling.
    """
    provider = _ig_provider_for_post(post)
    if provider is None:
        return {
            "success": False,
            "publication_id": "",
            "publication_url": "",
            "error": IG_RECONNECT_MESSAGE
            if (ig_acct := _ig_account(post)) and ig_acct.token_expired
            else "Instagram not connected",
            "error_type": ContentPostVariant.ErrorType.PERMISSION,
            "error_at": timezone.now(),
            "reconciling": False,
        }

    ig = _ig_account(post)
    if not ig:
        return {
            "success": False,
            "publication_id": "",
            "publication_url": "",
            "error": "Instagram business account not found",
            "error_type": ContentPostVariant.ErrorType.PERMISSION,
            "error_at": timezone.now(),
            "reconciling": False,
        }

    ig_user_id = ig.instagram_business_account_id
    caption = variant.caption if variant.caption else post.caption_for("instagram")
    tags = " ".join(f"#{t.lstrip('#')}" for t in post.hashtags)
    if tags:
        caption = f"{caption}\n\n{tags}".strip()

    url = (
        variant.media_urls[0]
        if variant.media_urls
        else (post.media_urls[0] if post.media_urls else "")
    )
    mt = post.media_type

    try:
        if mt == ContentPost.MediaType.IMAGE:
            _validate_ig_image_ratio(url)
            container_id = provider.create_image_container(ig_user_id, url, caption)
            _wait_for_ig_container(provider, container_id)
            ig_media_id = _ig_publish_container(provider, ig_user_id, container_id)
            return {
                "success": True,
                "publication_id": ig_media_id,
                "publication_url": _ig_permalink(provider, ig_media_id),
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
                "metadata": {"ig_container_id": container_id},
            }

        if mt in (ContentPost.MediaType.VIDEO, ContentPost.MediaType.REEL):
            container_id = provider.create_video_container(ig_user_id, url, caption)
            # Videos are async - return with container ID for polling
            return {
                "success": False,  # Not fully published yet
                "publication_id": "",
                "publication_url": "",
                "error": "Video publishing in progress",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
                "metadata": {"ig_container_id": container_id, "status": "PROCESSING"},
            }

        if mt == ContentPost.MediaType.STORY:
            if url.lower().endswith((".mp4", ".mov")):
                container_id = provider.create_story_video_container(ig_user_id, url)
            else:
                container_id = provider.create_story_image_container(ig_user_id, url)
            _wait_for_ig_container(provider, container_id)
            ig_media_id = _ig_publish_container(provider, ig_user_id, container_id)
            return {
                "success": True,
                "publication_id": ig_media_id,
                "publication_url": "",  # Stories don't have public URLs
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
            }

        if mt == ContentPost.MediaType.CAROUSEL:
            children = []
            for item_url in variant.media_urls or post.media_urls:
                child_id = provider.create_carousel_item_container(ig_user_id, item_url)
                children.append(child_id)
            container_id = provider.create_carousel_container(
                ig_user_id, children, caption
            )
            _wait_for_ig_container(provider, container_id)
            ig_media_id = _ig_publish_container(provider, ig_user_id, container_id)
            return {
                "success": True,
                "publication_id": ig_media_id,
                "publication_url": _ig_permalink(provider, ig_media_id),
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
            }

        return {
            "success": False,
            "publication_id": "",
            "publication_url": "",
            "error": f"Unsupported media type for Instagram: {mt}",
            "error_type": ContentPostVariant.ErrorType.VALIDATION,
            "error_at": timezone.now(),
            "reconciling": False,
        }

    except _IGPublishOutcomeUnknown as exc:
        return {
            "success": False,
            "publication_id": "",
            "publication_url": "",
            "error": f"Publication outcome unknown: {exc}",
            "error_type": ContentPostVariant.ErrorType.UNKNOWN,
            "error_at": timezone.now(),
            "reconciling": True,
            "metadata": {"ig_container_id": exc.container_id},
        }
    except InstagramPublishError as exc:
        return _provider_error_result(exc, post, "instagram")
    except Exception as exc:
        error_msg = str(exc).lower()
        is_timeout = "timeout" in error_msg or "timed out" in error_msg

        return {
            "success": False,
            "publication_id": "",
            "publication_url": "",
            "error": str(exc),
            "error_type": ContentPostVariant.ErrorType.TIMEOUT
            if is_timeout
            else ContentPostVariant.ErrorType.UNKNOWN,
            "error_at": timezone.now(),
            "reconciling": is_timeout,
        }


def orchestrate_post_publication(post: ContentPost) -> tuple[str, list[dict]]:
    """Orchestrate per-variant publishing for a ContentPost (Step 4.3).

    Returns:
    - parent_status: aggregated post status (published, partial_success, failed, publishing, reconciling)
    - variant_outcomes: list of dicts with variant platform and outcome

    Requires variants to exist. A post without variants cannot be published through
    the orchestration path. This is the sole supported publishing architecture.
    """
    # Check if post has explicit variants
    variants = list(post.variants.all())

    if not variants:
        # Reject publication if no variants exist
        raise ValueError(
            f"Cannot publish post {post.pk}: no variants defined. "
            "Posts must have at least one ContentPostVariant before publishing."
        )

    # Publish each eligible variant
    outcomes = []
    for variant in variants:
        # Skip already published or cancelled variants
        if variant.status in (
            ContentPostVariant.Status.PUBLISHED,
            ContentPostVariant.Status.CANCELLED,
        ):
            outcomes.append(
                {
                    "platform": variant.platform,
                    "status": variant.status,
                    "publication_id": variant.publication_id,
                    "error": "",
                    "error_type": "",
                }
            )
            continue

        # Publish the variant
        result = publish_variant(variant)
        # Phase A3: Use persisted status from result, not stale variant object
        # publish_variant() returned the authoritative DB state after publication
        outcomes.append(
            {
                "platform": variant.platform,
                "status": result.get("status", variant.status),
                "publication_id": result.get("publication_id", ""),
                "error": result.get("error", ""),
                "error_type": result.get("error_type", ""),
            }
        )

    # Aggregate parent post outcome from variant outcomes
    parent_status = _aggregate_parent_status(post, outcomes)

    # Update post-level fields for backward compatibility
    refresh_parent_post(post)

    return parent_status, outcomes


def _aggregate_parent_status(post: ContentPost, outcomes: list[dict]) -> str:
    """Determine parent post status from variant outcomes.

    Policy: parent remains PUBLISHING while any variant is actively publishing, processing,
    or awaiting reconciliation. Only reaches PUBLISHED when all required variants succeed,
    or FAILED when all required variants fail. Partial success (one published, another failed)
    remains PUBLISHING to allow per-channel recovery.

    Statuses considered "nonterminal" (keep parent nonterminal):
    - PUBLISHING: actively sending to provider
    - PROCESSING: awaiting async provider completion (e.g., IG video encoding)
    - RECONCILING: ambiguous outcome pending investigation
    """
    if not outcomes:
        return ContentPost.Status.SCHEDULED

    # A cancelled channel is no longer required; it neither blocks nor fails the post.
    required = [
        o for o in outcomes if o["status"] != ContentPostVariant.Status.CANCELLED
    ]
    if not required:
        return ContentPost.Status.CANCELLED
    statuses = [o["status"] for o in required]

    # Any variant actively working or pending → parent stays nonterminal
    if any(
        s
        in (
            ContentPostVariant.Status.PUBLISHING,
            ContentPostVariant.Status.PROCESSING,
            ContentPostVariant.Status.RECONCILING,
        )
        for s in statuses
    ):
        return ContentPost.Status.PUBLISHING

    # Terminal states: count published, failed, cancelled, draft, scheduled
    published_count = sum(
        1 for s in statuses if s == ContentPostVariant.Status.PUBLISHED
    )
    failed_count = sum(1 for s in statuses if s == ContentPostVariant.Status.FAILED)
    cancelled_count = sum(
        1 for s in statuses if s == ContentPostVariant.Status.CANCELLED
    )
    draft_or_scheduled = sum(
        1
        for s in statuses
        if s
        in (
            ContentPostVariant.Status.DRAFT,
            ContentPostVariant.Status.SCHEDULED,
        )
    )

    # All variants are in draft or scheduled (no publication attempted)
    if draft_or_scheduled == len(statuses):
        return ContentPost.Status.SCHEDULED

    # All variants are cancelled
    if cancelled_count == len(statuses):
        return ContentPost.Status.CANCELLED

    # All required variants published (no failures or cancellations)
    if failed_count == 0 and cancelled_count == 0 and published_count > 0:
        return ContentPost.Status.PUBLISHED

    # Partial success: at least one published, at least one failed or cancelled
    if published_count > 0 and (failed_count > 0 or cancelled_count > 0):
        return ContentPost.Status.PUBLISHING

    # All required variants failed (none cancelled, none drafted)
    if failed_count > 0 and cancelled_count == 0 and draft_or_scheduled == 0:
        return ContentPost.Status.FAILED

    # Edge case: mixed draft/scheduled with failures (shouldn't happen in normal flow)
    return ContentPost.Status.PUBLISHING


def refresh_parent_post(post: ContentPost) -> None:
    """Derive the parent post's status and error from its variants and persist them.

    The single authority for parent status. Every transition that changes a variant
    calls this, so callers never apply their own parent-status rules.
    """
    variants = list(post.variants.all())
    outcomes = [
        {
            "status": v.status,
            "platform": v.platform,
            "error": v.last_error,
            "publication_id": v.publication_id,
        }
        for v in variants
    ]
    status = _aggregate_parent_status(post, outcomes)
    was_published = post.status == ContentPost.Status.PUBLISHED

    def _published_id(platform: str) -> str:
        return next(
            (
                v.publication_id
                for v in variants
                if v.platform == platform
                and v.status == ContentPostVariant.Status.PUBLISHED
            ),
            "",
        )

    # Only failures and unresolved outcomes are errors; PROCESSING is normal progress.
    errors = [
        f"{v.platform.title()}: {v.last_error}"
        for v in variants
        if v.status
        in (ContentPostVariant.Status.FAILED, ContentPostVariant.Status.RECONCILING)
        and v.last_error
    ]

    post.status = status
    post.fb_post_id = _published_id("facebook")
    post.ig_media_id = _published_id("instagram")
    post.error = "; ".join(errors)
    if status == ContentPost.Status.PUBLISHED and post.published_at is None:
        post.published_at = timezone.now()
    post.save(
        update_fields=["status", "fb_post_id", "ig_media_id", "error", "published_at"]
    )

    if status == ContentPost.Status.PUBLISHED and not was_published and post.fb_post_id:
        from functools import partial

        from django.db import transaction

        from .tasks import fetch_fb_post_insights

        # After commit, and robust: this often runs inside a reconciliation transaction,
        # and a broker error must not roll back the operator's action.
        transaction.on_commit(
            partial(fetch_fb_post_insights.apply_async, args=[post.pk], countdown=900),
            robust=True,
        )


# Step 4.7B: Reconciliation Operations
# ---------------------------------------------------------------------------


def reconcile_variant_confirmed_published(
    variant: ContentPostVariant,
    publication_id: str,
    publication_url: str,
    evidence_notes: str,
    performed_by,
) -> dict:
    """Mark a RECONCILING variant as PUBLISHED based on operator evidence.

    Requires:
    - publication_id from provider (confirmed in provider)
    - publication_url (where the content is published)
    - evidence_notes describing how operator confirmed (e.g., "Found in IG Insights")

    Guarantees:
    - Atomic state transition (prevents concurrent overwrites)
    - Audit log of who confirmed and when
    - Publication metadata persisted to variant and post
    - No republication (status directly to PUBLISHED)
    """
    from django.db import transaction

    from .models import ReconciliationLog

    if not publication_id or not publication_id.strip():
        raise ValueError("Publication ID is required to confirm publication")

    if variant.status != ContentPostVariant.Status.RECONCILING:
        raise ValueError(
            f"Can only confirm published for RECONCILING variants, found {variant.status}"
        )

    with transaction.atomic():
        # Reload to ensure state hasn't changed
        variant = ContentPostVariant.objects.select_for_update().get(pk=variant.pk)

        if variant.status != ContentPostVariant.Status.RECONCILING:
            raise ValueError("Variant state changed; operation aborted")

        # Transition to PUBLISHED atomically
        variant.status = ContentPostVariant.Status.PUBLISHED
        variant.publication_id = publication_id
        variant.publication_url = publication_url
        variant.published_at = timezone.now()
        variant.last_error = ""
        variant.last_error_type = ""
        drop_claim_flag(variant)
        variant.save(
            update_fields=[
                "status",
                "publication_id",
                "publication_url",
                "published_at",
                "last_error",
                "last_error_type",
                "metadata",
            ]
        )

        # Record audit log
        refresh_parent_post(variant.post)
        ReconciliationLog.objects.create(
            variant=variant,
            action=ReconciliationLog.Action.CONFIRMED_PUBLISHED,
            performed_by=performed_by,
            provider_publication_id=publication_id,
            provider_publication_url=publication_url,
            evidence_notes=evidence_notes,
        )

        # Update post-level fields for backward compatibility
        post = variant.post
        if variant.platform == "facebook":
            post.fb_post_id = publication_id
        elif variant.platform == "instagram":
            post.ig_media_id = publication_id
        post.save(update_fields=["fb_post_id", "ig_media_id"])

        logger.info(
            "Variant confirmed published: variant=%s platform=%s publication_id=%s by=%s",
            variant.pk,
            variant.platform,
            publication_id,
            performed_by,
        )

    return {
        "success": True,
        "variant_id": variant.pk,
        "status": "published",
        "publication_id": publication_id,
    }


def reconcile_variant_retry(variant: ContentPostVariant, performed_by) -> dict:
    """Reset RECONCILING variant to FAILED and allow retry.

    Used when operator determines the variant was not actually published
    and should be retried.

    Guarantees:
    - Atomic state transition
    - Audit log of retry action
    - Variant ready for manual retry via view
    """
    from django.db import transaction

    from .models import ReconciliationLog

    if variant.status != ContentPostVariant.Status.RECONCILING:
        raise ValueError(f"Can only retry from RECONCILING, found {variant.status}")

    with transaction.atomic():
        variant = ContentPostVariant.objects.select_for_update().get(pk=variant.pk)

        if variant.status != ContentPostVariant.Status.RECONCILING:
            raise ValueError("Variant state changed; operation aborted")

        variant.status = ContentPostVariant.Status.FAILED
        variant.last_error = (
            "Operator determined publication did not succeed; retry available"
        )
        variant.last_error_type = ContentPostVariant.ErrorType.UNKNOWN
        variant.last_error_at = timezone.now()
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

        refresh_parent_post(variant.post)
        ReconciliationLog.objects.create(
            variant=variant,
            action=ReconciliationLog.Action.RETRY,
            performed_by=performed_by,
        )

        logger.info(
            "Variant reconciliation retry initiated: variant=%s platform=%s by=%s",
            variant.pk,
            variant.platform,
            performed_by,
        )

    return {
        "success": True,
        "variant_id": variant.pk,
        "status": "failed",
        "message": "Variant reset to FAILED; ready for manual retry",
    }


def reconcile_variant_leave_unresolved(
    variant: ContentPostVariant,
    next_step: str,
    performed_by,
) -> dict:
    """Leave variant in RECONCILING with notes for next investigation step.

    Used when operator needs more time to investigate or awaits provider response.

    Guarantees:
    - Atomic state transition
    - Audit log of decision and next step
    - Variant remains in RECONCILING (visible in queue)
    - Decision is recorded for team visibility
    """
    from django.db import transaction

    from .models import ReconciliationLog

    if variant.status != ContentPostVariant.Status.RECONCILING:
        raise ValueError(
            f"Can only leave unresolved if RECONCILING, found {variant.status}"
        )

    with transaction.atomic():
        # Reload to ensure state hasn't changed
        variant = ContentPostVariant.objects.select_for_update().get(pk=variant.pk)

        if variant.status != ContentPostVariant.Status.RECONCILING:
            raise ValueError("Variant state changed; operation aborted")

        # Update last error to note the pending investigation
        variant.last_error = f"Awaiting investigation: {next_step}"
        variant.save(update_fields=["last_error"])

        refresh_parent_post(variant.post)
        ReconciliationLog.objects.create(
            variant=variant,
            action=ReconciliationLog.Action.LEFT_UNRESOLVED,
            performed_by=performed_by,
            next_investigative_step=next_step,
        )

    logger.info(
        "Variant left unresolved: variant=%s platform=%s next_step=%s by=%s",
        variant.pk,
        variant.platform,
        next_step,
        performed_by,
    )

    return {
        "success": True,
        "variant_id": variant.pk,
        "status": "reconciling",
        "message": f"Left unresolved. Next step: {next_step}",
    }
