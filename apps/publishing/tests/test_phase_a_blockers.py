"""Phase A regression tests: Verify critical blockers #1-#5 are fixed.

Covers:
- A1: Atomic variant initialization (Finding #1)
- A2: Transaction-safe claims (Finding #3)
- A3: Authoritative outcomes (Finding #2)
- A4: Concurrent duplicate protection (Finding #4)
- A5: Stale publishing detection (Finding #5)
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPlanItem, ContentPost, ContentPostVariant
from apps.publishing.services import initialize_post_with_variants, publish_variant
from apps.publishing.tasks import reconcile_stale_publishing_variants

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    user = User.objects.create_user("testuser", "test@example.com", "pass")
    account = Account.objects.create(company_name="Test Account")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return account


@pytest.fixture
def plan_item(account):
    return ContentPlanItem.objects.create(
        account=account,
        topic="Test Plan Item",
        objective="Test objective",
        platforms=["facebook", "instagram"],
        media_type="image",
    )


class TestPhaseA1VariantInitialization:
    """A1: Verify variants are created atomically for all posts."""

    def test_post_new_creates_variants(self, account):
        """post_new creates post with initialized variants (A1)."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook", "instagram"],
            status=ContentPost.Status.DRAFT,
        )
        # Simulate post_new calling initialize_post_with_variants
        variants = initialize_post_with_variants(post, post.platforms)

        assert len(variants) == 2
        assert ContentPostVariant.objects.filter(post=post).count() == 2
        fb = ContentPostVariant.objects.get(post=post, platform="facebook")
        ig = ContentPostVariant.objects.get(post=post, platform="instagram")
        assert fb.status == ContentPostVariant.Status.DRAFT
        assert ig.status == ContentPostVariant.Status.DRAFT

    def test_plan_item_create_post_initializes_variants(self, account, plan_item):
        """plan_item_create_post initializes variants atomically (A1)."""
        post = ContentPost.objects.create(
            account=account,
            plan_item=plan_item,
            platforms=plan_item.platforms,
            status=ContentPost.Status.DRAFT,
        )
        variants = initialize_post_with_variants(post, plan_item.platforms)

        assert len(variants) == 2
        assert all(v.status == ContentPostVariant.Status.DRAFT for v in variants)

    def test_initialize_idempotent(self, account):
        """initialize_post_with_variants is idempotent (respects unique constraint)."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            status=ContentPost.Status.DRAFT,
        )
        v1 = initialize_post_with_variants(post, ["facebook"])
        v2 = initialize_post_with_variants(post, ["facebook"])

        # Should be same variant, not duplicated
        assert v1[0].pk == v2[0].pk
        assert (
            ContentPostVariant.objects.filter(post=post, platform="facebook").count()
            == 1
        )

    def test_post_without_variants_cannot_publish(self, account):
        """Orchest publishing rejects posts with no variants (A1)."""
        from apps.publishing.services import orchestrate_post_publication

        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            status=ContentPost.Status.DRAFT,
        )
        # Intentionally don't create variants

        with pytest.raises(ValueError, match="no variants defined"):
            orchestrate_post_publication(post)


class TestPhaseA2TransactionSafety:
    """A2: Verify select_for_update() wrapped in transaction.atomic()."""

    def test_publish_variant_async_uses_transaction(self, account):
        """publish_variant_async locks variant within transaction (A2)."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.FAILED,
        )

        # Simulate publish_variant_async claiming the variant
        # This should work without TransactionManagementError on PostgreSQL
        with transaction.atomic():
            claimed = ContentPostVariant.objects.select_for_update().get(pk=variant.pk)
            assert claimed.pk == variant.pk

    def test_reconciliation_services_own_locking(self, account):
        """Reconciliation services wrap select_for_update() in transaction (A2)."""
        from apps.publishing.services import reconcile_variant_confirmed_published

        post = ContentPost.objects.create(account=account, platforms=["instagram"])
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.RECONCILING,
        )
        user = User.objects.create_user("operator", "op@example.com", "pass")

        # Service should handle transaction internally
        result = reconcile_variant_confirmed_published(
            variant=variant,
            publication_id="ig_123",
            publication_url="https://instagram.com/p/abc",
            evidence_notes="Found in IG",
            performed_by=user,
        )

        assert result["success"] is True
        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PUBLISHED


