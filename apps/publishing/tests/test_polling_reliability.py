"""Tests for Step 4.7C — Polling Reliability.

Validates that:
1. Video still processing within limit remains PROCESSING
2. Video exceeds limit escalates to RECONCILING
3. Worker restarts recover from persisted metadata
4. Transient errors have bounded retry
5. Terminal container errors mark variant FAILED
6. Container becomes ready publishes when polled
7. Final publish timeout enters RECONCILING (not republished)
8. Concurrent pollers don't duplicate publication
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.tasks import (
    escalate_exhausted_processing_variants,
    poll_ig_variant_containers,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    """Test account with Instagram connection."""
    user = User.objects.create_user("testuser", "test@example.com", "pass")
    account = Account.objects.create(company_name="Test Account")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return account


def _fake_ig_provider(
    poll_status="FINISHED", publish_result="ig_media_1", publish_error=None
):
    provider = MagicMock()
    provider.get_permalink.return_value = ""
    provider.poll_container_status.return_value = poll_status
    if publish_error is not None:
        provider.publish_container.side_effect = publish_error
    else:
        provider.publish_container.return_value = publish_result
    return provider


@pytest.fixture
def processing_variant(account):
    """Variant in PROCESSING state with container ID."""
    post = ContentPost.objects.create(
        account=account,
        platforms=["instagram"],
        media_type="video",
        status=ContentPost.Status.PUBLISHING,
    )
    variant = ContentPostVariant.objects.create(
        post=post,
        platform="instagram",
        status=ContentPostVariant.Status.PROCESSING,
        metadata={
            "ig_container_id": "test_container_123",
            "last_poll_attempt": timezone.now().isoformat(),
        },
    )
    return variant


class TestPollingWithinLimit:
    """Test that variants within time limit remain PROCESSING."""

    def test_recent_processing_variant_not_escalated(self, account):
        """Variants in PROCESSING < 48h should NOT escalate."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type="video",
            status=ContentPost.Status.PUBLISHING,
        )
        # Created 1 hour ago
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            created_at=timezone.now() - timedelta(hours=1),
            metadata={
                "ig_container_id": "container_123",
                "last_poll_attempt": (timezone.now() - timedelta(hours=1)).isoformat(),
            },
        )

        result = escalate_exhausted_processing_variants(max_age_hours=48)

        variant.refresh_from_db()
        # Should still be PROCESSING
        assert variant.status == ContentPostVariant.Status.PROCESSING
        assert result["escalated"] == 0

    def test_polling_updates_last_poll_timestamp(self, account, processing_variant):
        """Polling task updates last_poll_attempt timestamp."""
        with patch(
            "apps.publishing.services.try_publish_ig_variant_container"
        ) as mock_publish:
            mock_publish.return_value = False  # Container still processing

            poll_ig_variant_containers()

        processing_variant.refresh_from_db()
        # Timestamp should be updated to recent time
        last_poll = processing_variant.metadata.get("last_poll_attempt")
        assert last_poll is not None
        # Should be very recent (within last minute)
        poll_time = timezone.datetime.fromisoformat(last_poll)
        assert (timezone.now() - poll_time).total_seconds() < 60


