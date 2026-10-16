"""Phase B3: Stale-publishing recovery verification (Finding #5).

Verify that last_attempt_at (publication attempts) and last_error_at (error timestamps)
are kept separate, and that stale PUBLISHING variants are correctly escalated to RECONCILING.

Critical: B3 handles PUBLISHING state timeout; B2 handles PROCESSING state timeout.
They must not interfere.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.tasks import reconcile_stale_publishing_variants

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    user = User.objects.create_user("testuser", "test@example.com", "pass")
    account = Account.objects.create(company_name="Test Account")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return account


class TestPhaseB3StalePublishingRecovery:
    """B3: Stale-publishing recovery uses last_attempt_at independently from last_error_at."""

    def test_newly_claimed_variant_not_stale(self, account):
        """A variant just claimed for publishing (recent last_attempt_at) is not stale."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])

        now = timezone.now()
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHING,
            last_attempt_at=now - timedelta(minutes=5),  # Just attempted
            last_error_at=None,
        )

        result = reconcile_stale_publishing_variants()

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PUBLISHING
        assert result["escalated"] == 0

    def test_old_error_does_not_mark_recent_attempt_stale(self, account):
        """Recent attempt is not stale even if there's an old error timestamp."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])

        now = timezone.now()
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHING,
            last_attempt_at=now - timedelta(hours=1),  # Recent attempt
            last_error_at=now - timedelta(hours=8),  # Old error from prior attempt
        )

        result = reconcile_stale_publishing_variants()

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PUBLISHING
        assert result["escalated"] == 0

    def test_stale_attempt_escalates_to_reconciling(self, account):
        """Variant exceeding timeout on last_attempt_at → RECONCILING."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])

        now = timezone.now()
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHING,
            last_attempt_at=now - timedelta(hours=7),  # Over 6-hour threshold
        )

        result = reconcile_stale_publishing_variants()

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.RECONCILING
        assert "enqueue may have failed" in variant.last_error.lower()
        assert result["escalated"] == 1

    def test_null_last_attempt_at_considered_stale(self, account):
        """Variant with no recorded attempt (NULL last_attempt_at) is escalated."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])

        now = timezone.now()
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHING,
            last_attempt_at=None,  # Never attempted (or legacy)
            created_at=now - timedelta(hours=7),  # Created 7h ago
        )

        result = reconcile_stale_publishing_variants()

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.RECONCILING
        assert result["escalated"] == 1

    def test_retry_refreshes_attempt_timestamp(self, account):
        """Each genuine retry should refresh last_attempt_at to prevent false escalation."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])

        now = timezone.now()

        # First attempt (old)
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHING,
            last_attempt_at=now - timedelta(hours=1),
            last_error_at=now - timedelta(hours=1),
            attempt_count=1,
        )

        # Simulate retry: refresh the attempt timestamp
        variant.last_attempt_at = now - timedelta(minutes=5)
        variant.attempt_count = 2
        variant.save()

        result = reconcile_stale_publishing_variants()

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PUBLISHING
        assert result["escalated"] == 0

    def test_processing_variant_not_handled_by_stale_publishing(self, account):
        """PROCESSING variants use B2 (processing_started_at); not checked by stale-publishing."""
        post = ContentPost.objects.create(account=account, platforms=["instagram"])

        now = timezone.now()
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=now - timedelta(hours=49),  # Old by B2 standards
            last_attempt_at=now - timedelta(hours=7),  # Old by B3 standards
        )

        # B3 only handles PUBLISHING, not PROCESSING
        result = reconcile_stale_publishing_variants()

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PROCESSING
        assert result["escalated"] == 0

    def test_published_variant_not_escalated(self, account):
        """Published variants are terminal; not escalated."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])

        now = timezone.now()
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHED,
            last_attempt_at=now - timedelta(hours=7),
        )

        result = reconcile_stale_publishing_variants()

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PUBLISHED
        assert result["escalated"] == 0

    def test_failed_variant_not_re_escalated(self, account):
        """FAILED variants are terminal; not escalated."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])

        now = timezone.now()
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.FAILED,
            last_attempt_at=now - timedelta(hours=7),
        )

        result = reconcile_stale_publishing_variants()

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.FAILED
        assert result["escalated"] == 0

    def test_repeated_recovery_idempotent(self, account):
        """Running stale-publishing recovery twice on same variant is idempotent."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])

        now = timezone.now()
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHING,
            last_attempt_at=now - timedelta(hours=7),
        )

        # First run: escalate
        result1 = reconcile_stale_publishing_variants()
        assert result1["escalated"] == 1

        # Second run: nothing to escalate (already RECONCILING)
        result2 = reconcile_stale_publishing_variants()
        assert result2["escalated"] == 0

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.RECONCILING

    def test_multiple_channels_mixed_states(self, account):
        """Multiple variants; only stale PUBLISHING ones are escalated by B3."""
        post = ContentPost.objects.create(
            account=account, platforms=["facebook", "instagram"]
        )

        now = timezone.now()

        # FB: stale PUBLISHING, should escalate
        fb_variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHING,
            last_attempt_at=now - timedelta(hours=7),
        )

        # IG: recently PUBLISHING, should not escalate
        ig_variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PUBLISHING,
            last_attempt_at=now - timedelta(minutes=10),
        )

        result = reconcile_stale_publishing_variants()

        fb_variant.refresh_from_db()
        ig_variant.refresh_from_db()

        assert fb_variant.status == ContentPostVariant.Status.RECONCILING
        assert ig_variant.status == ContentPostVariant.Status.PUBLISHING
        assert result["escalated"] == 1

    def test_b2_and_b3_independent_clocks(self, account):
        """B2 (processing_started_at) and B3 (last_attempt_at) use independent clocks."""
        post = ContentPost.objects.create(account=account, platforms=["instagram"])

        now = timezone.now()

        # This variant would be escalated by B2 (PROCESSING too long)
        # but not by B3 (never attempted publishing, so no last_attempt_at)
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=now - timedelta(hours=49),  # B2 threshold
            last_attempt_at=now - timedelta(minutes=5),  # Recent publication attempt
        )

        # B3 (stale-publishing) should not touch PROCESSING variants
        result = reconcile_stale_publishing_variants()

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PROCESSING
        assert result["escalated"] == 0

    def test_ambiguous_outcome_preserved_on_escalation(self, account):
        """Escalation to RECONCILING preserves ambiguity; doesn't assume failure."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])

        now = timezone.now()
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHING,
            last_attempt_at=now - timedelta(hours=8),
            publication_id="fb_123",  # Provider may have accepted the request
        )

        reconcile_stale_publishing_variants()

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.RECONCILING
        # Publication ID preserved for manual verification
        assert variant.publication_id == "fb_123"
        # Error indicates ambiguity, not definite failure
        assert "enqueue may have failed" in variant.last_error.lower()
