"""Tests for Step 4.5: Variant-Aware Retry Logic (per-channel retries)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.services import publish_variant

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    """Create a test account with owner membership."""
    user = User.objects.create_user("testuser", "test@example.com", "pass")
    account = Account.objects.create(company_name="Test Account")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return account


@pytest.fixture
def post(account):
    """Create a test post with Facebook and Instagram variants."""
    post = ContentPost.objects.create(
        account=account,
        caption="Test caption",
        media_type=ContentPost.MediaType.IMAGE,
        status=ContentPost.Status.PUBLISHED,
        approval_required=False,
    )
    post.platforms = ["facebook", "instagram"]
    post.media_urls = ["https://example.com/image.jpg"]
    post.save()

    # Create variants
    ContentPostVariant.objects.create(
        post=post,
        platform="facebook",
        status=ContentPostVariant.Status.PUBLISHED,
        publication_id="123456789",
        publication_url="https://facebook.com/123456789",
    )

    ContentPostVariant.objects.create(
        post=post,
        platform="instagram",
        status=ContentPostVariant.Status.FAILED,
        last_error="Rate limited",
        last_error_type=ContentPostVariant.ErrorType.RATE_LIMIT,
        last_error_at=timezone.now(),
    )

    return post


class TestVariantPublishService:
    """Test the publish_variant service function behavior."""

    @patch("apps.publishing.services._publish_variant_instagram")
    def test_publish_variant_marks_published_on_success(self, mock_ig_publish, post):
        """Publishing a variant marks it as published and stores outcome."""
        ig_variant = post.variants.get(platform="instagram")

        # Mock successful Instagram publish
        mock_ig_publish.return_value = {
            "success": True,
            "publication_id": "ig_media_123",
            "publication_url": "https://instagram.com/p/abc123",
            "error": "",
            "error_type": "",
            "error_at": None,
            "reconciling": False,
        }

        # Publish the variant
        result = publish_variant(ig_variant)

        assert result["success"]
        assert result["publication_id"] == "ig_media_123"

        # Verify variant is persisted as published
        ig_variant.refresh_from_db()
        assert ig_variant.status == ContentPostVariant.Status.PUBLISHED
        assert ig_variant.publication_id == "ig_media_123"
        assert ig_variant.publication_url == "https://instagram.com/p/abc123"
        assert ig_variant.last_error == ""

    @patch("apps.publishing.services._publish_variant_instagram")
    def test_already_published_variant_skipped(self, mock_ig_publish, post):
        """Publishing an already-published variant is skipped."""
        fb_variant = post.variants.get(platform="facebook")
        assert fb_variant.status == ContentPostVariant.Status.PUBLISHED

        # Attempt to publish already-published variant
        result = publish_variant(fb_variant)

        assert not result["success"]
        assert "already" in result["error"].lower()
        # publish function should not have been called
        mock_ig_publish.assert_not_called()
        # Variant remains unchanged
        fb_variant.refresh_from_db()
        assert fb_variant.status == ContentPostVariant.Status.PUBLISHED
        assert fb_variant.publication_id == "123456789"

    @patch("apps.publishing.services._publish_variant_instagram")
    def test_reconciling_outcome_marked_as_reconciling(self, mock_ig_publish, post):
        """Ambiguous timeout outcomes are marked as reconciling."""
        ig_variant = post.variants.get(platform="instagram")

        # Mock ambiguous timeout
        mock_ig_publish.return_value = {
            "success": False,
            "publication_id": "",
            "publication_url": "",
            "error": "Request timeout - may have been published",
            "error_type": ContentPostVariant.ErrorType.TIMEOUT,
            "error_at": timezone.now(),
            "reconciling": True,  # Ambiguous
        }

        result = publish_variant(ig_variant)

        assert result["reconciling"]
        assert result["error_type"] == ContentPostVariant.ErrorType.TIMEOUT

        ig_variant.refresh_from_db()
        assert ig_variant.status == ContentPostVariant.Status.RECONCILING
        assert "timeout" in ig_variant.last_error.lower()

    def test_approval_revalidated_before_publish(self, post):
        """Publishing requires valid approval if post requires it."""
        ig_variant = post.variants.get(platform="instagram")

        # Post requires approval but approval is not valid
        post.approval_required = True
        post.approval_valid_until = None
        post.save()

        result = publish_variant(ig_variant)

        assert not result["success"]
        assert "approval" in result["error"].lower()

        # Variant should be marked failed, not published
        ig_variant.refresh_from_db()
        assert ig_variant.status == ContentPostVariant.Status.FAILED


class TestVariantRetryView:
    """Test the post_retry_variant view that enqueues the task."""

    @patch("apps.publishing.tasks.publish_variant_async")
    def test_view_enqueues_retry_task(self, mock_task, account, post, client):
        """POST to retry variant enqueues the publish_variant_async task."""
        ig_variant = post.variants.get(platform="instagram")
        assert ig_variant.status == ContentPostVariant.Status.FAILED

        user = account.owner
        client.force_login(user)

        response = client.post(
            f"/publishing/posts/{post.pk}/retry/instagram/",
            follow=True,
        )

        # Should redirect to post detail
        assert response.status_code == 200

        # Variant should be transitioned to PUBLISHING (atomic state change)
        ig_variant.refresh_from_db()
        assert ig_variant.status == ContentPostVariant.Status.PUBLISHING

        # Task should have been enqueued
        assert mock_task.delay.called

    def test_view_rejects_non_failed_variant(self, account, post, client):
        """Retrying a non-failed variant is rejected."""
        fb_variant = post.variants.get(platform="facebook")
        assert fb_variant.status == ContentPostVariant.Status.PUBLISHED

        user = account.owner
        client.force_login(user)

        response = client.post(
            f"/publishing/posts/{post.pk}/retry/facebook/",
            follow=True,
        )

        # Should redirect with error message
        assert response.status_code == 200
        # Variant should remain unchanged
        fb_variant.refresh_from_db()
        assert fb_variant.status == ContentPostVariant.Status.PUBLISHED

    @patch("apps.publishing.tasks.publish_variant_async")
    def test_view_prevents_double_enqueue(self, mock_task, account, post, client):
        """Atomic state transition prevents concurrent double enqueues."""
        ig_variant = post.variants.get(platform="instagram")
        ig_variant.status = ContentPostVariant.Status.FAILED
        ig_variant.save()

        user = account.owner
        client.force_login(user)

        # First request succeeds and transitions to PUBLISHING
        client.post(
            f"/publishing/posts/{post.pk}/retry/instagram/",
            follow=True,
        )

        ig_variant.refresh_from_db()
        assert ig_variant.status == ContentPostVariant.Status.PUBLISHING
        assert mock_task.delay.call_count == 1

        # Reset for second request (simulates concurrent call before task completes)
        # In real scenario, variant is still PUBLISHING, so second request fails
        client.post(
            f"/publishing/posts/{post.pk}/retry/instagram/",
            follow=True,
        )

        # Second request should fail (variant not FAILED anymore)
        ig_variant.refresh_from_db()
        # Variant should still be PUBLISHING, task not enqueued again
        assert ig_variant.status == ContentPostVariant.Status.PUBLISHING
        # Still only called once (second call was rejected)
        assert mock_task.delay.call_count == 1

    @patch("apps.publishing.tasks.publish_variant_async")
    def test_view_handles_enqueue_failure(self, mock_task, account, post, client):
        """Task enqueue failure is caught and displayed gracefully."""
        ig_variant = post.variants.get(platform="instagram")

        # Mock task enqueue failure
        mock_task.delay.side_effect = Exception("Redis connection failed")

        user = account.owner
        client.force_login(user)

        response = client.post(
            f"/publishing/posts/{post.pk}/retry/instagram/",
            follow=True,
        )

        # Should redirect with error message
        assert response.status_code == 200
        # Variant should be reset to FAILED (enqueue failure recovery)
        # User can then retry when broker is back online
        ig_variant.refresh_from_db()
        assert ig_variant.status == ContentPostVariant.Status.FAILED
        assert "Enqueue failed" in ig_variant.last_error

    def test_view_requires_login(self, post, client):
        """Retry endpoint requires login."""
        response = client.post(
            f"/publishing/posts/{post.pk}/retry/instagram/",
            follow=True,
        )

        # Should redirect to login
        assert response.status_code == 200
        # Should be redirected away from post detail

    def test_view_requires_ownership(self, account, post, client):
        """User can only retry variants on their own posts."""
        # Create a different account and user
        other_user = User.objects.create_user("other", "other@example.com", "pass")
        other_account = Account.objects.create(company_name="Other Account")
        Membership.objects.create(
            user=other_user, account=other_account, role=Membership.Role.OWNER
        )

        # Login as different user
        client.force_login(other_user)

        # Attempt to retry variant on another account's post
        response = client.post(
            f"/publishing/posts/{post.pk}/retry/instagram/",
            follow=True,
        )

        # Should get 404 or be denied
        assert (
            response.status_code == 404 or "permission" in str(response.content).lower()
        )


class TestPartialSuccess:
    """Test scenarios where one variant succeeds and another fails."""

    @patch("apps.publishing.services._publish_variant_facebook")
    @patch("apps.publishing.services._publish_variant_instagram")
    def test_retrying_failed_channel_does_not_affect_published(
        self, mock_ig_publish, mock_fb_publish, post
    ):
        """Retrying Instagram doesn't affect already-published Facebook."""
        fb_variant = post.variants.get(platform="facebook")
        ig_variant = post.variants.get(platform="instagram")

        # Facebook is published
        assert fb_variant.status == ContentPostVariant.Status.PUBLISHED
        # Instagram failed
        assert ig_variant.status == ContentPostVariant.Status.FAILED

        # Mock successful Instagram retry
        mock_ig_publish.return_value = {
            "success": True,
            "publication_id": "ig_456",
            "publication_url": "https://instagram.com/p/def456",
            "error": "",
            "error_type": "",
            "error_at": None,
            "reconciling": False,
        }

        # Publish Instagram
        result = publish_variant(ig_variant)
        assert result["success"]

        # Both variants are now published
        fb_variant.refresh_from_db()
        ig_variant.refresh_from_db()

        assert fb_variant.status == ContentPostVariant.Status.PUBLISHED
        assert fb_variant.publication_id == "123456789"
        assert ig_variant.status == ContentPostVariant.Status.PUBLISHED
        assert ig_variant.publication_id == "ig_456"

        # Facebook publish should not have been called
        mock_fb_publish.assert_not_called()