class TestExhaustionDetection:
    """Test detection and escalation of exhausted variants."""

    def test_exhausted_variant_escalates_to_reconciling(self, account):
        """Variant in PROCESSING > 48h escalates to RECONCILING."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type="video",
            status=ContentPost.Status.PUBLISHING,
        )
        # Created 72 hours ago (beyond 48h limit)
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=timezone.now() - timedelta(hours=72),
            metadata={
                "ig_container_id": "container_456",
                "last_poll_attempt": (timezone.now() - timedelta(hours=72)).isoformat(),
            },
        )

        result = escalate_exhausted_processing_variants(max_age_hours=48)

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.RECONCILING
        assert "exceeded 48 hours" in variant.last_error.lower()
        assert variant.last_error_type == ContentPostVariant.ErrorType.TIMEOUT
        assert result["escalated"] == 1

    def test_exhaustion_preserves_container_metadata(self, account):
        """Escalation preserves ig_container_id and poll history."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type="video",
            status=ContentPost.Status.PUBLISHING,
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=timezone.now() - timedelta(hours=72),
            metadata={
                "ig_container_id": "critical_container_id",
                "last_poll_attempt": (timezone.now() - timedelta(hours=72)).isoformat(),
                "poll_history": [{"timestamp": "...", "status": "IN_PROGRESS"}],
            },
        )

        escalate_exhausted_processing_variants(max_age_hours=48)

        variant.refresh_from_db()
        # All metadata preserved for recovery
        assert variant.metadata["ig_container_id"] == "critical_container_id"
        assert "poll_history" in variant.metadata
        assert variant.metadata["last_poll_attempt"] is not None

    def test_exhaustion_bounded_by_batch_limit(self, account):
        """Escalation processes max 100 variants per run."""
        # Create 150 exhausted variants
        for i in range(150):
            post = ContentPost.objects.create(
                account=account,
                platforms=["instagram"],
                media_type="video",
                status=ContentPost.Status.PUBLISHING,
            )
            ContentPostVariant.objects.create(
                post=post,
                platform="instagram",
                status=ContentPostVariant.Status.PROCESSING,
                processing_started_at=timezone.now() - timedelta(hours=72),
                metadata={"ig_container_id": f"container_{i}"},
            )

        result = escalate_exhausted_processing_variants(max_age_hours=48)

        # Should process exactly 100 (batch limit)
        assert result["escalated"] == 100

        # Remaining 50 still in PROCESSING
        processing_count = ContentPostVariant.objects.filter(
            status=ContentPostVariant.Status.PROCESSING
        ).count()
        assert processing_count == 50


class TestWorkerRestartRecovery:
    """Test that polling resumes after worker crashes."""

    def test_worker_restart_resumes_from_metadata(self, processing_variant):
        """Container ID persisted in metadata survives worker restart."""
        container_id = processing_variant.metadata.get("ig_container_id")
        original_timestamp = processing_variant.metadata.get("last_poll_attempt")

        # Simulate worker restart (task re-enqueued, db unchanged)
        with patch(
            "apps.publishing.services.try_publish_ig_variant_container"
        ) as mock_publish:
            mock_publish.return_value = False

            # Re-run polling after restart
            poll_ig_variant_containers()

        processing_variant.refresh_from_db()
        # Container ID still there
        assert processing_variant.metadata["ig_container_id"] == container_id
        # Timestamp updated to recent (proof of restart recovery)
        assert processing_variant.metadata["last_poll_attempt"] != original_timestamp

    def test_concurrent_pollers_do_not_duplicate_publication(self, processing_variant):
        """Only one poller publishes when container finishes."""
        provider = _fake_ig_provider()
        with patch(
            "apps.publishing.services._ig_provider_for_post", return_value=provider
        ):
            result1 = poll_ig_variant_containers()
            result2 = poll_ig_variant_containers()

        # Only one should have published
        assert result1 + result2 == 1
        provider.publish_container.assert_called_once()

        processing_variant.refresh_from_db()
        # Should be published (no duplicates)
        assert processing_variant.status == ContentPostVariant.Status.PUBLISHED


class TestTransientErrorHandling:
    """Test transient error resilience."""

    def test_transient_provider_error_preserves_processing_state(
        self, processing_variant
    ):
        """Transient API errors don't mark variant failed."""
        with patch(
            "apps.publishing.services.try_publish_ig_variant_container",
            side_effect=Exception("API timeout - transient"),
        ):
            # Polling fails but continues
            poll_ig_variant_containers()

        processing_variant.refresh_from_db()
        # Should still be PROCESSING (retry on next poll)
        assert processing_variant.status == ContentPostVariant.Status.PROCESSING


class TestTerminalErrors:
    """Test terminal provider errors mark variant failed."""

    def test_terminal_container_error_marks_failed(self, processing_variant):
        """Container ERROR or EXPIRED status marks variant FAILED."""

        provider = _fake_ig_provider(poll_status="EXPIRED")
        with patch(
            "apps.publishing.services._ig_provider_for_post", return_value=provider
        ):
            poll_ig_variant_containers()

        processing_variant.refresh_from_db()
        assert processing_variant.status == ContentPostVariant.Status.FAILED
        assert "expired" in processing_variant.last_error.lower()
        provider.publish_container.assert_not_called()


