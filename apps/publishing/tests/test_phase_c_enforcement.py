"""Phase C: Approval enforcement at publishing boundaries.

Tests that approval checks block publication at the service level,
not just at the model level.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.services import publish_variant

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


class TestPublishVariantApprovalEnforcement:
    """Step 3: Enforcement in publish_variant()."""

    def test_unapproved_publication_blocked(self, account, reviewer):
        """Publishing without approval should be blocked before provider call."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            approval_required=True,
            approval_status="draft",  # Not approved
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            caption="Test caption",
        )

        # Attempt publication without approval
        result = publish_variant(variant)

        # Should fail with validation error
        assert result["success"] is False
        assert "approval" in result["error"].lower()
        assert result["error_type"] == ContentPostVariant.ErrorType.VALIDATION

    def test_content_changed_after_approval_blocks_publication(self, account, reviewer):
        """Publishing after content change should be blocked."""
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
            caption="Original caption",
        )

        # Approve the original content
        post.last_content_hash = post.compute_content_hash()
        post.save()

        # Change the variant after approval
        variant.caption = "Changed caption"
        variant.save()

        # Attempt publication with changed content
        result = publish_variant(variant)

        # Should fail with validation error
        assert result["success"] is False
        assert "approval" in result["error"].lower()

    def test_missing_reviewer_blocks_publication(self, account):
        """Publishing without a reviewer assignment should be blocked."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            approval_required=True,
            approval_status="approved",
            approved_by=None,  # No reviewer assigned
            approved_at=timezone.now(),
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            caption="Test caption",
        )

        post.last_content_hash = post.compute_content_hash()
        post.save()

        # Attempt publication without reviewer
        result = publish_variant(variant)

        # Should fail with validation error
        assert result["success"] is False
        assert "approval" in result["error"].lower()

    def test_solo_publishing_allowed_when_not_required(self, account):
        """Solo publishing should be allowed when approval_required=False."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            approval_required=False,  # Solo publishing
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            caption="Test caption",
        )

        # Attempt publication without approval (should pass approval check)
        # Note: This will fail at the provider call, but approval check should pass
        result = publish_variant(variant)

        # Should not fail on approval (may fail on provider call, but that's expected)
        if result["success"] is False:
            assert "approval" not in result["error"].lower()

    def test_valid_approval_allows_publication(self, account, reviewer):
        """Publishing with valid approval should proceed past approval check."""
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
            caption="Test caption",
        )

        # Approve with current content
        post.last_content_hash = post.compute_content_hash()
        post.save()

        # Attempt publication with valid approval
        result = publish_variant(variant)

        # Should pass approval check (may fail on provider call, but not approval)
        if result["success"] is False:
            assert "approval" not in result["error"].lower()

    def test_approval_check_race_safe(self, account, reviewer):
        """Approval check should be race-safe (checked before and after lock)."""
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
            caption="Test caption",
        )

        # Approve the content
        post.last_content_hash = post.compute_content_hash()
        post.save()

        # At this point, approval should be valid
        assert post.is_approval_valid()

        # Now simulate invalidation (variant changes)
        variant.caption = "Changed"
        variant.save()

        # Even though approval is now invalid, the service should catch it
        result = publish_variant(variant)

        # Should be blocked
        assert result["success"] is False
        assert "approval" in result["error"].lower()


class TestWorkflowApprovalEnforcement:
    """Step 4: Enforcement in _run_publish_post()."""

    def test_run_publish_post_unapproved_blocked(self, account):
        """_run_publish_post should block unapproved publication."""
        from apps.automation.models import WorkflowRun
        from apps.automation.workflow_engine import _run_publish_post

        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            approval_required=True,
            approval_status="draft",
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            caption="Test",
        )

        # Create a minimal workflow run
        from apps.automation.models import Workflow

        workflow = Workflow.objects.create(account=account, name="wf")
        contact = Contact.objects.create(account=account, phone="+15550000001")
        run = WorkflowRun.objects.create(workflow=workflow, contact=contact)

        # Attempt to publish unapproved post
        step = {"type": "publish_post", "post_id": str(post.pk)}

        # Expected: approval validation error
        with pytest.raises(ValueError, match=r"(?i)approval"):
            _run_publish_post(run, step)

    def test_run_publish_post_valid_approval(self, account, reviewer):
        """_run_publish_post should allow publication with valid approval."""
        from apps.automation.models import Workflow, WorkflowRun
        from apps.automation.workflow_engine import _run_publish_post

        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            approval_required=True,
            approval_status="approved",
            approved_by=reviewer,
            approved_at=timezone.now(),
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            caption="Test",
        )

        # Approve the content
        post.last_content_hash = post.compute_content_hash()
        post.save()

        workflow = Workflow.objects.create(account=account, name="wf")
        contact = Contact.objects.create(account=account, phone="+15550000001")
        run = WorkflowRun.objects.create(workflow=workflow, contact=contact)

        # Attempt to publish with valid approval
        step = {"type": "publish_post", "post_id": str(post.pk)}
        result = _run_publish_post(run, step)

        # Should proceed past approval check (may fail on provider, but not approval)
        if "_routed_to" in result or "error" in str(result).lower():
            # Check that error is not about approval
            result_str = str(result).lower()
            assert "approval" not in result_str


