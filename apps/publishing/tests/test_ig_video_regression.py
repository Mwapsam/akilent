"""Tests for Instagram async video publishing regression (Step 4.6A).

Tests cover:
- Legacy path: videos stay PUBLISHING until polling completes
- Variant path: container ID persisted, variants in PROCESSING state
- Polling: containers complete via polling task
- Retry isolation: PROCESSING variants not retried
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.providers.instagram import (
    CONTAINER_STATUS_ERROR,
    CONTAINER_STATUS_FINISHED,
)
from apps.publishing.services import (
    publish_post,
    publish_variant,
    try_publish_ig_container,
    try_publish_ig_variant_container,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    """Create a test account with owner membership and Instagram connection."""
    from apps.instagram.models import InstagramBusinessAccount

    user = User.objects.create_user("testuser", "test@example.com", "pass")
    account = Account.objects.create(company_name="Test Account")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)

    # Create Instagram business account for the account
    InstagramBusinessAccount.objects.create(
        account=account,
        instagram_business_account_id="123456789",
        access_token="test_token",
        is_active=True,
    )

    return account


class TestLegacyVideoPath:
    """Test legacy publish_post() for Instagram videos."""

    @patch("apps.publishing.services._ig_provider_for_post")
    @patch("apps.publishing.services._publish_facebook")
    def test_video_stays_publishing_until_polled(
        self, mock_fb, mock_ig_provider, account
    ):
        """Instagram video should be PUBLISHING, not PUBLISHED, until polling completes."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type=ContentPost.MediaType.VIDEO,
            status=ContentPost.Status.PUBLISHING,
        )

        provider = MagicMock()

        provider.get_permalink.return_value = ""
        mock_ig_provider.return_value = provider
        provider.create_video_container.return_value = "container_123"

        # Publish the post
        publish_post(post)

        post.refresh_from_db()
        # ✅ Should be PUBLISHING, NOT PUBLISHED (awaiting polling)
        assert post.status == ContentPost.Status.PUBLISHING
        assert post.ig_container_id == "container_123"
        assert post.ig_media_id == ""

    @patch("apps.publishing.services._ig_provider_for_post")
    @patch("apps.publishing.services._publish_facebook")
    def test_image_published_immediately(self, mock_fb, mock_ig_provider, account):
        """Instagram image should be PUBLISHED immediately (sync path)."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type=ContentPost.MediaType.IMAGE,
            status=ContentPost.Status.PUBLISHING,
        )

        provider = MagicMock()

        provider.get_permalink.return_value = ""
        mock_ig_provider.return_value = provider
        provider.create_image_container.return_value = "container_img"
        provider.poll_container_status.return_value = CONTAINER_STATUS_FINISHED
        provider.publish_container.return_value = "ig_media_img"

        # Publish the post
        publish_post(post)

        post.refresh_from_db()
        # ✅ Should be PUBLISHED (sync flow completed)
        assert post.status == ContentPost.Status.PUBLISHED
        assert post.ig_media_id == "ig_media_img"


class TestVariantVideoPath:
    """Test variant-based video publishing."""

    @patch("apps.publishing.services._ig_provider_for_post")
    def test_variant_container_persisted_in_metadata(self, mock_ig_provider, account):
        """Video container ID should be persisted in variant.metadata."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type=ContentPost.MediaType.VIDEO,
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PUBLISHING,
        )

        provider = MagicMock()

        provider.get_permalink.return_value = ""
        mock_ig_provider.return_value = provider
        provider.create_video_container.return_value = "container_456"

        # Publish the variant
        publish_variant(variant)

        variant.refresh_from_db()
        # ✅ Should be PROCESSING (not FAILED)
        assert variant.status == ContentPostVariant.Status.PROCESSING
        # ✅ Container ID should be stored in metadata
        assert variant.metadata.get("ig_container_id") == "container_456"
        # ✅ No publication ID yet
        assert variant.publication_id == ""

    @patch("apps.publishing.services._ig_provider_for_post")
    def test_variant_video_error_type_handling(self, mock_ig_provider, account):
        """Video container create errors should set appropriate error_type."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type=ContentPost.MediaType.VIDEO,
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PUBLISHING,
        )

        provider = MagicMock()

        provider.get_permalink.return_value = ""
        mock_ig_provider.return_value = provider
        # Use a non-timeout error to test error handling
        provider.create_video_container.side_effect = Exception("Rate limit exceeded")

        # Attempt to publish
        publish_variant(variant)

        variant.refresh_from_db()
        # Should be FAILED (exception during container create, not async processing)
        assert variant.status == ContentPostVariant.Status.FAILED
        assert "Rate limit exceeded" in variant.last_error


class TestVariantPolling:
    """Test polling of in-progress variant containers."""

    @patch("apps.publishing.services._ig_provider_for_post")
    def test_poll_completes_video_variant(self, mock_ig_provider, account):
        """Polling a PROCESSING variant with finished container publishes it."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type=ContentPost.MediaType.VIDEO,
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            metadata={"ig_container_id": "container_789"},
        )

        provider = MagicMock()

        provider.get_permalink.return_value = "https://www.instagram.com/p/REAL789/"
        mock_ig_provider.return_value = provider
        provider.poll_container_status.return_value = CONTAINER_STATUS_FINISHED
        provider.publish_container.return_value = "ig_media_789"

        # Poll and publish
        done = try_publish_ig_variant_container(variant)

        variant.refresh_from_db()
        # ✅ Should be PUBLISHED
        assert variant.status == ContentPostVariant.Status.PUBLISHED
        assert variant.publication_id == "ig_media_789"
        assert variant.publication_url == "https://www.instagram.com/p/REAL789/"
        assert done is True

    @patch("apps.publishing.services._ig_provider_for_post")
    def test_poll_marks_failed_on_container_error(self, mock_ig_provider, account):
        """Container ERROR status marks variant as FAILED."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type=ContentPost.MediaType.VIDEO,
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            metadata={"ig_container_id": "container_fail"},
        )

        provider = MagicMock()

        provider.get_permalink.return_value = ""
        mock_ig_provider.return_value = provider
        provider.poll_container_status.return_value = CONTAINER_STATUS_ERROR

        # Poll
        done = try_publish_ig_variant_container(variant)

        variant.refresh_from_db()
        # ✅ Should be FAILED
        assert variant.status == ContentPostVariant.Status.FAILED
        assert "container error" in variant.last_error.lower()
        assert done is False

    @patch("apps.publishing.services._ig_provider_for_post")
    def test_poll_keeps_processing_while_container_builds(
        self, mock_ig_provider, account
    ):
        """Container still processing should remain PROCESSING."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type=ContentPost.MediaType.VIDEO,
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            metadata={"ig_container_id": "container_slow"},
        )

        provider = MagicMock()

        provider.get_permalink.return_value = ""
        mock_ig_provider.return_value = provider
        # First poll: still processing (not FINISHED, not ERROR)
        provider.poll_container_status.return_value = "PROCESSING"

        # First poll attempt
        done = try_publish_ig_variant_container(variant)

        variant.refresh_from_db()
        # ✅ Should remain PROCESSING
        assert variant.status == ContentPostVariant.Status.PROCESSING
        assert done is False

        # Second poll: finished
        provider.poll_container_status.return_value = CONTAINER_STATUS_FINISHED
        provider.publish_container.return_value = "ig_media_456"

        done = try_publish_ig_variant_container(variant)

        variant.refresh_from_db()
        # ✅ Now PUBLISHED
        assert variant.status == ContentPostVariant.Status.PUBLISHED
        assert done is True

    @patch("apps.publishing.services._ig_provider_for_post")
    def test_poll_reconciles_on_final_publish_timeout(self, mock_ig_provider, account):
        """Final publication timeout enters RECONCILING."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type=ContentPost.MediaType.VIDEO,
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            metadata={"ig_container_id": "container_timeout"},
        )

        provider = MagicMock()

        provider.get_permalink.return_value = ""
        mock_ig_provider.return_value = provider
        provider.poll_container_status.return_value = CONTAINER_STATUS_FINISHED
        provider.publish_container.side_effect = Exception("Request timeout")

        # Poll with timeout on final publish
        done = try_publish_ig_variant_container(variant)

        variant.refresh_from_db()
        # ✅ Should be RECONCILING (ambiguous outcome)
        assert variant.status == ContentPostVariant.Status.RECONCILING
        # Error message should mention timeout or publication timing out
        assert (
            "timeout" in variant.last_error.lower()
            or "timed out" in variant.last_error.lower()
        )
        assert done is False


class TestRetryIsolation:
    """Test that PROCESSING variants are not retried."""

    def test_processing_variant_not_published_again(self, account):
        """Variant in PROCESSING state should not be republished."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type=ContentPost.MediaType.VIDEO,
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            metadata={"ig_container_id": "container_retry"},
        )

        # Attempt to publish variant that's already processing
        result = publish_variant(variant)

        # ✅ Should fail because variant is in PROCESSING state
        assert result["success"] is False
        assert (
            "already" in result["error"].lower()
            or "processing" in result["error"].lower()
        )