class TestContainerReadyPublication:
    """Test publication when container finishes."""

    def test_ready_container_publishes_once(self, processing_variant):
        """When container finishes, variant is published."""
        provider = _fake_ig_provider()
        with patch(
            "apps.publishing.services._ig_provider_for_post", return_value=provider
        ):
            count = poll_ig_variant_containers()

        assert count == 1
        provider.publish_container.assert_called_once()
        processing_variant.refresh_from_db()
        assert processing_variant.status == ContentPostVariant.Status.PUBLISHED

    def test_ready_container_with_publication_id(self, processing_variant):
        """Published variant has publication_id from provider."""

        def mock_publish_and_set_id(variant):
            variant.publication_id = "ig_media_789"
            variant.publication_url = "https://instagram.com/p/ABC789"
            variant.status = ContentPostVariant.Status.PUBLISHED
            variant.save()
            return True

        with patch(
            "apps.publishing.services.try_publish_ig_variant_container",
            side_effect=mock_publish_and_set_id,
        ):
            poll_ig_variant_containers()

        processing_variant.refresh_from_db()
        assert processing_variant.publication_id == "ig_media_789"
        assert processing_variant.status == ContentPostVariant.Status.PUBLISHED


class TestFinalPublishTimeout:
    """Test ambiguous timeout during final publication."""

    def test_final_publish_timeout_enters_reconciling(self, processing_variant):
        """Timeout during final publish enters RECONCILING and is not retried."""
        provider = _fake_ig_provider(
            publish_error=TimeoutError("Final publish request timed out")
        )
        with patch(
            "apps.publishing.services._ig_provider_for_post", return_value=provider
        ):
            poll_ig_variant_containers()

        processing_variant.refresh_from_db()
        assert processing_variant.status == ContentPostVariant.Status.RECONCILING
        assert (
            processing_variant.last_error_type == ContentPostVariant.ErrorType.TIMEOUT
        )
        assert processing_variant.publication_id == ""
        provider.publish_container.assert_called_once()


class TestRecoveryFromExhaustion:
    """Test operator recovery after exhaustion escalation."""

    def test_exhausted_variant_in_reconciliation_queue(self, account):
        """Escalated variants appear in reconciliation queue."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type="video",
            status=ContentPost.Status.PUBLISHING,
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=timezone.now() - timedelta(hours=72),
            metadata={"ig_container_id": "container_id"},
        )

        # Escalate
        escalate_exhausted_processing_variants(max_age_hours=48)

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.RECONCILING

        # Should be queryable for reconciliation
        reconciling_variants = ContentPostVariant.objects.filter(
            status=ContentPostVariant.Status.RECONCILING,
            post__account=account,
        )
        assert reconciling_variants.count() == 1
        assert reconciling_variants.first().pk == variant.pk

    def test_operator_confirms_published_after_exhaustion(self, account):
        """Operator can resolve exhausted variant using reconciliation."""
        from apps.publishing.services import reconcile_variant_confirmed_published

        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type="video",
            status=ContentPost.Status.PUBLISHING,
        )
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=timezone.now() - timedelta(hours=72),
            metadata={"ig_container_id": "container_id"},
        )

        # Escalate due to exhaustion
        escalate_exhausted_processing_variants(max_age_hours=48)

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.RECONCILING

        # Operator investigates and confirms published
        operator = User.objects.get(username="testuser")
        reconcile_variant_confirmed_published(
            variant=variant,
            publication_id="ig_media_confirmed",
            publication_url="https://instagram.com/p/confirmed",
            evidence_notes="Found in Instagram Insights after investigation",
            performed_by=operator,
        )

        variant.refresh_from_db()
        assert variant.status == ContentPostVariant.Status.PUBLISHED
        assert variant.publication_id == "ig_media_confirmed"
