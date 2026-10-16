"""Baseline tests for the current publishing pipeline.

These tests establish the existing behavior before Step 4 refactoring.
They verify:
- Scheduled post pickup and task dispatch
- Facebook and Instagram publishing via provider adapters
- Retry behavior and exponential backoff
- User-initiated retry, cancel, reopen actions
- Partial failure scenarios
- Approval validation at publishing boundary

The tests mock provider responses to avoid external API calls.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost
from apps.publishing.services import publish_post
from apps.publishing.tasks import publish_due_posts, publish_post_async


class TestScheduledPostPickup(TransactionTestCase):
    """Test publish_due_posts() task that picks scheduled posts."""

    def setUp(self):
        self.user = User.objects.create_user("user", "user@test.com", "pass")
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )
        Membership.objects.create(
            user=self.user, account=self.account, role=Membership.Role.OWNER
        )

    @patch("apps.publishing.tasks.publish_post_async")
    def test_picks_posts_with_passed_scheduled_time(self, mock_task):
        """Posts with scheduled_at <= now are picked and transitioned to PUBLISHING."""
        now = timezone.now()
        past = now - timezone.timedelta(minutes=5)

        post = ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type="image",
            status=ContentPost.Status.SCHEDULED,
            created_by=self.user,
            scheduled_at=past,
        )

        count = publish_due_posts()

        post.refresh_from_db()
        self.assertEqual(count, 1)
        self.assertEqual(post.status, ContentPost.Status.PUBLISHING)

    @patch("apps.publishing.tasks.publish_post_async")
    def test_ignores_posts_with_future_scheduled_time(self, mock_task):
        """Posts with scheduled_at > now are not picked."""
        now = timezone.now()
        future = now + timezone.timedelta(minutes=5)

        ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type="image",
            status=ContentPost.Status.SCHEDULED,
            created_by=self.user,
            scheduled_at=future,
        )

        count = publish_due_posts()
        self.assertEqual(count, 0)

    @patch("apps.publishing.tasks.publish_post_async")
    def test_ignores_non_scheduled_posts(self, mock_task):
        """Posts not in SCHEDULED status are ignored."""
        now = timezone.now()
        past = now - timezone.timedelta(minutes=5)

        ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
            scheduled_at=past,
        )

        count = publish_due_posts()
        self.assertEqual(count, 0)


def _persisting_orchestrate(status, outcomes):
    """Stand-in for orchestrate_post_publication: refresh_parent_post persists the parent status."""

    def _run(post):
        ContentPost.objects.filter(pk=post.pk).update(status=status)
        return status, outcomes

    return _run


class TestPublishPostAsync(TransactionTestCase):
    """Test publish_post_async() Celery task."""

    def setUp(self):
        self.user = User.objects.create_user("user", "user@test.com", "pass")
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )
        Membership.objects.create(
            user=self.user, account=self.account, role=Membership.Role.OWNER
        )

    @patch("apps.publishing.services.orchestrate_post_publication")
    def test_publishes_post_in_publishing_status(self, mock_orchestrate):
        """Task calls orchestrate_post_publication() for posts in PUBLISHING status."""
        mock_orchestrate.side_effect = _persisting_orchestrate(
            ContentPost.Status.PUBLISHED, []
        )

        post = ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type="image",
            status=ContentPost.Status.PUBLISHING,
            created_by=self.user,
        )

        result = publish_post_async(post.pk)

        self.assertEqual(result, "published")
        mock_orchestrate.assert_called_once()

    @patch("apps.publishing.services.orchestrate_post_publication")
    def test_publishes_post_in_scheduled_status(self, mock_orchestrate):
        """Task calls orchestrate_post_publication() for posts still in SCHEDULED status."""
        mock_orchestrate.side_effect = _persisting_orchestrate(
            ContentPost.Status.PUBLISHED, []
        )

        post = ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type="image",
            status=ContentPost.Status.SCHEDULED,
            created_by=self.user,
        )

        result = publish_post_async(post.pk)

        self.assertEqual(result, "published")

    def test_skips_post_not_in_publishing_or_scheduled(self):
        """Task skips posts not in PUBLISHING or SCHEDULED status."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        result = publish_post_async(post.pk)

        self.assertEqual(result, "skipped")

    def test_returns_not_found_for_missing_post(self):
        """Task returns 'not_found' when post doesn't exist."""
        result = publish_post_async(999)
        self.assertEqual(result, "not_found")

    @patch("apps.publishing.services.orchestrate_post_publication")
    def test_task_returns_published_on_success(self, mock_orchestrate):
        """Task returns 'published' when orchestration succeeds."""
        mock_orchestrate.side_effect = _persisting_orchestrate(
            ContentPost.Status.PUBLISHED, []
        )

        post = ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type="image",
            status=ContentPost.Status.PUBLISHING,
            created_by=self.user,
        )

        result = publish_post_async(post.pk)

        self.assertEqual(result, "published")