class TestBackwardCompatibility:
    """Test that existing publishing still works."""

    @patch("apps.publishing.services._ig_provider_for_post")
    @patch("apps.publishing.services._publish_facebook")
    def test_facebook_unaffected_by_video_changes(self, mock_fb, mock_ig, account):
        """Facebook publishing should be unaffected."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            media_type=ContentPost.MediaType.IMAGE,
            status=ContentPost.Status.PUBLISHING,
        )

        MagicMock()
        mock_fb.side_effect = lambda p: setattr(p, "fb_post_id", "fb_123")

        # Publish
        publish_post(post)

        post.refresh_from_db()
        # ✅ Should be PUBLISHED (Facebook has no async flow)
        assert post.status == ContentPost.Status.PUBLISHED
        assert post.fb_post_id == "fb_123"

    @patch("apps.publishing.services._ig_provider_for_post")
    def test_legacy_polling_still_works(self, mock_ig_provider, account):
        """Legacy try_publish_ig_container should still work."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type=ContentPost.MediaType.VIDEO,
            status=ContentPost.Status.PUBLISHING,
            ig_container_id="container_legacy",
        )

        provider = MagicMock()

        provider.get_permalink.return_value = ""
        mock_ig_provider.return_value = provider
        provider.poll_container_status.return_value = CONTAINER_STATUS_FINISHED
        provider.publish_container.return_value = "ig_media_legacy"

        # Poll legacy post
        done = try_publish_ig_container(post)

        post.refresh_from_db()
        # ✅ Should be PUBLISHED
        assert post.status == ContentPost.Status.PUBLISHED
        assert post.ig_media_id == "ig_media_legacy"
        assert done is True
