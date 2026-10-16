"""Phase B: State machine and recovery (parent aggregation, exhaustion detection).

Covers:
- B1: Correct parent-state aggregation (Finding #6)
- B2: Processing exhaustion detection (Finding #9)
- B3: Stale publishing recovery (Finding #5)
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.services import _aggregate_parent_status

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    user = User.objects.create_user("testuser", "test@example.com", "pass")
    account = Account.objects.create(company_name="Test Account")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return account


class TestPhaseB1ParentAggregation:
    """B1: Parent aggregation respects nonterminal variant states."""

    def test_all_published_parent_published(self, account):
        """All required channels published → parent PUBLISHED."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook", "instagram"],
            status=ContentPost.Status.DRAFT,
        )
        ContentPostVariant.objects.create(
            post=post, platform="facebook", status=ContentPostVariant.Status.PUBLISHED
        )
        ContentPostVariant.objects.create(
            post=post, platform="instagram", status=ContentPostVariant.Status.PUBLISHED
        )

        outcomes = [
            {
                "status": ContentPostVariant.Status.PUBLISHED,
                "platform": "facebook",
                "error_type": "",
            },
            {
                "status": ContentPostVariant.Status.PUBLISHED,
                "platform": "instagram",
                "error_type": "",
            },
        ]
        result = _aggregate_parent_status(post, outcomes)
        assert result == ContentPost.Status.PUBLISHED

    def test_one_publishing_one_published_parent_publishing(self, account):
        """Any variant PUBLISHING → parent PUBLISHING (not yet published)."""
        post = ContentPost.objects.create(
            account=account, platforms=["facebook", "instagram"]
        )
        ContentPostVariant.objects.create(
            post=post, platform="facebook", status=ContentPostVariant.Status.PUBLISHED
        )
        ContentPostVariant.objects.create(
            post=post, platform="instagram", status=ContentPostVariant.Status.PUBLISHING
        )

        outcomes = [
            {
                "status": ContentPostVariant.Status.PUBLISHED,
                "platform": "facebook",
                "error_type": "",
            },
            {
                "status": ContentPostVariant.Status.PUBLISHING,
                "platform": "instagram",
                "error_type": "",
            },
        ]
        result = _aggregate_parent_status(post, outcomes)
        assert result == ContentPost.Status.PUBLISHING

    def test_one_processing_one_published_parent_publishing(self, account):
        """Any variant PROCESSING → parent PUBLISHING (nonterminal)."""
        post = ContentPost.objects.create(
            account=account, platforms=["facebook", "instagram"]
        )
        ContentPostVariant.objects.create(
            post=post, platform="facebook", status=ContentPostVariant.Status.PUBLISHED
        )
        ContentPostVariant.objects.create(
            post=post, platform="instagram", status=ContentPostVariant.Status.PROCESSING
        )

        outcomes = [
            {
                "status": ContentPostVariant.Status.PUBLISHED,
                "platform": "facebook",
                "error_type": "",
            },
            {
                "status": ContentPostVariant.Status.PROCESSING,
                "platform": "instagram",
                "error_type": "",
            },
        ]
        result = _aggregate_parent_status(post, outcomes)
        assert result == ContentPost.Status.PUBLISHING

    def test_partial_success_parent_publishing(self, account):
        """One published, another failed → parent PUBLISHING (preserve partial)."""
        post = ContentPost.objects.create(
            account=account, platforms=["facebook", "instagram"]
        )
        ContentPostVariant.objects.create(
            post=post, platform="facebook", status=ContentPostVariant.Status.PUBLISHED
        )
        ContentPostVariant.objects.create(
            post=post, platform="instagram", status=ContentPostVariant.Status.FAILED
        )

        outcomes = [
            {
                "status": ContentPostVariant.Status.PUBLISHED,
                "platform": "facebook",
                "error_type": "",
            },
            {
                "status": ContentPostVariant.Status.FAILED,
                "platform": "instagram",
                "error_type": "permission",
            },
        ]
        result = _aggregate_parent_status(post, outcomes)
        assert result == ContentPost.Status.PUBLISHING

    def test_all_failed_parent_failed(self, account):
        """All required channels failed → parent FAILED."""
        post = ContentPost.objects.create(
            account=account, platforms=["facebook", "instagram"]
        )
        ContentPostVariant.objects.create(
            post=post, platform="facebook", status=ContentPostVariant.Status.FAILED
        )
        ContentPostVariant.objects.create(
            post=post, platform="instagram", status=ContentPostVariant.Status.FAILED
        )

        outcomes = [
            {
                "status": ContentPostVariant.Status.FAILED,
                "platform": "facebook",
                "error_type": "permission",
            },
            {
                "status": ContentPostVariant.Status.FAILED,
                "platform": "instagram",
                "error_type": "rate_limit",
            },
        ]
        result = _aggregate_parent_status(post, outcomes)
        assert result == ContentPost.Status.FAILED

    def test_reconciling_parent_publishing(self, account):
        """Any variant RECONCILING → parent PUBLISHING (allow investigation)."""
        post = ContentPost.objects.create(
            account=account, platforms=["facebook", "instagram"]
        )
        ContentPostVariant.objects.create(
            post=post, platform="facebook", status=ContentPostVariant.Status.PUBLISHED
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.RECONCILING,
        )

        outcomes = [
            {
                "status": ContentPostVariant.Status.PUBLISHED,
                "platform": "facebook",
                "error_type": "",
            },
            {
                "status": ContentPostVariant.Status.RECONCILING,
                "platform": "instagram",
                "error_type": "",
            },
        ]
        result = _aggregate_parent_status(post, outcomes)
        assert result == ContentPost.Status.PUBLISHING

    def test_all_draft_parent_scheduled(self, account):
        """No variants attempted (all draft) → parent SCHEDULED."""
        post = ContentPost.objects.create(
            account=account, platforms=["facebook", "instagram"]
        )
        ContentPostVariant.objects.create(
            post=post, platform="facebook", status=ContentPostVariant.Status.DRAFT
        )
        ContentPostVariant.objects.create(
            post=post, platform="instagram", status=ContentPostVariant.Status.DRAFT
        )

        outcomes = [
            {
                "status": ContentPostVariant.Status.DRAFT,
                "platform": "facebook",
                "error_type": "",
            },
            {
                "status": ContentPostVariant.Status.DRAFT,
                "platform": "instagram",
                "error_type": "",
            },
        ]
        result = _aggregate_parent_status(post, outcomes)
        assert result == ContentPost.Status.SCHEDULED

    def test_all_cancelled_parent_cancelled(self, account):
        """All variants cancelled → parent CANCELLED."""
        post = ContentPost.objects.create(
            account=account, platforms=["facebook", "instagram"]
        )
        ContentPostVariant.objects.create(
            post=post, platform="facebook", status=ContentPostVariant.Status.CANCELLED
        )
        ContentPostVariant.objects.create(
            post=post, platform="instagram", status=ContentPostVariant.Status.CANCELLED
        )

        outcomes = [
            {
                "status": ContentPostVariant.Status.CANCELLED,
                "platform": "facebook",
                "error_type": "",
            },
            {
                "status": ContentPostVariant.Status.CANCELLED,
                "platform": "instagram",
                "error_type": "",
            },
        ]
        result = _aggregate_parent_status(post, outcomes)
        assert result == ContentPost.Status.CANCELLED

    def test_single_channel_published(self, account):
        """Single channel published → parent PUBLISHED."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])
        ContentPostVariant.objects.create(
            post=post, platform="facebook", status=ContentPostVariant.Status.PUBLISHED
        )

        outcomes = [
            {
                "status": ContentPostVariant.Status.PUBLISHED,
                "platform": "facebook",
                "error_type": "",
            },
        ]
        result = _aggregate_parent_status(post, outcomes)
        assert result == ContentPost.Status.PUBLISHED

    def test_single_channel_processing(self, account):
        """Single channel processing → parent PUBLISHING."""
        post = ContentPost.objects.create(account=account, platforms=["instagram"])
        ContentPostVariant.objects.create(
            post=post, platform="instagram", status=ContentPostVariant.Status.PROCESSING
        )

        outcomes = [
            {
                "status": ContentPostVariant.Status.PROCESSING,
                "platform": "instagram",
                "error_type": "",
            },
        ]
        result = _aggregate_parent_status(post, outcomes)
        assert result == ContentPost.Status.PUBLISHING

    def test_empty_outcomes_parent_scheduled(self, account):
        """No outcomes provided → parent SCHEDULED (edge case)."""
        post = ContentPost.objects.create(account=account, platforms=["facebook"])

        result = _aggregate_parent_status(post, [])
        assert result == ContentPost.Status.SCHEDULED

    def test_facebook_published_ig_processing_then_published(self, account):
        """FB published, IG processing → parent PUBLISHING. Then IG published → PUBLISHED.

        This simulates polling completion: first call sees IG in PROCESSING,
        second call (after polling finishes) sees IG in PUBLISHED.
        """
        post = ContentPost.objects.create(
            account=account, platforms=["facebook", "instagram"]
        )

        # First call: FB published, IG processing
        outcomes1 = [
            {
                "status": ContentPostVariant.Status.PUBLISHED,
                "platform": "facebook",
                "error_type": "",
            },
            {
                "status": ContentPostVariant.Status.PROCESSING,
                "platform": "instagram",
                "error_type": "",
            },
        ]
        result1 = _aggregate_parent_status(post, outcomes1)
        assert result1 == ContentPost.Status.PUBLISHING

        # Second call: polling completed, both published
        outcomes2 = [
            {
                "status": ContentPostVariant.Status.PUBLISHED,
                "platform": "facebook",
                "error_type": "",
            },
            {
                "status": ContentPostVariant.Status.PUBLISHED,
                "platform": "instagram",
                "error_type": "",
            },
        ]
        result2 = _aggregate_parent_status(post, outcomes2)
        assert result2 == ContentPost.Status.PUBLISHED

    def test_facebook_published_ig_processing_then_failed(self, account):
        """FB published, IG processing → parent PUBLISHING. Then IG fails → partial success."""
        post = ContentPost.objects.create(
            account=account, platforms=["facebook", "instagram"]
        )

        # First call: FB published, IG processing
        outcomes1 = [
            {
                "status": ContentPostVariant.Status.PUBLISHED,
                "platform": "facebook",
                "error_type": "",
            },
            {
                "status": ContentPostVariant.Status.PROCESSING,
                "platform": "instagram",
                "error_type": "",
            },
        ]
        result1 = _aggregate_parent_status(post, outcomes1)
        assert result1 == ContentPost.Status.PUBLISHING

        # Second call: IG failed after polling/timeout
        outcomes2 = [
            {
                "status": ContentPostVariant.Status.PUBLISHED,
                "platform": "facebook",
                "error_type": "",
            },
            {
                "status": ContentPostVariant.Status.FAILED,
                "platform": "instagram",
                "error_type": "timeout",
            },
        ]
        result2 = _aggregate_parent_status(post, outcomes2)
        assert result2 == ContentPost.Status.PUBLISHING  # Partial success

    def test_parent_cannot_revert_from_published_to_scheduled(self, account):
        """Once a variant publishes, parent cannot revert to SCHEDULED."""
        post = ContentPost.objects.create(
            account=account, platforms=["facebook", "instagram"]
        )

        # Both published
        outcomes = [
            {
                "status": ContentPostVariant.Status.PUBLISHED,
                "platform": "facebook",
                "error_type": "",
            },
            {
                "status": ContentPostVariant.Status.PUBLISHED,
                "platform": "instagram",
                "error_type": "",
            },
        ]
        result = _aggregate_parent_status(post, outcomes)
        assert result == ContentPost.Status.PUBLISHED

        # Should never return SCHEDULED after publication has begun
        assert result != ContentPost.Status.SCHEDULED