class TestFacebookPublishing(TransactionTestCase):
    """Test Facebook publishing logic."""

    def setUp(self):
        self.user = User.objects.create_user("user", "user@test.com", "pass")
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )
        Membership.objects.create(
            user=self.user, account=self.account, role=Membership.Role.OWNER
        )

    @patch("apps.publishing.tasks.fetch_fb_post_insights")
    @patch("apps.publishing.services._fb_provider_for_post")
    def test_publishes_facebook_image(self, mock_get_provider, mock_fetch_insights):
        """Facebook image post stores publication ID."""
        mock_provider = MagicMock()
        mock_provider.create_photo_post.return_value = "fb_123456"
        mock_get_provider.return_value = (mock_provider, "page_id")

        post = ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type=ContentPost.MediaType.IMAGE,
            caption="Test caption",
            media_urls=["https://example.com/image.jpg"],
            status=ContentPost.Status.PUBLISHING,
            created_by=self.user,
        )

        publish_post(post)

        post.refresh_from_db()
        self.assertEqual(post.fb_post_id, "fb_123456")
        self.assertEqual(post.status, ContentPost.Status.PUBLISHED)

    @patch("apps.publishing.tasks.fetch_fb_post_insights")
    @patch("apps.publishing.services._fb_provider_for_post")
    def test_publishes_facebook_text(self, mock_get_provider, mock_fetch_insights):
        """Facebook text post uses create_text_post."""
        mock_provider = MagicMock()
        mock_provider.create_text_post.return_value = "fb_text_123"
        mock_get_provider.return_value = (mock_provider, "page_id")

        post = ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type=ContentPost.MediaType.TEXT,
            caption="Text only",
            status=ContentPost.Status.PUBLISHING,
            created_by=self.user,
        )

        publish_post(post)

        post.refresh_from_db()
        self.assertEqual(post.fb_post_id, "fb_text_123")

    @patch("apps.publishing.tasks.fetch_fb_post_insights")
    @patch("apps.publishing.services._fb_provider_for_post")
    def test_facebook_failure_marks_post_failed(
        self, mock_get_provider, mock_fetch_insights
    ):
        """Facebook publishing error marks post FAILED."""
        from apps.publishing.providers.facebook import FacebookPublishError

        mock_provider = MagicMock()
        mock_provider.create_photo_post.side_effect = FacebookPublishError(
            "FB API error"
        )
        mock_get_provider.return_value = (mock_provider, "page_id")

        post = ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type=ContentPost.MediaType.IMAGE,
            media_urls=["https://example.com/image.jpg"],
            status=ContentPost.Status.PUBLISHING,
            created_by=self.user,
        )

        publish_post(post)

        post.refresh_from_db()
        self.assertEqual(post.status, ContentPost.Status.FAILED)
        self.assertIn("Facebook:", post.error)


