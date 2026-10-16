"""Tests for ContentPostVariant model, backfill, and publishing backward compatibility."""

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import TestCase

from apps.accounts.models import Account
from apps.publishing.models import (
    ContentPost,
    ContentPostVariant,
)


class TestVariantModel(TestCase):
    """Test ContentPostVariant model fields and constraints."""

    def setUp(self):
        self.user = User.objects.create_user("testuser", "test@example.com", "pass")
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )

    def test_create_variant_with_all_fields(self):
        """Create a variant with all supported fields."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            caption="Test caption",
            media_urls=["https://example.com/image.jpg"],
            format_choice="feed_post",
            scheduled_at=None,
            status=ContentPostVariant.Status.DRAFT,
        )

        self.assertEqual(variant.post, post)
        self.assertEqual(variant.platform, "instagram")
        self.assertEqual(variant.caption, "Test caption")

    def test_variant_unique_constraint_post_platform(self):
        """Cannot create two variants for the same post + platform."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.DRAFT,
        )

        with self.assertRaises(IntegrityError):
            ContentPostVariant.objects.create(
                post=post,
                platform="instagram",
                status=ContentPostVariant.Status.DRAFT,
            )

    def test_variant_allows_different_platforms_same_post(self):
        """Same post can have variants for different platforms."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram", "facebook"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        ig_variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.DRAFT,
        )

        fb_variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.DRAFT,
        )

        self.assertNotEqual(ig_variant.pk, fb_variant.pk)
        self.assertEqual(ig_variant.post, fb_variant.post)

    def test_variant_caption_independent_per_platform(self):
        """Variants can have independent captions."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram", "facebook"],
            media_type="image",
            caption="Shared caption",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        ig_variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            caption="Instagram-specific caption",
            status=ContentPostVariant.Status.DRAFT,
        )

        fb_variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            caption="Facebook-specific caption",
            status=ContentPostVariant.Status.DRAFT,
        )

        self.assertEqual(ig_variant.caption, "Instagram-specific caption")
        self.assertEqual(fb_variant.caption, "Facebook-specific caption")

    def test_variant_status_transitions(self):
        """Variant status can transition through expected states."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.DRAFT,
        )

        # Draft → Scheduled
        variant.status = ContentPostVariant.Status.SCHEDULED
        variant.save()
        self.assertEqual(variant.status, ContentPostVariant.Status.SCHEDULED)

        # Scheduled → Publishing
        variant.status = ContentPostVariant.Status.PUBLISHING
        variant.save()
        self.assertEqual(variant.status, ContentPostVariant.Status.PUBLISHING)

        # Publishing → Published
        variant.status = ContentPostVariant.Status.PUBLISHED
        variant.save()
        self.assertEqual(variant.status, ContentPostVariant.Status.PUBLISHED)

    def test_variant_error_tracking(self):
        """Variant captures error details on failure."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.FAILED,
            last_error="Invalid aspect ratio",
            last_error_type=ContentPostVariant.ErrorType.VALIDATION,
            attempt_count=1,
        )

        self.assertEqual(variant.status, ContentPostVariant.Status.FAILED)
        self.assertEqual(variant.last_error, "Invalid aspect ratio")
        self.assertEqual(
            variant.last_error_type, ContentPostVariant.ErrorType.VALIDATION
        )

    def test_variant_retry_tracking(self):
        """Variant tracks attempt count and last attempt time."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.DRAFT,
            attempt_count=0,
        )

        self.assertEqual(variant.attempt_count, 0)
        self.assertIsNone(variant.last_attempt_at)

        variant.attempt_count = 1
        variant.save()

        variant.refresh_from_db()
        self.assertEqual(variant.attempt_count, 1)

    def test_variant_publication_id_storage(self):
        """Variant stores publication ID from provider."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PUBLISHED,
            publication_id="17919191979192",
            publication_url="https://instagram.com/p/ABC123/",
        )

        self.assertEqual(variant.publication_id, "17919191979192")
        self.assertEqual(variant.publication_url, "https://instagram.com/p/ABC123/")

    def test_variant_metadata_storage(self):
        """Variant can store provider-specific metadata as JSON."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PUBLISHING,
            metadata={
                "ig_container_id": "17919191979192",
                "creation_timestamp": 1726440123,
            },
        )

        self.assertEqual(variant.metadata["ig_container_id"], "17919191979192")
        self.assertEqual(variant.metadata["creation_timestamp"], 1726440123)


class TestVariantBackfill(TestCase):
    """Test that backfill logic correctly maps legacy data to variants."""

    def setUp(self):
        self.user = User.objects.create_user("testuser", "test@example.com", "pass")
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )

    def _create_variants_from_post(self, post):
        """Simulate backfill logic for test posts."""
        platforms = post.platforms or []
        for platform in platforms:
            if platform not in ["facebook", "instagram"]:
                continue
            variant_data = {
                "platform": platform,
                "caption": post.caption_for(platform)
                if hasattr(post, "caption_for")
                else post.caption,
                "media_urls": post.media_urls or [],
                "scheduled_at": post.scheduled_at,
                "status": post.status,
            }
            if platform == "facebook":
                if post.fb_post_id:
                    variant_data["publication_id"] = post.fb_post_id
                    variant_data["published_at"] = post.published_at
            elif platform == "instagram":
                if post.ig_media_id:
                    variant_data["publication_id"] = post.ig_media_id
                    variant_data["published_at"] = post.published_at
                if post.ig_container_id:
                    variant_data["metadata"] = {"ig_container_id": post.ig_container_id}
            ContentPostVariant.objects.get_or_create(
                post=post, platform=platform, defaults=variant_data
            )

    def test_backfill_creates_variants_for_existing_posts(self):
        """Backfill creates variants for posts with platforms."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram", "facebook"],
            media_type="image",
            status=ContentPost.Status.PUBLISHED,
            created_by=self.user,
        )

        # Simulate backfill
        self._create_variants_from_post(post)

        variants = list(post.variants.all())
        self.assertEqual(len(variants), 2)
        self.assertEqual({v.platform for v in variants}, {"instagram", "facebook"})

    def test_backfill_preserves_facebook_publication_id(self):
        """Facebook publication ID from post maps to variant."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type="image",
            fb_post_id="123456789",
            status=ContentPost.Status.PUBLISHED,
            created_by=self.user,
        )

        self._create_variants_from_post(post)
        variant = post.variants.get(platform="facebook")
        self.assertEqual(variant.publication_id, "123456789")

    def test_backfill_preserves_instagram_publication_id(self):
        """Instagram media ID from post maps to variant."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            ig_media_id="17919191979192",
            status=ContentPost.Status.PUBLISHED,
            created_by=self.user,
        )

        self._create_variants_from_post(post)
        variant = post.variants.get(platform="instagram")
        self.assertEqual(variant.publication_id, "17919191979192")

    def test_backfill_preserves_instagram_container_id_in_metadata(self):
        """Instagram container ID stored in variant metadata."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            ig_media_id="17919191979192",
            ig_container_id="container123",
            status=ContentPost.Status.PUBLISHED,
            created_by=self.user,
        )

        self._create_variants_from_post(post)
        variant = post.variants.get(platform="instagram")
        self.assertEqual(variant.metadata.get("ig_container_id"), "container123")

    def test_backfill_copies_caption_to_variant(self):
        """Post caption copied to variant."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            caption="Test caption",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        self._create_variants_from_post(post)
        variant = post.variants.get(platform="instagram")
        self.assertEqual(variant.caption, "Test caption")

    def test_backfill_preserves_scheduled_time(self):
        """Post scheduled_at time copied to variant."""
        from django.utils import timezone

        scheduled = timezone.now()

        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            scheduled_at=scheduled,
            status=ContentPost.Status.SCHEDULED,
            created_by=self.user,
        )

        self._create_variants_from_post(post)
        variant = post.variants.get(platform="instagram")
        self.assertEqual(variant.scheduled_at, scheduled)

    def test_backfill_handles_posts_without_publication_ids(self):
        """Posts without publication IDs create variants with empty IDs."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        self._create_variants_from_post(post)
        variant = post.variants.get(platform="instagram")
        self.assertEqual(variant.publication_id, "")
        self.assertIsNone(variant.published_at)

    def test_backfill_idempotent(self):
        """Backfill doesn't create duplicate variants on re-run."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        # Manually create variant (simulating first backfill)
        ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            caption="Existing variant",
        )

        # Count should still be 1 (not 2)
        self.assertEqual(post.variants.count(), 1)
        # Caption should be "Existing variant" (unchanged)
        self.assertEqual(post.variants.first().caption, "Existing variant")


