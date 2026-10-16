"""Phase B2: Processing exhaustion detection (Finding #9).

Uses processing_started_at to distinguish provider operation duration from polling health.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.tasks import escalate_exhausted_processing_variants

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    user = User.objects.create_user("testuser", "test@example.com", "pass")
    account = Account.objects.create(company_name="Test Account")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return account


class TestPhaseB2ExhaustionDetection:
    """B2: Exhaustion uses processing_started_at, not polling health."""

    def test_processing_deadline_does_not_move_on_polling(self, account):
        """Processing deadline is fixed at processing_started_at, not updated by polls."""
        post = ContentPost.objects.create(account=account, platforms=["instagram"])

        now = timezone.now()
        processing_started = now - timedelta(hours=47)

        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=processing_started,
            metadata={"ig_container_id": "container_123"},
        )

        # Simulate polling attempts (updating last_poll_attempt in metadata)
        variant.metadata["last_poll_attempt"] = now.isoformat()
        variant.save()

        # Run exhaustion check with 48-hour threshold
        result = escalate_exhausted_processing_variants(max_age_hours=48)

        # Should NOT escalate (only 47 hours elapsed, 1 hour to go)
        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PROCESSING
        assert result["escalated"] == 0

    def test_overdue_variant_escalates_to_reconciling(self, account):
        """Variant exceeding processing deadline → RECONCILING."""
        post = ContentPost.objects.create(account=account, platforms=["instagram"])

        now = timezone.now()
        processing_started = now - timedelta(hours=49)  # Over 48-hour threshold

        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=processing_started,
            metadata={"ig_container_id": "container_456"},
        )

        result = escalate_exhausted_processing_variants(max_age_hours=48)

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.RECONCILING
        assert variant.last_error_type == ContentPostVariant.ErrorType.TIMEOUT
        assert result["escalated"] == 1

    def test_recently_started_variant_not_escalated(self, account):
        """Variant just entered PROCESSING → not escalated."""
        post = ContentPost.objects.create(account=account, platforms=["instagram"])

        now = timezone.now()
        processing_started = now - timedelta(hours=1)  # Just started

        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=processing_started,
            metadata={"ig_container_id": "container_789"},
        )

        result = escalate_exhausted_processing_variants(max_age_hours=48)

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PROCESSING
        assert result["escalated"] == 0

    def test_variant_published_before_lock_not_escalated(self, account):
        """Variant that publishes before exhaustion task acquires lock is not escalated."""
        post = ContentPost.objects.create(account=account, platforms=["instagram"])

        now = timezone.now()
        processing_started = now - timedelta(hours=49)

        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=processing_started,
        )

        # Simulate variant publishing before escalation lock
        original_select_for_update = ContentPostVariant.objects.select_for_update

        def publish_before_check(*args, **kwargs):
            ContentPostVariant.objects.filter(pk=variant.pk).update(
                status=ContentPostVariant.Status.PUBLISHED, processing_started_at=None
            )
            return original_select_for_update(*args, **kwargs)

        with patch.object(
            ContentPostVariant.objects,
            "select_for_update",
            side_effect=publish_before_check,
        ):
            result = escalate_exhausted_processing_variants(max_age_hours=48)

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PUBLISHED
        assert result["escalated"] == 0

    def test_missing_processing_started_at_handled_safely(self, account):
        """Legacy variant without processing_started_at is not escalated (safe fallback)."""
        post = ContentPost.objects.create(account=account, platforms=["instagram"])

        # Create variant with processing_started_at = None (legacy)
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=None,  # Legacy: no timestamp
        )

        result = escalate_exhausted_processing_variants(max_age_hours=48)

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PROCESSING
        assert result["escalated"] == 0

    def test_repeated_exhaustion_runs_idempotent(self, account):
        """Running exhaustion task twice on same variant doesn't duplicate actions."""
        post = ContentPost.objects.create(account=account, platforms=["instagram"])

        now = timezone.now()
        processing_started = now - timedelta(hours=49)

        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=processing_started,
        )

        # First run: escalate
        result1 = escalate_exhausted_processing_variants(max_age_hours=48)
        assert result1["escalated"] == 1

        # Second run: nothing to escalate (already RECONCILING)
        result2 = escalate_exhausted_processing_variants(max_age_hours=48)
        assert result2["escalated"] == 0

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.RECONCILING

    def test_processing_restart_resets_deadline(self, account):
        """When processing is restarted (new container), deadline resets."""
        post = ContentPost.objects.create(account=account, platforms=["instagram"])

        now = timezone.now()
        old_start = now - timedelta(hours=49)

        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=old_start,
        )

        # Simulate processing restart: new attempt sets fresh processing_started_at
        variant.processing_started_at = now  # Fresh clock
        variant.metadata = {"ig_container_id": "new_container"}
        variant.save()

        result = escalate_exhausted_processing_variants(max_age_hours=48)

        # Should NOT escalate (just restarted)
        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PROCESSING
        assert result["escalated"] == 0

    def test_metadata_preserved_on_escalation(self, account):
        """Escalation preserves polling history and container ID."""
        post = ContentPost.objects.create(account=account, platforms=["instagram"])

        now = timezone.now()
        processing_started = now - timedelta(hours=50)
        polling_history = {
            "ig_container_id": "container_xyz",
            "last_poll_attempt": (now - timedelta(hours=1)).isoformat(),
            "poll_count": 5,
        }

        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=processing_started,
            metadata=polling_history,
        )

        escalate_exhausted_processing_variants(max_age_hours=48)

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.RECONCILING
        # Metadata preserved for recovery
        assert variant.metadata["ig_container_id"] == "container_xyz"
        assert variant.metadata["poll_count"] == 5

    def test_non_instagram_processing_not_checked(self, account):
        """Exhaustion check only looks at Instagram variants."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])

        now = timezone.now()
        processing_started = now - timedelta(hours=50)

        # Create a hypothetical Facebook variant in PROCESSING (shouldn't happen in practice)
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=processing_started,
        )

        result = escalate_exhausted_processing_variants(max_age_hours=48)

        variant.refresh_from_db()
        # Facebook variants are not checked, so not escalated
        # (This test documents the current behavior; could be extended in the future)
        assert result["escalated"] == 0

    def test_multiple_channels_mixed_states(self, account):
        """Multiple variants; only exhausted PROCESSING ones escalated."""
        post = ContentPost.objects.create(
            account=account, platforms=["facebook", "instagram"]
        )

        now = timezone.now()

        # FB: already published (no action)
        fb_variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHED,
        )

        # IG: exhausted, should escalate
        ig_variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=now - timedelta(hours=50),
        )

        result = escalate_exhausted_processing_variants(max_age_hours=48)

        fb_variant.refresh_from_db()
        ig_variant.refresh_from_db()

        assert fb_variant.status == ContentPostVariant.Status.PUBLISHED
        assert ig_variant.status == ContentPostVariant.Status.RECONCILING
        assert result["escalated"] == 1
