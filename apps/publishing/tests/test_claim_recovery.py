"""Worker-crash recovery for the in-flight publication claim."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.services import (
    CLAIM_IN_FLIGHT_KEY,
    publish_variant,
    reconcile_variant_retry,
)
from apps.publishing.tasks import reconcile_stale_publishing_variants

pytestmark = pytest.mark.django_db

FB_OK = {
    "success": True,
    "publication_id": "fb_1",
    "publication_url": "https://facebook.com/fb_1",
    "error": "",
    "error_type": "",
    "error_at": None,
    "reconciling": False,
}


@pytest.fixture
def approved_variant():
    reviewer = User.objects.create_user("rev", "rev@example.com", "pass")
    account = Account.objects.create(company_name="Recovery Co")
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        approval_required=True,
        approval_status="approved",
        approved_by=reviewer,
        approved_at=timezone.now(),
    )
    variant = ContentPostVariant.objects.create(
        post=post, platform="facebook", caption="Approved"
    )
    post.last_content_hash = post.compute_content_hash()
    post.save()
    return variant


def _abandon_claim(variant, age_hours):
    """Simulate a worker that claimed the variant and died before persisting an outcome."""
    ContentPostVariant.objects.filter(pk=variant.pk).update(
        status=ContentPostVariant.Status.PUBLISHING,
        metadata={CLAIM_IN_FLIGHT_KEY: True},
        last_attempt_at=timezone.now() - timedelta(hours=age_hours),
    )


class TestClaimRecovery:
    def test_abandoned_claim_is_escalated_and_flag_cleared(self, approved_variant):
        _abandon_claim(approved_variant, age_hours=7)

        with patch("apps.publishing.services._publish_variant_facebook") as fb:
            result = reconcile_stale_publishing_variants()

        approved_variant.refresh_from_db()
        assert result["escalated"] == 1
        assert approved_variant.status == ContentPostVariant.Status.RECONCILING
        assert CLAIM_IN_FLIGHT_KEY not in approved_variant.metadata
        fb.assert_not_called()

    def test_live_claim_is_not_cleared(self, approved_variant):
        _abandon_claim(approved_variant, age_hours=0)

        result = reconcile_stale_publishing_variants()

        approved_variant.refresh_from_db()
        assert result["escalated"] == 0
        assert approved_variant.status == ContentPostVariant.Status.PUBLISHING
        assert approved_variant.metadata.get(CLAIM_IN_FLIGHT_KEY) is True

    def test_live_claim_blocks_a_concurrent_attempt(self, approved_variant):
        _abandon_claim(approved_variant, age_hours=0)

        with patch(
            "apps.publishing.services._publish_variant_facebook", return_value=FB_OK
        ) as fb:
            result = publish_variant(
                ContentPostVariant.objects.get(pk=approved_variant.pk)
            )

        assert result["success"] is False
        fb.assert_not_called()

    def test_ambiguous_abandoned_claim_is_never_republished(self, approved_variant):
        _abandon_claim(approved_variant, age_hours=7)
        reconcile_stale_publishing_variants()

        with patch(
            "apps.publishing.services._publish_variant_facebook", return_value=FB_OK
        ) as fb:
            result = publish_variant(
                ContentPostVariant.objects.get(pk=approved_variant.pk)
            )

        assert result["success"] is False
        fb.assert_not_called()

    def test_operator_retry_after_recovery_can_publish(self, approved_variant):
        _abandon_claim(approved_variant, age_hours=7)
        reconcile_stale_publishing_variants()
        operator = User.objects.create_user("op", "op@example.com", "pass")
        reconcile_variant_retry(
            variant=ContentPostVariant.objects.get(pk=approved_variant.pk),
            performed_by=operator,
        )

        # The UI retry sets PUBLISHING and enqueues; the queued attempt must be allowed to claim.
        ContentPostVariant.objects.filter(pk=approved_variant.pk).update(
            status=ContentPostVariant.Status.PUBLISHING
        )
        with patch(
            "apps.publishing.services._publish_variant_facebook", return_value=FB_OK
        ) as fb:
            result = publish_variant(
                ContentPostVariant.objects.get(pk=approved_variant.pk)
            )

        assert result["success"] is True
        fb.assert_called_once()

    def test_successful_publish_clears_the_flag(self, approved_variant):
        with patch(
            "apps.publishing.services._publish_variant_facebook", return_value=FB_OK
        ):
            publish_variant(ContentPostVariant.objects.get(pk=approved_variant.pk))

        approved_variant.refresh_from_db()
        assert CLAIM_IN_FLIGHT_KEY not in approved_variant.metadata

    def test_repeated_recovery_runs_are_idempotent(self, approved_variant):
        _abandon_claim(approved_variant, age_hours=7)

        first = reconcile_stale_publishing_variants()
        second = reconcile_stale_publishing_variants()

        assert first["escalated"] == 1
        assert second["escalated"] == 0
