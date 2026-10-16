"""publish_variant_async must schedule at most one retry per failed attempt."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from celery.exceptions import Retry
from django.utils import timezone

from apps.accounts.models import Account
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.tasks import publish_variant_async

pytestmark = pytest.mark.django_db


@pytest.fixture
def variant():
    account = Account.objects.create(company_name="Retry Co")
    post = ContentPost.objects.create(account=account, platforms=["facebook"])
    return ContentPostVariant.objects.create(
        post=post, platform="facebook", status=ContentPostVariant.Status.FAILED
    )


def _failure():
    return {
        "success": False,
        "publication_id": "",
        "publication_url": "",
        "error": "provider error",
        "error_type": "provider_outage",
        "error_at": timezone.now(),
        "reconciling": False,
        "status": ContentPostVariant.Status.FAILED,
    }


def test_failed_attempt_schedules_exactly_one_retry(variant):
    with (
        patch("apps.publishing.services.publish_variant", return_value=_failure()),
        patch.object(publish_variant_async, "retry", side_effect=Retry()) as retry,
    ):
        with pytest.raises(Retry):
            publish_variant_async.run(variant.pk)

    assert retry.call_count == 1


def test_instagram_processing_is_not_a_failure_and_is_not_retried(variant):
    processing = _failure() | {
        "error": "Video publishing in progress",
        "status": ContentPostVariant.Status.PROCESSING,
    }
    with (
        patch("apps.publishing.services.publish_variant", return_value=processing),
        patch.object(publish_variant_async, "retry") as retry,
    ):
        outcome = publish_variant_async.run(variant.pk)

    assert outcome == "processing"
    retry.assert_not_called()


def test_permanent_failure_after_max_retries_is_not_retried(variant):
    with (
        patch("apps.publishing.services.publish_variant", return_value=_failure()),
        patch.object(publish_variant_async, "retry") as retry,
        patch.object(publish_variant_async, "max_retries", 0),
    ):
        outcome = publish_variant_async.run(variant.pk)

    assert outcome == "failed"
    retry.assert_not_called()