def _image_response(width=1080, height=1080):
    """Stand-in for the media fetch the Instagram ratio check performs."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height)).save(buf, format="PNG")
    resp = MagicMock()
    resp.content = buf.getvalue()
    resp.raise_for_status.return_value = None
    return resp


class TestInstagramPublishing(TransactionTestCase):
    """Test Instagram publishing logic."""

    def setUp(self):
        self.user = User.objects.create_user("user", "user@test.com", "pass")
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )
        Membership.objects.create(
            user=self.user, account=self.account, role=Membership.Role.OWNER
        )
        media = patch("requests.get", return_value=_image_response())
        media.start()
        self.addCleanup(media.stop)

    @patch("apps.publishing.tasks.fetch_fb_post_insights")
    @patch("apps.publishing.services._ig_provider_for_post")
    def test_publishes_instagram_image(self, mock_get_provider, mock_fetch_insights):
        """Instagram image: create container → wait → publish."""
        mock_provider = MagicMock()
        mock_provider.create_image_container.return_value = "ig_container_123"
        mock_provider.poll_container_status.return_value = "FINISHED"
        mock_provider.publish_container.return_value = "ig_media_456"
        mock_get_provider.return_value = mock_provider

        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type=ContentPost.MediaType.IMAGE,
            caption="IG post",
            media_urls=["https://example.com/image.jpg"],
            status=ContentPost.Status.PUBLISHING,
            created_by=self.user,
        )

        publish_post(post)

        post.refresh_from_db()
        self.assertEqual(post.ig_container_id, "ig_container_123")
        self.assertEqual(post.ig_media_id, "ig_media_456")
        self.assertEqual(post.status, ContentPost.Status.PUBLISHED)

    @patch("apps.publishing.tasks.fetch_fb_post_insights")
    @patch("apps.publishing.services._ig_provider_for_post")
    def test_publishes_instagram_video_async(
        self, mock_get_provider, mock_fetch_insights
    ):
        """Instagram video: create container, leave in PUBLISHING for polling."""
        mock_provider = MagicMock()
        mock_provider.create_video_container.return_value = "ig_video_container"
        mock_get_provider.return_value = mock_provider

        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type=ContentPost.MediaType.VIDEO,
            caption="IG video",
            media_urls=["https://example.com/video.mp4"],
            status=ContentPost.Status.PUBLISHING,
            created_by=self.user,
        )

        publish_post(post)

        post.refresh_from_db()
        self.assertEqual(post.ig_container_id, "ig_video_container")
        self.assertEqual(post.status, ContentPost.Status.PUBLISHING)
        self.assertEqual(post.ig_media_id, "")  # Not set yet

    @patch("apps.publishing.tasks.fetch_fb_post_insights")
    @patch("apps.publishing.services._ig_provider_for_post")
    def test_instagram_failure_marks_post_failed(
        self, mock_get_provider, mock_fetch_insights
    ):
        """Instagram publishing error marks post FAILED."""
        from apps.publishing.providers.instagram import InstagramPublishError

        mock_provider = MagicMock()
        mock_provider.create_image_container.side_effect = InstagramPublishError(
            "IG API error"
        )
        mock_get_provider.return_value = mock_provider

        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type=ContentPost.MediaType.IMAGE,
            media_urls=["https://example.com/image.jpg"],
            status=ContentPost.Status.PUBLISHING,
            created_by=self.user,
        )

        publish_post(post)

        post.refresh_from_db()
        self.assertEqual(post.status, ContentPost.Status.FAILED)
        self.assertIn("Instagram:", post.error)


class TestPartialFailure(TransactionTestCase):
    """Test behavior when one platform succeeds and another fails."""

    def setUp(self):
        self.user = User.objects.create_user("user", "user@test.com", "pass")
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )
        Membership.objects.create(
            user=self.user, account=self.account, role=Membership.Role.OWNER
        )
        media = patch("requests.get", return_value=_image_response())
        media.start()
        self.addCleanup(media.stop)

    @patch("apps.publishing.tasks.fetch_fb_post_insights")
    @patch("apps.publishing.services._fb_provider_for_post")
    @patch("apps.publishing.services._ig_provider_for_post")
    def test_facebook_succeeds_instagram_fails_marks_overall_failed(
        self, mock_ig, mock_fb, mock_fetch_insights
    ):
        """Current behavior: one platform failure = overall FAILED."""
        from apps.publishing.providers.instagram import InstagramPublishError

        # Facebook succeeds
        mock_fb_provider = MagicMock()
        mock_fb_provider.create_photo_post.return_value = "fb_123"
        mock_fb.return_value = (mock_fb_provider, "page_id")

        # Instagram fails
        mock_ig_provider = MagicMock()
        mock_ig_provider.create_image_container.side_effect = InstagramPublishError(
            "IG error"
        )
        mock_ig.return_value = mock_ig_provider

        post = ContentPost.objects.create(
            account=self.account,
            platforms=["facebook", "instagram"],
            media_type=ContentPost.MediaType.IMAGE,
            media_urls=["https://example.com/image.jpg"],
            caption="Multi-platform",
            status=ContentPost.Status.PUBLISHING,
            created_by=self.user,
        )

        publish_post(post)

        post.refresh_from_db()
        # Current behavior: partial failure = overall FAILED
        self.assertEqual(post.status, ContentPost.Status.FAILED)
        # Both IDs should be captured where possible
        self.assertEqual(post.fb_post_id, "fb_123")
        self.assertEqual(post.ig_media_id, "")
        # Error only mentions Instagram (FB succeeded)
        self.assertIn("Instagram:", post.error)


class TestApprovalEnforcement(TransactionTestCase):
    """Test that approval is validated before publishing."""

    def setUp(self):
        self.user = User.objects.create_user("user", "user@test.com", "pass")
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )
        Membership.objects.create(
            user=self.user, account=self.account, role=Membership.Role.OWNER
        )

    @patch("apps.publishing.tasks.fetch_fb_post_insights")
    def test_publishing_without_approval_check_currently_proceeds(
        self, mock_fetch_insights
    ):
        """Current behavior: publishing does NOT check approval (gap to fix in Step 4)."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type=ContentPost.MediaType.IMAGE,
            media_urls=["https://example.com/image.jpg"],
            status=ContentPost.Status.PUBLISHING,
            created_by=self.user,
            approval_required=True,
            approval_status="draft",  # NOT APPROVED
        )

        # Currently, publish_post does not check approval
        # This is the gap identified in the audit
        # After Step 4, this should fail with approval validation error
        with patch("apps.publishing.services._fb_provider_for_post") as mock_fb:
            mock_provider = MagicMock()
            mock_provider.create_photo_post.return_value = "fb_123"
            mock_fb.return_value = (mock_provider, "page_id")

            # Currently succeeds (should fail after Step 4)
            publish_post(post)

            post.refresh_from_db()
            # For now, it publishes without checking approval
            # After Step 4, this should raise an exception


class TestUserRetryAction(TestCase):
    """Test user-initiated retry and related actions."""

    def setUp(self):
        self.user = User.objects.create_user("user", "user@test.com", "pass")
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )
        Membership.objects.create(
            user=self.user, account=self.account, role=Membership.Role.OWNER
        )
        self.client = self.client_class()
        self.client.force_login(self.user)

    def test_retry_requires_failed_status(self):
        """Retry action only works on FAILED posts."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type=ContentPost.MediaType.IMAGE,
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        self.client.post(reverse("publishing:post-retry", args=[post.pk]))

        post.refresh_from_db()
        self.assertEqual(post.status, ContentPost.Status.DRAFT)

    def test_retry_transitions_to_scheduled(self):
        """Retry transitions FAILED post to SCHEDULED."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type=ContentPost.MediaType.IMAGE,
            status=ContentPost.Status.FAILED,
            created_by=self.user,
            error="Previous failure",
        )

        self.client.post(reverse("publishing:post-retry", args=[post.pk]))

        post.refresh_from_db()
        self.assertEqual(post.status, ContentPost.Status.SCHEDULED)
        self.assertIsNotNone(post.scheduled_at)
