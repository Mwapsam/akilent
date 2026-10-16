from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.services import (
    PUBLISH_CLAIM_AT_KEY,
    PUBLISH_CLAIM_KEY,
    _claim_publish,
    _persist_publish_outcome,
)
from apps.publishing.tasks import recover_abandoned_publish_claims

pytestmark = pytest.mark.django_db
Status = ContentPostVariant.Status


@pytest.fixture
def claimed():
    account = Account.objects.create(company_name="Lease Co")
    post = ContentPost.objects.create(
        account=account, platforms=["instagram"], media_type="video"
    )
    variant = ContentPostVariant.objects.create(
        post=post,
        platform="instagram",
        status=Status.PROCESSING,
        processing_started_at=timezone.now(),
        metadata={"ig_container_id": "c1"},
    )
    token = _claim_publish(variant.pk)
    return variant, token


def _age_claim(variant, minutes):
    variant.refresh_from_db()
    metadata = dict(variant.metadata)
    metadata[PUBLISH_CLAIM_AT_KEY] = (
        timezone.now() - timedelta(minutes=minutes)
    ).isoformat()
    ContentPostVariant.objects.filter(pk=variant.pk).update(metadata=metadata)


def test_claim_within_lease_is_left_alone(claimed):
    variant, _ = claimed

    result = recover_abandoned_publish_claims()

    variant.refresh_from_db()
    assert result["recovered"] == 0
    assert variant.status == Status.PROCESSING


def test_expired_claim_is_reconciled_and_never_republished(claimed):
    variant, token = claimed
    _age_claim(variant, minutes=45)
    provider = MagicMock()

    with patch("apps.publishing.services._ig_provider_for_post", return_value=provider):
        result = recover_abandoned_publish_claims()

    variant.refresh_from_db()
    assert result["recovered"] == 1
    assert variant.status == Status.RECONCILING
    assert PUBLISH_CLAIM_KEY not in variant.metadata
    provider.publish_container.assert_not_called()


def test_late_write_from_the_original_worker_is_refused_after_recovery(claimed):
    variant, token = claimed
    _age_claim(variant, minutes=45)
    recover_abandoned_publish_claims()

    written = _persist_publish_outcome(
        variant.pk,
        token,
        Status.PUBLISHED,
        publication_id="m1",
        publication_url="https://x",
    )

    variant.refresh_from_db()
    assert written is False
    assert variant.status == Status.RECONCILING


def test_facebook_abandoned_claim_is_reconciled_and_late_write_refused():
    from apps.publishing.services import (
        CLAIM_IN_FLIGHT_KEY,
        CLAIM_TOKEN_KEY,
        publish_variant,
    )

    account = Account.objects.create(company_name="FB Lease Co")
    post = ContentPost.objects.create(
        account=account, platforms=["facebook"], media_type="image"
    )
    variant = ContentPostVariant.objects.create(
        post=post, platform="facebook", caption="x", status=Status.PUBLISHING
    )
    ContentPostVariant.objects.filter(pk=variant.pk).update(
        metadata={CLAIM_IN_FLIGHT_KEY: True, CLAIM_TOKEN_KEY: "old"},
        last_attempt_at=timezone.now() - timedelta(minutes=45),
    )

    recover_abandoned_publish_claims()
    variant.refresh_from_db()
    assert variant.status == Status.RECONCILING
    assert CLAIM_TOKEN_KEY not in variant.metadata

    provider = MagicMock()
    provider.create_text_post.return_value = "fb_late"
    with patch(
        "apps.publishing.services._fb_provider_for_post",
        return_value=(provider, "page"),
    ):
        publish_variant(ContentPostVariant.objects.get(pk=variant.pk))

    variant.refresh_from_db()
    assert variant.status == Status.RECONCILING


def _fb_variant():
    account = Account.objects.create(company_name="FB Late Co")
    post = ContentPost.objects.create(
        account=account, platforms=["facebook"], media_type="image"
    )
    return ContentPostVariant.objects.create(
        post=post, platform="facebook", caption="x"
    )


def _expire_and_recover(variant):
    ContentPostVariant.objects.filter(pk=variant.pk).update(
        last_attempt_at=timezone.now() - timedelta(minutes=45)
    )
    recover_abandoned_publish_claims()


def test_late_facebook_success_is_refused_after_recovery():
    from apps.publishing.services import publish_variant

    variant = _fb_variant()
    ok = {
        "success": True,
        "publication_id": "fb_late",
        "publication_url": "https://facebook.com/fb_late",
        "error": "",
        "error_type": "",
        "error_at": None,
        "reconciling": False,
    }

    def late_worker(v, p):
        _expire_and_recover(v)
        return ok

    with patch(
        "apps.publishing.services._publish_variant_facebook", side_effect=late_worker
    ):
        publish_variant(ContentPostVariant.objects.get(pk=variant.pk))

    variant.refresh_from_db()
    assert variant.status == Status.RECONCILING
    assert variant.publication_id == ""


def test_late_facebook_failure_is_refused_after_recovery():
    from apps.publishing.services import publish_variant

    variant = _fb_variant()

    def late_worker(v, p):
        _expire_and_recover(v)
        return {
            "success": False,
            "publication_id": "",
            "publication_url": "",
            "error": "Invalid image",
            "error_type": "unknown",
            "error_at": timezone.now(),
            "reconciling": False,
        }

    with patch(
        "apps.publishing.services._publish_variant_facebook", side_effect=late_worker
    ):
        publish_variant(ContentPostVariant.objects.get(pk=variant.pk))

    variant.refresh_from_db()
    assert variant.status == Status.RECONCILING
    # The refusal must leave the reason recovery recorded, untouched.
    assert variant.last_error == (
        "Publish attempt abandoned before its outcome was recorded; "
        "reconcile before retrying"
    )
    assert variant.last_error_type == ContentPostVariant.ErrorType.TIMEOUT
    assert variant.publication_id == ""