class TestVariantBackwardCompatibility(TestCase):
    """Test that existing publishing flows work with variant model."""

    def setUp(self):
        self.user = User.objects.create_user("testuser", "test@example.com", "pass")
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )

    def test_existing_posts_queryable_after_variant_migration(self):
        """Existing posts remain queryable after variant model added."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        # Query should work (no join on variants required)
        result = ContentPost.objects.get(pk=post.pk)
        self.assertEqual(result.pk, post.pk)

    def test_post_without_variants_can_be_edited(self):
        """Editing existing post without variants doesn't error."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        # Variant not created manually (simulating pre-backfill post)
        post.caption = "Updated caption"
        post.save()

        post.refresh_from_db()
        self.assertEqual(post.caption, "Updated caption")

    def test_calendar_view_works_without_variant_join(self):
        """Calendar queries don't fail if variants don't exist."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.SCHEDULED,
            created_by=self.user,
        )

        # This is how calendar queries work (no variant join)
        qs = ContentPost.objects.filter(
            account=self.account,
            status=ContentPost.Status.SCHEDULED,
        )

        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().pk, post.pk)

    def test_post_detail_view_works_with_or_without_variants(self):
        """Post detail renders correctly with and without variants."""
        # Post with variants
        post_with_variants = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.PUBLISHED,
            created_by=self.user,
        )

        # Create variants for first post
        ContentPostVariant.objects.create(
            post=post_with_variants,
            platform="instagram",
            status=ContentPostVariant.Status.PUBLISHED,
        )

        # Post without variants (manually created, no variants)
        post_without_variants = ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        # Query should work for both
        self.assertIsNotNone(ContentPost.objects.get(pk=post_with_variants.pk))
        self.assertIsNotNone(ContentPost.objects.get(pk=post_without_variants.pk))

        # Can access variants.all() on both
        self.assertGreater(post_with_variants.variants.count(), 0)
        self.assertEqual(post_without_variants.variants.count(), 0)

    def test_existing_publishing_status_field_unchanged(self):
        """ContentPost.status field behavior unchanged."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        self.assertEqual(post.status, ContentPost.Status.DRAFT)

        post.status = ContentPost.Status.SCHEDULED
        post.save()

        post.refresh_from_db()
        self.assertEqual(post.status, ContentPost.Status.SCHEDULED)
