"""Instagram container publication is claimed atomically; ambiguous outcomes reconcile."""

from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.services import (
    PUBLISH_CLAIM_KEY,
    _claim_publish,
    _persist_publish_outcome,
    try_publish_ig_variant_container,
)

pytestmark = pytest.mark.django_db
Status = ContentPostVariant.Status


@pytest.fixture
def processing():
    account = Account.objects.create(company_name="Claim Co")
    post = ContentPost.objects.create(
        account=account, platforms=["instagram"], media_type="video"
    )
    return ContentPostVariant.objects.create(
        post=post,
        platform="instagram",
        status=Status.PROCESSING,
        processing_started_at=timezone.now(),
        metadata={"ig_container_id": "c1"},
    )


def _provider(poll="FINISHED", publish_error=None):
    provider = MagicMock()
    provider.poll_container_status.return_value = poll
    if publish_error is not None:
        provider.publish_container.side_effect = publish_error
    else:
        provider.publish_container.return_value = "m1"
    provider.get_permalink.return_value = "https://www.instagram.com/p/P1/"
    return provider


def test_second_claim_on_the_same_variant_is_refused(processing):
    first = _claim_publish(processing.pk)
    second = _claim_publish(processing.pk)

    assert first is not None
    assert second is None


def test_two_pollers_observing_finished_publish_exactly_once(processing):
    provider = _provider()
    with patch("apps.publishing.services._ig_provider_for_post", return_value=provider):
        try_publish_ig_variant_container(
            ContentPostVariant.objects.get(pk=processing.pk)
        )
        try_publish_ig_variant_container(
            ContentPostVariant.objects.get(pk=processing.pk)
        )

    assert provider.publish_container.call_count == 1
    processing.refresh_from_db()
    assert processing.status == Status.PUBLISHED


def test_unknown_publish_outcome_reconciles_and_is_never_retried(processing):
    provider = _provider(publish_error=ConnectionError("Connection aborted after send"))
    with patch("apps.publishing.services._ig_provider_for_post", return_value=provider):
        try_publish_ig_variant_container(
            ContentPostVariant.objects.get(pk=processing.pk)
        )
        try_publish_ig_variant_container(
            ContentPostVariant.objects.get(pk=processing.pk)
        )

    processing.refresh_from_db()
    assert processing.status == Status.RECONCILING
    assert provider.publish_container.call_count == 1
    assert PUBLISH_CLAIM_KEY not in processing.metadata


def test_stale_worker_cannot_overwrite_a_later_published_state(processing):
    stale_token = _claim_publish(processing.pk)
    processing.refresh_from_db()
    # The live attempt finishes first and clears its claim.
    _persist_publish_outcome(
        processing.pk,
        (processing.metadata or {}).get(PUBLISH_CLAIM_KEY),
        Status.PUBLISHED,
        publication_id="m1",
        publication_url="https://www.instagram.com/p/P1/",
    )

    written = _persist_publish_outcome(
        processing.pk,
        stale_token,
        Status.FAILED,
        error="late",
        error_type="unknown",
    )

    processing.refresh_from_db()
    assert written is False
    assert processing.status == Status.PUBLISHED


def test_error_poll_cannot_downgrade_a_published_variant(processing):
    ContentPostVariant.objects.filter(pk=processing.pk).update(
        status=Status.PUBLISHED, publication_id="m1"
    )

    with patch(
        "apps.publishing.services._ig_provider_for_post",
        return_value=_provider(poll="ERROR"),
    ):
        try_publish_ig_variant_container(
            ContentPostVariant.objects.get(pk=processing.pk)
        )

    processing.refresh_from_db()
    assert processing.status == Status.PUBLISHED