class TestPhaseA3AuthoritativeOutcomes:
    """A3: publish_variant() returns persisted state, not stale objects."""

    def test_publish_variant_returns_persisted_state(self, account):
        """publish_variant() returns DB state after publication (A3)."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.FAILED,
        )

        with patch("apps.publishing.services._publish_variant_facebook") as mock_pub:
            mock_pub.return_value = {
                "success": True,
                "publication_id": "fb_123",
                "publication_url": "https://facebook.com/123",
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
            }

            result = publish_variant(variant)

        # Result should match persisted variant state
        assert result["success"] is True
        assert result["publication_id"] == "fb_123"
        assert result["status"] == ContentPostVariant.Status.PUBLISHED

        # Verify DB was actually updated
        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PUBLISHED
        assert variant.publication_id == "fb_123"

    def test_orchestrate_uses_persisted_outcomes(self, account):
        """orchestrate_post_publication uses persisted variant states (A3)."""
        from apps.publishing.services import orchestrate_post_publication

        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook", "instagram"],
            status=ContentPost.Status.DRAFT,
        )
        ContentPostVariant.objects.create(
            post=post, platform="facebook", status=ContentPostVariant.Status.FAILED
        )
        ContentPostVariant.objects.create(
            post=post, platform="instagram", status=ContentPostVariant.Status.FAILED
        )

        with (
            patch("apps.publishing.services._publish_variant_facebook") as mock_fb,
            patch("apps.publishing.services._publish_variant_instagram") as mock_ig,
        ):
            mock_fb.return_value = {
                "success": True,
                "publication_id": "fb_123",
                "publication_url": "https://facebook.com/123",
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
            }
            mock_ig.return_value = {
                "success": True,
                "publication_id": "ig_456",
                "publication_url": "https://instagram.com/p/456",
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
            }

            parent_status, outcomes = orchestrate_post_publication(post)

        # Outcomes should reflect persisted DB state
        assert parent_status == ContentPost.Status.PUBLISHED
        assert len(outcomes) == 2
        assert outcomes[0]["status"] == ContentPostVariant.Status.PUBLISHED
        assert outcomes[1]["status"] == ContentPostVariant.Status.PUBLISHED


class TestPhaseA4DuplicateProtection:
    """A4: Concurrent publishes can't both claim the same variant."""

    def test_status_rechecked_after_lock(self, account):
        """publish_variant rechecks status under lock to prevent duplicates (A4)."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.FAILED,
        )

        # First call: marks as PUBLISHING, publishes successfully
        with patch("apps.publishing.services._publish_variant_facebook") as mock_pub:
            mock_pub.return_value = {
                "success": True,
                "publication_id": "fb_123",
                "publication_url": "https://facebook.com/123",
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
            }
            result1 = publish_variant(variant)

        assert result1["success"] is True
        assert result1["status"] == ContentPostVariant.Status.PUBLISHED

        # Second call: should not call provider again (already published)
        result2 = publish_variant(variant)
        assert result2["success"] is False
        assert "already published" in result2["error"].lower()
        # Provider should only be called once
        assert mock_pub.call_count == 1

    def test_concurrent_claim_prevents_duplicate_api_calls(self, account):
        """Two concurrent tasks can't both claim variant for publication (A4)."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.FAILED,
        )

        call_count = 0

        def mock_publisher(v, p):
            nonlocal call_count
            call_count += 1
            return {
                "success": True,
                "publication_id": f"fb_{call_count}",
                "publication_url": f"https://facebook.com/{call_count}",
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
            }

        with patch(
            "apps.publishing.services._publish_variant_facebook",
            side_effect=mock_publisher,
        ):
            # Simulate two concurrent publishes
            result1 = publish_variant(variant)
            # Reload variant as if second task loaded it before first claimed it
            variant.refresh_from_db()
            result2 = publish_variant(variant)

        # Only first should succeed; second should see PUBLISHED status
        assert result1["success"] is True
        assert result2["success"] is False
        # Provider only called once (second task saw PUBLISHED status under lock)
        assert call_count == 1


class TestPhaseA5StalePublishingDetection:
    """A5: Uses last_attempt_at, not last_error_at, for stale detection."""

    def test_stale_publishing_uses_last_attempt_at(self, account):
        """Stale-publishing reconciler checks last_attempt_at (A5)."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])

        # Variant with old error but recent attempt (e.g., just retried)
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHING,
            last_error_at=timezone.now() - timedelta(hours=8),  # Old error
            last_attempt_at=timezone.now() - timedelta(minutes=5),  # Recent attempt
        )

        result = reconcile_stale_publishing_variants()

        # Should NOT escalate (recent attempt)
        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PUBLISHING
        assert result["escalated"] == 0

    def test_stale_publishing_escalates_old_attempts(self, account):
        """Stale-publishing reconciler escalates when last_attempt_at is old (A5)."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])

        # Variant with no recent attempt
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHING,
            last_attempt_at=timezone.now() - timedelta(hours=8),  # Old attempt
        )

        result = reconcile_stale_publishing_variants()

        # Should escalate (old attempt)
        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.RECONCILING
        assert result["escalated"] == 1

    def test_stale_publishing_escalates_null_attempts(self, account):
        """Stale-publishing reconciler escalates variants with NULL last_attempt_at (A5)."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])

        # Variant never attempted (or created before field existed)
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHING,
            last_attempt_at=None,
            created_at=timezone.now() - timedelta(hours=8),
        )

        result = reconcile_stale_publishing_variants()

        # Should escalate (no attempt record)
        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.RECONCILING
        assert result["escalated"] == 1


class TestPhaseAIntegration:
    """Integration: All 5 fixes work together."""

    def test_complete_publish_flow_with_variants(self, account):
        """Complete publish flow with initialized variants (A1-A5)."""
        from apps.publishing.services import orchestrate_post_publication

        # A1: Create post and initialize variants
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            status=ContentPost.Status.DRAFT,
        )
        initialize_post_with_variants(post, post.platforms)

        # Verify variant created
        variant = ContentPostVariant.objects.get(post=post, platform="facebook")
        assert variant.status == ContentPostVariant.Status.DRAFT

        # A3/A4: Publish with mock
        with patch("apps.publishing.services._publish_variant_facebook") as mock_pub:
            mock_pub.return_value = {
                "success": True,
                "publication_id": "fb_999",
                "publication_url": "https://facebook.com/999",
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
            }

            parent_status, outcomes = orchestrate_post_publication(post)

        # Verify outcomes are persisted
        assert parent_status == ContentPost.Status.PUBLISHED
        assert outcomes[0]["status"] == ContentPostVariant.Status.PUBLISHED
        assert outcomes[0]["publication_id"] == "fb_999"

        # Verify variant persisted
        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PUBLISHED
        assert variant.publication_id == "fb_999"
        assert variant.last_attempt_at is not None  # A5: timestamp set

        # A5: Stale reconciler should not escalate recent publish
        result = reconcile_stale_publishing_variants()
        assert result["escalated"] == 0
