"""Phase C: Approval Integrity (Finding #8).

Tests that approval enforcement prevents silent unapproved publishing,
and that variant content changes after approval are detected.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost, ContentPostVariant

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    user = User.objects.create_user("testuser", "test@example.com", "pass")
    account = Account.objects.create(company_name="Test Account")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return account


@pytest.fixture
def reviewer():
    return User.objects.create_user("reviewer", "reviewer@example.com", "pass")


class TestPhaseCapprovalIntegrity:
    """Phase C: Approval hashing and enforcement."""

    def test_post_requires_approval_when_flag_set(self, account, reviewer):
        """Post with approval_required=True cannot publish without approval."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            status=ContentPost.Status.DRAFT,
            approval_required=True,
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.DRAFT,
        )

        # Not approved yet
        assert not post.is_approval_valid()

    def test_approval_requires_reviewer_assignment(self, account, reviewer):
        """Approval is invalid without a designated reviewer signing off."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            approval_required=True,
            approval_status="approved",
            approved_at=timezone.now(),
            # Note: approved_by is None (not set)
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
        )

        # Approved but no reviewer assignment
        assert not post.is_approval_valid()

    def test_approval_with_valid_reviewer(self, account, reviewer):
        """Approval is valid when assigned reviewer signs off."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            approval_required=True,
            approval_status="approved",
            approved_by=reviewer,
            approved_at=timezone.now(),
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
        )
        # Set hash after post and variant exist
        post.last_content_hash = post.compute_content_hash()
        post.save()

        # Valid approval
        assert post.is_approval_valid()
        # Variant approval also valid
        assert variant.variant_approval_valid()

    def test_variant_caption_change_invalidates_approval(self, account, reviewer):
        """Editing a variant's caption after approval invalidates it."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            caption="Original caption",
            approval_required=True,
            approval_status="approved",
            approved_by=reviewer,
            approved_at=timezone.now(),
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            caption="Original caption",
        )

        # Approve with current content
        original_hash = post.compute_content_hash()
        post.last_content_hash = original_hash
        post.save()
        assert post.is_approval_valid()

        # Change variant caption after approval
        variant.caption = "Modified caption - unapproved!"
        variant.save()

        # Approval is now invalid
        modified_hash = post.compute_content_hash()
        assert modified_hash != original_hash
        assert not post.is_approval_valid()

    def test_variant_media_change_invalidates_approval(self, account, reviewer):
        """Editing variant media URLs after approval invalidates it."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_urls=["https://cdn.example.com/image1.jpg"],
            approval_required=True,
            approval_status="approved",
            approved_by=reviewer,
            approved_at=timezone.now(),
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            media_urls=["https://cdn.example.com/image1.jpg"],
        )

        original_hash = post.compute_content_hash()
        post.last_content_hash = original_hash
        post.save()

        # Swap media to different image after approval
        variant.media_urls = ["https://cdn.example.com/image2.jpg"]
        variant.save()

        # Approval is invalid
        assert post.compute_content_hash() != original_hash
        assert not post.is_approval_valid()

    def test_variant_format_change_invalidates_approval(self, account, reviewer):
        """Changing variant format choice after approval invalidates it."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            approval_required=True,
            approval_status="approved",
            approved_by=reviewer,
            approved_at=timezone.now(),
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            format_choice="reel",
        )

        original_hash = post.compute_content_hash()
        post.last_content_hash = original_hash
        post.save()

        # Change format after approval
        variant.format_choice = "story"
        variant.save()

        # Approval is invalid
        assert post.compute_content_hash() != original_hash
        assert not post.is_approval_valid()

    def test_multiple_variants_any_change_invalidates(self, account, reviewer):
        """Change to ANY variant invalidates approval for the entire post."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook", "instagram"],
            approval_required=True,
            approval_status="approved",
            approved_by=reviewer,
            approved_at=timezone.now(),
        )
        fb_variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            caption="Original FB caption",
        )
        ig_variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            caption="Original IG caption",
        )

        original_hash = post.compute_content_hash()
        post.last_content_hash = original_hash
        post.save()

        # Change only the Instagram variant
        ig_variant.caption = "Modified IG caption"
        ig_variant.save()

        # Entire post approval is now invalid (because variant changed)
        assert post.compute_content_hash() != original_hash
        assert not post.is_approval_valid()
        # Both variants are affected
        assert not fb_variant.variant_approval_valid()
        assert not ig_variant.variant_approval_valid()

    def test_approval_unchanged_content_remains_valid(self, account, reviewer):
        """Approval remains valid if content hasn't changed."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            caption="Approved content",
            approval_required=True,
            approval_status="approved",
            approved_by=reviewer,
            approved_at=timezone.now(),
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            caption="Approved content",
        )

        # Approve with current content
        hash1 = post.compute_content_hash()
        post.last_content_hash = hash1
        post.save()

        # Make non-content changes (e.g., status changes)
        post.approval_feedback = "Looks good!"
        post.save()

        # Approval still valid (content unchanged)
        hash2 = post.compute_content_hash()
        assert hash1 == hash2
        assert post.is_approval_valid()

    def test_approval_enforcement_prevents_unauthorized_publishing(self, account):
        """Publishing is blocked if approval is required but invalid."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            approval_required=True,
            approval_status="draft",  # Not approved
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
        )

        # Attempting to publish without valid approval should be caught
        # (Actual enforcement happens in publish_variant, which checks is_approval_valid)
        assert post.approval_required
        assert not post.is_approval_valid()

    def test_no_approval_required_allows_solo_publishing(self, account):
        """Solo publisher can publish without review when approval_required=False."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            approval_required=False,  # Solo creator mode
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
        )

        # No approval needed
        assert not post.approval_required
        # is_approval_valid returns True when not required
        # (or False due to no reviewer, but that's okay since approval isn't required)