FB_OK = {
    "success": True,
    "publication_id": "fb_1",
    "publication_url": "https://facebook.com/fb_1",
    "error": "",
    "error_type": "",
    "error_at": None,
    "reconciling": False,
}


def _approved_post(account, reviewer, platforms=("facebook",), caption="", **extra):
    post = ContentPost.objects.create(
        account=account,
        platforms=list(platforms),
        approval_required=True,
        approval_status="approved",
        approved_by=reviewer,
        approved_at=timezone.now(),
        caption=caption,
        **extra,
    )
    variants = [
        ContentPostVariant.objects.create(post=post, platform=p, caption="Approved")
        for p in platforms
    ]
    post.last_content_hash = post.compute_content_hash()
    post.save()
    return post, variants


class TestClaimToCallInvariant:
    """Approval must bind to the exact payload handed to the provider."""

    def test_post_level_fallback_edit_blocks_publication(self, account, reviewer):
        """Post caption is the fallback when the variant caption is empty, so editing it must invalidate approval."""
        post, (variant,) = _approved_post(
            account, reviewer, caption="Approved fallback"
        )
        variant.caption = ""
        variant.save()
        post.last_content_hash = post.compute_content_hash()
        post.save()

        ContentPost.objects.filter(pk=post.pk).update(caption="Unapproved fallback")

        with patch(
            "apps.publishing.services._publish_variant_facebook", return_value=FB_OK
        ) as fb:
            result = publish_variant(variant)

        assert result["success"] is False
        fb.assert_not_called()

    def test_edit_after_claim_does_not_change_sent_payload(self, account, reviewer):
        """An edit committed after the claim must not alter what the provider receives."""
        post, (variant,) = _approved_post(account, reviewer)
        sent = {}

        def provider(v, p):
            sent["caption"] = v.caption
            ContentPostVariant.objects.filter(pk=v.pk).update(caption="Unapproved edit")
            return FB_OK

        with patch(
            "apps.publishing.services._publish_variant_facebook", side_effect=provider
        ):
            result = publish_variant(variant)

        assert result["success"] is True
        assert sent["caption"] == "Approved"
        variant.refresh_from_db()
        assert variant.caption == "Unapproved edit"

    def test_edit_between_check_and_claim_publishes_approved_snapshot(
        self, account, reviewer
    ):
        """An edit racing the locked approval check must not reach the provider."""
        post, (variant,) = _approved_post(account, reviewer)
        original = ContentPost.is_approval_valid
        sent = {}

        def edit_during_locked_check(self, variants=None):
            if variants is not None:
                ContentPostVariant.objects.filter(pk=variant.pk).update(
                    caption="Raced edit"
                )
            return original(self, variants)

        def provider(v, p):
            sent["caption"] = v.caption
            return FB_OK

        with (
            patch.object(ContentPost, "is_approval_valid", edit_during_locked_check),
            patch(
                "apps.publishing.services._publish_variant_facebook",
                side_effect=provider,
            ),
        ):
            publish_variant(variant)

        assert sent["caption"] == "Approved"

    def test_one_channel_edit_blocks_every_channel(self, account, reviewer):
        """Changing one variant invalidates the post-level approval for all channels."""
        post, (fb_variant, ig_variant) = _approved_post(
            account, reviewer, platforms=("facebook", "instagram")
        )
        ig_variant.caption = "Changed IG"
        ig_variant.save()

        with patch(
            "apps.publishing.services._publish_variant_facebook", return_value=FB_OK
        ) as fb:
            result = publish_variant(fb_variant)

        assert result["success"] is False
        fb.assert_not_called()

    def test_revoked_approval_blocks_publication(self, account, reviewer):
        post, (variant,) = _approved_post(account, reviewer)
        ContentPost.objects.filter(pk=post.pk).update(
            approval_status="changes_requested"
        )

        with patch(
            "apps.publishing.services._publish_variant_facebook", return_value=FB_OK
        ) as fb:
            result = publish_variant(variant)

        assert result["success"] is False
        fb.assert_not_called()

    def test_stale_attempt_cannot_republish_after_success(self, account, reviewer):
        """A second caller holding a stale in-memory variant must not publish again."""
        post, (variant,) = _approved_post(account, reviewer)
        stale = ContentPostVariant.objects.get(pk=variant.pk)

        with patch(
            "apps.publishing.services._publish_variant_facebook", return_value=FB_OK
        ) as fb:
            assert publish_variant(variant)["success"] is True
            second = publish_variant(stale)

        assert second["success"] is False
        assert fb.call_count == 1

    def test_valid_approval_publishes_normally(self, account, reviewer):
        post, (variant,) = _approved_post(account, reviewer)

        with patch(
            "apps.publishing.services._publish_variant_facebook", return_value=FB_OK
        ) as fb:
            result = publish_variant(variant)

        assert result["success"] is True
        fb.assert_called_once()
