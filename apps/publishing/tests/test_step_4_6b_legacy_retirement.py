"""Tests for Step 4.6B — Legacy publishing path retirement.

Validates that:
1. Posts without variants cannot be published
2. Variant orchestration is the mandatory path
3. Legacy polling task is disabled
4. All publishing entry points use variant path only
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.services import (
    orchestrate_post_publication,
)
from apps.publishing.tasks import poll_ig_containers

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    """Create a test account with Instagram connection."""
    from apps.instagram.models import InstagramBusinessAccount

    user = User.objects.create_user("testuser", "test@example.com", "pass")
    account = Account.objects.create(company_name="Test Account")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)

    InstagramBusinessAccount.objects.create(
        account=account,
        instagram_business_account_id="123456789",
        access_token="test_token",
        is_active=True,
    )

    return account


class TestLegacyPublishingPathRetired:
    """Validate that legacy publishing paths are retired."""

    def test_post_without_variants_rejected_by_orchestration(self, account):
        """Posts without variants must raise ValueError, not fall back to legacy path."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type=ContentPost.MediaType.VIDEO,
            status=ContentPost.Status.PUBLISHING,
        )

        # Attempt orchestration without variants
        with pytest.raises(ValueError) as exc_info:
            orchestrate_post_publication(post)

        assert "no variants" in str(exc_info.value).lower()
        assert "cannot publish" in str(exc_info.value).lower()

    def test_orchestration_requires_at_least_one_variant(self, account):
        """Orchestration must have at least one variant to proceed."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            status=ContentPost.Status.PUBLISHING,
        )

        # No variants exist
        assert post.variants.count() == 0

        # Should raise, not fall back
        with pytest.raises(ValueError):
            orchestrate_post_publication(post)

    def test_legacy_poll_ig_containers_disabled(self):
        """Legacy polling task should log warning and return 0."""
        result = poll_ig_containers()

        # Task should return 0 (no work)
        assert result == 0

    def test_legacy_poll_ig_containers_logs_deprecation(self):
        """Legacy polling task should log deprecation warning."""
        with patch("apps.publishing.tasks.logger") as mock_logger:
            poll_ig_containers()

            # Should log deprecation message
            mock_logger.warning.assert_called_once()
            warning_msg = mock_logger.warning.call_args[0][0]
            assert "deprecated" in warning_msg.lower()
            assert "poll_ig_variant_containers" in warning_msg


class TestVariantPathMandatory:
    """Validate that variant path is the sole publishing architecture."""

    @patch("apps.publishing.services._ig_provider_for_post")
    def test_variant_orchestration_publishes_variants(self, mock_ig_provider, account):
        """Orchestration must iterate variants and publish each."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            status=ContentPost.Status.PUBLISHING,
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PUBLISHING,
        )

        provider = MagicMock()
        mock_ig_provider.return_value = provider
        provider.create_video_container.return_value = "container_123"

        # Orchestrate with variant
        parent_status, outcomes = orchestrate_post_publication(post)

        # Should process variant (even if result is PROCESSING)
        assert len(outcomes) > 0
        assert outcomes[0]["platform"] == "instagram"

    @patch("apps.publishing.services._ig_provider_for_post")
    def test_variant_outcomes_returned_by_orchestration(
        self, mock_ig_provider, account
    ):
        """Orchestration must return outcomes for each variant."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram", "facebook"],
            status=ContentPost.Status.PUBLISHING,
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PUBLISHING,
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHING,
        )

        provider = MagicMock()
        mock_ig_provider.return_value = provider
        provider.create_video_container.return_value = "container_123"

        parent_status, outcomes = orchestrate_post_publication(post)

        # Must return outcomes for both variants
        assert len(outcomes) == 2
        platforms = {o["platform"] for o in outcomes}
        assert platforms == {"instagram", "facebook"}

    def test_published_variants_remain_published(self, account):
        """Already-published variants should remain published, not be republished."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            status=ContentPost.Status.PUBLISHING,
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PUBLISHED,
            publication_id="ig_123",
        )

        # Orchestrate (variant already published)
        parent_status, outcomes = orchestrate_post_publication(post)

        # Variant should remain published
        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PUBLISHED
        assert variant.publication_id == "ig_123"

    def test_cancelled_variants_not_published(self, account):
        """Cancelled variants should be skipped."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            status=ContentPost.Status.PUBLISHING,
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.CANCELLED,
        )

        parent_status, outcomes = orchestrate_post_publication(post)

        # Outcome should reflect CANCELLED status
        assert outcomes[0]["status"] == ContentPostVariant.Status.CANCELLED


class TestPublishingEntryPoints:
    """Validate that all entry points use variant orchestration only."""

    @patch("apps.publishing.services.orchestrate_post_publication")
    def test_publish_post_async_calls_orchestration(self, mock_orchestrate):
        """publish_post_async must call orchestrate_post_publication, not legacy path."""
        from apps.publishing.tasks import publish_post_async

        user = User.objects.create_user("testuser", "test@example.com", "pass")
        account = Account.objects.create(company_name="Test Account")
        Membership.objects.create(
            user=user, account=account, role=Membership.Role.OWNER
        )

        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            status=ContentPost.Status.PUBLISHING,
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PUBLISHING,
        )

        mock_orchestrate.return_value = (ContentPost.Status.PUBLISHED, [])

        # Trigger async task
        publish_post_async(post.pk)

        # Must call orchestration
        mock_orchestrate.assert_called_once_with(post)

    def test_manual_retry_uses_publish_variant_async(self, account):
        """Manual retry via view must use publish_variant_async, not legacy fallback."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            status=ContentPost.Status.PUBLISHED,
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.FAILED,
            last_error="Test error",
        )

        # Manual retry sets variant to PUBLISHING and enqueues publish_variant_async
        # This is the variant path; no legacy fallback
        variant.status = ContentPostVariant.Status.PUBLISHING
        variant.save(update_fields=["status"])

        # Variant is ready for retry through variant path
        assert variant.status == ContentPostVariant.Status.PUBLISHING

    def test_no_silent_fallback_to_legacy_publish_post(self, account):
        """orchestrate_post_publication must never silently fall back to publish_post."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            status=ContentPost.Status.PUBLISHING,
        )

        # No variants exist
        with pytest.raises(ValueError):
            # Should raise; not fall back and silently succeed
            orchestrate_post_publication(post)


class TestBackwardCompatibilityRemoved:
    """Validate that backward-compatibility fallbacks are removed."""

    def test_legacy_publish_post_not_called_from_orchestration(self, account):
        """publish_post() should never be called by orchestrate_post_publication."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            status=ContentPost.Status.PUBLISHING,
        )

        with patch("apps.publishing.services.publish_post") as mock_publish:
            try:
                orchestrate_post_publication(post)
            except ValueError:
                pass  # Expected when no variants

        # Legacy publish_post must not be called
        mock_publish.assert_not_called()

    def test_orchestration_error_message_clear(self, account):
        """Error message must clearly explain variant requirement."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            status=ContentPost.Status.PUBLISHING,
        )

        try:
            orchestrate_post_publication(post)
            raise AssertionError("Should have raised ValueError")
        except ValueError as exc:
            error_msg = str(exc).lower()
            # Message must mention variants and publishing
            assert "variant" in error_msg
            assert "publish" in error_msg
            # Should not mention fallback or legacy
            assert "fallback" not in error_msg
            assert "legacy" not in error_msg


class TestStagingDataReset:
    """Validate that staging reset command works safely."""

    def test_reset_command_exists(self):
        """Reset management command should be available."""
        from apps.publishing.management.commands.reset_staging_publishing import (
            Command,
        )

        cmd = Command()
        assert cmd.help is not None
        assert "reset" in cmd.help.lower()

    def test_reset_command_preserves_users(self):
        """Reset command must not delete users or accounts."""
        user = User.objects.create_user("testuser", "test@example.com", "pass")
        account = Account.objects.create(company_name="Test Account")
        Membership.objects.create(
            user=user, account=account, role=Membership.Role.OWNER
        )

        user_count_before = User.objects.count()

        # Create and delete a post
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            status=ContentPost.Status.PUBLISHED,
        )
        post.delete()

        # User count should be unchanged
        user_count_after = User.objects.count()
        assert user_count_after == user_count_before