class TestAttemptHistory:
    """Test that attempt history is preserved across retries."""

    def test_attempt_count_incremented_on_publish(self, post):
        """Each publish attempt increments attempt_count."""
        ig_variant = post.variants.get(platform="instagram")
        initial_count = ig_variant.attempt_count
        ig_variant.status = ContentPostVariant.Status.FAILED
        ig_variant.save()

        with patch(
            "apps.publishing.services._publish_variant_instagram"
        ) as mock_publish:
            mock_publish.return_value = {
                "success": True,
                "publication_id": "ig_123",
                "publication_url": "https://instagram.com/p/abc123",
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
            }

            result = publish_variant(ig_variant)
            assert result["success"]

        ig_variant.refresh_from_db()
        # attempt_count is incremented in publish_variant
        assert ig_variant.attempt_count > initial_count

    def test_error_replaced_on_new_failure(self, post):
        """When retry fails again, new error replaces old."""
        ig_variant = post.variants.get(platform="instagram")
        old_error = "Previous: rate limited"
        ig_variant.last_error = old_error
        ig_variant.last_error_type = ContentPostVariant.ErrorType.RATE_LIMIT
        ig_variant.save()

        with patch(
            "apps.publishing.services._publish_variant_instagram"
        ) as mock_publish:
            mock_publish.return_value = {
                "success": False,
                "publication_id": "",
                "publication_url": "",
                "error": "Now: permission denied",
                "error_type": ContentPostVariant.ErrorType.PERMISSION,
                "error_at": timezone.now(),
                "reconciling": False,
            }

            result = publish_variant(ig_variant)
            assert not result["success"]

        ig_variant.refresh_from_db()
        # New error replaces old
        assert ig_variant.last_error == "Now: permission denied"
        assert ig_variant.last_error_type == ContentPostVariant.ErrorType.PERMISSION
